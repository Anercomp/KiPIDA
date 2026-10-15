"""Local smoke check: real wx controls and installed CUDA, no board writes."""
import sys
import wx
import kipy
from ui.main_dialog import KiPIDA_MainDialog
from compact_storage import Coordinates, Voltages
from mesh import Mesh
from plotter import Plotter

app = wx.App(False)
board = kipy.KiCad(timeout_ms=3000).get_board()
streams = sys.stdout, sys.stderr
dialog = None
try:
    dialog = KiPIDA_MainDialog(None, board)
    sys.stdout, sys.stderr = streams
    assert dialog.choice_backend.GetItems() == ['Auto', 'CPU', 'GPU (CUDA)']
    assert dialog.choice_backend.GetSelection() == 0
    assert float(dialog.txt_memory_gb.GetValue()) > 0
    assert dialog.chk_thermal.GetValue()
    assert dialog.results_notebook.GetPageText(0) == 'Temperature (board average)'
    mesh = Mesh()
    mesh.node_coords = Coordinates()
    mesh.node_coords.append([[0, 0, 3], [1, 0, 3], [0, 1, 3], [1, 1, 3]])
    mesh.results = Voltages([12, 11.9, 11.8, 11.7])
    bitmap = Plotter().plot_layer_2d(mesh, 3)
    assert bitmap and bitmap.IsOk()
    other = Mesh()
    other.node_coords = Coordinates()
    other.node_coords.append([[2, 0, 3], [3, 0, 3], [2, 1, 3], [3, 1, 3]])
    other.results = Voltages([11.7, 11.6, 11.5, 11.4])
    for item in (mesh, other):
        item.nodes = range(4)
    dialog.system_results = {
        name: dict(mesh=m, results=m.results, stats=(float(m.results.array.min()), float(m.results.array.max()), .3),
                   diagnostics=dict(copper_loss_w=0))
        for name, m in [('VIN', mesh), ('VOUT', other)]}
    dialog.component_results = []
    dialog._update_results_ui()
    assert [dialog.results_notebook.GetPageText(i) for i in range(dialog.results_notebook.GetPageCount())] == ['Temperature (board average)', 'All Rails', 'VIN', 'VOUT']
    assert dialog.results_notebook.GetPage(1).GetPageCount() == 1
    dialog.chk_thermal.SetValue(False)
    dialog._update_results_ui()
    page = dialog.results_notebook.GetPage(0)
    assert any(isinstance(c, wx.StaticText) and 'disabled' in c.GetLabel() for c in page.GetChildren())
    button = next(c for c in page.GetChildren() if isinstance(c, wx.Button))
    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
    button.GetEventHandler().ProcessEvent(event)
    assert dialog.chk_thermal.GetValue() and dialog.notebook.GetSelection() == 0
    dialog.thermal_error = 'Thermal grid refinement failed (test)'
    dialog._update_results_ui()
    assert any(isinstance(c, wx.StaticText) and 'Thermal grid refinement failed' in c.GetLabel()
               for c in dialog.results_notebook.GetPage(0).GetChildren())
    print('Temperature tab visibility before a run, while disabled and after failure passed.')
    print('Real wx: combined-layer result tabs and voltage plots passed.')
    print('Real wx: solver controls, RAM budget, compact voltage plot passed.')
finally:
    sys.stdout, sys.stderr = streams
    app.ProcessPendingEvents()
    if dialog:
        dialog.Destroy()
    app.ProcessPendingEvents()

