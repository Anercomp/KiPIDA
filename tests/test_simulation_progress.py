import unittest
from types import SimpleNamespace as NS
from shapely.geometry import box
from mesh import Mesher
from thermal_solver import ThermalGrid, ThermalSettings, add_copper_heat
from network_solver import solve_system


class TestSimulationProgress(unittest.TestCase):
    def test_mesh_batches_report_monotonic_completion(self):
        values = []
        mesher = Mesher(NS(footprints=[], vias=[]))
        mesher.progress_callback = values.append
        stackup = {'copper': {3: {'thickness_mm': .035}, 34: {'thickness_mm': .035}}, 'resistivity': 1.68e-5}
        mesher.generate_mesh('PWR', {3: box(0, 0, 1, 1), 34: box(0, 0, 1, 1)}, stackup, .1)
        self.assertEqual(values[-1], 1)
        self.assertEqual(values, sorted(values))
        self.assertGreater(len(values), 4)

    def test_network_completion_after_validation(self):
        from tests.test_network_solver import TestStitchedNetwork
        values = []
        solve_system(*TestStitchedNetwork().fixture(), progress=values.append)
        self.assertEqual(values, [1])
        values.clear()
        with self.assertRaises(ValueError):
            solve_system(*TestStitchedNetwork().fixture(closed=False), progress=values.append)
        self.assertEqual(values, [])

    def test_thermal_grid_and_solve_report_completion(self):
        values = []
        grid = ThermalGrid(box(0, 0, 4, 4), ThermalSettings(), progress=lambda fraction, stage: values.append(fraction))
        grid.heat[:] = grid.areas*100
        grid.solve({'substrate': [{'thickness_mm': 1.6}]}, {}, 'cpu')
        self.assertEqual(values[-1], 1)
        self.assertEqual(values, sorted(values))
