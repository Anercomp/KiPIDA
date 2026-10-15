"""Development benchmark: compare retained operators and refined warm starts."""
import argparse
import json
from pathlib import Path
import time

import numpy as np

from compact_storage import Coordinates, GridLookup
from mesh import Mesh
from models import PowerRail, UnifiedSource, UnifiedLoad, ComponentRef
from network_cache import PreparedNetworkCache
from network_solver import solve_system, stamp_edges


def sheet(side):
    mesh = Mesh(); mesh.nodes = range(side*side); mesh.net_name = 'VIN'
    mesh.grid_step = 50/(side-1)
    y, x = np.indices((side, side))
    mesh.node_coords = Coordinates()
    mesh.node_coords.append(np.column_stack((x.ravel()*mesh.grid_step,
                                             y.ravel()*mesh.grid_step, np.zeros(side*side))))
    grid = np.arange(side*side, dtype=np.int32).reshape(side, side)
    mesh.grid_lookup = GridLookup(grid[np.newaxis], {0:0}, (0,0), mesh.grid_step)
    u = np.concatenate((grid[:, :-1].ravel(), grid[:-1, :].ravel()))
    v = np.concatenate((grid[:, 1:].ravel(), grid[1:, :].ravel()))
    stamp_edges(mesh, u, v, np.full(len(u), 10.))
    mesh.benchmark_side = side
    return mesh


def benchmark(side, backend):
    rails = [PowerRail('VIN', 12, sources=[UnifiedSource(ComponentRef('J1'), ['1'])],
                       loads=[UnifiedLoad(ComponentRef('L1'), 1, ['1'])])]
    fine, coarse = sheet(side), sheet((side+1)//2)
    def resolve(mesh, ref, pads):
        offset = 0 if ref == 'J1' else mesh.benchmark_side-1
        return list(range(offset, len(mesh.nodes), mesh.benchmark_side))
    cache = PreparedNetworkCache(max_bytes=4*1024**3)
    records = {}
    def run(label, mesh, **kwargs):
        started = time.perf_counter()
        result, _ = solve_system(rails, {'VIN':mesh}, resolve, backend=backend, **kwargs)
        diagnostics = result['VIN']['diagnostics']
        records[label] = dict(elapsed_s=time.perf_counter()-started,
                              **{key: diagnostics.get(key) for key in
                                 ('backend', 'iterations', 'preconditioner', 'prepared_cache_hit',
                                  'refined_start_nodes', 'warm_start', 'setup_seconds',
                                  'refined_start_skip_reason', 'iteration_seconds', 'residual_a', 'balance_a', 'copper_loss_w')})
        return result
    try:
        run('first_fine', fine, cache=cache)
        rails[0].loads[0].total_current = 1.0001
        retained = run('changed_load_retained', fine, cache=cache)
        fresh = run('changed_load_fresh', fine)
        records['reuse_max_voltage_difference_v'] = float(np.max(np.abs(retained['VIN']['results'].array-fresh['VIN']['results'].array)))
        prior = run('coarse', coarse)
        warm = run('fine_warm_from_coarse', fine, initial_results=prior)
        records['refinement_max_voltage_difference_v'] = float(np.max(np.abs(warm['VIN']['results'].array-fresh['VIN']['results'].array)))
        records.update(side=side, nodes=side*side, backend_requested=backend, retained_bytes=cache.bytes_used)
    finally:
        cache.close()
    return records


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--side', type=int, default=513)
    parser.add_argument('--backend', choices=('cpu','gpu'), default='cpu')
    parser.add_argument('--output', default='benchmark-session-cache.json')
    args = parser.parse_args()
    data = benchmark(args.side, args.backend)
    Path(args.output).write_text(json.dumps(data, indent=2), encoding='utf-8')
    print(json.dumps(data, indent=2))
