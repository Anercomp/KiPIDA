"""PCB overlay geometry preparation, independent of the wx UI thread."""
from dataclasses import dataclass
import threading
import numpy as np
from matplotlib.path import Path
from shapely.geometry.polygon import orient


def polygon_paths(geometry):
    if geometry is None or geometry.is_empty or geometry.area <= 0:
        return []
    polygons = polygon_paths_geometry(geometry)
    paths = []
    for polygon in polygons:
        polygon = orient(polygon, sign=1)
        vertices, codes = [], []
        for ring in [polygon.exterior, *polygon.interiors]:
            coordinates = np.asarray(ring.coords).copy()
            coordinates[:, 1] *= -1
            vertices.extend(coordinates)
            codes.extend([Path.MOVETO]+[Path.LINETO]*(len(coordinates)-2)+[Path.CLOSEPOLY])
        paths.append(Path(vertices, codes))
    return paths


def polygon_paths_geometry(geometry):
    if geometry.geom_type == 'Polygon':
        return [geometry]
    if hasattr(geometry, 'geoms'):
        return [p for part in geometry.geoms for p in polygon_paths_geometry(part)]
    return []


def polygon_outline_segments(geometry):
    """Read-only polylines for exterior and drill/zone-hole outlines in PCB X, -Y."""
    if geometry is None or geometry.is_empty or geometry.area <= 0:
        return []
    segments = []
    for polygon in polygon_paths_geometry(geometry):
        for ring in (polygon.exterior, *polygon.interiors):
            coordinates = np.asarray(ring.coords, dtype=np.float64).copy()
            coordinates[:, 1] *= -1
            coordinates.setflags(write=False)
            segments.append(coordinates)
    return segments


@dataclass(frozen=True)
class PreparedOverlay:
    geometry: dict
    segments: dict


class OverlayCache:
    """One board's geometry and render segments, shared across temperature views.

    Extraction and preparation run on a daemon worker. Callbacks are dispatched
    through the supplied scheduler; cancellation/invalidation drops late results.
    A loader must use a captured board snapshot or its own IPC client.
    """
    def __init__(self):
        self._lock = threading.RLock()
        self._generation = 0
        self._key = None
        self._prepared = None
        self._loading = False
        self._callbacks = {}
        self._next_subscription = 0

    def invalidate(self, key=None):
        with self._lock:
            self._generation += 1
            self._key = key
            self._prepared = None
            self._loading = False
            self._callbacks.clear()

    def request(self, loader, callback, dispatch, key=None):
        with self._lock:
            if key is not None and key != self._key:
                self.invalidate(key)
            generation = self._generation
            subscription = self._next_subscription
            self._next_subscription += 1
            self._callbacks[subscription] = (callback, dispatch)
            prepared = self._prepared
            start = prepared is None and not self._loading
            if start:
                self._loading = True
        def cancel():
            with self._lock:
                self._callbacks.pop(subscription, None)
        if prepared is not None:
            self._dispatch(subscription, generation, prepared, None)
        elif start:
            thread = threading.Thread(target=self._load, args=(loader, generation),
                                      name='KiPIDA PCB overlay', daemon=True)
            thread.start()
        return cancel

    def _load(self, loader, generation):
        try:
            geometry = loader()
            prepared = PreparedOverlay(geometry, {
                layer: polygon_outline_segments(shape) for layer, shape in geometry.items()})
            error = None
        except Exception as exc:
            prepared, error = None, exc
        with self._lock:
            if generation != self._generation:
                return
            self._loading = False
            self._prepared = prepared
            subscriptions = list(self._callbacks)
        for subscription in subscriptions:
            self._dispatch(subscription, generation, prepared, error)

    def _dispatch(self, subscription, generation, prepared, error):
        with self._lock:
            target = self._callbacks.get(subscription)
        if target is None:
            return
        callback, dispatch = target
        def deliver():
            with self._lock:
                if generation != self._generation or subscription not in self._callbacks:
                    return
                self._callbacks.pop(subscription, None)
            callback(prepared, error)
        dispatch(deliver)


def make_overlay_loader(board):
    """Use the supplied snapshot, or clone an IPC connection for worker ownership."""
    from extractor import GeometryExtractor
    client = getattr(board, '_kicad', None)
    if client is None:
        return lambda: GeometryExtractor(board).get_board_copper_geometry()
    # Never share a Req0 request/reply socket with simulation or UI operations.
    endpoint = client._socket_path
    token = client._kicad_token
    timeout_ms = client._timeout_ms
    document = type(board.document)()
    document.CopyFrom(board.document)
    def load():
        from kipy.board import Board
        from kipy.client import KiCadClient
        worker_client = KiCadClient(endpoint, 'KiPIDA overlay', token, timeout_ms)
        try:
            return GeometryExtractor(Board(worker_client, document)).get_board_copper_geometry()
        finally:
            connection = getattr(worker_client, '_conn', None)
            if connection is not None:
                connection.close()
    return load
