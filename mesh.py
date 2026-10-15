
import sys
import math
import time
from array import array
from compact_storage import Coordinates, GridLookup
from resource_budget import default_budget_gb, grid_estimate, check_budget
from copper_geometry import pad_geometry, drill_geometry

# Use explicit check if needed, but we assume kipy objects are passed
def to_mm(val):
    return val / 1e6

try:
    import numpy as np
    from shapely.geometry import Point, box, LineString
    from shapely.prepared import prep
    import matplotlib.path
except ImportError:
    np = None
    Point = box = prep = None
    matplotlib = None

class Mesh:
    def __init__(self):
        self.nodes = [] # List of node_ids (integers)
        self.node_coords = {} # { node_id: (x_mm, y_mm, layer_id) }
        self.edges = [] # List of (node_a, node_b, conductance_G) - Legacy support
        self.node_map = {} # { (x_idx, y_idx, layer_id): node_id }
        self.grid_origin = (0, 0)
        self.grid_step = 0
        
        # New sparse matrix components
        self.G_coo_data = array('d') # [g, g, -g, -g, ...]
        self.G_coo_row = array('i')
        self.G_coo_col = array('i')
        self.G_final_csr = None # To be filled by solver or mesher if configured
        
    def add_edge_direct(self, u, v, g):
        """Adds an edge directly to the sparse data arrays."""
        # G[u,u] += g
        self.G_coo_row.append(u)
        self.G_coo_col.append(u)
        self.G_coo_data.append(g)
        
        # G[v,v] += g
        self.G_coo_row.append(v)
        self.G_coo_col.append(v)
        self.G_coo_data.append(g)
        
        # G[u,v] -= g
        self.G_coo_row.append(u)
        self.G_coo_col.append(v)
        self.G_coo_data.append(-g)
        
        # G[v,u] -= g
        self.G_coo_row.append(v)
        self.G_coo_col.append(u)
        self.G_coo_data.append(-g)

class Mesher:
    def __init__(self, board, debug=False, log_callback=None, memory_budget_gb=None):
        self._last_progress = 0
        self.memory_budget_gb = default_budget_gb() if memory_budget_gb is None else memory_budget_gb
        self.board = board
        self.debug = debug
        self.log_callback = log_callback
        if np is None or Point is None or matplotlib is None:
            raise ImportError("NumPy, Shapely, and Matplotlib are required for Meshing.")
    
    def _get_val(self, obj, attr_name, default=None):
        """Robustly get attribute value from object (property or getter)."""
        if obj is None: return default
        if hasattr(obj, attr_name):
            val = getattr(obj, attr_name)
            if val is not None: return val
        for prefix in ["get_", ""]:
            method_name = prefix + attr_name
            if hasattr(obj, method_name):
                try:
                    val = getattr(obj, method_name)()
                    if val is not None: return val
                except: pass
        return default

    def _get_board_items(self, attr_name):
        """Robustly get items from board (property or getter)."""
        if hasattr(self.board, attr_name):
            return getattr(self.board, attr_name)
        method_name = f"get_{attr_name}"
        if hasattr(self.board, method_name):
            try: return getattr(self.board, method_name)()
            except: pass
        return []

    def _log(self, msg):
        """Helper to log debug messages."""
        if self.debug and self.log_callback:
            self.log_callback(f"[MESH] {msg}")

    def generate_mesh(self, net_name, geometry_by_layer, stackup, grid_size_mm=0.5):
        """
        Generates a resistive mesh from the geometry using vectorized operations.
        """
        mesh = Mesh()
        if not math.isfinite(grid_size_mm) or grid_size_mm <= 0:
            raise ValueError("Mesh spacing must be positive and finite.")
        mesh.net_name = net_name
        mesh.copper_layers = stackup.get('copper', {})
        mesh.grid_step = grid_size_mm
        mesh.node_coords = Coordinates()
        _, estimate = grid_estimate(geometry_by_layer, grid_size_mm)
        check_budget(estimate, self.memory_budget_gb)
        
        # 1. Calculate Bounding Box
        min_x, min_y, max_x, max_y = float('inf'), float('inf'), float('-inf'), float('-inf')
        
        has_geometry = False
        valid_layers = []
        for lid, poly in geometry_by_layer.items():
            if poly.is_empty: continue
            has_geometry = True
            valid_layers.append(lid)
            b = poly.bounds
            min_x = min(min_x, b[0])
            min_y = min(min_y, b[1])
            max_x = max(max_x, b[2])
            max_y = max(max_y, b[3])
            
        if not has_geometry:
            return mesh

        # Pad bounds slightly
        pad = grid_size_mm
        min_x -= pad
        min_y -= pad
        max_x += pad
        max_y += pad
        
        mesh.grid_origin = (min_x, min_y)
        
        # 2. Create Grid Coordinates
        width_mm = max_x - min_x
        height_mm = max_y - min_y
        
        nx = int(math.ceil(width_mm / grid_size_mm))
        ny = int(math.ceil(height_mm / grid_size_mm))
        
        # Suppress floating addition drift at exact copper boundaries. This
        # precision is a million times finer than KiCad's nanometre coordinates;
        # copper geometry is not buffered or widened.
        x_coords = np.round(np.linspace(min_x, min_x + (nx * grid_size_mm), nx + 1), 12)
        y_coords = np.round(np.linspace(min_y, min_y + (ny * grid_size_mm), ny + 1), 12)
        
        # Coordinates are generated a stripe at a time during rasterization.
        stripe_rows = max(1, 131072//(nx+1))

        if self.debug:
            self._log(f"Grid setup: {nx+1}x{ny+1} points, bounds ({min_x:.1f},{min_y:.1f}) to ({max_x:.1f},{max_y:.1f})")

        # 3. Vectorized Rasterization
        # We will build a 3D boolean mask: presence[layer_idx, y_idx, x_idx]
        # But layer IDs are sparse (e.g. 0, 1, 31). So we map them.
        
        sorted_layers = sorted(valid_layers)
        layer_map = { lid: idx for idx, lid in enumerate(sorted_layers) }
        
        # node_indices: [layer, y, x] -> node_id (or -1 if empty)
        node_grid = np.full((len(sorted_layers), ny + 1, nx + 1), -1, dtype=np.int32)
        mesh.grid_lookup = GridLookup(node_grid, layer_map, mesh.grid_origin, grid_size_mm)
        
        node_counter = 0
        
        for layer_index, lid in enumerate(sorted_layers):
            poly = geometry_by_layer[lid]
            if poly.is_empty: continue
            from shapely import prepare
            prepare(poly)
            
            # Restrict temporary point coordinates to bounded stripes.
            if self.log_callback:
                self.log_callback(f"Rasterizing {net_name}, layer {lid}: {(ny+1)*(nx+1):,} grid points.")
            mask_2d = np.zeros((ny+1, nx+1), dtype=bool)
            from shapely import intersects_xy
            for y_start in range(0, ny+1, stripe_rows):
                ys = y_coords[y_start:y_start+stripe_rows]
                xs_grid, ys_grid = np.meshgrid(x_coords, ys)
                mask_2d[y_start:y_start+len(ys)] = intersects_xy(poly, xs_grid, ys_grid)
                callback = getattr(self, 'progress_callback', None)
                if callback:
                    callback(.9*(layer_index+.25*(y_start+len(ys))/(ny+1))/len(sorted_layers))
                now = time.perf_counter()
                if self.log_callback and now-self._last_progress > 2:
                    self.log_callback(f'Rasterization {net_name}: {(y_start+len(ys))/(ny+1):.0%} on layer {lid}.')
                    self._last_progress = now

            # Assign Node IDs
            count_on_layer = np.count_nonzero(mask_2d)
            if count_on_layer > 0:
                # Get indices where mask is true
                y_idxs, x_idxs = np.nonzero(mask_2d)
                
                # Generate new IDs
                new_ids = np.arange(node_counter, node_counter + count_on_layer)
                node_grid[layer_map[lid], y_idxs, x_idxs] = new_ids
                
                mesh.node_coords.append(np.column_stack((x_coords[x_idxs],
                                                         y_coords[y_idxs],
                                                         np.full(count_on_layer, lid))))
                node_counter += count_on_layer
                mesh.nodes = range(node_counter)

                # 4. Generate Lateral Edges (Vectorized)
                # Physical props
                copper_info = stackup.get('copper', {}).get(lid, {})
                thick = copper_info.get('thickness_mm', 0.035)
                rho = stackup.get('resistivity', 1.7e-5)
                g_lat_val = thick / rho
                if thick <= 0 or rho <= 0 or not math.isfinite(g_lat_val):
                    raise ValueError("Invalid copper thickness or resistivity.")
                
                # Horizontal Neighbors (x, y) <-> (x+1, y)
                # Check where node and right-neighbor both exist
                
                # mask_2d is boolean. node_grid has IDs.
                # Valid H edges: mask[:, :-1] & mask[:, 1:]
                
                # Right neighbors
                right_mask = mask_2d[:, :-1] & mask_2d[:, 1:]
                if np.any(right_mask):
                    y_r, x_r = np.nonzero(right_mask)
                    # Nodes at (y,x)
                    u_ids = node_grid[layer_map[lid], y_r, x_r]
                    # Nodes at (y, x+1)
                    v_ids = node_grid[layer_map[lid], y_r, x_r + 1]
                    
                    self._batch_progress = lambda fraction: self.progress_callback(.9*(layer_index+.25+.35*fraction)/len(sorted_layers)) if getattr(self, 'progress_callback', None) else None
                    self._add_lateral_batch(mesh, poly, u_ids, v_ids, g_lat_val)
                
                # Vertical (Top) Neighbors (x, y) <-> (x, y+1)
                top_mask = mask_2d[:-1, :] & mask_2d[1:, :]
                if np.any(top_mask):
                    y_t, x_t = np.nonzero(top_mask)
                    # Nodes at (y, x)
                    u_ids = node_grid[layer_map[lid], y_t, x_t]
                    # Nodes at (y+1, x)
                    v_ids = node_grid[layer_map[lid], y_t + 1, x_t]
                    
                    self._batch_progress = lambda fraction: self.progress_callback(.9*(layer_index+.6+.4*fraction)/len(sorted_layers)) if getattr(self, 'progress_callback', None) else None
                    self._add_lateral_batch(mesh, poly, u_ids, v_ids, g_lat_val)

            if self.debug:
                self._log(f"  Layer {lid} vectorized mesh: {count_on_layer} nodes.")

        # 5. Vertical Connections (Vias & PTH)
        if self.log_callback:
            self.log_callback("Adding vertical interconnects...")
            self.log_callback(f"Via/PTH plating assumption: {stackup.get('via_plating_mm', 0.025)*1000:g} micrometres (finished-hole model).")
        
        # Helper to check if item matches net
        def match_net(item, name):
            net = self._get_val(item, 'net')
            n_name = self._get_val(net, 'name', "")
            return n_name == name

        # Get vias using proper API
        vias = self._get_board_items('vias')
        for via in vias:
            if match_net(via, net_name):
                self._add_vertical_link(mesh, via, stackup)
                    
        footprints = self._get_board_items('footprints')
        for fp in footprints:
            pads = self._get_val(fp, 'pads')
            if pads is None:
                defn = self._get_val(fp, 'definition')
                pads = self._get_val(defn, 'pads', [])
                
            for pad in pads:
                if match_net(pad, net_name):
                    # Check if PTH using numeric pad_type value
                    # pad_type: 0=SMD, 1=PTH, 2=CONN, 3=NPTH
                    p_type_val = self._get_val(pad, 'pad_type', None)
                    p_type_str = str(self._get_val(pad, 'type', ''))
                    
                    is_pth = (p_type_val == 1) or ('THROUGH' in p_type_str and 'NON' not in p_type_str)
                    
                    if is_pth:
                            # Drill Size
                            drill_size = self._get_val(pad, 'drill_size')
                            if drill_size is None:
                                drill_size = self._get_val(self._get_val(self._get_val(pad, 'padstack'), 'drill'), 'diameter')
                            d_x = self._get_val(drill_size, 'x', 0)
                            d_y = self._get_val(drill_size, 'y', d_x)
                            if d_x <= 0 or d_y <= 0:
                                raise ValueError("Missing plated pad drill dimensions.")
                            
                            pos = self._get_val(pad, 'position')
                            
                            # Get layers from padstack
                            layers = self._get_val(pad, 'layers')
                            if not layers:
                                ps = self._get_val(pad, 'padstack')
                                if ps:
                                    layers = self._get_val(ps, 'layers')
                            
                            self._add_vertical_stack(mesh, pos, 
                                                    layers=layers, 
                                                    diameter=to_mm(d_x), 
                                                    stackup=stackup, slot_length=to_mm(max(d_x, d_y)),
                                                    slot_width=to_mm(min(d_x, d_y)),
                                                    pad_shapes=pad_geometry(self.board, pad, stackup['copper']),
                                                    hole_shape=drill_geometry(pad))

        callback = getattr(self, 'progress_callback', None)
        if callback:
            callback(1)
        return mesh

    def _add_lateral_batch(self, mesh, poly, u_ids, v_ids, sheet_g):
        from shapely import linestrings, covers, intersection, length
        # Use vectorized GEOS predicates; preserve the exact finite-volume faces.
        for start in range(0, len(u_ids), 32768):
            u, v = u_ids[start:start+32768], v_ids[start:start+32768]
            a = mesh.node_coords.take(u)[:, :2]
            b = mesh.node_coords.take(v)[:, :2]
            connected = covers(poly, linestrings(np.stack((a, b), axis=1)))
            middle = (a+b)/2
            offset = np.zeros_like(middle)
            horizontal = a[:, 0] != b[:, 0]
            offset[horizontal, 1] = mesh.grid_step/2
            offset[~horizontal, 0] = mesh.grid_step/2
            faces = linestrings(np.stack((middle-offset, middle+offset), axis=1))
            # Interior faces have full width; intersect only clipped boundary faces.
            full = covers(poly, faces)
            widths = np.full(len(u), mesh.grid_step)
            clipped = connected & ~full
            widths[clipped] = length(intersection(poly, faces[clipped]))
            conductance = sheet_g*widths/mesh.grid_step
            valid = connected & (conductance > 0)
            self._bulk_add_edges(mesh, u[valid], v[valid], conductance[valid])
            now = time.perf_counter()
            callback = getattr(self, '_batch_progress', None)
            if callback:
                callback(min(start+32768, len(u_ids))/len(u_ids))
            if self.log_callback and now-self._last_progress > 2:
                self.log_callback(f'Copper connections: {min(start+32768, len(u_ids)):,}/{len(u_ids):,} in this direction.')
                self._last_progress = now

    def _add_lateral_edge(self, mesh, poly, u, v, sheet_g):
        """Finite-volume face width with no links across gaps or holes."""
        x1, y1, _ = mesh.node_coords[u]
        x2, y2, _ = mesh.node_coords[v]
        line = LineString([(x1, y1), (x2, y2)])
        if not poly.covers(line):
            return
        half = mesh.grid_step/2
        mx, my = (x1+x2)/2, (y1+y2)/2
        face = LineString([(mx, my-half), (mx, my+half)]) if x1 != x2 else LineString([(mx-half, my), (mx+half, my)])
        width = poly.intersection(face).length
        if width > 0:
            mesh.add_edge_direct(u, v, sheet_g * width / mesh.grid_step)

    def _bulk_add_edges(self, mesh, u_ids, v_ids, g):
        """Adds multiple edges at once to sparse arrays."""
        # This is where we gain massive speed in construction
        n = len(u_ids)
        # We need to replicate g for all edges
        gs = np.broadcast_to(g, (n,))
        neg_gs = -gs
        
        # Prepare arrays
        rows = []
        cols = []
        data = []
        
        # u,u
        rows.append(u_ids); cols.append(u_ids); data.append(gs)
        # v,v
        rows.append(v_ids); cols.append(v_ids); data.append(gs)
        # u,v
        rows.append(u_ids); cols.append(v_ids); data.append(neg_gs)
        # v,u
        rows.append(v_ids); cols.append(u_ids); data.append(neg_gs)
        
        # Concatenate and extend
        mesh.G_coo_row.frombytes(np.concatenate(rows).astype(np.int32, copy=False).tobytes())
        mesh.G_coo_col.frombytes(np.concatenate(cols).astype(np.int32, copy=False).tobytes())
        mesh.G_coo_data.frombytes(np.concatenate(data).astype(np.float64, copy=False).tobytes())

    def _calculate_vertical_g(self, layer_a, layer_b, stackup, diameter_mm, slot_length=None, slot_width=None):
        # Drill is the finished hole diameter; copper lies outside that hole.
        plating_thick = stackup.get('via_plating_mm', 0.025)
        if diameter_mm <= 0 or plating_thick <= 0:
            raise ValueError("Invalid drill or plating thickness.")
        width = slot_width if slot_width is not None else diameter_mm
        length = slot_length if slot_length is not None else diameter_mm
        perimeter = math.pi * width + 2 * (length-width)
        area = perimeter * plating_thick + math.pi * plating_thick**2
        rho = stackup.get('resistivity', 1.68e-5)
        order = stackup.get('layer_order', sorted(stackup['copper']))
        a, b = sorted((order.index(layer_a), order.index(layer_b)))
        h = stackup['copper'][layer_a].get('thickness_mm', 0.035)/2
        h += stackup['copper'][layer_b].get('thickness_mm', 0.035)/2
        for lid in order[a+1:b]:
            h += stackup['copper'][lid].get('thickness_mm', 0.035)
        found = set()
        for sub in stackup.get('substrate', []):
            sb = sub['between']
            if sb[0] in order and sb[1] in order:
                i, j = sorted((order.index(sb[0]), order.index(sb[1])))
                if a <= i < j <= b:
                    h += sub['thickness_mm']
                    found.update(range(i, j))
        if found != set(range(a, b)) or h <= 0 or rho <= 0:
            raise ValueError("Incomplete physical stackup for plated interconnect.")
        return area / (rho * h)

    def _nearest_compact(self, mesh, x, y, layer, radius, shape=None):
        from shapely import intersects_xy
        if hasattr(mesh, 'grid_lookup'):
            bounds = shape.bounds if shape is not None else (x-radius, y-radius, x+radius, y+radius)
            candidates = mesh.grid_lookup.window(layer, bounds)
            coordinates = mesh.node_coords.take(candidates)
            distance = np.hypot(coordinates[:, 0]-x, coordinates[:, 1]-y)
            valid = intersects_xy(shape, coordinates[:, 0], coordinates[:, 1]) if shape is not None else distance <= radius+1e-9
            candidates, distance = candidates[valid], distance[valid]
            return int(candidates[np.argmin(distance)]) if len(candidates) else None
        offset = 0
        best = None
        for part in mesh.node_coords.parts:
            if len(part) and int(part[0, 2]) == layer:
                candidates = np.flatnonzero((np.abs(part[:, 0]-x) <= radius+1e-9) &
                                           (np.abs(part[:, 1]-y) <= radius+1e-9))
                if len(candidates):
                    distance = np.hypot(part[candidates, 0]-x, part[candidates, 1]-y)
                    valid = intersects_xy(shape, part[candidates, 0], part[candidates, 1]) if shape is not None else distance <= radius+1e-9
                    candidates, distance = candidates[valid], distance[valid]
                    if len(candidates):
                        i = int(np.argmin(distance))
                        choice = (float(distance[i]), int(offset+candidates[i]))
                        if best is None or choice < best:
                            best = choice
            offset += len(part)
        return best[1] if best else None

    def _get_best_node_in_radius(self, mesh, x_mm, y_mm, layer, radius_mm):
        """Find a node for the via/pad on the given layer."""
        if hasattr(mesh.node_coords, 'parts'):
            return self._nearest_compact(mesh, x_mm, y_mm, layer, radius_mm)
        candidates = [(math.hypot(x-x_mm, y-y_mm), nid)
                      for nid, (x, y, lid) in mesh.node_coords.items()
                      if lid == layer and math.hypot(x-x_mm, y-y_mm) <= radius_mm + 1e-9]
        return min(candidates)[1] if candidates else None

    def _add_vertical_link(self, mesh, via, stackup):
        """Adds vertical connectivity for a via."""
        layers = self._get_val(via, 'layers')
        if not layers:
            ps = self._get_val(via, 'padstack')
            if ps:
                layers = self._get_val(ps, 'layers')
        
        if not layers:
            lp = self._get_val(via, 'layer_pair')
            if lp:
                s_lid, e_lid = min(lp), max(lp)
                all_cu = sorted(stackup['copper'].keys())
                layers = [l for l in all_cu if s_lid <= l <= e_lid]
            else:
                # Default to all copper layers
                layers = list(stackup['copper'].keys())

        pos = getattr(via, 'start', None)
        if not pos: 
             pos = getattr(via, 'position', None)
             
        if not pos: return

        x_mm, y_mm = to_mm(pos.x), to_mm(pos.y)
        dia_mm = to_mm(self._get_val(via, 'diameter', self._get_val(via, 'width', 0)))
        drill_mm = to_mm(self._get_val(via, 'drill_diameter', 0))
        if drill_mm <= 0 or dia_mm <= drill_mm:
            raise ValueError("Via drill/diameter unavailable or invalid.")
        radius_mm = dia_mm / 2.0
        
        hole = Point(x_mm, y_mm).buffer(drill_mm/2, quad_segs=64)
        annulus = Point(x_mm, y_mm).buffer(radius_mm, quad_segs=64).difference(hole)
        self._distributed_barrel(mesh, layers, stackup, drill_mm, hole,
                                 {lid: annulus for lid in layers}, label='Via')

    def _add_vertical_stack(self, mesh, pos, layers, diameter, stackup, slot_length=None, slot_width=None, pad_shapes=None, hole_shape=None):
        if layers is None or len(layers) == 0:
            layers = sorted(stackup['copper'].keys())
        else:
             layers = sorted(list(layers))
             
        x_mm, y_mm = to_mm(pos.x), to_mm(pos.y)
        radius_mm = max(slot_length or diameter, diameter) / 2.0 + stackup.get('via_plating_mm', 0.025)
        
        hole = hole_shape if hole_shape is not None else Point(x_mm, y_mm).buffer(diameter/2, quad_segs=64)
        shapes = pad_shapes if pad_shapes is not None else {lid: Point(x_mm, y_mm).buffer(radius_mm).difference(hole) for lid in layers}
        self._distributed_barrel(mesh, layers, stackup, diameter, hole, shapes,
                                 slot_length, slot_width, label='PTH pad')

    def _distributed_barrel(self, mesh, layers, stackup, diameter, hole, shapes,
                            slot_length=None, slot_width=None, label='Via'):
        from shapely import intersects_xy, line_interpolate_point, get_coordinates
        from scipy.spatial import cKDTree
        order = stackup.get('layer_order', sorted(stackup['copper']))
        boundary = hole.exterior
        samples = min(4096, max(64, int(math.ceil(8*boundary.length/mesh.grid_step))))
        points = get_coordinates(line_interpolate_point(boundary, (np.arange(samples)+.5)/samples, normalized=True))
        inner_hole = hole.buffer(-1e-9)
        contacts = []
        for layer in [lid for lid in order if lid in layers and lid in mesh.copper_layers]:
            shape = shapes.get(layer)
            if shape is None:
                continue
            if hasattr(mesh, 'grid_lookup'):
                candidates = mesh.grid_lookup.window(layer, shape.bounds)
                coordinates = mesh.node_coords.take(candidates)
                valid = intersects_xy(shape, coordinates[:, 0], coordinates[:, 1]) & ~intersects_xy(inner_hole, coordinates[:, 0], coordinates[:, 1])
                candidates, xy = candidates[valid], coordinates[valid, :2]
            else:
                selected = [(nid, (x, y)) for nid, (x, y, lid) in mesh.node_coords.items()
                            if lid == layer and shape.covers(Point(x, y)) and not inner_hole.covers(Point(x, y))]
                candidates = np.array([nid for nid, _ in selected], dtype=int)
                xy = np.array([point for _, point in selected])
            if not len(candidates):
                raise ValueError(f'{label} has no annular mesh contact on layer {layer}. Reduce mesh spacing.')
            distance, nearest = cKDTree(xy).query(points)
            if np.max(distance) > mesh.grid_step*math.sqrt(2)+1e-9:
                raise ValueError(f'{label} annular contact is unresolved on layer {layer}. Reduce mesh spacing.')
            contacts.append((layer, candidates[nearest]))
        for (la, a), (lb, b) in zip(contacts, contacts[1:]):
            pairs, counts = np.unique(np.column_stack((a, b)), axis=0, return_counts=True)
            conductance = self._calculate_vertical_g(la, lb, stackup, diameter, slot_length, slot_width)
            self._bulk_add_edges(mesh, pairs[:, 0], pairs[:, 1], conductance*counts/samples)


