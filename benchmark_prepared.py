"""Repeat-RHS sparse benchmark comparing Jacobi with cached AMG/warm starts."""
import argparse
import json
import time
import numpy as np
from scipy.sparse import diags, kronsum
from prepared_backend import PreparedReducedSystem

parser = argparse.ArgumentParser()
parser.add_argument('--size', type=int, default=512)
parser.add_argument('--backend', choices=['cpu', 'gpu'], default='gpu')
args = parser.parse_args()
size = args.size
diagonal = np.full(size, 2.); diagonal[-1] = 1
x = diags([-np.ones(size-1), diagonal, -np.ones(size-1)], [-1, 0, 1], format='csr')
diagonal[0] = 1
y = diags([-np.ones(size-1), diagonal, -np.ones(size-1)], [-1, 0, 1], format='csr')
matrix = kronsum(x, y, format='csr')
b = np.zeros((size, size)); b[:, -1] = -1/size; b = b.ravel()
expected = np.tile(-np.arange(1, size+1)/size, size)
report = dict(nodes=len(b), backend=args.backend, preconditioners={})
for name in ('jacobi', 'amg'):
    prepared = PreparedReducedSystem(matrix, args.backend, print, preconditioner=name)
    records = []
    try:
        for index, amount in enumerate((1., 1.000001)):
            values, diagnostics = prepared.solve(b*amount)
            error = float(np.max(np.abs(values-expected*amount)))
            assert error < 1e-7, error
            records.append(dict(**diagnostics, maximum_analytical_error_v=error))
        report['preconditioners'][name] = records
    finally:
        prepared.close()
print(json.dumps(report, indent=2))
from pathlib import Path
Path(f'benchmark-prepared-{args.backend}-{size}.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
