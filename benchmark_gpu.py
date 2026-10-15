"""Reproducible insulated rectangular-sheet CPU/CUDA benchmark."""
import argparse
import json
import numpy as np
from scipy.sparse import diags, kronsum
from threadpoolctl import threadpool_limits
from solver_backend import solve_reduced


def benchmark(size=512, gpu_only=False):
    diagonal = np.full(size, 2.0); diagonal[-1] = 1
    x = diags([-np.ones(size-1), diagonal, -np.ones(size-1)], [-1, 0, 1], format='csr')
    diagonal[0] = 1
    y = diags([-np.ones(size-1), diagonal, -np.ones(size-1)], [-1, 0, 1], format='csr')
    matrix = kronsum(x, y, format='csr')
    rhs = np.zeros((size, size)); rhs[:, -1] = -1/size
    rhs = rhs.ravel()
    cpu, cpu_diag = None, None
    if not gpu_only:
        with threadpool_limits(limits=1):
            cpu, cpu_diag = solve_reduced(matrix, rhs, 'cpu', print)
    gpu, gpu_diag = solve_reduced(matrix, rhs, 'gpu', print)
    expected = np.tile(-np.arange(1, size+1)/size, size)
    report = dict(nodes=size*size, nonzeros=matrix.nnz, cpu=cpu_diag, gpu=gpu_diag,
                  maximum_voltage_difference_v=None if cpu is None else float(np.max(np.abs(cpu-gpu))),
                  maximum_analytical_error_v=float(np.max(np.abs(gpu-expected))),
                  gpu_residual_a=float(np.max(np.abs(matrix@gpu-rhs))))
    print(json.dumps(report, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--size', type=int, default=512)
    parser.add_argument('--output')
    parser.add_argument('--gpu-only', action='store_true')
    args = parser.parse_args(); report = benchmark(args.size, args.gpu_only)
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(json.dumps(report, indent=2), encoding='utf-8')
