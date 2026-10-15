"""Development-only extraction of invariant network setup from result evaluation."""
from pathlib import Path
import textwrap

p = Path(__file__).resolve().parent/'network_solver.py'
s = p.read_text(encoding='utf-8')
start = s.index('def solve_group(')
body_start = s.index('    layout = Layout(meshes)', start)
setup_end = s.index('    solver = Solver(', body_start)
finish_start = s.index('    if isinstance(solution, Voltages):', setup_end)
end = s.index('\n\ndef solve_system(', finish_start)
setup = s[body_start:setup_end]
finish = s[finish_start:end]
replacement = '''class PreparedGroup:
    def __init__(self, meshes, links, sources, loads, resolve, log=None, backend='auto'):
        import time
        started = time.perf_counter()
'''+textwrap.indent(setup, '    ')+'''
        self.meshes, self.layout, self.before = meshes, layout, before
        self.contacts, self.node_id, self.edges = contacts, node_id, edges
        self.combined, self.mapped_sources, self.mapped_loads = combined, mapped_sources, mapped_loads
        self.sources = [dict(node_id=int(node_id[node]), voltage=v) for node, v in mapped_sources]
        self.solver = Solver(log_callback=log, backend=backend)
        self.solver.prepare(combined, self.sources)
        self.log, self.calls = log, 0
        self.setup_seconds = time.perf_counter()-started
        if log:
            log(f'Network {", ".join(meshes)} prepared once: {len(combined.nodes):,} nodes, {self.setup_seconds:.2f} s.')

    def solve_amounts(self, amounts, initial=None, label='final'):
        self.last_mapped_loads = [(node, value) for (node, _), value in zip(self.mapped_loads, amounts)]
        if len(amounts) != len(self.mapped_loads):
            raise ValueError('Prepared network load count changed.')
        self.calls += 1
        if self.log:
            self.log(f'Network {", ".join(self.meshes)} / {label}: RHS solve {self.calls}; matrix and terminals reused.')
        return self.solver.solve(self.combined, self.sources,
                                 [dict(node_id=int(self.node_id[node]), current=value) for node, value in self.last_mapped_loads],
                                 initial_voltage=initial)

    def finish(self, solution):
        layout, before, node_id, edges = self.layout, self.before, self.node_id, self.edges
        meshes, contacts, combined = self.meshes, self.contacts, self.combined
        mapped_sources, mapped_loads, solver = self.mapped_sources, self.last_mapped_loads, self.solver
        solver.diagnostics.update(linear_rhs_solves=self.calls, network_setup_seconds=self.setup_seconds)
'''+textwrap.indent(finish, '    ')+'''
    def close(self):
        self.solver.close()


def solve_group(meshes, links, sources, loads, resolve, log=None, backend='auto'):
    prepared = PreparedGroup(meshes, links, sources, loads, resolve, log, backend)
    try:
        solution = prepared.solve_amounts([value for _, _, _, value in loads])
        return prepared.finish(solution)
    finally:
        prepared.close()
'''
s = s[:start]+replacement+s[end:]
loop_start = s.index('        for iteration in range(40):')
loop_end = s.index('        supplied = {}', loop_start)
new_loop = '''        regulator_loads = [(r.input_rail_name, r.input_ref_des, r.input_pad_names, i) for r, i in zip(outgoing, input_currents)]
        records = loads+regulator_loads
        prepared = PreparedGroup({n: meshes[n] for n in nets}, local_links, sources, records, resolve, log, backend)
        try:
            switching = [index for index, reg in enumerate(outgoing) if reg.reg_type == 'SWITCHING']
            amounts = [value for _, _, _, value in records]
            if switching:
                from regulator_ports import solve_port_currents
                for index in switching:
                    amounts[len(loads)+index] = 0
                base = prepared.solve_amounts(amounts, label='regulator base')
                port_nodes = [int(prepared.node_id[prepared.mapped_loads[len(loads)+index][0]]) for index in switching]
                v0 = base.array[port_nodes]
                responses = []
                for column, index in enumerate(switching):
                    impulse = list(amounts); impulse[len(loads)+index] = 1
                    response = prepared.solve_amounts(impulse, label=f'unit-current port {column+1}/{len(switching)}')
                    responses.append(np.nan_to_num(response.array-base.array, nan=0))
                resistance = -np.column_stack([response[port_nodes] for response in responses])
                powers = [max(0, solved[groups.find(outgoing[index].output_rail_name)][outgoing[index].name])*
                          rail_map[outgoing[index].output_rail_name].nominal_voltage/outgoing[index].efficiency for index in switching]
                current, port_iterations = solve_port_currents(v0, resistance, powers, log)
                guess = base.array.copy()
                for column, index in enumerate(switching):
                    amounts[len(loads)+index] = float(current[column])
                    guess += responses[column]*current[column]
                solution = prepared.solve_amounts(amounts, initial=guess, label='final regulator operating point')
                actual_voltage = solution.array[port_nodes]
                mismatch = np.abs(actual_voltage*current-powers)
                if np.any(actual_voltage <= 0) or np.any(mismatch > 1e-8+1e-6*np.maximum(powers, 1e-6)):
                    raise ValueError('Regulator operating point failed final full-network power validation.')
                prepared.solver.diagnostics.update(regulator_port_steps=port_iterations,
                                                    regulator_power_error_w=float(np.max(mismatch)))
            else:
                solution = prepared.solve_amounts(amounts)
            data, parts, diagnostics, ids = prepared.finish(solution)
        finally:
            prepared.close()
'''
s = s[:loop_start]+new_loop+s[loop_end:]
# A per-mesh local cache avoids repeated board/IPC terminal searches.
anchor = '    rail_map = {rail.net_name: rail for rail in rails}'
s = s.replace(anchor, '''    original_resolve = resolve
    terminals = {}
    def resolve(mesh, ref, pads):
        key = (id(mesh), ref, tuple(pads))
        if key not in terminals:
            terminals[key] = original_resolve(mesh, ref, pads)
        return terminals[key]
'''+anchor)
s = s.replace('mesh.G_coo_row.extend(np.concatenate((u, v, u, v)))', 'mesh.G_coo_row.frombytes(np.concatenate((u, v, u, v)).astype(np.int32, copy=False).tobytes())')
s = s.replace('mesh.G_coo_col.extend(np.concatenate((u, v, v, u)))', 'mesh.G_coo_col.frombytes(np.concatenate((u, v, v, u)).astype(np.int32, copy=False).tobytes())')
s = s.replace('mesh.G_coo_data.extend(np.concatenate((g, g, -g, -g)))', 'mesh.G_coo_data.frombytes(np.concatenate((g, g, -g, -g)).astype(np.float64, copy=False).tobytes())')
p.write_text(s, encoding='utf-8')
