"""Validated sparse DC network solver; conductances are in siemens."""
import warnings
import numpy as np
import scipy.sparse as sparse
import scipy.sparse.linalg as linalg
from scipy.sparse.csgraph import connected_components
from compact_storage import Voltages
from prepared_backend import PreparedReducedSystem


class Solver:
    def __init__(self, debug=False, log_callback=None, backend='auto'):
        self.debug = debug
        self.log_callback = log_callback
        self.diagnostics = {}
        self.backend = backend
        self.prepared = None

    def _log(self, msg):
        if self.log_callback:
            self.log_callback(f"[SOLVER] {msg}")

    def prepare(self, mesh, sources):
        self.close()
        compact = isinstance(mesh.nodes, range) and mesh.nodes == range(len(mesh.nodes))
        nodes = mesh.nodes if compact else list(mesh.nodes)
        if not nodes:
            raise ValueError("Mesh has no nodes. Reduce mesh spacing or check geometry.")
        if not compact and len(set(nodes)) != len(nodes):
            raise ValueError("Duplicate mesh node IDs.")
        index = range(len(nodes)) if compact else {nid: i for i, nid in enumerate(nodes)}
        n = len(nodes)
        if len(getattr(mesh, 'G_coo_data', [])):
            try:
                rows = np.asarray(mesh.G_coo_row) if compact else [index[nid] for nid in mesh.G_coo_row]
                cols = np.asarray(mesh.G_coo_col) if compact else [index[nid] for nid in mesh.G_coo_col]
                if compact and (np.any(rows < 0) or np.any(rows >= n) or np.any(cols < 0) or np.any(cols >= n)):
                    raise KeyError('node outside mesh')
            except (KeyError, IndexError) as exc:
                raise ValueError("Matrix references an unknown mesh node.") from exc
            g = sparse.coo_matrix((mesh.G_coo_data, (rows, cols)), shape=(n, n)).tocsr()
        else:
            rows, cols, values = [], [], []
            for u, v, conductance in mesh.edges:
                if u not in index or v not in index or not np.isfinite(conductance) or conductance <= 0:
                    raise ValueError("Invalid mesh edge.")
                a, b = index[u], index[v]
                rows.extend([a, b, a, b]); cols.extend([a, b, b, a])
                values.extend([conductance, conductance, -conductance, -conductance])
            g = sparse.coo_matrix((values, (rows, cols)), shape=(n, n)).tocsr()
        g.eliminate_zeros()
        if not np.all(np.isfinite(g.data)):
            raise ValueError("Non-finite mesh conductance.")
        asymmetry = g - g.T
        if asymmetry.nnz and np.max(np.abs(asymmetry.data)) > 1e-9:
            raise ValueError("Conductance matrix is not symmetric.")
        fixed = {}
        for source in sources:
            nid, value = source['node_id'], float(source['voltage'])
            if nid not in index or not np.isfinite(value):
                raise ValueError("Invalid or unmapped voltage source.")
            i = index[nid]
            if i in fixed and fixed[i] != value:
                raise ValueError("Conflicting sources on the same node.")
            fixed[i] = value
        if not fixed:
            raise ValueError("No mapped voltage source.")
        count, labels = connected_components(g, directed=False)
        powered = set(labels[list(fixed)])
        active = np.isin(labels, list(powered))
        excluded = int(np.count_nonzero(~active))
        if excluded:
            self._log(f"Excluded {excluded} unpowered copper nodes (no assigned voltage).")
        fixed_indices = np.array(sorted(fixed), dtype=int)
        is_free = active.copy(); is_free[fixed_indices] = False
        free = np.flatnonzero(is_free)
        self._log(f'Prepared matrix: {n:,} nodes, {len(free):,} free unknowns, {count} islands; topology and boundaries cached.')
        voltage = np.zeros(n)
        voltage[fixed_indices] = [fixed[i] for i in fixed_indices]
        reference = np.zeros(count)
        for i in fixed:
            reference[labels[i]] = fixed[i]
        baseline = reference[labels]
        voltage[fixed_indices] -= baseline[fixed_indices]
        reduced = g[free][:, free] if len(free) else None
        context = PreparedReducedSystem(reduced, self.backend, self.log_callback) if len(free) else None
        self.prepared = dict(mesh=mesh, nodes=nodes, compact=compact, index=index, n=n, g=g,
                             fixed=fixed, labels=labels, active=active, excluded=excluded, count=count,
                             fixed_indices=fixed_indices, free=free, baseline=baseline,
                             boundary=voltage, context=context,
                             boundary_rhs=g[free][:, fixed_indices]@voltage[fixed_indices] if len(free) else None)
        return self

    def solve(self, mesh, sources, loads, initial_voltage=None):
        self.diagnostics = {}
        if self.prepared is None or self.prepared['mesh'] is not mesh:
            self.close()
            self.prepare(mesh, sources)
        p = self.prepared
        nodes, compact, index, n = p['nodes'], p['compact'], p['index'], p['n']
        g, active, free, fixed_indices = p['g'], p['active'], p['free'], p['fixed_indices']
        baseline, excluded, count = p['baseline'], p['excluded'], p['count']
        supplied_sources = {}
        for source in sources:
            nid, value = source['node_id'], float(source['voltage'])
            if nid not in index or not np.isfinite(value):
                raise ValueError('Invalid or unmapped voltage source.')
            key = index[nid]
            if key in supplied_sources and supplied_sources[key] != value:
                raise ValueError('Conflicting sources on the same node.')
            supplied_sources[key] = value
        if supplied_sources != p['fixed']:
            self.prepare(mesh, sources)
            return self.solve(mesh, sources, loads)
        rhs = np.zeros(n)
        for load in loads:
            nid, current = load['node_id'], float(load['current'])
            if nid not in index or not np.isfinite(current) or current < 0:
                raise ValueError("Invalid or unmapped current load.")
            rhs[index[nid]] -= current
        if np.any((~active) & (rhs != 0)):
            raise ValueError("Load disconnected from every source. Check layout and mesh spacing.")
        voltage = p['boundary'].copy()
        backend_diagnostics = dict(backend='boundary only', iterations=0, solve_seconds=0)
        if len(free):
            b = rhs[free]-p['boundary_rhs']
            guess = None
            if initial_voltage is not None:
                initial = initial_voltage.array if isinstance(initial_voltage, Voltages) else np.asarray(initial_voltage)
                guess = initial[free]-baseline[free]
            try:
                voltage[free], backend_diagnostics = p['context'].solve(b, guess)
            except (linalg.MatrixRankWarning, RuntimeError) as exc:
                raise ValueError('Singular or invalid electrical mesh.') from exc
        if not np.all(np.isfinite(voltage[active])):
            raise ValueError("Non-finite voltage solution.")
        residual = g @ voltage - rhs
        max_residual = float(np.max(np.abs(residual[free]))) if len(free) else 0.0
        load_current = float(-rhs.sum())
        supplied = float(residual[fixed_indices].sum())
        balance = abs(supplied - load_current)
        tolerance = 1e-8 + 1e-7 * max(1.0, load_current, abs(supplied))
        if max_residual > tolerance or balance > tolerance:
            raise ValueError(f"Electrical validation failed: residual {max_residual:.3g} A, imbalance {balance:.3g} A.")
        # Solve voltage deviations to avoid cancellation against the nominal supply.
        power = float(voltage @ (g @ voltage))
        self.diagnostics = dict(load_current_a=load_current, source_current_a=supplied,
                                source_currents_a={nodes[i]: float(residual[i]) for i in fixed_indices},
                                residual_a=max_residual, balance_a=balance,
                                copper_loss_w=max(0.0, power), excluded_nodes=excluded, islands=count)
        self.diagnostics.update(backend_diagnostics)
        self._log(f"Validated: source {supplied:.6g} A, load {load_current:.6g} A, copper loss {power:.6g} W, residual {max_residual:.3g} A.")
        voltage += baseline
        if compact:
            voltage[~active] = np.nan
            return Voltages(voltage)
        return {nid: float(voltage[i]) for i, nid in enumerate(nodes) if active[i]}

    def close(self):
        if self.prepared is not None:
            context = self.prepared.get('context')
            if context is not None:
                context.close()
        self.prepared = None
