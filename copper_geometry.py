"""Shared, layer-specific copper geometry for extraction and terminal mapping."""
import math
from shapely.geometry import Polygon, Point, LineString, box
from shapely import affinity
from shapely import make_valid
from shapely.ops import unary_union
from shapely.validation import explain_validity


def value(obj, name, default=None):
    result = getattr(obj, name, default) if obj is not None else default
    return result() if callable(result) else result


def polygon_from_api(poly):
    def coordinates(outline):
        points = []
        for node in value(outline, 'nodes', []):
            if value(node, 'has_arc', False):
                from kipy.geometry import arc_center
                arc = node.arc
                center = arc_center(arc.start, arc.mid, arc.end)
                if center is None:
                    raise ValueError("Degenerate arc in copper polygon.")
                start = math.atan2(arc.start.y-center.y, arc.start.x-center.x)
                mid = math.atan2(arc.mid.y-center.y, arc.mid.x-center.x)
                end = math.atan2(arc.end.y-center.y, arc.end.x-center.x)
                sweep = (end-start) % (2*math.pi)
                angle = sweep if (mid-start) % (2*math.pi) <= sweep else sweep-2*math.pi
                if arc.start == arc.end:
                    angle = 2*math.pi
                radius = math.hypot(arc.start.x-center.x, arc.start.y-center.y)
                steps = max(8, int(math.ceil(abs(angle) / math.radians(2))))
                points.extend(((center.x + radius*math.cos(start+angle*i/steps))/1e6,
                               (center.y + radius*math.sin(start+angle*i/steps))/1e6)
                              for i in range(steps+1))
            else:
                p = value(node, 'point')
                if p is not None:
                    points.append((p.x/1e6, p.y/1e6))
        return points
    outline = coordinates(poly.outline)
    if len(outline) < 3:
        raise ValueError("Missing copper polygon outline.")
    result = Polygon(outline, [coordinates(h) for h in value(poly, 'holes', [])])
    if not result.is_valid:
        # Filled-zone outlines may encode holes with doubled, zero-width seams.
        # Recover the area polygons; the leftover lines are not copper surfaces.
        repaired = make_valid(result)
        def surfaces(shape):
            if shape.geom_type == 'Polygon':
                return [shape]
            return [part for child in getattr(shape, 'geoms', ()) for part in surfaces(child)]
        parts = surfaces(repaired)
        area_tolerance = max(1e-12, abs(result.area)*1e-10)
        if not parts or abs(sum(part.area for part in parts)-result.area) > area_tolerance:
            raise ValueError(f"Invalid copper polygon from KiCad: {explain_validity(result)}. Repair would change copper area.")
        result = unary_union(parts)
        if not result.is_valid or result.is_empty:
            raise ValueError("Invalid copper polygon from KiCad after boundary reconstruction.")
    return result


def pad_geometry(board, pad, copper_layers):
    """Return board-coordinate copper polygons; never infer unspecified layers."""
    ps = value(pad, 'padstack')
    layers = value(pad, 'layers') or value(ps, 'layers', [])
    layers = [lid for lid in layers if lid in copper_layers]
    result = {}
    if hasattr(board, 'get_pad_shapes_as_polygons'):
        for lid in layers:
            poly = board.get_pad_shapes_as_polygons(pad, lid)
            if poly is not None:
                try:
                    result[lid] = polygon_from_api(poly)
                except ValueError as exc:
                    raise ValueError(f"Pad {value(pad, 'number', '?')} on net {value(value(pad, 'net'), 'name', '?')}, layer {lid}: {exc}") from exc
                if value(pad, 'pad_type') == 1:
                    result[lid] = result[lid].difference(drill_geometry(pad))
        return result
    # Simple adapter fallback for offline fixtures; IPC uses authoritative polygons.
    pos = value(pad, 'position')
    size = value(pad, 'size')
    if pos is None or size is None:
        raise ValueError("Pad geometry unavailable; update kicad-python or check the KiCad API.")
    w, h = size.x/1e6, size.y/1e6
    shape = value(pad, 'shape')
    if shape in (1, 'PSS_CIRCLE'):
        poly = Point(0, 0).buffer(w/2, quad_segs=32)
    elif shape in (2, 'PSS_RECTANGLE'):
        poly = box(-w/2, -h/2, w/2, h/2)
    elif shape in (3, 'PSS_OVAL'):
        radius = min(w, h)/2
        ends = [(-(w-h)/2, 0), ((w-h)/2, 0)] if w >= h else [(0, -(h-w)/2), (0, (h-w)/2)]
        poly = LineString(ends).buffer(radius, quad_segs=32)
    else:
        raise ValueError("Unsupported pad shape in offline adapter.")
    angle = value(pad, 'rotation', 0)
    angle = value(angle, 'degrees', angle)
    poly = affinity.translate(affinity.rotate(poly, angle), pos.x/1e6, pos.y/1e6)
    for lid in layers:
        result[lid] = poly
    return result


def terminal_nodes(mesh, shapes):
    nodes = []
    if hasattr(mesh, 'grid_lookup'):
        from shapely import intersects_xy
        for lid, shape in shapes.items():
            candidates = mesh.grid_lookup.window(lid, shape.bounds)
            coordinates = mesh.node_coords.take(candidates)
            accepted = intersects_xy(shape, coordinates[:, 0], coordinates[:, 1])
            nodes.extend(candidates[accepted].tolist())
        return sorted(set(nodes))
    if hasattr(mesh.node_coords, 'parts'):
        import numpy as np
        from shapely import intersects_xy
        offset = 0
        for part in mesh.node_coords.parts:
            if len(part):
                shape = shapes.get(int(part[0, 2]))
                if shape is not None:
                    xmin, ymin, xmax, ymax = shape.bounds
                    candidates = np.flatnonzero((part[:, 0] >= xmin) & (part[:, 0] <= xmax) &
                                                (part[:, 1] >= ymin) & (part[:, 1] <= ymax))
                    accepted = intersects_xy(shape, part[candidates, 0], part[candidates, 1])
                    nodes.extend((offset+candidates[accepted]).tolist())
            offset += len(part)
        return sorted(set(nodes))
    for lid, shape in shapes.items():
        nodes.extend(nid for nid, (x, y, layer) in mesh.node_coords.items()
                     if layer == lid and shape.covers(Point(x, y)))
    return sorted(set(nodes))


def drill_geometry(pad):
    """Finished hole/slot in board coordinates (also removed after copper unions)."""
    ps = value(pad, 'padstack')
    drill = value(value(ps, 'drill'), 'diameter')
    if drill is None or drill.x <= 0 or drill.y <= 0:
        raise ValueError("PTH pad drill unavailable.")
    pos = pad.position
    w, h = drill.x/1e6, drill.y/1e6
    ends = [(-(w-h)/2, 0), ((w-h)/2, 0)] if w >= h else [(0, -(h-w)/2), (0, (h-w)/2)]
    hole = LineString(ends).buffer(min(w, h)/2, quad_segs=32)
    angle = value(value(ps, 'angle'), 'degrees', 0)
    return affinity.translate(affinity.rotate(hole, angle), pos.x/1e6, pos.y/1e6)
