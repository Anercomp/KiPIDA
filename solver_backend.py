"""Validated one-shot wrapper; repeated RHS solves use PreparedReducedSystem."""
from prepared_backend import PreparedReducedSystem


def solve_reduced(matrix, rhs, backend='auto', log=None):
    system = PreparedReducedSystem(matrix, backend, log)
    try:
        return system.solve(rhs)
    finally:
        system.close()
