import unittest
from unittest.mock import patch
import numpy as np
from shapely.geometry import box
from thermal_solver import ThermalGrid, ThermalSettings


class TestThermalOptimization(unittest.TestCase):
    def test_geometry_is_clipped_once_per_layer_not_each_batch(self):
        import shapely
        grid = ThermalGrid(box(0, 0, 4, 4), ThermalSettings())
        plane = box(0, 0, 4, 4)
        grid.deposit([[.5, .5]], [.001])
        intersection = shapely.intersection
        clipped = []
        def record(a, b, *args, **kwargs):
            if a is plane and b is grid.region:
                clipped.append(1)
            return intersection(a, b, *args, **kwargs)
        with patch('thermal_solver.shapely.intersection', side_effect=record):
            result = grid.solve({'substrate': [{'thickness_mm': 1.6}], 'copper': {3: {'thickness_mm': .035}}}, {3: plane}, 'cpu')
        self.assertEqual(clipped, [1])
        self.assertLess(result['balance_w'], 1e-9)

    def test_batched_deposit_accumulates_repeated_cells(self):
        grid = ThermalGrid(box(0, 0, 2, 1), ThermalSettings())
        grid.deposit([[.1, .1], [.2, .2], [1.5, .5]], [.1, .2, .4])
        grid.deposit([[.5, .5]], [.3])
        np.testing.assert_allclose(grid.heat, [.6, .4])
