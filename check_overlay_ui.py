"""Read-only real-wx checks for overlay lifetime, responsiveness and exported plots."""
import io
import threading
import time
from types import SimpleNamespace as NS
import numpy as np
import wx
from shapely.geometry import box
from pcb_overlay import OverlayCache
from ui.temperature_panel import TemperaturePanel

app = wx.App(False)
frame = wx.Frame(None)
result = dict(xy=np.array([[0., 0.], [1., 0.], [0., 1.], [1., 1.]]),
              temperature_c=np.array([25., 26., 27., 28.]), settings=NS(spacing_mm=1.),
              region=box(-.5, -.5, 1.5, 1.5), overlay_layer_names={0: 'F.Cu'},
              overlay_cache_token='board')
cache = OverlayCache()
entered, release = threading.Event(), threading.Event()
calls = []
def loader():
    calls.append(threading.get_ident())
    entered.set()
    release.wait(5)
    return {0: box(.2, .2, .8, .8)}

try:
    panel = TemperaturePanel(frame, result, loader, cache)
    panel.canvas.draw()
    panel.overlay_toggle.SetValue(True)
    started = time.perf_counter()
    panel.on_overlay(None)
    assert time.perf_counter()-started < .25, 'Toggle blocked on extraction'
    assert entered.wait(2), 'Worker did not start'
    ticks = []
    wx.CallAfter(lambda: ticks.append(True))
    deadline = time.perf_counter()+2
    while not ticks and time.perf_counter() < deadline:
        app.Yield()
        time.sleep(.01)
    assert ticks and panel._overlay_pending, 'wx events blocked during extraction'
    panel.Destroy()
    app.ProcessPendingEvents()
    release.set()
    replacement = TemperaturePanel(frame, result, loader, cache)
    replacement.overlay_toggle.SetValue(True)
    replacement.on_overlay(None)
    deadline = time.perf_counter()+5
    while replacement._overlay_pending and time.perf_counter() < deadline:
        app.Yield()
        time.sleep(.01)
    assert replacement._overlay_prepared is not None, 'Prepared overlay missing'
    assert len(calls) == 1, 'Reconstructed view repeated extraction'
    assert calls[0] != threading.get_ident(), 'Extraction ran on UI thread'
    replacement.canvas.draw()
    app.Yield()
    enabled_png = io.BytesIO()
    replacement.figure.savefig(enabled_png, format='png')
    replacement.overlay_toggle.SetValue(False)
    replacement.on_overlay(None)
    disabled_png = io.BytesIO()
    replacement.figure.savefig(disabled_png, format='png')
    assert enabled_png.getvalue() != disabled_png.getvalue(), 'Saved figure omitted visible overlay'
    print('Real wx: nonblocking load, events, destroyed callbacks, shared cache and saved overlay passed.')
finally:
    release.set()
    frame.Destroy()
    app.ProcessPendingEvents()
