import unittest
import threading
import numpy as np
from shapely.geometry import box
from pcb_overlay import polygon_paths, polygon_outline_segments, OverlayCache
from matplotlib.path import Path
from extractor import GeometryExtractor
from types import SimpleNamespace as NS


class TestPCBOverlay(unittest.TestCase):
    def test_board_coordinate_flip_and_hole_winding(self):
        paths = polygon_paths(box(10, 20, 14, 24).difference(box(11, 21, 13, 23)))
        self.assertEqual(len(paths), 1)
        path = paths[0]
        self.assertEqual(np.count_nonzero(path.codes == Path.MOVETO), 2)
        self.assertEqual(np.count_nonzero(path.codes == Path.CLOSEPOLY), 2)
        np.testing.assert_array_equal(path.vertices.min(axis=0), [10, -24])
        np.testing.assert_array_equal(path.vertices.max(axis=0), [14, -20])
        def signed_area(ring):
            return np.sum(ring[:-1, 0]*ring[1:, 1]-ring[1:, 0]*ring[:-1, 1])/2
        self.assertLess(signed_area(path.vertices[:5]), 0)
        self.assertGreater(signed_area(path.vertices[5:]), 0)

    def test_islands_and_empty_layer(self):
        self.assertEqual(len(polygon_paths(box(0, 0, 1, 1).union(box(2, 2, 3, 3)))), 2)
        self.assertEqual(polygon_paths(None), [])
        self.assertEqual(polygon_paths(box(0, 0, 0, 0)), [])

    def test_outline_only_preserves_holes_and_read_only_coordinates(self):
        segments = polygon_outline_segments(box(10, 20, 14, 24).difference(box(11, 21, 13, 23)))
        self.assertEqual(len(segments), 2)
        for segment in segments:
            self.assertFalse(segment.flags.writeable)
            np.testing.assert_array_equal(segment[0], segment[-1])
        vertices = np.concatenate(segments)
        np.testing.assert_array_equal(vertices.min(axis=0), [10, -24])
        np.testing.assert_array_equal(vertices.max(axis=0), [14, -20])

    def test_async_cache_reuses_prepared_segments_across_views(self):
        cache, queued, calls = OverlayCache(), [], []
        completed = threading.Event()
        main_thread = threading.get_ident()
        def loader():
            calls.append(threading.get_ident())
            return {3: box(0, 0, 1, 1)}
        def dispatch(callback):
            queued.append(callback)
            completed.set()
        results = []
        cache.request(loader, lambda *args: results.append(args), dispatch, key='board A')
        self.assertTrue(completed.wait(5))
        queued.pop(0)()
        first = results[0][0]
        self.assertIsNone(results[0][1])
        self.assertNotEqual(calls[0], main_thread)
        cache.request(loader, lambda *args: results.append(args), dispatch, key='board A')
        queued.pop(0)()
        self.assertIs(results[1][0], first)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(first.segments[3]), 1)

    def test_invalidated_and_cancelled_requests_cannot_touch_closed_views(self):
        cache, queued, results = OverlayCache(), [], []
        dispatch_ready, entered, release = threading.Event(), threading.Event(), threading.Event()
        def loader():
            entered.set()
            release.wait(5)
            return {3: box(0, 0, 1, 1)}
        def dispatch(callback):
            queued.append(callback)
            dispatch_ready.set()
        cancel = cache.request(loader, lambda *args: results.append(args), dispatch)
        self.assertTrue(entered.wait(5))
        release.set()
        self.assertTrue(dispatch_ready.wait(5))
        cancel()
        queued.pop(0)()
        self.assertEqual(results, [])
        cache.request(loader, lambda *args: results.append(args), dispatch)
        cache.invalidate('board B')
        queued.pop(0)()
        self.assertEqual(results, [])

    def test_all_net_extraction_includes_unsimulated_and_unconnected_tracks(self):
        def track(name, y):
            return NS(net=NS(name=name), start=NS(x=0, y=y), end=NS(x=1000000, y=y), width=200000, layer=3)
        board = NS(tracks=[track('VIN', 0), track('GND', 2000000), track('', 4000000)], vias=[], footprints=[], zones=[])
        extractor = GeometryExtractor(board)
        extractor._stackup_cache = {'copper': {3: {'thickness_mm': .035}}}
        selected = extractor.get_net_geometry('VIN')[3]
        overlay = extractor.get_board_copper_geometry()[3]
        self.assertGreater(overlay.area, selected.area*2.9)
        self.assertAlmostEqual(overlay.bounds[3], 4.1)
