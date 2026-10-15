import unittest
import numpy as np
from types import SimpleNamespace as NS
from shapely.geometry import box
from thermal_solver import ThermalGrid, ThermalSettings, board_region, add_copper_heat
from compact_storage import Coordinates, Voltages
from mesh import Mesh

STACKUP = {'substrate': [{'thickness_mm': 1.6}], 'copper': {3: {'thickness_mm': .035}}}


class TestThermal(unittest.TestCase):
    def test_uniform_plate_matches_analytical_cooling(self):
        grid = ThermalGrid(box(0, 0, 100, 100), ThermalSettings(spacing_mm=10))
        grid.heat[:] = .01  # 1 W over 0.01 m², two faces, h=10: rise 5 K.
        result = grid.solve(STACKUP, {}, 'cpu')
        np.testing.assert_allclose(result['temperature_c'], 30, atol=1e-9)
        self.assertAlmostEqual(result['area_m2'], .01)
        self.assertLess(result['balance_w'], 1e-9)

    def test_two_cell_conduction_and_cooling(self):
        grid = ThermalGrid(box(0, 0, 2, 1), ThermalSettings())
        grid.deposit([[.5, .5]], [.001])
        result = grid.solve(STACKUP, {}, 'cpu')
        g, cooling = .3*.0016, 20e-6
        peak = .001/(2*cooling)*(1+cooling/(cooling+2*g))
        self.assertAlmostEqual(result['temperature_c'][0], 25+peak)
        self.assertGreater(result['temperature_c'][0], result['temperature_c'][1])
        warmer = result['temperature_c'][0]
        spread = grid.solve(STACKUP, {3: box(0, 0, 2, 1)}, 'cpu')
        self.assertLess(spread['temperature_c'][0], warmer)

    def test_cutout_area_and_disconnected_islands(self):
        region = box(0, 0, 4, 4).difference(box(1, 1, 3, 3))
        grid = ThermalGrid(region, ThermalSettings())
        result = grid.solve(STACKUP, {}, 'cpu')
        self.assertAlmostEqual(result['area_m2'], 12e-6)
        np.testing.assert_array_equal(result['temperature_c'], 25)
        with self.assertRaisesRegex(ValueError, 'outside'):
            grid.deposit([[2, 2]], [1])
        islands = ThermalGrid(box(0, 0, 1, 1).union(box(2, 0, 3, 1)), ThermalSettings())
        islands.deposit([[.5, .5]], [.001])
        result = islands.solve(STACKUP, {}, 'cpu')
        self.assertAlmostEqual(result['temperature_c'][1], 25)

    def test_copper_edge_power_and_via_mapping_conserve_heat(self):
        grid = ThermalGrid(box(0, 0, 2, 1), ThermalSettings())
        mesh = Mesh(); mesh.nodes = range(3)
        mesh.node_coords = Coordinates()
        mesh.node_coords.append([[.5, .5, 3], [1.5, .5, 3]])
        mesh.node_coords.append([[1.5, .5, 34]])
        mesh.add_edge_direct(0, 1, 100); mesh.add_edge_direct(1, 2, 100)
        data = {'PWR': dict(mesh=mesh, results=Voltages([12, 11.9, 11.8]), diagnostics={'copper_loss_w': 2})}
        self.assertAlmostEqual(add_copper_heat(grid, data), 2)
        np.testing.assert_allclose(grid.heat, [.5, 1.5])
        data['PWR']['diagnostics']['copper_loss_w'] = 3
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            add_copper_heat(ThermalGrid(box(0, 0, 2, 1), ThermalSettings()), data)

    def test_boundary_partial_cell_area(self):
        grid = ThermalGrid(box(0, 0, 2.4, 1.3), ThermalSettings())
        grid.heat[:] = grid.areas*100
        result = grid.solve(STACKUP, {}, 'cpu')
        self.assertAlmostEqual(result['area_m2'], 2.4*1.3e-6)
        np.testing.assert_allclose(result['temperature_c'], 30, atol=1e-9)

    def test_invalid_settings_and_budget(self):
        for settings in [ThermalSettings(convection_w_m2k=0), ThermalSettings(ambient_c=-300),
                         ThermalSettings(substrate_k_w_mk=float('nan')), ThermalSettings(spacing_mm=.01)]:
            with self.assertRaises(ValueError):
                settings.validate()
        with self.assertRaisesRegex(ValueError, 'budget'):
            ThermalGrid(box(0, 0, 100, 100), ThermalSettings(), budget_gb=.00001)

    def test_refinement_preserves_uniform_heating_solution(self):
        peaks = []
        for step in (2, 1):
            grid = ThermalGrid(box(0, 0, 20, 20), ThermalSettings(spacing_mm=step))
            grid.heat[:] = grid.areas*100
            result = grid.solve(STACKUP, {3: box(0, 0, 20, 20)}, 'cpu')
            peaks.append(float(result['temperature_c'].max()))
            self.assertAlmostEqual(result['input_w'], .04)
        np.testing.assert_allclose(peaks, [30, 30], atol=1e-8)

    def test_outline_holes_and_open_contour_rejection(self):
        from kipy.board_types import BoardRectangle, BoardSegment
        from kipy.geometry import Vector2
        from kipy.proto.board.board_types_pb2 import BL_Edge_Cuts
        outer, hole = BoardRectangle(), BoardRectangle()
        for shape, a, b in [(outer, (0, 0), (4000000, 4000000)), (hole, (1000000, 1000000), (3000000, 3000000))]:
            shape.layer = BL_Edge_Cuts
            shape.top_left, shape.bottom_right = Vector2.from_xy(*a), Vector2.from_xy(*b)
        region = board_region(NS(get_shapes=lambda: [outer, hole]))
        self.assertEqual(region.area, 12)
        line = BoardSegment(); line.layer = BL_Edge_Cuts
        line.start, line.end = Vector2.from_xy(0, 0), Vector2.from_xy(1000000, 0)
        with self.assertRaisesRegex(ValueError, 'closed'):
            board_region(NS(get_shapes=lambda: [line]))
