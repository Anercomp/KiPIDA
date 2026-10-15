import unittest
from unittest.mock import patch

import numpy as np

from compact_storage import Voltages, GridLookup
from mesh import Mesh
from models import ComponentRef, PowerRail, UnifiedLoad, UnifiedSource, VoltageRegulator
from network_cache import PreparedNetworkCache
from network_solver import solve_system, stamp_edges
from refined_start import interpolate_voltage


def line_fixture():
    rail = PowerRail('VIN', 12, sources=[UnifiedSource(ComponentRef('J1'), ['1'])],
                     loads=[UnifiedLoad(ComponentRef('L1'), 1, ['1'])])
    mesh = Mesh(); mesh.nodes = range(3); mesh.net_name = 'VIN'
    mesh.node_coords = {0: (0, 0, 0), 1: (1, 0, 0), 2: (2, 0, 0)}
    mesh.grid_step = 1
    mesh.add_edge_direct(0, 1, 10); mesh.add_edge_direct(1, 2, 10)
    def resolve(mesh, ref, pads):
        return [0 if ref == 'J1' else 2]
    return [rail], {'VIN': mesh}, resolve


def square_mesh(step, net='VIN'):
    mesh = Mesh(); mesh.net_name = net; mesh.grid_step = step
    count = round(1/step)+1
    mesh.nodes = range(count*count)
    mesh.node_coords = {y*count+x: (x*step, y*step, 0) for y in range(count) for x in range(count)}
    for y in range(count):
        for x in range(count):
            if x+1 < count: mesh.add_edge_direct(y*count+x, y*count+x+1, 10)
            if y+1 < count: mesh.add_edge_direct(y*count+x, (y+1)*count+x, 10)
    return mesh


class TestNetworkCache(unittest.TestCase):
    def test_changed_current_reuses_factor_but_solves_fresh_rhs(self):
        rails, meshes, resolve = line_fixture()
        cache = PreparedNetworkCache()
        try:
            first, _ = solve_system(rails, meshes, resolve, backend='cpu', cache=cache)
            prepared = next(iter(cache.entries.values()))[0]
            factor = prepared.solver.prepared['context'].factor
            rails[0].loads[0].total_current = 2
            with patch('network_solver.stamp_edges', wraps=stamp_edges) as stamp:
                result, _ = solve_system(rails, meshes, resolve, backend='cpu', cache=cache)
            self.assertEqual(stamp.call_count, 0)
            self.assertIs(factor, prepared.solver.prepared['context'].factor)
            self.assertTrue(result['VIN']['diagnostics']['prepared_cache_hit'])
            self.assertEqual(result['VIN']['diagnostics']['linear_rhs_solves'], 1)
            self.assertAlmostEqual(first['VIN']['results'][2], 11.8)
            self.assertAlmostEqual(result['VIN']['results'][2], 11.6)
            fresh, _ = solve_system(rails, meshes, resolve, backend='cpu')
            np.testing.assert_allclose(result['VIN']['results'].array, fresh['VIN']['results'].array)
            self.assertGreater(cache.bytes_used, 0)
        finally:
            cache.close()
        self.assertIsNone(prepared.solver.prepared)
        self.assertEqual(cache.bytes_used, 0)

    def test_boundary_terminal_matrix_and_backend_changes_invalidate(self):
        rails, meshes, resolve = line_fixture()
        cache = PreparedNetworkCache(max_entries=1)
        try:
            solve_system(rails, meshes, resolve, backend='cpu', cache=cache)
            previous = next(iter(cache.entries.values()))[0]
            rails[0].nominal_voltage = 5
            result, _ = solve_system(rails, meshes, resolve, backend='cpu', cache=cache)
            self.assertFalse(result['VIN']['diagnostics']['prepared_cache_hit'])
            self.assertAlmostEqual(result['VIN']['results'][2], 4.8)
            self.assertIsNone(previous.solver.prepared)
            previous = next(iter(cache.entries.values()))[0]
            meshes['VIN'].add_edge_direct(0, 2, 10)
            result, _ = solve_system(rails, meshes, resolve, backend='cpu', cache=cache)
            self.assertFalse(result['VIN']['diagnostics']['prepared_cache_hit'])
            self.assertIsNone(previous.solver.prepared)
            def different_terminal(mesh, ref, pads):
                return [0 if ref == 'J1' else 1]
            result, _ = solve_system(rails, meshes, different_terminal, backend='cpu', cache=cache)
            self.assertFalse(result['VIN']['diagnostics']['prepared_cache_hit'])
            result, _ = solve_system(rails, meshes, different_terminal, backend='auto', cache=cache)
            self.assertFalse(result['VIN']['diagnostics']['prepared_cache_hit'])
        finally:
            cache.close()

    def test_error_and_reduced_budget_release_native_resources(self):
        rails, meshes, resolve = line_fixture()
        cache = PreparedNetworkCache()
        solve_system(rails, meshes, resolve, backend='cpu', cache=cache)
        prepared = next(iter(cache.entries.values()))[0]
        rails[0].loads[0].total_current = -1
        with self.assertRaisesRegex(ValueError, 'current load'):
            solve_system(rails, meshes, resolve, backend='cpu', cache=cache)
        self.assertIsNone(prepared.solver.prepared)
        self.assertEqual(len(cache.entries), 0)
        rails[0].loads[0].total_current = 1
        solve_system(rails, meshes, resolve, backend='cpu', cache=cache)
        prepared = next(iter(cache.entries.values()))[0]
        cache.set_budget(0)
        self.assertEqual(len(cache.entries), 0)
        self.assertIsNone(prepared.solver.prepared)
        result, _ = solve_system(rails, meshes, resolve, backend='cpu', cache=cache)
        self.assertAlmostEqual(result['VIN']['results'][2], 11.8)
        self.assertEqual(len(cache.entries), 0)

    def test_switching_regulator_changed_load_matches_uncached_operating_point(self):
        rails, meshes, base_resolve = line_fixture()
        rails[0].loads.clear()
        rails[0].child_regulators.append(VoltageRegulator('U1', 'VIN', 'U1', ['IN'], '5V', 'U1', ['OUT'], 'SWITCHING', .8))
        rails.append(PowerRail('5V', 5, loads=[UnifiedLoad(ComponentRef('L2'), 1, ['1'])]))
        child = Mesh(); child.nodes = range(2); child.net_name = '5V'; child.add_edge_direct(0, 1, 10)
        meshes['5V'] = child
        def resolve(mesh, ref, pads):
            return [0 if pads == ['OUT'] else 1] if mesh.net_name == '5V' else base_resolve(mesh, ref, pads)
        cache = PreparedNetworkCache()
        try:
            solve_system(rails, meshes, resolve, backend='cpu', cache=cache)
            rails[1].loads[0].total_current = 2
            cached, _ = solve_system(rails, meshes, resolve, backend='cpu', cache=cache)
            fresh, _ = solve_system(rails, meshes, resolve, backend='cpu')
            for net in meshes:
                self.assertTrue(cached[net]['diagnostics']['prepared_cache_hit'])
                np.testing.assert_allclose(cached[net]['results'].array, fresh[net]['results'].array, atol=1e-10)
            self.assertLess(cached['VIN']['diagnostics']['regulator_power_error_w'], 1e-8)
        finally:
            cache.close()


class TestRefinedStart(unittest.TestCase):
    def test_bilinear_interpolation_is_exact_for_linear_voltage_field(self):
        coarse = square_mesh(1); fine = square_mesh(.5)
        values = Voltages([12-x-.2*y for x, y, layer in coarse.node_coords.values()])
        guess = interpolate_voltage(dict(mesh=coarse, results=values), fine)
        expected = [12-x-.2*y for x, y, layer in fine.node_coords.values()]
        np.testing.assert_allclose(guess, expected, atol=1e-12)

    def test_grid_lookup_holes_and_nan_placeholder_preserve_exact_boundary(self):
        coarse = Mesh(); coarse.nodes = range(5); coarse.grid_step = 1
        coarse.node_coords = {0:(3,0,0), 1:(0,0,0), 2:(1,0,0), 3:(0,1,0), 4:(1,1,0)}
        for a, b in ((1,2),(1,3),(2,4),(3,4)): coarse.add_edge_direct(a,b,10)
        coarse.grid_lookup = GridLookup(np.array([[[1,2,-1,0],[3,4,-1,-1]]], dtype=np.int32), {0:0}, (0,0), 1)
        fine = square_mesh(.5)
        guess = interpolate_voltage(dict(mesh=coarse, results=Voltages([np.nan,12,11,10,9])), fine)
        np.testing.assert_allclose(guess, [12-x-2*y for x,y,layer in fine.node_coords.values()], atol=1e-12)

    def test_missing_corners_unpowered_islands_and_layers_are_not_extrapolated(self):
        coarse = square_mesh(1); fine = square_mesh(.5)
        values = Voltages([12, 11, np.nan, 10])
        guess = interpolate_voltage(dict(mesh=coarse, results=values), fine)
        self.assertTrue(np.isnan(guess[4]))
        self.assertAlmostEqual(guess[1], 11.5)
        fine.node_coords[4] = (.5, .5, 1)
        self.assertTrue(np.isnan(interpolate_voltage(dict(mesh=coarse, results=values), fine)[4]))
        # Two coarse islands geometrically surround the center; never average them.
        coarse = Mesh(); coarse.nodes = range(4); coarse.grid_step = 1
        coarse.node_coords = {0:(0,0,0), 1:(1,0,0), 2:(0,1,0), 3:(1,1,0)}
        coarse.add_edge_direct(0, 1, 10); coarse.add_edge_direct(2, 3, 10)
        guess = interpolate_voltage(dict(mesh=coarse, results=Voltages([12,11,5,4])), square_mesh(.5))
        self.assertTrue(np.all(np.isnan(guess)))  # Refined island joins two coarse islands.

    def test_refined_solve_uses_guess_and_matches_cold_solution(self):
        rails, _, _ = line_fixture()
        coarse, fine = square_mesh(1), square_mesh(.5)
        def resolve(mesh, ref, pads):
            target = 0 if ref == 'J1' else 1
            return [node for node, (x,y,layer) in mesh.node_coords.items() if x == target]
        prior, _ = solve_system(rails, {'VIN':coarse}, resolve, backend='cpu')
        # Exercise interpolation for the slow iterative coarse case that enables
        # it, using the same numerically exact coarse solution on a small fixture.
        prior['VIN']['diagnostics'].update(backend='CPU CG', preconditioner='Jacobi', iterations=2000)
        from prepared_backend import PreparedReducedSystem
        def iterative_context(matrix, backend, log):
            return PreparedReducedSystem(matrix, backend, log, preconditioner='amg')
        with patch('electrical_solver.PreparedReducedSystem', side_effect=iterative_context):
            warmed, _ = solve_system(rails, {'VIN':fine}, resolve, backend='cpu', initial_results=prior)
        cold, _ = solve_system(rails, {'VIN':fine}, resolve, backend='cpu')
        self.assertTrue(warmed['VIN']['diagnostics']['warm_start'])
        self.assertEqual(warmed['VIN']['diagnostics']['refined_start_nodes'], 5)
        np.testing.assert_allclose(warmed['VIN']['results'].array, cold['VIN']['results'].array, atol=1e-12)
        self.assertLess(warmed['VIN']['diagnostics']['residual_a'], 1e-8)
        direct, _ = solve_system(rails, {'VIN':fine}, resolve, backend='cpu', initial_results=prior)
        self.assertEqual(direct['VIN']['diagnostics']['refined_start_nodes'], 0)
        self.assertFalse(direct['VIN']['diagnostics']['warm_start'])
        prior['VIN']['diagnostics'].update(preconditioner='AMG V-cycle', iterations=20)
        with patch('electrical_solver.PreparedReducedSystem', side_effect=iterative_context):
            fast_amg, _ = solve_system(rails, {'VIN':fine}, resolve, backend='cpu', initial_results=prior)
        self.assertEqual(fast_amg['VIN']['diagnostics']['refined_start_nodes'], 0)
        self.assertIn('fewer than 50', fast_amg['VIN']['diagnostics']['refined_start_skip_reason'])


if __name__ == '__main__':
    unittest.main()
