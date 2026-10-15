"""Experimental steady-state thin-board model, SI thermal units and mm geometry.

Through-thickness temperature is uniform. Only supplied copper geometry spreads
heat. No radiation, package/junction models or temperature feedback into DC.
"""
from dataclasses import dataclass
import math
import numpy as np
import shapely
from shapely.geometry import LineString, Polygon, Point, box
from shapely.ops import polygonize_full, unary_union
from scipy import sparse
from scipy.spatial import cKDTree
from solver_backend import solve_reduced
from resource_budget import check_budget
from network_solver import copper_edges


@dataclass
class ThermalSettings:
    ambient_c: float = 25
    convection_w_m2k: float = 10  # Each face independently.
    substrate_k_w_mk: float = .3
    copper_k_w_mk: float = 400
    spacing_mm: float = 1

    def validate(self):
        for name, value in vars(self).items():
            if not math.isfinite(value) or (name != 'ambient_c' and value <= 0):
                raise ValueError(f'Invalid thermal setting: {name}.')
        if self.ambient_c <= -273.15 or self.spacing_mm < .1:
            raise ValueError('Thermal ambient must exceed absolute zero; spacing must be at least 0.1 mm.')


def board_region(board):
    """Read closed Edge.Cuts; reject missing or unsupported boundaries."""
    from kipy.proto.board.board_types_pb2 import BL_Edge_Cuts
    from kipy.geometry import arc_center
    lines = []
    for shape in board.get_shapes():
        if shape.layer != BL_Edge_Cuts:
            continue
        kind = shape.proto.shape.WhichOneof('geometry')
        def xy(p):
            return p.x/1e6, p.y/1e6
        if kind == 'segment':
            lines.append(LineString([xy(shape.start), xy(shape.end)]))
        elif kind == 'rectangle':
            a, b = xy(shape.top_left), xy(shape.bottom_right)
            lines.append(box(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])).exterior)
        elif kind == 'circle':
            lines.append(Point(xy(shape.center)).buffer(shape.radius()/1e6, quad_segs=90).exterior)
        elif kind == 'arc':
            center = arc_center(shape.start, shape.mid, shape.end)
            if center is None:
                raise ValueError('Degenerate Edge.Cuts arc.')
            start = math.atan2(shape.start.y-center.y, shape.start.x-center.x)
            mid = math.atan2(shape.mid.y-center.y, shape.mid.x-center.x)
            end = math.atan2(shape.end.y-center.y, shape.end.x-center.x)
            sweep = (end-start) % (2*math.pi)
            if (mid-start) % (2*math.pi) > sweep:
                sweep -= 2*math.pi
            radius = math.hypot(shape.start.x-center.x, shape.start.y-center.y)
            steps = max(2, math.ceil(abs(sweep)/math.radians(2)))
            points = [((center.x+radius*math.cos(start+sweep*i/steps))/1e6,
                       (center.y+radius*math.sin(start+sweep*i/steps))/1e6) for i in range(steps+1)]
            points[0], points[-1] = xy(shape.start), xy(shape.end)
            lines.append(LineString(points))
        else:
            raise ValueError(f'Thermal outline does not yet support Edge.Cuts {kind}.')
    if not lines:
        raise ValueError('Thermal analysis requires a closed Edge.Cuts outline.')
    polygons, cuts, dangles, invalid = polygonize_full(unary_union(lines))
    if not cuts.is_empty or not dangles.is_empty or not invalid.is_empty or polygons.is_empty:
        raise ValueError('Thermal analysis requires closed, noncrossing Edge.Cuts contours.')
    region = Polygon()
    for p in polygons.geoms:
        region = region.symmetric_difference(Polygon(p.exterior))
    if region.is_empty or not region.is_valid:
        raise ValueError('Invalid thermal board outline.')
    return region


class ThermalGrid:
    def __init__(self, region, settings, budget_gb=8, retained_bytes=0, progress=None):
        self.progress = progress
        settings.validate()
        if region.is_empty or not region.is_valid or region.area <= 0:
            raise ValueError('Thermal domain must be a valid board area.')
        self.settings, self.region = settings, region
        self.step = h = settings.spacing_mm
        xmin, ymin, xmax, ymax = region.bounds
        self.origin = np.array([xmin, ymin])
        nx, ny = math.ceil((xmax-xmin)/h), math.ceil((ymax-ymin)/h)
        if nx < 1 or ny < 1:
            raise ValueError('Empty thermal domain.')
        check_budget(nx*ny*1000, budget_gb, retained_bytes)
        xx, yy = np.meshgrid(xmin+(np.arange(nx)+.5)*h, ymin+(np.arange(ny)+.5)*h)
        centers = np.column_stack((xx.ravel(), yy.ravel()))
        areas = np.empty(len(centers))
        for start in range(0, len(centers), 32768):
            c = centers[start:start+32768]
            cells = shapely.box(c[:, 0]-h/2, c[:, 1]-h/2, c[:, 0]+h/2, c[:, 1]+h/2)
            areas[start:start+len(c)] = shapely.area(shapely.intersection(cells, region))
            if progress:
                progress(.2*(start+len(c))/len(centers), 'Building thermal grid')
        valid = areas > h*h*1e-9
        self.xy, self.areas = centers[valid], areas[valid]*1e-6
        self.ids = np.full(nx*ny, -1, dtype=np.int32)
        self.ids[valid] = np.arange(valid.sum())
        self.ids = self.ids.reshape(ny, nx)
        self.heat = np.zeros(valid.sum())
        self.tree = None

    def deposit(self, points, watts):
        points, watts = np.asarray(points), np.asarray(watts)
        if np.any(~np.isfinite(watts)) or np.any(watts < 0):
            raise ValueError('Thermal heat must be finite and nonnegative.')
        if not len(points):
            return
        if not np.all(shapely.intersects_xy(self.region, points[:, 0], points[:, 1])):
            raise ValueError('A thermal heat source lies outside Edge.Cuts.')
        indices = np.floor((points[:, :2]-self.origin)/self.step).astype(int)
        indices[:, 0] = np.clip(indices[:, 0], 0, self.ids.shape[1]-1)
        indices[:, 1] = np.clip(indices[:, 1], 0, self.ids.shape[0]-1)
        nodes = self.ids[indices[:, 1], indices[:, 0]]
        missing = nodes < 0
        if missing.any():
            if self.tree is None:
                self.tree = cKDTree(self.xy)
            distance, near = self.tree.query(points[missing, :2])
            if np.any(distance > self.step*1.5):
                raise ValueError('Heat source cannot be mapped to a thermal cell.')
            nodes[missing] = near
        np.add.at(self.heat, nodes, watts)

    def solve(self, stackup, copper, backend='auto', log=None):
        substrate_mm = sum(s['thickness_mm'] for s in stackup.get('substrate', []))
        if not math.isfinite(substrate_mm) or substrate_mm <= 0:
            raise ValueError('Thermal analysis needs a positive physical substrate thickness.')
        rows, cols, data = [], [], []
        h, settings = self.step, self.settings
        for direction, (a, b) in enumerate([(self.ids[:, :-1], self.ids[:, 1:]), (self.ids[:-1], self.ids[1:])]):
            valid = (a >= 0) & (b >= 0)
            u, v = a[valid], b[valid]
            for start in range(0, len(u), 32768):
                ua, va = u[start:start+32768], v[start:start+32768]
                center = (self.xy[ua]+self.xy[va])/2
                delta = self.xy[va]-self.xy[ua]
                tangent = np.column_stack((-delta[:, 1], delta[:, 0]))/2
                faces = shapely.linestrings(np.stack((center-tangent, center+tangent), axis=1))
                width = shapely.length(shapely.intersection(faces, self.region))
                g = settings.substrate_k_w_mk*substrate_mm*1e-3*width/h
                for layer, geometry in copper.items():
                    thickness = stackup['copper'][layer]['thickness_mm']
                    if not math.isfinite(thickness) or thickness <= 0:
                        raise ValueError('Thermal copper thickness must be finite and positive.')
                    clipped = shapely.intersection(geometry, self.region)
                    width_cu = shapely.length(shapely.intersection(faces, clipped))
                    g += settings.copper_k_w_mk*thickness*1e-3*width_cu/h
                rows.extend((ua, va, ua, va)); cols.extend((ua, va, va, ua)); data.extend((g, g, -g, -g))
                if self.progress:
                    self.progress(.45+.3*(direction+(start+len(ua))/max(1, len(u)))/2, 'Assembling thermal conductance')
        cooling = 2*settings.convection_w_m2k*self.areas
        matrix = sparse.coo_matrix((np.concatenate(data) if data else [],
                                   (np.concatenate(rows) if rows else [], np.concatenate(cols) if cols else [])),
                                  shape=(len(self.xy), len(self.xy))).tocsr()+sparse.diags(cooling)
        if self.progress:
            self.progress(.8, 'Solving thermal temperatures')
        rise, diagnostics = solve_reduced(matrix, self.heat, backend, log)
        residual = float(np.max(np.abs(matrix@rise-self.heat)))
        rejected = float(cooling@rise)
        heat = float(self.heat.sum())
        if np.any(~np.isfinite(rise)) or np.min(rise) < -1e-7 or residual > 1e-8*max(1, heat) or abs(rejected-heat) > 1e-7*max(1, heat):
            raise ValueError('Thermal solve failed finite-value or heat-balance checks.')
        if self.progress:
            self.progress(1, 'Thermal solve validated')
        return dict(xy=self.xy, temperature_c=settings.ambient_c+rise, settings=settings, region=self.region,
                    input_w=heat, rejected_w=rejected, balance_w=abs(rejected-heat),
                    area_m2=float(self.areas.sum()), diagnostics=diagnostics)


def add_copper_heat(grid, results, progress=None):
    """G*(Va-Vb)^2 per physical edge, half to each endpoint; includes via loss."""
    total = 0
    for index, (name, result) in enumerate(results.items()):
        mesh, voltage = result['mesh'], result['results']
        edges = copper_edges(mesh)
        rail_heat = 0
        for start in range(0, len(edges.u), 131072):
            u, v = edges.u[start:start+131072], edges.v[start:start+131072]
            g = edges.g[start:start+131072]
            if hasattr(voltage, 'array'):
                delta = voltage.array[u]-voltage.array[v]
            else:
                delta = np.array([voltage.get(int(a), np.nan)-voltage.get(int(b), np.nan) for a, b in zip(u, v)])
            power = np.nan_to_num(g*delta**2, nan=0)
            active = power > 0
            def coords(ids):
                return mesh.node_coords.take(ids) if hasattr(mesh.node_coords, 'take') else np.array([mesh.node_coords[int(n)] for n in ids])
            grid.deposit(coords(u[active]), power[active]/2)
            grid.deposit(coords(v[active]), power[active]/2)
            rail_heat += float(power.sum())
            if progress:
                progress((index+(start+len(u))/max(1, len(edges.u)))/len(results))
        expected = result['diagnostics']['copper_loss_w']
        if not np.isclose(rail_heat, expected, rtol=1e-8, atol=1e-10):
            raise ValueError(f'Thermal copper power mapping mismatch on {name}.')
        total += rail_heat
        if progress:
            progress((index+1)/len(results))
    return total
