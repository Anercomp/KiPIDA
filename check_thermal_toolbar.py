"""Verify the embedded toolbar in a modal wx dialog, without KiCad API access."""
import wx
import numpy as np
from shapely.geometry import box
from ui.temperature_panel import TemperaturePanel
from thermal_solver import ThermalSettings

app = wx.App(False)
dialog = wx.Dialog(None, title='KiPIDA toolbar smoke check', size=(900, 650))
result = dict(xy=np.array([[.5, .5], [1.5, .5], [.5, 1.5], [1.5, 1.5]]),
              temperature_c=np.array([25, 30, 35, 40]), settings=ThermalSettings(), region=box(0, 0, 2, 2))
panel = TemperaturePanel(dialog, result)
sizer = wx.BoxSizer(wx.VERTICAL); sizer.Add(panel, 1, wx.EXPAND); dialog.SetSizer(sizer)
errors = []
def check():
    try:
        toolbar = panel.toolbar
        assert 'Subplots' not in toolbar.wx_ids
        assert {'Home', 'Back', 'Forward', 'Pan', 'Zoom', 'Save'}.issubset(toolbar.wx_ids)
        panel.canvas.draw()
        before = panel.axes.get_xlim(), panel.axes.get_ylim()
        toolbar.push_current()
        panel.axes.set_xlim(.25, 1); panel.axes.set_ylim(-1, -.25)
        toolbar.push_current()
        toolbar.home()
        assert (panel.axes.get_xlim(), panel.axes.get_ylim()) == before
        toolbar.zoom(); assert 'zoom' in str(toolbar.mode).lower(); toolbar.zoom()
        toolbar.pan(); assert 'pan' in str(toolbar.mode).lower(); toolbar.pan()
    except Exception as exc:
        errors.append(exc)
    finally:
        dialog.EndModal(wx.ID_OK)
wx.CallAfter(check)
dialog.ShowModal()
dialog.Destroy()
app.ProcessPendingEvents()
if errors:
    raise errors[0]
print('Modal wx toolbar: subplot tool removed; reset, zoom and pan work; dialog closes normally.')
