import importlib.util
import unittest
from unittest.mock import patch
import numpy as np
from scipy.sparse import diags
from solver_backend import solve_reduced
from mesh import Mesh
from electrical_solver import Solver


class TestOptionalBackend(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('cupy'), 'optional CUDA runtime not installed')
    def test_gpu_amg_is_symmetric_and_reused(self):
        from prepared_backend import PreparedReducedSystem
        import cupy as cp
        matrix = diags([-1, 2, -1], [-1, 0, 1], shape=(1000, 1000), format='csr')
        context = PreparedReducedSystem(matrix, 'gpu', preconditioner='amg')
        try:
            b = np.zeros(1000); b[500] = 1
            first, diagnostics = context.solve(b)
            self.assertEqual(diagnostics['preconditioner'], 'AMG V-cycle')
            cached = context.gpu_matrix
            second, new = context.solve(b*1.000001)
            self.assertIs(context.gpu_matrix, cached)
            self.assertLess(new['iterations'], diagnostics['iterations'])
            np.testing.assert_allclose(matrix@second, b*1.000001, atol=1e-8)
            u = cp.asarray(np.random.default_rng(12).normal(size=1000)); v = cp.asarray(np.random.default_rng(13).normal(size=1000))
            mu, mv = context.gpu_preconditioner@u, context.gpu_preconditioner@v
            self.assertGreater(float(u@mu), 0)
            self.assertAlmostEqual(float(u@mv), float(v@mu), delta=1e-8*max(1, abs(float(u@mv))))
        finally:
            context.close()
    def test_auto_falls_back_when_gpu_unavailable(self):
        matrix = diags(np.ones(50000), format='csr')
        with patch.dict('sys.modules', {'cupy': None}):
            values, diagnostics = solve_reduced(matrix, np.ones(50000), 'auto')
        np.testing.assert_allclose(values, 1)
        self.assertEqual(diagnostics['backend'], 'CPU direct')
        self.assertTrue(diagnostics['backend_notes'])

    def test_explicit_gpu_failure_is_reported(self):
        with patch.dict('sys.modules', {'cupy': None}):
            with self.assertRaisesRegex(ValueError, 'GPU solve unavailable'):
                solve_reduced(diags([1], format='csr'), np.ones(1), 'gpu')

    @unittest.skipUnless(importlib.util.find_spec('cupy'), 'optional CUDA runtime not installed')
    def test_real_cuda_matches_cpu_voltage_loss_and_balance(self):
        mesh = Mesh(); mesh.nodes = range(500)
        for i in range(499):
            mesh.add_edge_direct(i, i+1, 100)
        source, load = [dict(node_id=0, voltage=12)], [dict(node_id=499, current=1)]
        cpu, gpu = Solver(backend='cpu'), Solver(backend='gpu')
        a, b = cpu.solve(mesh, source, load), gpu.solve(mesh, source, load)
        np.testing.assert_allclose(a.array, b.array, atol=1e-9, rtol=1e-10)
        self.assertAlmostEqual(gpu.diagnostics['copper_loss_w'], 4.99, places=8)
        self.assertLess(gpu.diagnostics['balance_a'], 1e-7)
        self.assertEqual(gpu.diagnostics['backend'], 'CUDA CG')

    @unittest.skipUnless(importlib.util.find_spec('cupy'), 'optional CUDA runtime not installed')
    def test_compact_stitched_network_cuda_with_ideal_and_finite_fuse(self):
        from tests.test_network_solver import TestStitchedNetwork
        from network_solver import solve_system
        for resistance in (0, .02):
            rails, old_meshes, old_resolve = TestStitchedNetwork().fixture(resistance)
            meshes = {}
            for net in old_meshes:
                mesh = Mesh(); mesh.nodes = range(2); mesh.net_name = net
                mesh.add_edge_direct(0, 1, 10)
                meshes[net] = mesh
            def resolve(mesh, ref, pads):
                return [0 if n == 10 else 1 for n in old_resolve(mesh, ref, pads)]
            results, parts = solve_system(rails, meshes, resolve, backend='gpu')
            self.assertAlmostEqual(results['VOUT']['results'][1], 11.6-2*resistance)
            self.assertAlmostEqual(parts[0]['current_a'], 2)
            self.assertAlmostEqual(parts[0]['loss_w'], 4*resistance)
            self.assertAlmostEqual(sum(d['diagnostics']['copper_loss_w'] for d in results.values()), .8)

    @unittest.skipUnless(importlib.util.find_spec('cupy'), 'optional CUDA runtime not installed')
    def test_thermal_cuda_matches_analytical_plate(self):
        from thermal_solver import ThermalGrid, ThermalSettings
        from shapely.geometry import box
        grid = ThermalGrid(box(0, 0, 10, 10), ThermalSettings())
        grid.heat[:] = grid.areas*100
        result = grid.solve({'substrate': [{'thickness_mm': 1.6}]}, {}, 'gpu')
        np.testing.assert_allclose(result['temperature_c'], 30, atol=1e-9)
        self.assertLess(result['balance_w'], 1e-10)
