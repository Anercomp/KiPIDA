"""Exercise production thermal UI on the live board with synthetic test heat."""
import sys
import time
import threading
from types import SimpleNamespace as NS
import wx
import kipy
from shapely.geometry import box
from ui.main_dialog import KiPIDA_MainDialog
from thermal_solver import board_region
from extractor import GeometryExtractor
from mesh import Mesh
from compact_storage import Coordinates, Voltages
from models import SeriesComponent

app = wx.App(False)
board = kipy.KiCad(timeout_ms=3000).get_board()
region = board_region(board)
stackup = GeometryExtractor(board).get_board_stackup()
point = region.representative_point()
x, y = point.x, point.y
streams = sys.stdout, sys.stderr
dialog = None
try:
    dialog = KiPIDA_MainDialog(None, board)
    sys.stdout, sys.stderr = streams
    app.ProcessPendingEvents()
    layer = stackup['layer_order'][0]
    results, geometry = {}, {}
    for net in ['VIN', 'VOUT']:
        mesh = Mesh(); mesh.nodes = range(2); mesh.net_name = net
        mesh.node_coords = Coordinates()
        mesh.node_coords.append([[x-.1, y, layer], [x+.1, y, layer]])
        mesh.add_edge_direct(0, 1, 100)
        volts = Voltages([12, 11.9])
        results[net] = dict(mesh=mesh, results=volts, stats=(11.9, 12, .1), diagnostics={'copper_loss_w': 1})
        geometry[net] = {layer: box(x-.3, y-.3, x+.3, y+.3)}
    dialog.system_results = results
    dialog.power_tree.rails = [NS(series_components=[SeriesComponent('F1', 'VIN', '1', 'VOUT', '2', .02)])]
    dialog.component_results = [dict(ref='F1', net_a='VIN', net_b='VOUT', closed=True, current_a=2, drop_v=.04, loss_w=.08)]
    dialog._get_mesh_nodes = lambda *args: [0]
    dialog.chk_convergence.SetValue(False)
    dialog._run_thermal(stackup, geometry, 'auto', 8)
    result = dialog.thermal_result
    assert abs(result['input_w']-2.08) < 1e-9
    assert result['balance_w'] < 1e-7
    dialog._update_results_ui()
    assert dialog.results_notebook.GetPageText(0) == 'Temperature (board average)'
    assert 'Thermal estimate' in dialog.result_text.GetValue()
    panel = dialog.results_notebook.GetPage(0)
    assert 'Subplots' not in panel.toolbar.wx_ids
    assert {'Home', 'Back', 'Forward', 'Pan', 'Zoom', 'Save'}.issubset(panel.toolbar.wx_ids)
    assert f"{result['temperature_c'].max():.2f} °C" in panel.maximum_label.GetLabel()
    from matplotlib.backend_bases import MouseEvent
    dialog.Layout()
    panel.canvas.draw()
    px, py = panel.axes.transData.transform((x, -y))
    event = MouseEvent('motion_notify_event', panel.canvas, px, py)
    panel.canvas.callbacks.process('motion_notify_event', event)
    expected = panel.temperatures.lookup(event.xdata, -event.ydata)
    assert expected is not None
    assert f'{expected:.2f} °C' in panel.pointer_label.GetLabel()
    assert f'Y={-event.ydata:.2f}' in panel.pointer_label.GetLabel()
    panel.on_motion(NS(inaxes=None, xdata=None, ydata=None))
    assert 'move over the board' in panel.pointer_label.GetLabel()
    limits = panel.axes.get_xlim(), panel.axes.get_ylim()
    panel.overlay_toggle.SetValue(True)
    started = time.perf_counter()
    panel.on_overlay(None)
    assert time.perf_counter()-started < .25, 'Overlay toggle must return before IPC/extraction completes'
    deadline = time.perf_counter()+30
    while panel._overlay_pending and time.perf_counter() < deadline:
        app.Yield()
        time.sleep(.01)
    assert not panel._overlay_pending, 'Overlay extraction did not finish'
    cached = panel.overlay_geometry
    assert cached and any(p.get_visible() for patches in panel.overlay_artists.values() for p in patches)
    assert (panel.axes.get_xlim(), panel.axes.get_ylim()) == limits
    from matplotlib.collections import LineCollection
    assert all(isinstance(p, LineCollection) for patches in panel.overlay_artists.values() for p in patches)
    assert all(len(p.get_facecolors()) == 0 for patches in panel.overlay_artists.values() for p in patches)
    for patches in panel.overlay_artists.values():
        for p in patches:
            rgba = p.get_colors()[0]
            assert rgba[0] == rgba[1] == rgba[2], 'Overlay must use neutral grey'
    panel.overlay_opacity.SetValue(65)
    panel.on_overlay(None)
    assert panel.opacity_label.GetLabel() == 'Opacity: 65%'
    assert all(p.get_alpha() == .65 for patches in panel.overlay_artists.values() for p in patches)
    if len(panel.layer_ids) > 1:
        panel.overlay_layer.SetSelection(1)
        panel.on_overlay(None)
        assert not any(p.get_visible() for p in panel.overlay_artists[panel.layer_ids[0]])
    panel.overlay_toggle.SetValue(False)
    panel.on_overlay(None)
    assert not any(p.get_visible() for patches in panel.overlay_artists.values() for p in patches)
    assert panel.overlay_geometry is cached
    panel.canvas.callbacks.process('motion_notify_event', event)
    assert f'{expected:.2f} °C' in panel.pointer_label.GetLabel()
    print('All-net PCB overlay, layer selection, opacity, caching, zoom preservation and hover checks passed.')
    # Rebuilding results destroys old canvas callbacks and installs a fresh view.
    dialog._update_results_ui()
    from unittest.mock import patch
    with patch('ui.temperature_panel.TemperaturePanel', side_effect=RuntimeError('forced canvas failure (test)')):
        dialog._update_results_ui()
        page = dialog.results_notebook.GetPage(0)
        assert dialog.results_notebook.GetPageText(0) == 'Temperature (board average)'
        assert any(isinstance(c, wx.StaticText) and 'Highest simulated' in c.GetLabel() for c in page.GetChildren())
        assert any(isinstance(c, wx.StaticBitmap) for c in page.GetChildren())
        assert dialog.system_results is results and dialog.thermal_result is result
    dialog._update_results_ui()
    print('Canvas failure preserves temperature tab, maximum, fallback map and electrical results.')
    print('Maximum label, canvas pointer transformation, PCB Y coordinate and result refresh passed.')
    print(f"Live-board outline: {region.area:.2f} mm²; thermal cells: {len(result['xy'])}; test heat: {result['input_w']:.2f} W; balance error: {result['balance_w']:.3g} W.")
    print('Production thermal pipeline, series heat mapping, summary and wx temperature plot passed.')
    # A deliberately blocked extraction must not block wx events. Its prepared
    # segments must outlive a view, but callbacks must not touch destroyed views.
    from ui.temperature_panel import TemperaturePanel
    from pcb_overlay import OverlayCache
    shared_cache = OverlayCache()
    entered, release = threading.Event(), threading.Event()
    extraction_calls = []
    def slow_loader():
        extraction_calls.append(threading.get_ident())
        entered.set()
        release.wait(5)
        return {layer: box(x-.3, y-.3, x+.3, y+.3)}
    frame = wx.Frame(None)
    test_result = dict(result, overlay_cache_token='synthetic board')
    first = TemperaturePanel(frame, test_result, slow_loader, shared_cache)
    first.overlay_toggle.SetValue(True)
    first.on_overlay(None)
    assert entered.wait(2)
    timer_fired = []
    wx.CallAfter(lambda: timer_fired.append(True))
    deadline = time.perf_counter()+2
    while not timer_fired and time.perf_counter() < deadline:
        app.Yield()
        time.sleep(.01)
    assert timer_fired and first._overlay_pending
    first.Destroy()
    app.ProcessPendingEvents()
    release.set()
    second = TemperaturePanel(frame, test_result, slow_loader, shared_cache)
    second.overlay_toggle.SetValue(True)
    second.on_overlay(None)
    deadline = time.perf_counter()+5
    while second._overlay_pending and time.perf_counter() < deadline:
        app.Yield()
        time.sleep(.01)
    assert second._overlay_prepared is not None
    assert len(extraction_calls) == 1
    assert extraction_calls[0] != threading.get_ident()
    prepared = second._overlay_prepared
    second.Destroy()
    app.ProcessPendingEvents()
    third = TemperaturePanel(frame, test_result, slow_loader, shared_cache)
    third.overlay_toggle.SetValue(True)
    third.on_overlay(None)
    app.Yield()
    assert third._overlay_prepared is prepared
    assert len(extraction_calls) == 1
    third.Destroy()
    frame.Destroy()
    app.ProcessPendingEvents()
    print('Blocked overlay load stays responsive; closed views are safe; reconstructed views reuse prepared paths.')
finally:
    sys.stdout, sys.stderr = streams
    app.ProcessPendingEvents()
    if dialog:
        dialog.Destroy()
    app.ProcessPendingEvents()
