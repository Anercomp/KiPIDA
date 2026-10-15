"""Thermal-cell lookup shared by rendering and pointer inspection."""
import numpy as np


class TemperatureMap:
    def __init__(self, result):
        self.xy = np.asarray(result['xy'])
        self.temperature = np.asarray(result['temperature_c'])
        self.step = result['settings'].spacing_mm
        self.region = result.get('region')
        self.origin = self.xy.min(axis=0)-self.step/2
        index = np.rint((self.xy-self.origin-self.step/2)/self.step).astype(int)
        self.image = np.full((index[:, 1].max()+1, index[:, 0].max()+1), np.nan)
        self.image[index[:, 1], index[:, 0]] = self.temperature
        peak = int(np.argmax(self.temperature))
        self.maximum_c = float(self.temperature[peak])
        self.maximum_xy = self.xy[peak]

    @property
    def extent(self):
        return (self.origin[0], self.origin[0]+self.image.shape[1]*self.step,
                -(self.origin[1]+self.image.shape[0]*self.step), -self.origin[1])

    def lookup(self, x_mm, y_mm):
        """PCB coordinates; no snapping to nearby copper across gaps or cutouts."""
        if x_mm is None or y_mm is None or not np.isfinite([x_mm, y_mm]).all():
            return None
        if self.region is not None:
            from shapely import intersects_xy
            if not intersects_xy(self.region, x_mm, y_mm):
                return None
        ix, iy = np.floor((np.array([x_mm, y_mm])-self.origin)/self.step).astype(int)
        if ix < 0 or iy < 0 or iy >= self.image.shape[0] or ix >= self.image.shape[1]:
            return None
        value = self.image[iy, ix]
        return float(value) if np.isfinite(value) else None
