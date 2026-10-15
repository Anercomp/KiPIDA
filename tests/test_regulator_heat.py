import copy
from types import SimpleNamespace as NS
import unittest
import numpy as np
from shapely.geometry import box
from compact_storage import Coordinates
from mesh import Mesh
from models import PowerRail, ComponentRef, UnifiedSource, UnifiedLoad, VoltageRegulator
from network_solver import solve_system
from network_cache import PreparedNetworkCache
from regulator_heat import regulator_losses, add_regulator_heat
from thermal_solver import ThermalGrid, ThermalSettings, add_copper_heat
from mesh_convergence import compare_meshes, release_coarse_arrays


def fixture(kind='LINEAR', efficiency=.8, current=1, vin=12):
    upstream = PowerRail('VIN', vin, sources=[UnifiedSource(ComponentRef('J1'), ['1'])])
    downstream = PowerRail('5V', 5, loads=[UnifiedLoad(ComponentRef('L1'), current, ['1'])])
    reg = VoltageRegulator('U1', 'VIN', 'U1', ['IN'], '5V', 'U1', ['OUT'], kind, efficiency)
    upstream.child_regulators = [reg]
    meshes = {}
    for name, xyz in [('VIN', [[45, 25, 3], [50, 25, 3]]), ('5V', [[50, 25, 3], [55, 25, 3]])]:
        mesh = Mesh(); mesh.nodes = range(2); mesh.net_name = name
        mesh.node_coords = Coordinates(); mesh.node_coords.append(xyz)
        mesh.add_edge_direct(0, 1, 10)
        meshes[name] = mesh
    def resolve(mesh, ref, pads):
        return [0 if ref == 'J1' or pads == ['OUT'] else 1]
    board = NS(footprints=[NS(reference='U1', position=NS(x=50000000, y=25000000))])
    return [upstream, downstream], meshes, resolve, board


class TestRegulatorHeat(unittest.TestCase):
    def test_linear_loss_uses_local_voltage_and_global_energy_conserves(self):
        rails, meshes, resolve, _ = fixture()
        results, parts = solve_system(rails, meshes, resolve)
        self.assertEqual(parts, [])  # Series API stays separate.
        record, = regulator_losses(results)
        self.assertAlmostEqual(record['input_voltage_v'], 11.9)
        self.assertAlmostEqual(record['input_power_w'], 11.9)
        self.assertAlmostEqual(record['output_power_w'], 5)
        self.assertAlmostEqual(record['loss_w'], 6.9)
        source_power = 12*results['VIN']['diagnostics']['source_current_a']
        load_power = results['5V']['results'][1]
        copper = sum(data['diagnostics']['copper_loss_w'] for data in results.values())
        self.assertAlmostEqual(source_power, copper+record['loss_w']+load_power)

    def test_switching_efficiency_loss_not_upstream_copper(self):
        rails, meshes, resolve, _ = fixture('SWITCHING', .8)
        results, _ = solve_system(rails, meshes, resolve)
        record, = regulator_losses(results)
        self.assertAlmostEqual(record['input_power_w'], 6.25, places=7)
        self.assertAlmostEqual(record['loss_w'], 1.25, places=7)
        self.assertLess(record['input_voltage_v'], 12)
        source_power = 12*results['VIN']['diagnostics']['source_current_a']
        copper = sum(data['diagnostics']['copper_loss_w'] for data in results.values())
        self.assertAlmostEqual(source_power, copper+record['loss_w']+results['5V']['results'][1], places=7)

    def test_unity_efficiency_zero_load_and_invalid_linear_boost(self):
        for kind, efficiency, current in [('SWITCHING', 1, 1), ('SWITCHING', .8, 0), ('LINEAR', .8, 0)]:
            rails, meshes, resolve, _ = fixture(kind, efficiency, current)
            results, _ = solve_system(rails, meshes, resolve)
            self.assertAlmostEqual(regulator_losses(results)[0]['loss_w'], 0, places=8)
        rails, meshes, resolve, _ = fixture(vin=4)
        with self.assertRaisesRegex(ValueError, 'linear regulator cannot boost'):
            solve_system(rails, meshes, resolve)

    def test_cached_efficiency_change_recomputes_heat(self):
        rails, meshes, resolve, _ = fixture('SWITCHING', .8)
        cache = PreparedNetworkCache()
        try:
            before, _ = solve_system(rails, meshes, resolve, cache=cache)
            rails[0].child_regulators[0].efficiency = .9
            after, _ = solve_system(rails, meshes, resolve, cache=cache)
            self.assertTrue(after['VIN']['diagnostics']['prepared_cache_hit'])
            self.assertAlmostEqual(regulator_losses(before)[0]['loss_w'], 1.25)
            self.assertAlmostEqual(regulator_losses(after)[0]['loss_w'], 5/.9-5)
        finally:
            cache.close()

    def test_regulator_heat_raises_temperature_and_conserves_watts(self):
        rails, meshes, resolve, board = fixture()
        results, _ = solve_system(rails, meshes, resolve)
        settings = ThermalSettings(spacing_mm=5)
        grid = ThermalGrid(box(0, 0, 100, 50), settings)
        stackup = {'substrate': [{'thickness_mm': 1.6}], 'copper': {3: {'thickness_mm': .035}}}
        try:
            copper_w = add_copper_heat(grid, results)
            before = grid.solve(stackup, {}, 'cpu')
            reg_w = add_regulator_heat(grid, results, board, resolve)
            self.assertAlmostEqual(reg_w, 6.9)
            self.assertAlmostEqual(grid.heat.sum(), copper_w+reg_w)
            after = grid.solve(stackup, {}, 'cpu')
            self.assertGreater(after['temperature_c'].max(), before['temperature_c'].max())
            self.assertAlmostEqual(after['input_w'], 7.1)
            self.assertAlmostEqual(after['rejected_w'], 7.1)
        finally:
            grid.close()

    def test_body_center_mapping_fallback_and_split_refs_conserve(self):
        rails, meshes, resolve, board = fixture()
        results, _ = solve_system(rails, meshes, resolve)
        grid = ThermalGrid(box(0, 0, 100, 50), ThermalSettings(spacing_mm=5))
        try:
            self.assertAlmostEqual(add_regulator_heat(grid, results, board, resolve), 6.9)
            self.assertEqual(np.count_nonzero(grid.heat), 1)
            grid.heat.fill(0)
            board.footprints = []
            add_regulator_heat(grid, results, board, resolve)
            self.assertAlmostEqual(grid.heat.sum(), 6.9)
            record = regulator_losses(results)[0]
            record['output_ref'] = 'U2'
            board.footprints = [NS(reference='U1', position=NS(x=25000000, y=25000000)),
                                NS(reference='U2', position=NS(x=75000000, y=25000000))]
            grid.heat.fill(0)
            add_regulator_heat(grid, results, board, resolve)
            nonzero = grid.heat[grid.heat > 0]
            np.testing.assert_allclose(nonzero, [3.45, 3.45])
        finally:
            grid.close()

    def test_duplicate_records_rejected_and_refinement_keeps_loss_check(self):
        rails, meshes, resolve, _ = fixture()
        coarse, _ = solve_system(rails, meshes, resolve)
        fine = copy.deepcopy(coarse)
        regulator_losses(fine)[0]['loss_w'] *= 1.2
        release_coarse_arrays(coarse)
        self.assertTrue(any('Regulator U1 loss_w' in message for message in compare_meshes(coarse, fine, [], [])))
        coarse['5V']['regulator_losses'] = coarse['VIN']['regulator_losses']
        with self.assertRaisesRegex(ValueError, 'Duplicate regulator heat'):
            regulator_losses(coarse)
