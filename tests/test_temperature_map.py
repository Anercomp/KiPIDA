import unittest
from types import SimpleNamespace as NS
import numpy as np
from shapely.geometry import box
from temperature_map import TemperatureMap


class TestTemperatureMap(unittest.TestCase):
    def setUp(self):
        self.data = dict(xy=np.array([[10.5, 20.5], [11.5, 20.5], [10.5, 21.5]]),
                         temperature_c=np.array([25., 55., 35.]), settings=NS(spacing_mm=1))
        self.map = TemperatureMap(self.data)

    def test_peak_value_and_board_coordinates(self):
        self.assertEqual(self.map.maximum_c, 55)
        np.testing.assert_array_equal(self.map.maximum_xy, [11.5, 20.5])
        self.assertEqual(self.map.extent, (10, 12, -22, -20))

    def test_pointer_selects_cell_without_crossing_gap(self):
        self.assertEqual(self.map.lookup(10.1, 20.9), 25)
        self.assertEqual(self.map.lookup(11.2, 20.9), 55)
        self.assertEqual(self.map.lookup(10.2, 21.9), 35)
        self.assertIsNone(self.map.lookup(11.2, 21.9))
        self.assertIsNone(self.map.lookup(9.9, 20.9))
        self.assertIsNone(self.map.lookup(12, 20.9))
        self.assertIsNone(self.map.lookup(10.2, 22))

    def test_partial_cell_board_cutout_is_not_reported_as_board(self):
        self.data['region'] = box(10, 20, 12, 22).difference(box(10.1, 20.1, 10.3, 20.3))
        mapping = TemperatureMap(self.data)
        self.assertIsNone(mapping.lookup(10.2, 20.2))
        self.assertEqual(mapping.lookup(10.5, 20.5), 25)

    def test_invalid_pointer_has_no_temperature(self):
        for x, y in [(None, 20), (10, None), (float('nan'), 20), (10, float('inf'))]:
            self.assertIsNone(self.map.lookup(x, y))
