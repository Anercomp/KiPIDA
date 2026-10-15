"""Read-only board snapshots and bounded, session-local mesh reuse."""
from collections import OrderedDict
from dataclasses import asdict
import copy
import hashlib
import json
import threading


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     default=str, allow_nan=False).encode()).hexdigest()


def rail_signature(rails):
    return signature([asdict(rail) for rail in rails])


class BoardSnapshot:
    """Capture all simulation inputs once; never share an IPC client with workers."""
    def __init__(self, board):
        self.original = board
        self._snapshot_thread = threading.get_ident()
        self._pad_polygons = {}
        digest = hashlib.sha256()
        self.cache_token = None
        if board is None:
            return
        supported = True
        for name in ('tracks', 'vias', 'footprints', 'zones', 'shapes', 'stackup'):
            getter = getattr(board, 'get_'+name, None)
            if getter is not None:
                value = getter()
            elif hasattr(board, name):
                value = getattr(board, name)
            else:
                supported = False
                continue
            items = [self._copy_item(item) for item in ([value] if name == 'stackup' else list(value))]
            captured = items[0] if name == 'stackup' else items
            # Some consumers check properties before getters. Both must read the
            # captured data, even for an offline board adapter.
            setattr(self, name, captured)
            setattr(self, 'get_'+name, lambda captured=captured: captured)
            serialized = []
            for item in items:
                proto = getattr(item, 'proto', item)
                if not hasattr(proto, 'SerializeToString'):
                    supported = False
                    break
                serialized.append(proto.SerializeToString(deterministic=True))
            digest.update(name.encode())
            for data in sorted(serialized):
                digest.update(len(data).to_bytes(8, 'little')); digest.update(data)
        if supported:
            self.cache_token = digest.hexdigest()
        if hasattr(board, 'get_pad_shapes_as_polygons'):
            self.get_pad_shapes_as_polygons = self._get_pad_shapes_as_polygons

    @staticmethod
    def _copy_item(item):
        proto = getattr(item, 'proto', item)
        if hasattr(proto, 'SerializeToString'):
            cloned = type(proto)()
            cloned.CopyFrom(proto)
            if proto is item:
                return cloned
            try:
                return type(item)(cloned)
            except TypeError:
                pass
        return copy.deepcopy(item)

    def _get_pad_shapes_as_polygons(self, pad, layer):
        # KiCad's socket belongs to the capture thread. Overlay workers create
        # their own client instead of invoking this memoized main-thread method.
        if threading.get_ident() != self._snapshot_thread:
            raise RuntimeError('Snapshot pad polygons must be read on the capture thread; use a separate IPC client for workers.')
        proto = getattr(pad, 'proto', None)
        token = proto.SerializeToString(deterministic=True) if proto is not None else id(pad)
        key = token, layer
        if key not in self._pad_polygons:
            value = self.original.get_pad_shapes_as_polygons(pad, layer)
            self._pad_polygons[key] = self._copy_item(value) if value is not None else None
        return self._pad_polygons[key]

    def __getattr__(self, name):
        return getattr(self.original, name)


def mesh_bytes(mesh):
    total = 0
    for name in ('G_coo_data', 'G_coo_row', 'G_coo_col'):
        data = getattr(mesh, name, ())
        total += len(data)*getattr(data, 'itemsize', 8)
    coords = getattr(mesh, 'node_coords', {})
    total += sum(part.nbytes for part in coords.parts) if hasattr(coords, 'parts') else len(coords)*128
    lookup = getattr(mesh, 'grid_lookup', None)
    if lookup is not None:
        total += lookup.grid.nbytes
    matrix = getattr(mesh, 'G_final_csr', None)
    if matrix is not None:
        total += matrix.data.nbytes+matrix.indices.nbytes+matrix.indptr.nbytes
    total += len(getattr(mesh, 'edges', ()))*96
    return total+4096


class MeshCache:
    def __init__(self, max_bytes=256*1024**2):
        self.max_bytes = max(0, int(max_bytes))
        self.entries = OrderedDict()
        self.bytes_used = 0

    def set_budget(self, size):
        self.max_bytes = max(0, int(size))
        self._trim()

    def _trim(self):
        while self.entries and self.bytes_used > self.max_bytes:
            _, (_, size) = self.entries.popitem(last=False)
            self.bytes_used -= size

    def get(self, key):
        record = self.entries.get(key)
        if record is None:
            return None
        self.entries.move_to_end(key)
        return record[0]

    def put(self, key, mesh):
        if key in self.entries:
            self.bytes_used -= self.entries.pop(key)[1]
        size = mesh_bytes(mesh)
        if size <= self.max_bytes:
            self.entries[key] = mesh, size
            self.bytes_used += size
        self._trim()

    def clear(self):
        self.entries.clear(); self.bytes_used = 0
