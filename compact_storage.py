"""Array-backed mapping interfaces for existing mesh and plotting consumers."""
from collections.abc import Mapping
import numpy as np


class Coordinates(Mapping):
    def __init__(self):
        self.parts = []
        self.ends = []

    def append(self, coordinates):
        self.parts.append(np.asarray(coordinates, dtype=np.float64))
        self.ends.append((self.ends[-1] if self.ends else 0)+len(coordinates))

    def __len__(self):
        return self.ends[-1] if self.ends else 0

    def __iter__(self):
        return iter(range(len(self)))

    def __getitem__(self, key):
        if not isinstance(key, (int, np.integer)) or key < 0 or key >= len(self):
            raise KeyError(key)
        part = int(np.searchsorted(self.ends, key, side='right'))
        return self.parts[part][key-(self.ends[part-1] if part else 0)]

    def items(self):
        offset = 0
        for part in self.parts:
            for i, coordinate in enumerate(part):
                yield offset+i, coordinate
            offset += len(part)

    def values(self):
        return (coordinate for part in self.parts for coordinate in part)

    def take(self, keys):
        keys = np.asarray(keys)
        which = np.searchsorted(self.ends, keys, side='right')
        output = np.empty((len(keys), 3))
        for i, part in enumerate(self.parts):
            mask = which == i
            output[mask] = part[keys[mask]-(self.ends[i-1] if i else 0)]
        return output


class Voltages(Mapping):
    def __init__(self, values):
        self.array = np.asarray(values, dtype=np.float64)
        self.count = int(np.isfinite(self.array).sum())

    def __len__(self):
        return self.count

    def __iter__(self):
        return iter(np.flatnonzero(np.isfinite(self.array)))

    def __getitem__(self, key):
        if not isinstance(key, (int, np.integer)) or key < 0 or key >= len(self.array) or not np.isfinite(self.array[key]):
            raise KeyError(key)
        return float(self.array[key])

    def values(self):
        return iter(self.array[np.isfinite(self.array)])


class GridLookup:
    def __init__(self, grid, layers, origin, step):
        self.grid, self.layers, self.origin, self.step = grid, layers, origin, step

    def window(self, layer, bounds):
        if layer not in self.layers:
            return np.empty(0, dtype=np.int32)
        xmin, ymin, xmax, ymax = bounds
        ox, oy = self.origin
        ny, nx = self.grid.shape[1:]
        x0 = max(0, int(np.floor((xmin-ox)/self.step))-1)
        x1 = min(nx, int(np.ceil((xmax-ox)/self.step))+2)
        y0 = max(0, int(np.floor((ymin-oy)/self.step))-1)
        y1 = min(ny, int(np.ceil((ymax-oy)/self.step))+2)
        if x1 <= x0 or y1 <= y0:
            return np.empty(0, dtype=np.int32)
        nodes = self.grid[self.layers[layer], y0:y1, x0:x1].ravel()
        return nodes[nodes >= 0]
