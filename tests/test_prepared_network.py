import unittest
from unittest.mock import patch
import numpy as np
from scipy.sparse import diags
from prepared_backend import PreparedReducedSystem
from electrical_solver import Solver
from mesh import Mesh
from models import PowerRail, UnifiedSource, UnifiedLoad, VoltageRegulator, ComponentRef
from network_solver import solve_system
from regulator_ports import solve_port_currents


class TestPreparedNetwork(unittest.TestCase):
    def test_matrix_factor_and_sources_reuse_then_boundary_invalidation(self):
        mesh = Mesh(); mesh.nodes = range(3)
        mesh.add_edge_direct(0, 1, 10); mesh.add_edge_direct(1, 2, 10)
        solver = Solver(backend='cpu')
        source = [dict(node_id=0, voltage=12)]
        solver.solve(mesh, source, [dict(node_id=2, current=1)])
        context = solver.prepared['context']; factor = context.factor
        values = solver.solve(mesh, source, [dict(node_id=2, current=1.001)])
        self.assertIs(context, solver.prepared['context'])
        self.assertIs(factor, context.factor)
        self.assertTrue(solver.diagnostics['matrix_reused'])
        self.assertTrue(solver.diagnostics['warm_start'])
        self.assertAlmostEqual(values[2], 12-.2002)
        values = solver.solve(mesh, [dict(node_id=0, voltage=5)], [dict(node_id=2, current=1)])
        self.assertAlmostEqual(values[2], 4.8)
        self.assertIsNot(context, solver.prepared['context'])
        solver.close()

    def test_amg_cpu_and_warm_start_preserve_residual(self):
        matrix = diags([-1, 2, -1], [-1, 0, 1], shape=(1000, 1000), format='csr')
        context = PreparedReducedSystem(matrix, 'cpu', preconditioner='amg')
        try:
            b = np.zeros(1000); b[500] = 1
            values, diagnostics = context.solve(b)
            self.assertEqual(diagnostics['preconditioner'], 'AMG V-cycle')
            hierarchy = context.hierarchy
            second, new = context.solve(b*1.000001)
            np.testing.assert_allclose(matrix@second, b*1.000001, atol=1e-8)
            self.assertIs(context.hierarchy, hierarchy)
            self.assertTrue(new['warm_start'])
            self.assertLess(new['iterations'], diagnostics['iterations'])
        finally:
            context.close()

    def test_single_port_high_voltage_branch_and_voltage_collapse(self):
        current, steps = solve_port_currents([12.], [[.22]], [6.25])
        voltage = 12-.22*current[0]
        self.assertAlmostEqual(voltage*current[0], 6.25)
        self.assertGreater(voltage, 6)
        self.assertEqual(steps, 0)
        with self.assertRaisesRegex(ValueError, 'maximum power'):
            solve_port_currents([1.], [[1.]], [1.])
        with self.assertRaisesRegex(ValueError, 'collapse'):
            solve_port_currents([1.], [[1.]], [.25])

    def test_multiple_ports_and_nonpassive_response_rejection(self):
        r = np.array([[.2, .1], [.1, .3]])
        current, _ = solve_port_currents([11.9, 11.9], r, [6.25, 6.25])
        np.testing.assert_allclose(current*(11.9-r@current), 6.25, atol=1e-8)
        with self.assertRaisesRegex(ValueError, 'passive'):
            solve_port_currents([12., 12.], [[-1., 0], [0, 1.]], [1., 1.])

    def test_two_regulators_prepare_once_and_use_four_rhs(self):
        root = PowerRail('VIN', 12, sources=[UnifiedSource(ComponentRef('J0'), ['1'])], loads=[UnifiedLoad(ComponentRef('B'), 1, ['1'])])
        meshes = {}; mesh = Mesh(); mesh.nodes = range(4); mesh.net_name = 'VIN'
        for u, v, g in [(0, 1, 10), (1, 2, 10), (1, 3, 5)]: mesh.add_edge_direct(u, v, g)
        meshes['VIN'] = mesh
        rails = [root]
        for index in (1, 2):
            name = f'5V_{index}'
            root.child_regulators.append(VoltageRegulator(f'U{index}', 'VIN', f'U{index}', ['IN'], name, f'U{index}', ['OUT'], 'SWITCHING', .8))
            rails.append(PowerRail(name, 5, loads=[UnifiedLoad(ComponentRef(f'L{index}'), 1, ['1'])]))
            mesh = Mesh(); mesh.nodes = range(2); mesh.net_name = name; mesh.add_edge_direct(0, 1, 100)
            meshes[name] = mesh
        calls = []
        def resolve(mesh, ref, pads):
            calls.append((mesh.net_name, ref, tuple(pads)))
            if mesh.net_name == 'VIN': return [dict(J0=0, B=1, U1=2, U2=3)[ref]]
            return [0 if pads == ['OUT'] else 1]
        from network_solver import stamp_edges
        with patch('network_solver.stamp_edges', wraps=stamp_edges) as stamp:
            results, _ = solve_system(rails, meshes, resolve, backend='cpu')
        self.assertEqual(stamp.call_count, 3)
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(results['VIN']['diagnostics']['linear_rhs_solves'], 4)
        self.assertTrue(results['VIN']['diagnostics']['warm_start'])
        self.assertLess(results['VIN']['diagnostics']['regulator_power_error_w'], 1e-8)
