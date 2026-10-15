"""Interactive thermal view with an exact cell readout in PCB coordinates."""
import wx
import weakref
from matplotlib.backends.backend_wxagg import FigureCanvasWxAgg, NavigationToolbar2WxAgg
from matplotlib.collections import LineCollection
from plotter import Plotter
from temperature_map import TemperatureMap
from pcb_overlay import OverlayCache, polygon_outline_segments


class ThermalToolbar(NavigationToolbar2WxAgg):
    # Constrained layout already arranges this single plot and its colorbar.
    # The generic subplot editor opens an unmanaged window from a modal dialog.
    toolitems = tuple(item for item in NavigationToolbar2WxAgg.toolitems
                      if item[3] != 'configure_subplots')


class TemperaturePanel(wx.Panel):
    def __init__(self, parent, result, overlay_loader=None, overlay_cache=None):
        super().__init__(parent)
        self.temperatures = TemperatureMap(result)
        peak = self.temperatures
        x, y = peak.maximum_xy
        self.maximum_label = wx.StaticText(self, label=f'Highest simulated temperature: {peak.maximum_c:.2f} °C at X={x:.2f}, Y={y:.2f} mm')
        self.pointer_label = wx.StaticText(self, label='Pointer temperature: move the mouse over the board')
        self.figure, self.axes = Plotter().temperature_figure(result, peak)
        self.overlay_loader = overlay_loader
        self.overlay_geometry = result.get('overlay_copper')
        self.overlay_cache = overlay_cache if overlay_cache is not None else OverlayCache()
        self.overlay_cache_key = result.get('overlay_cache_token')
        self._overlay_prepared = None
        self._overlay_pending = False
        self._cancel_overlay = None
        self._destroyed = False
        self._redraw_timer = None
        self._background = None
        self.overlay_artists = {}
        self.overlay_outline = []
        self.layer_ids = list(result.get('overlay_layer_names', {}))
        self.overlay_toggle = wx.CheckBox(self, label='PCB copper outlines (grey)')
        self.overlay_status = wx.StaticText(self, label='')
        self.overlay_layer = wx.Choice(self, choices=[result['overlay_layer_names'][lid] for lid in self.layer_ids])
        if self.layer_ids:
            self.overlay_layer.SetSelection(0)
        self.overlay_opacity = wx.Slider(self, value=35, minValue=0, maxValue=100, size=(150, -1))
        self.opacity_label = wx.StaticText(self, label='Opacity: 35%')
        controls = wx.BoxSizer(wx.HORIZONTAL)
        controls.Add(self.overlay_toggle, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        controls.Add(self.overlay_layer, 0, wx.RIGHT, 10)
        controls.Add(self.opacity_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        controls.Add(self.overlay_opacity, 0)
        self.overlay_toggle.Enable(bool(self.layer_ids) and (overlay_loader is not None or self.overlay_geometry is not None))
        self.overlay_layer.Disable()
        self.overlay_opacity.Disable()
        self.overlay_toggle.Bind(wx.EVT_CHECKBOX, self.on_overlay)
        self.overlay_layer.Bind(wx.EVT_CHOICE, self.on_overlay)
        self.overlay_opacity.Bind(wx.EVT_SLIDER, self.on_overlay)
        self.canvas = FigureCanvasWxAgg(self, wx.ID_ANY, self.figure)
        self.toolbar = ThermalToolbar(self.canvas)
        self.toolbar.SetToolTip('Plot navigation: reset, back/forward, pan, zoom and save image')
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.maximum_label, 0, wx.ALL, 5)
        sizer.Add(self.pointer_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        sizer.Add(controls, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        sizer.Add(self.overlay_status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        sizer.Add(self.canvas, 1, wx.EXPAND)
        sizer.Add(self.toolbar, 0, wx.EXPAND)
        self.SetSizer(sizer)
        self._connections = [self.canvas.mpl_connect('motion_notify_event', self.on_motion),
                             self.canvas.mpl_connect('figure_leave_event', self.on_leave),
                             self.canvas.mpl_connect('axes_leave_event', self.on_leave),
                             self.canvas.mpl_connect('draw_event', self._on_draw)]
        self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)

    def on_overlay(self, event):
        enabled = self.overlay_toggle.GetValue()
        opacity = self.overlay_opacity.GetValue()/100
        self.opacity_label.SetLabel(f'Opacity: {self.overlay_opacity.GetValue()}%')
        self.overlay_layer.Enable(enabled)
        self.overlay_opacity.Enable(enabled)
        if enabled and self._overlay_prepared is None and not self._overlay_pending:
            self._overlay_pending = True
            self.overlay_status.SetLabel('Preparing PCB copper outlines…')
            # Only immutable geometry/IPC work runs on the worker. No wx objects
            # are touched there; weak references and cancellation protect closed views.
            panel_ref = weakref.ref(self)
            def ready(prepared, error):
                panel = panel_ref()
                if panel is not None and not panel._destroyed:
                    panel._overlay_ready(prepared, error)
            loader = self.overlay_loader
            if self.overlay_geometry is not None:
                geometry = self.overlay_geometry
                loader = lambda: geometry
            self._cancel_overlay = self.overlay_cache.request(
                loader, ready, wx.CallAfter, self.overlay_cache_key)
        if self._overlay_prepared is None:
            return
        layer = self.layer_ids[self.overlay_layer.GetSelection()] if self.layer_ids else None
        if enabled and layer not in self.overlay_artists:
            collection = LineCollection(self._overlay_prepared.segments.get(layer, []),
                                        colors='#777777', linewidths=.65,
                                        zorder=3, animated=True)
            self.axes.add_collection(collection, autolim=False)
            self.overlay_artists[layer] = [collection]
            if not self.overlay_outline:
                outline = LineCollection(polygon_outline_segments(self.temperatures.region),
                                         colors='#555555', linewidths=1,
                                         zorder=4, animated=True)
                self.axes.add_collection(outline, autolim=False)
                self.overlay_outline.append(outline)
        for lid, artists in self.overlay_artists.items():
            for artist in artists:
                artist.set_visible(enabled and lid == layer)
                artist.set_alpha(opacity)
        for outline in self.overlay_outline:
            outline.set_visible(enabled)
            outline.set_alpha(opacity)
        # A slider produces many events. Coalesce them and reuse the rendered
        # temperature/colorbar background instead of redrawing the heat map.
        if self._redraw_timer is not None:
            self._redraw_timer.Stop()
        self._redraw_timer = wx.CallLater(40, self._draw_overlay)

    def _overlay_ready(self, prepared, error):
        self._overlay_pending = False
        self._cancel_overlay = None
        if error is not None:
            self.overlay_toggle.SetValue(False)
            self.overlay_layer.Disable()
            self.overlay_opacity.Disable()
            self.overlay_status.SetLabel(f'PCB overlay unavailable: {error}')
            self.Layout()
            return
        self._overlay_prepared = prepared
        self.overlay_geometry = prepared.geometry
        self.overlay_status.SetLabel('')
        self.on_overlay(None)

    def _on_draw(self, event):
        if self._destroyed or event.canvas.is_saving():
            return
        self._background = self.canvas.copy_from_bbox(self.figure.bbox)
        # WxAgg emits draw_event before creating its wx.Bitmap on the first
        # draw. Blit only after that draw has returned to the event loop.
        panel_ref = weakref.ref(self)
        def draw_after_bitmap():
            panel = panel_ref()
            if panel is not None and not panel._destroyed:
                panel._draw_overlay()
        wx.CallAfter(draw_after_bitmap)

    def _draw_overlay(self):
        self._redraw_timer = None
        if self._destroyed:
            return
        if self._background is None:
            self.canvas.draw_idle()
            return
        self.canvas.restore_region(self._background)
        for artists in self.overlay_artists.values():
            for artist in artists:
                if artist.get_visible():
                    self.axes.draw_artist(artist)
        for outline in self.overlay_outline:
            if outline.get_visible():
                self.axes.draw_artist(outline)
        self.canvas.blit(self.figure.bbox)

    def on_motion(self, event):
        if event.inaxes is not self.axes or event.xdata is None or event.ydata is None:
            self.on_leave(event)
            return
        x, y = event.xdata, -event.ydata  # Plot Y is flipped; report PCB Y.
        temperature = self.temperatures.lookup(x, y)
        value = f'{temperature:.2f} °C (thermal cell)' if temperature is not None else 'outside modeled board'
        self.pointer_label.SetLabel(f'X={x:.2f}, Y={y:.2f} mm | Temperature: {value}')

    def on_leave(self, event):
        self.pointer_label.SetLabel('Pointer temperature: — (move over the board)')

    def on_destroy(self, event):
        if event.GetEventObject() is self:
            self._destroyed = True
            if self._cancel_overlay is not None:
                self._cancel_overlay()
            if self._redraw_timer is not None:
                self._redraw_timer.Stop()
            self._background = None
            for connection in self._connections:
                self.canvas.mpl_disconnect(connection)
            self.figure.clear()
        event.Skip()
