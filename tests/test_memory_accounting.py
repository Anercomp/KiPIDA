"""Host RAM accounting for shared caches, results and native solver state."""
from array import array
import sys
from types import SimpleNamespace
import unittest

import numpy as np
from scipy.sparse import eye
from scipy.sparse.linalg import splu

from network_cache import PreparedNetworkCache, prepared_bytes, retained_host_bytes


class TestRetainedHostMemory(unittest.TestCase):
    def test_numpy_views_retain_the_whole_backing_allocation_once(self):
        base = np.zeros(1024*128)
        first, second = base[:4], base[::100]
        seen = set()
        self.assertEqual(retained_host_bytes(first, seen), sys.getsizeof(first)+sys.getsizeof(base))
        self.assertEqual(retained_host_bytes(second, seen), sys.getsizeof(second))
        self.assertEqual(retained_host_bytes(base, seen), 0)
        self.assertEqual(retained_host_bytes(first, seen), 0)

    def test_array_stamps_and_numpy_memoryviews_share_their_storage(self):
        stamps = array('d', range(1000))
        view = np.frombuffer(stamps, dtype=np.float64)
        seen = set()
        self.assertEqual(retained_host_bytes(stamps, seen), sys.getsizeof(stamps))
        self.assertEqual(retained_host_bytes(view, seen), sys.getsizeof(view)+sys.getsizeof(view.base))
        # The order of cache/result roots must not change the total.
        reverse = set()
        self.assertEqual(retained_host_bytes(view, reverse)+retained_host_bytes(stamps, reverse),
                         sys.getsizeof(stamps)+sys.getsizeof(view)+sys.getsizeof(view.base))

    def test_mesh_prepared_and_result_roots_are_deduplicated_together(self):
        mesh = SimpleNamespace(coordinates=np.zeros((50000, 3)), edges=array('d', range(1000)))
        values = np.zeros(50000)
        mesh_entries = {'coarse': (mesh, 123)}
        prepared_entries = {'coarse': (SimpleNamespace(meshes={'VIN': mesh}, values=values), 456)}
        result = {'VIN': {'mesh': mesh, 'results': SimpleNamespace(array=values)}}
        separate = sum(retained_host_bytes(root) for root in (mesh_entries, prepared_entries, result))
        seen = set()
        shared = sum(retained_host_bytes(root, seen) for root in (mesh_entries, prepared_entries, result))
        self.assertGreater(separate-shared, 2*mesh.coordinates.nbytes+values.nbytes)
        self.assertEqual(retained_host_bytes(mesh, seen), 0)
        self.assertEqual(retained_host_bytes(values, seen), 0)

    def test_cuda_storage_is_not_charged_to_the_host_budget(self):
        gpu_array = type('ndarray', (), {'__module__': 'cupy._core.core', 'nbytes': 8*1024**3})()
        gpu_sparse = type('csr_matrix', (), {'__module__': 'cupyx.scipy.sparse._csr'})()
        gpu_sparse.data = np.zeros(50000)  # Sentinel: never traverse a device wrapper.
        cuda_foreign = type('CudaArray', (), {'__cuda_array_interface__': {}, 'nbytes': 1024**3})()
        self.assertEqual(retained_host_bytes(gpu_array), 0)
        self.assertEqual(retained_host_bytes(gpu_sparse), 0)
        self.assertEqual(retained_host_bytes(cuda_foreign), 0)
        host = np.zeros(1000)
        retained = SimpleNamespace(matrix=host, gpu_matrix=gpu_sparse, gpu_last=gpu_array)
        self.assertGreaterEqual(prepared_bytes(retained), host.nbytes)
        self.assertLess(prepared_bytes(retained), host.nbytes+10000)

    def test_superlu_native_factors_are_included_and_shared_once(self):
        factor = splu(eye(200, format='csc'))
        sparse_payload = sum(item.nbytes for matrix in (factor.L, factor.U)
                             for item in (matrix.data, matrix.indices, matrix.indptr))
        seen = set()
        self.assertGreaterEqual(retained_host_bytes(factor, seen), int(1.5*sparse_payload))
        self.assertEqual(retained_host_bytes(factor, seen), 0)

    def test_cycles_and_external_client_callbacks_are_not_traversed(self):
        snapshot_class = type('BoardSnapshot', (), {})
        snapshot = snapshot_class()
        snapshot.original = SimpleNamespace(arbitrary=np.zeros(50000))
        snapshot.data = np.zeros(100)
        snapshot.callback = lambda: snapshot.original
        snapshot.module = np
        snapshot.type = type(snapshot)
        snapshot.cycle = snapshot
        amount = retained_host_bytes(snapshot)
        self.assertGreaterEqual(amount, snapshot.data.nbytes)
        self.assertLess(amount, 10000)

    def test_prepared_cache_counts_shared_mesh_once_and_updated_host_state(self):
        class Prepared:
            def __init__(self, mesh):
                self.mesh = mesh
                self.closed = False
            def close(self):
                self.closed = True
        mesh = SimpleNamespace(coordinates=np.zeros(50000))
        first, second = Prepared(mesh), Prepared(mesh)
        cache = PreparedNetworkCache(max_bytes=10*1024**2)
        try:
            self.assertTrue(cache.store('first', first))
            self.assertTrue(cache.store('second', second))
            separate = prepared_bytes(first)+prepared_bytes(second)
            self.assertGreater(separate-cache.bytes_used, mesh.coordinates.nbytes)
            before = cache.bytes_used
            first.last = np.zeros(10000)
            self.assertGreaterEqual(cache.bytes_used-before, first.last.nbytes)
        finally:
            cache.close()


if __name__ == '__main__':
    unittest.main()
