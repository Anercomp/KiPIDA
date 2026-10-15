"""Development-only mechanical refactor of the validated electrical core."""
from pathlib import Path

root = Path(__file__).resolve().parent
backend = root/'solver_backend.py'
backend.write_text('''"""Validated one-shot wrapper; repeated RHS solves use PreparedReducedSystem."""
from prepared_backend import PreparedReducedSystem


def solve_reduced(matrix, rhs, backend='auto', log=None):
    system = PreparedReducedSystem(matrix, backend, log)
    try:
        return system.solve(rhs)
    finally:
        system.close()
''', encoding='utf-8')

p = root/'electrical_solver.py'
s = p.read_text(encoding='utf-8')
s = s.replace('from solver_backend import solve_reduced', 'from prepared_backend import PreparedReducedSystem')
s = s.replace('        self.backend = backend', '        self.backend = backend\n        self.prepared = None')
s = s.replace('    def solve(self, mesh, sources, loads):\n        self.diagnostics = {}', '    def prepare(self, mesh, sources):')
rhs_start = s.index('        rhs = np.zeros(n)')
labels_start = s.index('        count, labels = connected_components', rhs_start)
rhs_code = s[rhs_start:labels_start]
s = s[:rhs_start]+s[labels_start:]
check_start = s.index('        if np.any((~active) & (rhs != 0)):')
check_end = s.index('        excluded = ', check_start)
disconnected = s[check_start:check_end]
s = s[:check_start]+s[check_end:]
solve_start = s.index('        backend_diagnostics = dict')
validate_start = s.index('        if not np.all(np.isfinite(voltage[active])):', solve_start)
replacement = '''        reduced = g[free][:, free] if len(free) else None
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
            raise ValueError('Prepared voltage boundaries changed; prepare a new network.')
'''+rhs_code+disconnected+'''        voltage = p['boundary'].copy()
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
'''
s = s[:solve_start]+replacement+s[validate_start:]
s += '''
    def close(self):
        if self.prepared is not None:
            context = self.prepared.get('context')
            if context is not None:
                context.close()
        self.prepared = None
'''
p.write_text(s, encoding='utf-8')
