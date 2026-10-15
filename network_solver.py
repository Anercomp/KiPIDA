"""Assemble copper nets and explicit passive component contacts into DC networks."""
import math
import numpy as np
from compact_storage import Voltages
from collections import defaultdict
from copy import deepcopy
from mesh import Mesh
from electrical_solver import Solver


class Union:
    def __init__(self, items):
        self.parent = {item: item for item in items}

    def find(self, item):
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            item, self.parent[item] = self.parent[item], root
        return root

    def join(self, a, b):
        self.parent[self.find(b)] = self.find(a)


def validate_power_feeds(rails, net_names=None):
    """Check logical feeds without extracting copper or allocating a mesh."""
    rail_map = {rail.net_name: rail for rail in rails}
    groups = Union(rail_map if net_names is None else net_names)
    for rail in rails:
        for link in rail.series_components:
            # Open links stay grouped; electrical connectivity is checked later.
            groups.join(link.net_a, link.net_b)
    members = defaultdict(list)
    for net in groups.parent:
        members[groups.find(net)].append(net)
    regs = [reg for rail in rails for reg in rail.child_regulators]
    for nets in members.values():
        has_source = any(rail_map[net].sources for net in nets)
        has_regulator_feed = any(reg.output_rail_name in nets for reg in regs)
        if not has_source and not has_regulator_feed:
            names = ', '.join(sorted(nets))
            raise ValueError(
                f"No voltage source configured for network: {names}. "
                "Add a Source at its supply pad, restore its incoming regulator/series link, "
                "or use Undo Remove if the feed was just removed. Nominal voltage alone is not a source.")
    return groups, members


class EdgeArrays:
    def __init__(self, mesh):
        if mesh.G_coo_data:
            data = np.asarray(mesh.G_coo_data)
            row, col = np.asarray(mesh.G_coo_row), np.asarray(mesh.G_coo_col)
            mask = (row < col) & (data < 0)
            self.u, self.v, self.g = row[mask], col[mask], -data[mask]
        elif mesh.edges:
            edges = np.asarray(mesh.edges)
            self.u, self.v, self.g = edges[:, 0].astype(int), edges[:, 1].astype(int), edges[:, 2]
        else:
            self.u, self.v, self.g = np.empty(0, dtype=int), np.empty(0, dtype=int), np.empty(0)

    def __iter__(self):
        return iter(zip(self.u, self.v, self.g))


def copper_edges(mesh):
    return EdgeArrays(mesh)


class Layout:
    def __init__(self, meshes):
        self.offsets, self.sparse = {}, {}
        self.count = 0
        for net, mesh in meshes.items():
            self.offsets[net] = self.count
            if not isinstance(mesh.nodes, range) or mesh.nodes != range(len(mesh.nodes)):
                self.sparse[net] = {int(node): i for i, node in enumerate(mesh.nodes)}
            self.count += len(mesh.nodes)

    def indices(self, net, nodes):
        if net in self.sparse:
            nodes = [self.sparse[net][int(node)] for node in nodes]
        return self.offsets[net]+np.asarray(nodes, dtype=np.int32)


class NodeIDs:
    def __init__(self, layout, ids):
        self.layout, self.ids = layout, ids

    def __getitem__(self, key):
        net, node = key
        offset = self.layout.offsets[net]
        index = self.layout.sparse[net][int(node)] if net in self.layout.sparse else int(node)
        return int(self.ids[offset+index])


class ArrayUnion:
    def __init__(self, count):
        self.parent = np.arange(count, dtype=np.int32)

    def find(self, node):
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != node:
            node, self.parent[node] = self.parent[node], root
        return int(root)

    def join(self, a, b):
        self.parent[self.find(b)] = self.find(a)

    def roots(self):
        while True:
            parents = self.parent[self.parent]
            if np.array_equal(parents, self.parent):
                return parents
            self.parent = parents


def stamp_edges(mesh, u, v, g):
    valid = u != v
    u, v, g = u[valid], v[valid], g[valid]
    mesh.G_coo_row.frombytes(np.concatenate((u, v, u, v)).astype(np.int32, copy=False).tobytes())
    mesh.G_coo_col.frombytes(np.concatenate((u, v, v, u)).astype(np.int32, copy=False).tobytes())
    mesh.G_coo_data.frombytes(np.concatenate((g, g, -g, -g)).astype(np.float64, copy=False).tobytes())


class PreparedGroup:
    def __init__(self, meshes, links, sources, loads, resolve, log=None, backend='auto'):
        links = deepcopy(links)
        import time
        started = time.perf_counter()
        layout = Layout(meshes)
        union = ArrayUnion(layout.count)

        def terminal(net, ref, pads):
            nodes = resolve(meshes[net], ref, pads)
            if not nodes:
                raise ValueError(f"No mapped terminal: {ref} {pads} on {net}.")
            ids = layout.indices(net, nodes)
            for node in ids[1:]:
                union.join(ids[0], node)
            return int(ids[0])

        contacts, seen = [], set()
        for link in links:
            if not math.isfinite(link.resistance_ohm) or link.resistance_ohm < 0:
                raise ValueError(f"Invalid resistance for {link.ref_des}.")
            key = frozenset(((link.ref_des, link.pad_a), (link.ref_des, link.pad_b)))
            if key in seen or len(key) != 2 or link.net_a == link.net_b:
                raise ValueError(f"Duplicate or invalid series link: {link.ref_des}.")
            seen.add(key)
            contacts.append((link, terminal(link.net_a, link.ref_des, [link.pad_a]),
                             terminal(link.net_b, link.ref_des, [link.pad_b])))
        mapped_sources = [(terminal(net, ref, pads), amount) for net, ref, pads, amount in sources]
        mapped_loads = [(terminal(net, ref, pads), amount) for net, ref, pads, amount in loads]
        before = union.roots()
        for link, a, b in contacts:
            if link.closed and link.resistance_ohm == 0:
                union.join(a, b)
        unique, node_id = np.unique(union.roots(), return_inverse=True)
        node_id = node_id.astype(np.int32)
        combined = Mesh(); combined.nodes = range(len(unique))
        edges = {net: copper_edges(mesh) for net, mesh in meshes.items()}
        for net, items in edges.items():
            u, v = node_id[layout.indices(net, items.u)], node_id[layout.indices(net, items.v)]
            stamp_edges(combined, u, v, items.g)
        for link, a, b in contacts:
            if link.closed and link.resistance_ohm > 0:
                combined.add_edge_direct(int(node_id[a]), int(node_id[b]), 1/link.resistance_ohm)

        self.meshes, self.layout, self.before = meshes, layout, before
        self.contacts, self.node_id, self.edges = contacts, node_id, edges
        self.combined, self.mapped_sources, self.mapped_loads = combined, mapped_sources, mapped_loads
        self.sources = [dict(node_id=int(node_id[node]), voltage=v) for node, v in mapped_sources]
        self.log, self.calls, self.current_label = log, 0, 'setup'
        self.run_initial = None
        self.refined_start_nodes = 0
        self.refined_start_skip_reason = ''
        self.prepared_cache_hit = False
        def scoped_log(message):
            if self.log:
                self.log(f'[Network {", ".join(meshes)} / {self.current_label}] {message}')
        self.solver = Solver(log_callback=scoped_log, backend=backend)
        try:
            self.solver.prepare(combined, self.sources)
        except BaseException:
            self.solver.close()
            raise
        self.log, self.calls = log, 0
        self.setup_seconds = time.perf_counter()-started
        if log:
            log(f'Network {", ".join(meshes)} prepared once: {len(combined.nodes):,} nodes, {self.setup_seconds:.2f} s.')

    def start_run(self, log, cache_hit=False, initial_results=None):
        self.log, self.calls, self.current_label = log, 0, 'setup'
        self.prepared_cache_hit = cache_hit
        self.refined_start_nodes = 0
        self.refined_start_skip_reason = ''
        self.run_initial = None
        if cache_hit and log:
            log(f'Network {", ".join(self.meshes)}: prepared matrix, terminals and backend reused from previous run.')
        if not initial_results:
            return
        def skip(reason):
            self.refined_start_skip_reason = reason
            if log:
                log(f'Network {", ".join(self.meshes)}: refined interpolation skipped ({reason}).')
        context = self.solver.prepared['context']
        if context is None:
            skip('boundary-only network')
            return
        # A direct factorization ignores an initial guess. Avoid paying for
        # interpolation when the backend policy already selects that path.
        if (context.preconditioner != 'amg' and
                ((self.solver.backend == 'cpu' and len(self.solver.prepared['free']) < 200000) or
                 (self.solver.backend == 'auto' and len(self.solver.prepared['free']) < 50000))):
            skip('direct factorization ignores an initial guess')
            return
        if cache_hit and context is not None and context.last is not None:
            # The previous solution on this exact mesh is already a valid warm start.
            skip('retained solution on the exact mesh is already available')
            return
        coarse_diagnostics = [initial_results[net].get('diagnostics', {})
                              for net in self.meshes if net in initial_results]
        if coarse_diagnostics and all(d.get('preconditioner') == 'AMG V-cycle' and
                                      d.get('iterations', 50) < 50 for d in coarse_diagnostics):
            skip('coarse AMG converged in fewer than 50 iterations; interpolation overhead is unlikely to pay off')
            return
        if coarse_diagnostics and all(d.get('backend') == 'CPU direct' for d in coarse_diagnostics):
            # The common direct-coarse/AMG-fine case was slower with interpolation
            # in calibration. Keep it for Jacobi or unavailable AMG fallbacks.
            import importlib.util
            if context.preconditioner != 'jacobi' and importlib.util.find_spec('pyamg') is not None:
                skip('coarse direct solve and available AMG favor a cold refined solve')
                return
        from refined_start import interpolate_voltage
        total = np.zeros(len(self.combined.nodes))
        count = np.zeros(len(self.combined.nodes), dtype=np.int32)
        for net, mesh in self.meshes.items():
            if net not in initial_results:
                continue
            local = interpolate_voltage(initial_results[net], mesh)
            valid = np.isfinite(local)
            offset = self.layout.offsets[net]
            mapped = self.node_id[offset:offset+len(mesh.nodes)][valid]
            np.add.at(total, mapped, local[valid])
            np.add.at(count, mapped, 1)
        known = count > 0
        self.refined_start_nodes = int(np.count_nonzero(known))
        if self.refined_start_nodes:
            guess = self.solver.prepared['baseline'].copy()
            guess[known] = total[known]/count[known]
            self.run_initial = guess
            if log:
                log(f'Network {", ".join(self.meshes)}: coarse solution interpolated at {self.refined_start_nodes:,} refined nodes; residual validation remains enabled.')

    def solve_amounts(self, amounts, initial=None, label='final'):
        self.current_label = label
        self.last_mapped_loads = [(node, value) for (node, _), value in zip(self.mapped_loads, amounts)]
        if len(amounts) != len(self.mapped_loads):
            raise ValueError('Prepared network load count changed.')
        if self.calls == 0 and initial is None:
            initial = self.run_initial
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
        solver.diagnostics.update(linear_rhs_solves=self.calls,
                                  network_setup_seconds=0 if self.prepared_cache_hit else self.setup_seconds,
                                  prepared_cache_hit=self.prepared_cache_hit,
                                  refined_start_nodes=self.refined_start_nodes,
                                  refined_start_skip_reason=self.refined_start_skip_reason)
        if isinstance(solution, Voltages):
            voltages = solution.array
        else:
            voltages = np.full(len(combined.nodes), np.nan)
            for node, voltage in solution.items():
                voltages[node] = voltage
        results = {}; external = np.zeros(layout.count)
        for net, mesh in meshes.items():
            count, offset = len(mesh.nodes), layout.offsets[net]
            local = voltages[node_id[offset:offset+count]]
            values = Voltages(local) if net not in layout.sparse else {n: float(local[i]) for i, n in enumerate(mesh.nodes) if np.isfinite(local[i])}
            items = edges[net]
            a, b = layout.indices(net, items.u), layout.indices(net, items.v)
            delta = voltages[node_id[a]]-voltages[node_id[b]]
            current = np.nan_to_num(items.g*delta, nan=0.0)
            loss = float(np.nansum(current*delta))
            np.add.at(external, before[a], current)
            np.add.at(external, before[b], -current)
            if len(values):
                lo, hi = float(np.nanmin(local)), float(np.nanmax(local))
                results[net] = dict(mesh=mesh, results=values, stats=(lo, hi, hi-lo),
                                    diagnostics={**solver.diagnostics, 'copper_loss_w': loss})
        for node, current in mapped_loads:
            external[before[node]] += current
        source_contacts = defaultdict(set)
        for node, _ in mapped_sources:
            source_contacts[int(node_id[node])].add(int(before[node]))
        ambiguous = set()
        for nid, terminals in source_contacts.items():
            if len(terminals) == 1:
                external[next(iter(terminals))] -= solver.diagnostics['source_currents_a'][nid]
            else:
                ambiguous.update(terminals)
        component_results, ideal = [], []
        for link, a, b in contacts:
            va, vb = voltages[node_id[a]], voltages[node_id[b]]
            drop = float(va-vb) if np.isfinite(va) and np.isfinite(vb) else None
            current = 0.0 if not link.closed else (drop/link.resistance_ohm if drop is not None and link.resistance_ohm else None)
            record = dict(ref=link.ref_des, net_a=link.net_a, net_b=link.net_b, closed=link.closed,
                          current_a=current, drop_v=drop, loss_w=0.0 if current is None else current*current*link.resistance_ohm)
            component_results.append(record)
            if link.closed and link.resistance_ohm == 0 and drop is not None:
                ideal.append((int(before[a]), int(before[b]), record))
            elif current is not None:
                external[before[a]] += current
                external[before[b]] -= current
        remaining = list(ideal)
        while remaining:
            degree = defaultdict(int)
            for a, b, _ in remaining:
                degree[a] += 1; degree[b] += 1
            progress = False
            for a, b, record in list(remaining):
                if degree[a] == 1 and a not in ambiguous:
                    current = -external[a]
                elif degree[b] == 1 and b not in ambiguous:
                    current = external[b]
                else:
                    continue
                record['current_a'] = float(current)
                external[a] += current; external[b] -= current
                remaining.remove((a, b, record)); progress = True
                break
            if not progress:
                break
        return results, component_results, solver.diagnostics, NodeIDs(layout, node_id)

    def close(self):
        self.solver.close()


def solve_group(meshes, links, sources, loads, resolve, log=None, backend='auto'):
    prepared = PreparedGroup(meshes, links, sources, loads, resolve, log, backend)
    try:
        solution = prepared.solve_amounts([value for _, _, _, value in loads])
        return prepared.finish(solution)
    finally:
        prepared.close()


def solve_system(rails, meshes, resolve, log=None, backend='auto', progress=None,
                 cache=None, initial_results=None):
    """Solve all current RHS values, optionally retaining only prepared operators."""
    try:
        return _solve_system(rails, meshes, resolve, log, backend, progress, cache, initial_results)
    except BaseException:
        if cache is not None:
            cache.clear()
        raise


def _solve_system(rails, meshes, resolve, log, backend, progress, cache, initial_results):
    rails = deepcopy(rails)
    original_resolve = resolve
    terminals = {}
    def resolve(mesh, ref, pads):
        key = (id(mesh), ref, tuple(pads))
        if key not in terminals:
            terminals[key] = original_resolve(mesh, ref, pads)
        return terminals[key]
    rail_map = {rail.net_name: rail for rail in rails}
    if len(rail_map) != len(rails):
        raise ValueError('Power rail net names must be unique.')
    links = [link for rail in rails for link in rail.series_components]
    regs = [reg for rail in rails for reg in rail.child_regulators]
    if len({reg.name for reg in regs}) != len(regs):
        raise ValueError("Regulator names must be unique.")
    groups, members = validate_power_feeds(rails, meshes)
    results, components, solved, visiting = {}, [], {}, set()

    def solve(root):
        if root in solved:
            return
        if root in visiting:
            raise ValueError("Regulator dependency cycle through stitched rails.")
        visiting.add(root)
        nets = members[root]
        sources, loads = [], []
        for net in nets:
            rail = rail_map[net]
            sources.extend((net, s.component_ref.ref_des, s.pad_names, rail.nominal_voltage) for s in rail.sources)
            loads.extend((net, l.component_ref.ref_des, l.pad_names, l.total_current) for l in rail.loads)
        incoming, outgoing = [], []
        for reg in regs:
            upstream, downstream = groups.find(reg.input_rail_name), groups.find(reg.output_rail_name)
            if upstream == downstream:
                raise ValueError(f"Regulator {reg.name} input and output are in one stitched network.")
            if downstream == root:
                incoming.append(reg)
                sources.append((reg.output_rail_name, reg.output_ref_des, reg.output_pad_names, rail_map[reg.output_rail_name].nominal_voltage))
            if upstream == root:
                solve(downstream)
                outgoing.append(reg)
        source_voltage = max((s[3] for s in sources), default=0)
        input_currents = []
        for reg in outgoing:
            if reg.reg_type not in ('LINEAR', 'SWITCHING') or not 0 < reg.efficiency <= 1:
                raise ValueError(f"Invalid regulator model: {reg.name}.")
            current = solved[groups.find(reg.output_rail_name)][reg.name]
            if current < -1e-8:
                raise ValueError(f"Regulator {reg.name} is absorbing current; reverse operation is unsupported.")
            if source_voltage <= 0:
                raise ValueError("Regulator input network needs a positive supply voltage.")
            input_currents.append(max(0, current) if reg.reg_type == 'LINEAR' else
                                  max(0, current)*rail_map[reg.output_rail_name].nominal_voltage/reg.efficiency/source_voltage)
        local_links = [link for link in links if link.net_a in nets]
        regulator_loads = [(r.input_rail_name, r.input_ref_des, r.input_pad_names, i) for r, i in zip(outgoing, input_currents)]
        records = loads+regulator_loads
        local_meshes = {n: meshes[n] for n in nets}
        cache_key, prepared = None, None
        if cache is not None:
            from network_cache import network_signature
            cache_key = network_signature(local_meshes, local_links, sources, records, resolve, backend)
            prepared = cache.get(cache_key)
        cache_hit = prepared is not None
        if prepared is None:
            prepared = PreparedGroup(local_meshes, local_links, sources, records, resolve, log, backend)
        completed = False
        try:
            prepared.start_run(log, cache_hit, initial_results)
            switching = [index for index, reg in enumerate(outgoing) if reg.reg_type == 'SWITCHING' and input_currents[index] > 0]
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
            for index, reg in enumerate(outgoing):
                node = int(prepared.node_id[prepared.mapped_loads[len(loads)+index][0]])
                vin = solution.array[node]
                if not np.isfinite(vin) or vin <= 0:
                    raise ValueError(f'Regulator {reg.name} input is unpowered or has nonpositive voltage.')
            data, parts, diagnostics, ids = prepared.finish(solution)
            for index, reg in enumerate(outgoing):
                node = int(prepared.node_id[prepared.mapped_loads[len(loads)+index][0]])
                vin = float(solution.array[node])
                iin = float(amounts[len(loads)+index])
                iout = max(0.0, float(solved[groups.find(reg.output_rail_name)][reg.name]))
                vout = float(rail_map[reg.output_rail_name].nominal_voltage)
                pin, pout = vin*iin, vout*iout
                loss = pin-pout
                tolerance = 1e-8+1e-6*max(pin, pout, 1e-6)
                if not all(np.isfinite(value) for value in (pin, pout, loss)) or loss < -tolerance:
                    raise ValueError(f'Regulator {reg.name} has negative dissipation ({loss:.6g} W). '
                                     'Check its model and input/output voltage; a linear regulator cannot boost voltage.')
                record = dict(name=reg.name, reg_type=reg.reg_type,
                              input_rail=reg.input_rail_name, output_rail=reg.output_rail_name,
                              input_ref=reg.input_ref_des, input_pads=list(reg.input_pad_names),
                              output_ref=reg.output_ref_des, output_pads=list(reg.output_pad_names),
                              input_voltage_v=vin, input_current_a=iin, output_voltage_v=vout,
                              output_current_a=iout, input_power_w=pin, output_power_w=pout,
                              loss_w=max(0.0, loss))
                data[reg.input_rail_name].setdefault('regulator_losses', []).append(record)
                if log:
                    log(f'Regulator {reg.name}: input {pin:.6g} W, output {pout:.6g} W, device heat {record["loss_w"]:.6g} W.')
            completed = True
        finally:
            if completed and cache is not None:
                cache.store(cache_key, prepared)
            else:
                prepared.close()
        supplied = {}
        claimed = set()
        for reg in incoming:
            nodes = resolve(meshes[reg.output_rail_name], reg.output_ref_des, reg.output_pad_names)
            source_ids = {ids[reg.output_rail_name, n] for n in nodes}
            if claimed & source_ids:
                raise ValueError("Regulator outputs share a terminal; current allocation is ambiguous.")
            standard_ids = {ids[net, n] for net, ref, pads, _ in sources[:len(sources)-len(incoming)] for n in resolve(meshes[net], ref, pads)}
            if source_ids & standard_ids:
                raise ValueError("Remove the manual source at the regulator output terminal.")
            claimed.update(source_ids)
            supplied[reg.name] = sum(diagnostics['source_currents_a'][n] for n in source_ids)
        results.update(data)
        components.extend(parts)
        solved[root] = supplied
        if progress:
            progress(len(solved)/len(members))
        visiting.remove(root)
    for root in members:
        solve(root)
    return results, components
