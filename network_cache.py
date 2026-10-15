"""Bounded session cache for prepared DC networks, never for their solutions."""
from collections import OrderedDict
from array import array
import hashlib
import sys
import types

import numpy as np
from scipy.sparse import issparse


def _mesh_signature(mesh):
    """Check mutable meshes too; object identity alone cannot detect edited edges."""
    digest = hashlib.blake2b(digest_size=20)
    if isinstance(mesh.nodes, range):
        digest.update(repr(mesh.nodes).encode())
    else:
        digest.update(np.asarray(mesh.nodes, dtype=np.int64).tobytes())
    for name in ('G_coo_row', 'G_coo_col', 'G_coo_data'):
        value = getattr(mesh, name, ())
        digest.update(memoryview(value) if len(value) else b'')
    if not len(getattr(mesh, 'G_coo_data', ())):
        digest.update(repr(mesh.edges).encode())
    return id(mesh), digest.digest()


def network_signature(meshes, links, sources, loads, resolve, backend):
    """Currents change the RHS; terminals, boundaries and conductances change A."""
    def terminal(net, ref, pads):
        return net, ref, tuple(pads), tuple(int(node) for node in resolve(meshes[net], ref, pads))
    return (backend, tuple((net, _mesh_signature(mesh)) for net, mesh in meshes.items()),
            tuple((link.ref_des, link.net_a, link.pad_a, link.net_b, link.pad_b,
                   link.resistance_ohm, link.closed, link.kind,
                   terminal(link.net_a, link.ref_des, [link.pad_a]),
                   terminal(link.net_b, link.ref_des, [link.pad_b])) for link in links),
            tuple((terminal(net, ref, pads), voltage) for net, ref, pads, voltage in sources),
            tuple(terminal(net, ref, pads) for net, ref, pads, _ in loads))


def retained_host_bytes(value, seen=None):
    """Estimate retained host RAM, counting each backing allocation once.

    Pass one ``seen`` set across mesh, prepared-network and result roots: these
    deliberately share meshes and array views. Device allocations belong to the
    independent VRAM budget and must not reduce the host RAM allowance. Callers
    should pass simulation data, rather than UI windows or live IPC clients.
    """
    seen = set() if seen is None else seen
    def size(value):
        if value is None or isinstance(value, (types.ModuleType, type)) or callable(value):
            return 0
        module = type(value).__module__.split('.', 1)[0]
        if module in ('cupy', 'cupyx') or hasattr(type(value), '__cuda_array_interface__'):
            return 0
        key = id(value)
        if key in seen:
            return 0
        seen.add(key)
        if isinstance(value, np.ndarray):
            # getsizeof owns the payload only when OWNDATA is true. A tiny slice
            # can retain a much larger base, including array.array/bytes buffers.
            amount = sys.getsizeof(value)
            if value.base is not None:
                amount += size(value.base)
            elif value.dtype.hasobject:
                amount += sum(size(item) for item in value.flat)
            return amount
        if isinstance(value, memoryview):
            return sys.getsizeof(value)+size(value.obj)
        if issparse(value):
            names = ('data', 'indices', 'indptr', 'row', 'col', 'rows')
            return sys.getsizeof(value)+sum(size(getattr(value, name)) for name in names if hasattr(value, name))
        if isinstance(value, dict):
            return sys.getsizeof(value)+sum(size(key)+size(item) for key, item in value.items())
        if isinstance(value, (list, tuple, set, frozenset)):
            return sys.getsizeof(value)+sum(size(item) for item in value)
        if isinstance(value, array):
            return sys.getsizeof(value)
        if type(value).__name__ == 'SuperLU':
            # SuperLU's native factors are not in __dict__. L/U expose their
            # sparse buffers; leave headroom for permutations/native metadata.
            return sys.getsizeof(value)+int(1.5*(size(value.L)+size(value.U)))
        if hasattr(value, '__dict__'):
            attributes = vars(value)
            # BoardSnapshot retains a live client for lazy pad requests. It is
            # shared infrastructure, not owned simulation-cache memory.
            if type(value).__name__ == 'BoardSnapshot':
                return sys.getsizeof(value)+sys.getsizeof(attributes)+sum(
                    size(name)+size(item) for name, item in attributes.items() if name != 'original')
            return sys.getsizeof(value)+size(attributes)
        slots = getattr(type(value), '__slots__', ())
        if isinstance(slots, str):
            slots = (slots,)
        if slots:
            return sys.getsizeof(value)+sum(size(getattr(value, name)) for name in slots
                                           if name not in ('__dict__', '__weakref__') and hasattr(value, name))
        return sys.getsizeof(value)
    return size(value)


def prepared_bytes(prepared, seen=None):
    """Conservative retained host RAM estimate for a prepared network."""
    return retained_host_bytes(prepared, seen)


class PreparedNetworkCache:
    """LRU cache owned by a simulation window and released when it closes."""
    def __init__(self, max_bytes=1024**3, max_entries=8):
        self.max_bytes = max(0, int(max_bytes))
        self.max_entries = max(0, int(max_entries))
        self.entries = OrderedDict()
        self.hits = self.misses = 0

    @property
    def bytes_used(self):
        seen = set()
        return sum(prepared_bytes(prepared, seen) for prepared, _ in self.entries.values())

    def get(self, key):
        record = self.entries.pop(key, None)
        if record is None:
            self.misses += 1
            return None
        self.entries[key] = record
        self.hits += 1
        return record[0]

    def store(self, key, prepared):
        old = self.entries.pop(key, None)
        if old is not None and old[0] is not prepared:
            old[0].close()
        amount = prepared_bytes(prepared)
        if self.max_entries == 0 or amount > self.max_bytes:
            prepared.close()
            return False
        self.entries[key] = prepared, amount
        self.trim()
        return key in self.entries

    def trim(self):
        while self.entries and (len(self.entries) > self.max_entries or self.bytes_used > self.max_bytes):
            _, (prepared, _) = self.entries.popitem(last=False)
            prepared.close()

    def set_budget(self, max_bytes):
        self.max_bytes = max(0, int(max_bytes))
        self.trim()

    def clear(self):
        while self.entries:
            _, (prepared, _) = self.entries.popitem()
            prepared.close()

    close = clear
