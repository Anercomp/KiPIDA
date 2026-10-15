"""Snapshot freshness and bounded cache checks using actual KiCad protos."""
from array import array
from pathlib import Path
from types import SimpleNamespace as NS
import threading
import unittest
from unittest.mock import patch

import numpy as np
from scipy import sparse
from kipy.board import BoardStackup
from kipy.board_types import Track, Via, FootprintInstance, Zone, BoardShape, Pad

from compact_storage import Coordinates, GridLookup
from models import PowerRail, UnifiedLoad, ComponentRef
from resource_budget import GIB, auto_budget_gb, resolve_budget, check_budget
from simulation_cache import BoardSnapshot, MeshCache, mesh_bytes, rail_signature, signature


class ProtoBoard:
    def __init__(self):
        first, second = Track(), Track()
        first.proto.width.value_nm = 100000
        second.proto.width.value_nm = 200000
        self.tracks = [first, second]
        self.vias = [Via()]
        self.footprints = [FootprintInstance()]
        self.zones = [Zone()]
        self.shapes = [BoardShape()]
        self.stackup = BoardStackup()
        self.calls = []
        for name in ('tracks', 'vias', 'footprints', 'zones', 'shapes', 'stackup'):
            setattr(self, 'get_'+name, lambda name=name: self.read(name))

    def read(self, name):
        self.calls.append(name)
        return getattr(self, name)


class TestBoardSnapshot(unittest.TestCase):
    def test_captures_once_and_preserves_types_without_live_mutation(self):
        board = ProtoBoard()
        snapshot = BoardSnapshot(board)
        self.assertIsNotNone(snapshot.cache_token)
        self.assertEqual(board.calls, ['tracks', 'vias', 'footprints', 'zones', 'shapes', 'stackup'])
        self.assertIsInstance(snapshot.get_tracks()[0], Track)
        self.assertIsInstance(snapshot.get_stackup(), BoardStackup)
        self.assertIsNot(snapshot.get_tracks()[0].proto, board.tracks[0].proto)
        self.assertIs(snapshot.tracks, snapshot.get_tracks())
        original = snapshot.get_tracks()[0].proto.SerializeToString()
        board.tracks[0].proto.width.value_nm = 300000
        board.tracks.reverse()
        self.assertEqual(snapshot.get_tracks()[0].proto.SerializeToString(), original)
        self.assertEqual(len(board.calls), 6)
        self.assertNotEqual(BoardSnapshot(board).cache_token, snapshot.cache_token)

    def test_list_order_changes_do_not_invalidate_identical_geometry(self):
        board = ProtoBoard()
        original = BoardSnapshot(board).cache_token
        board.tracks.reverse()
        self.assertEqual(BoardSnapshot(board).cache_token, original)
        board.stackup.proto.finish.type_name = 'ENIG'
        self.assertNotEqual(BoardSnapshot(board).cache_token, original)

    def test_missing_or_unserializable_inputs_disable_reuse_and_copy_properties(self):
        board = NS(tracks=[NS(width=10)], footprints=[], vias=[], zones=[], shapes=[], stackup=NS(thickness=1))
        snapshot = BoardSnapshot(board)
        self.assertIsNone(snapshot.cache_token)
        board.tracks[0].width = 20
        self.assertEqual(snapshot.get_tracks()[0].width, 10)
        self.assertIsNone(BoardSnapshot(None).cache_token)
        self.assertIsNone(BoardSnapshot(NS(get_tracks=lambda: [])).cache_token)

    def test_pad_polygon_requests_are_lazy_memoized_and_never_share_worker_socket(self):
        board = ProtoBoard()
        calls = []
        polygon = NS(area=1)
        board.get_pad_shapes_as_polygons = lambda pad, layer: (calls.append((pad, layer)) or polygon)
        snapshot = BoardSnapshot(board)
        self.assertFalse(calls)
        pad = Pad()
        self.assertEqual(snapshot.get_pad_shapes_as_polygons(pad, 3).area, 1)
        polygon.area = 9
        self.assertEqual(snapshot.get_pad_shapes_as_polygons(Pad(pad.proto), 3).area, 1)
        self.assertEqual(len(calls), 1)
        snapshot.get_pad_shapes_as_polygons(pad, 34)
        self.assertEqual(len(calls), 2)
        errors = []
        def worker():
            try:
                snapshot.get_pad_shapes_as_polygons(pad, 3)
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=worker)
        thread.start(); thread.join(3)
        self.assertEqual(len(calls), 2)
        self.assertIn('separate IPC client', errors[0])

    def test_voltage_load_and_series_changes_invalidate_electrical_signature(self):
        rails = [PowerRail('A', 12, loads=[UnifiedLoad(ComponentRef('U1'), 1, ['1'])])]
        original = rail_signature(rails)
        rails[0].loads[0].total_current = 2
        self.assertNotEqual(rail_signature(rails), original)
        self.assertEqual(signature({'a': 1, 'b': 2}), signature({'b': 2, 'a': 1}))
        with self.assertRaises(ValueError):
            signature(float('nan'))


def fixture_mesh(nodes=3):
    coords = Coordinates()
    coords.append(np.zeros((nodes, 3)))
    stamps = array('d', [1, 1, -1, -1])
    grid = GridLookup(np.zeros((1, 1, nodes), dtype=np.int32), {3: 0}, (0, 0), 1)
    return NS(G_coo_data=stamps, G_coo_row=array('i', [0, 1, 0, 1]), G_coo_col=array('i', [0, 1, 1, 0]),
              node_coords=coords, grid_lookup=grid, G_final_csr=sparse.eye(nodes, format='csr'), edges=[])


class TestMeshCache(unittest.TestCase):
    def test_estimate_accounts_for_coordinate_grid_sparse_and_stamp_payloads(self):
        mesh = fixture_mesh()
        payload = sum(len(getattr(mesh, name))*getattr(mesh, name).itemsize
                      for name in ('G_coo_data', 'G_coo_row', 'G_coo_col'))
        payload += mesh.node_coords.parts[0].nbytes + mesh.grid_lookup.grid.nbytes
        payload += sum(value.nbytes for value in (mesh.G_final_csr.data, mesh.G_final_csr.indices, mesh.G_final_csr.indptr))
        self.assertGreaterEqual(mesh_bytes(mesh), payload)

    def test_lru_replacement_trimming_and_oversized_entries(self):
        mesh = fixture_mesh()
        size = mesh_bytes(mesh)
        cache = MeshCache(max_bytes=size*2)
        cache.put('A', mesh); cache.put('B', mesh)
        self.assertEqual(cache.bytes_used, size*2)
        self.assertIs(cache.get('A'), mesh)
        cache.put('C', mesh)
        self.assertIsNone(cache.get('B'))
        self.assertEqual(list(cache.entries), ['A', 'C'])
        cache.put('A', mesh)
        self.assertEqual(cache.bytes_used, size*2)
        cache.set_budget(size)
        self.assertEqual(list(cache.entries), ['A'])
        oversized = fixture_mesh(nodes=1000)
        cache.put('BIG', oversized)
        self.assertIsNone(cache.get('BIG'))
        self.assertLessEqual(cache.bytes_used, cache.max_bytes)
        cache.set_budget(0)
        self.assertEqual(cache.bytes_used, 0)
        self.assertFalse(cache.entries)
        cache.put('A', mesh)
        self.assertIsNone(cache.get('A'))
        cache.clear()
        self.assertEqual(cache.bytes_used, 0)


class TestAutomaticMemoryBudget(unittest.TestCase):
    def test_dynamic_budget_rechecks_physical_ram_and_reserves_headroom(self):
        with patch('resource_budget.available_ram', side_effect=[16*GIB, 2*GIB]):
            first = resolve_budget('Auto')
            second = resolve_budget('dynamic')
        self.assertLessEqual(first, 16*.65)
        self.assertLessEqual(second, 2*.65)
        self.assertGreater(first, second)
        with patch('resource_budget.available_ram', return_value=8*1024**2):
            self.assertGreater(auto_budget_gb(), 0)
            self.assertLessEqual(auto_budget_gb()*GIB, 8*1024**2*.65)

    def test_invalid_budget_and_actual_free_ram_limit(self):
        for value in (0, -1, float('nan'), float('inf'), True, None, 'bad'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_budget(value)
        self.assertEqual(resolve_budget('2.5'), 2.5)
        with patch('resource_budget.available_ram', return_value=0), self.assertRaises(ValueError):
            auto_budget_gb()
        with patch('resource_budget.available_ram', return_value=GIB), self.assertRaises(ValueError):
            check_budget(.8*GIB, 10, retained_bytes=.3*GIB)


if __name__ == '__main__':
    unittest.main()
