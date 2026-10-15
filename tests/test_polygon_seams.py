import unittest
from types import SimpleNamespace as NS
from shapely.geometry import Point
from copper_geometry import polygon_from_api


def api_polygon(points, holes=()):
    def line(coords):
        return NS(nodes=[NS(point=NS(x=x*1e6, y=y*1e6), has_arc=False) for x, y in coords])
    return NS(outline=line(points), holes=[line(hole) for hole in holes])


class TestZoneBoundarySeams(unittest.TestCase):
    def test_bridged_hole_preserves_area_and_void(self):
        shape = polygon_from_api(api_polygon([
            (0, 0), (10, 0), (10, 10), (0, 10), (0, 0),
            (2, 2), (2, 4), (4, 4), (4, 2), (2, 2), (0, 0)]))
        self.assertTrue(shape.is_valid)
        self.assertEqual(shape.area, 96)
        self.assertEqual(len(shape.interiors), 1)
        self.assertFalse(shape.covers(Point(3, 3)))
        self.assertTrue(shape.covers(Point(1, 1)))

    def test_zero_width_link_does_not_connect_copper_islands(self):
        shape = polygon_from_api(api_polygon([
            (0, 0), (2, 0), (2, 2), (0, 2), (0, 0),
            (4, 0), (6, 0), (6, 2), (4, 2), (4, 0), (0, 0)]))
        self.assertTrue(shape.is_valid)
        self.assertEqual(shape.geom_type, 'MultiPolygon')
        self.assertEqual(len(shape.geoms), 2)
        self.assertEqual(shape.area, 8)
        self.assertFalse(shape.covers(Point(3, 0)))

    def test_crossed_boundary_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError, 'change copper area'):
            polygon_from_api(api_polygon([(0, 0), (2, 2), (0, 2), (2, 0)]))

    def test_explicit_hole_stays_empty(self):
        shape = polygon_from_api(api_polygon([(0, 0), (10, 0), (10, 10), (0, 10)],
                                            [[(2, 2), (4, 2), (4, 4), (2, 4)]]))
        self.assertEqual(shape.area, 96)
        self.assertFalse(shape.covers(Point(3, 3)))
