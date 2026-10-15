"""Conservative coarse-to-fine voltage guesses; full residual checks still apply."""
import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree


def _coordinates(mesh):
    nodes = (np.arange(len(mesh.nodes), dtype=np.int64) if isinstance(mesh.nodes, range) and mesh.nodes == range(len(mesh.nodes))
             else np.asarray(mesh.nodes, dtype=np.int64))
    if hasattr(mesh.node_coords, 'take'):
        coordinates = mesh.node_coords.take(nodes)
    else:
        coordinates = np.asarray([mesh.node_coords[int(node)] for node in nodes], dtype=float)
    return nodes, coordinates


def _islands(mesh, nodes):
    compact = isinstance(mesh.nodes, range) and mesh.nodes == range(len(mesh.nodes))
    if len(mesh.G_coo_data):
        row, col = np.asarray(mesh.G_coo_row), np.asarray(mesh.G_coo_col)
        mask = (row < col) & (np.asarray(mesh.G_coo_data) < 0)
        row, col = row[mask], col[mask]
    elif mesh.edges:
        row, col = np.asarray(mesh.edges)[:, :2].T.astype(np.int64)
    else:
        return np.arange(len(nodes), dtype=np.int32)
    if not compact:
        index = {int(node): i for i, node in enumerate(nodes)}
        row = np.fromiter((index[int(node)] for node in row), dtype=np.int64, count=len(row))
        col = np.fromiter((index[int(node)] for node in col), dtype=np.int64, count=len(col))
    graph = coo_matrix((np.ones(len(row), dtype=np.int8), (row, col)), shape=(len(nodes), len(nodes))).tocsr()
    return connected_components(graph, directed=False)[1]


def _layer_query(mesh, nodes, xyz, values, layer):
    lookup = getattr(mesh, 'grid_lookup', None)
    compact = isinstance(mesh.nodes, range) and mesh.nodes == range(len(mesh.nodes))
    if lookup is not None and compact and layer in lookup.layers and lookup.step > 0:
        plane = lookup.grid[lookup.layers[layer]]
        origin = np.asarray(lookup.origin)
        def query(points):
            position = np.rint((points-origin)/lookup.step).astype(np.int64)
            valid = ((position[:, 0] >= 0) & (position[:, 0] < plane.shape[1]) &
                     (position[:, 1] >= 0) & (position[:, 1] < plane.shape[0]))
            nearest = np.zeros(len(points), dtype=np.int64)
            nearest[valid] = plane[position[valid, 1], position[valid, 0]]
            valid &= nearest >= 0
            nearest[~valid] = 0
            valid &= np.isfinite(values[nearest])
            distance = np.linalg.norm(origin+position*lookup.step-points, axis=1)
            distance[~valid] = np.inf
            return distance, nearest
        return query
    indices = np.flatnonzero((xyz[:, 2] == layer) & np.isfinite(values))
    if len(indices) == 0:
        return None
    tree = cKDTree(xyz[indices, :2])
    def query(points):
        distance, nearest = tree.query(points)
        return distance, indices[nearest]
    return query


def interpolate_voltage(coarse, fine_mesh):
    """Return NaN wherever an unambiguous same-island interpolation is unavailable.

    Corners must be present, powered, on the same layer and copper island. Each
    fine island must have coincident powered coarse nodes identifying that island;
    no nearest-neighbour extrapolation across holes, gaps or missing coarse copper.
    """
    if len(fine_mesh.node_coords) != len(fine_mesh.nodes) or len(coarse['mesh'].node_coords) != len(coarse['mesh'].nodes):
        return np.full(len(fine_mesh.nodes), np.nan)
    fine_nodes, fine_xyz = _coordinates(fine_mesh)
    result = np.full(len(fine_nodes), np.nan)
    coarse_mesh = coarse['mesh']
    coarse_nodes, coarse_xyz = _coordinates(coarse_mesh)
    values = coarse['results']
    if hasattr(values, 'array'):
        coarse_values = values.array[coarse_nodes]
    else:
        coarse_values = np.fromiter((values.get(int(node), np.nan) for node in coarse_nodes), float, count=len(coarse_nodes))
    if len(coarse_xyz) == 0 or len(fine_xyz) == 0:
        return result
    coarse_island = _islands(coarse_mesh, coarse_nodes)
    fine_island = _islands(fine_mesh, fine_nodes)
    step = float(getattr(coarse_mesh, 'grid_step', 0))
    tolerance = max(1e-9, step*1e-7)
    queries = {}
    anchor_fine, anchor_coarse = [], []
    for layer in np.unique(fine_xyz[:, 2]):
        fi = np.flatnonzero(fine_xyz[:, 2] == layer)
        query = _layer_query(coarse_mesh, coarse_nodes, coarse_xyz, coarse_values, layer)
        if query is None:
            continue
        queries[layer] = query, fi
        for offset in range(0, len(fi), 65536):
            ids = fi[offset:offset+65536]
            distance, nearest = query(fine_xyz[ids, :2])
            exact = distance <= tolerance
            ids, nearest = ids[exact], nearest[exact]
            anchor_fine.append(fine_island[ids])
            anchor_coarse.append(coarse_island[nearest])
    if not anchor_fine:
        return result
    pairs = np.unique(np.column_stack((np.concatenate(anchor_fine), np.concatenate(anchor_coarse))), axis=0)
    unique_fine, counts = np.unique(pairs[:, 0], return_counts=True)
    allowed = np.full(int(fine_island.max())+1, -1, dtype=np.int32)
    # A refined island joining previously separated islands gets no interpolated guess.
    single = unique_fine[counts == 1]
    selected = np.isin(pairs[:, 0], single)
    allowed[pairs[selected, 0]] = pairs[selected, 1]
    origin = np.asarray(getattr(coarse_mesh, 'grid_origin', (0, 0)), dtype=float)
    for query, fi in queries.values():
        for offset in range(0, len(fi), 65536):
            ids = fi[offset:offset+65536]
            xy = fine_xyz[ids, :2]
            if step <= 0:
                distance, nearest = query(xy)
                valid = (distance <= tolerance) & (allowed[fine_island[ids]] == coarse_island[nearest])
                result[ids[valid]] = coarse_values[nearest[valid]]
                continue
            position = (xy-origin)/step
            # Stabilize exact grid-line coordinates before floor.
            position = np.where(np.abs(position-np.rint(position)) <= 1e-7, np.rint(position), position)
            floor = np.floor(position)
            fraction = position-floor
            weighted = np.zeros(len(ids))
            valid = allowed[fine_island[ids]] >= 0
            for corner in ((0, 0), (1, 0), (0, 1), (1, 1)):
                target = origin+(floor+corner)*step
                distance, nearest = query(target)
                weight = ((fraction[:, 0] if corner[0] else 1-fraction[:, 0]) *
                          (fraction[:, 1] if corner[1] else 1-fraction[:, 1]))
                used = weight > 1e-12
                valid &= ~used | ((distance <= tolerance) &
                                  (allowed[fine_island[ids]] == coarse_island[nearest]))
                # Missing zero-weight corners must not contaminate an exact
                # powered corner with 0*NaN from the placeholder node ID.
                weighted += weight*np.nan_to_num(coarse_values[nearest], nan=0.0)
            result[ids[valid]] = weighted[valid]
    return result
