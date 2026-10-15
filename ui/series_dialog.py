"""Explicit pad-to-pad passive link editor. Resistance is entered in milliohms."""
import math
import wx
from copper_geometry import value
from models import SeriesComponent
from ui.sorting import natural_key


def component_terminals(board):
    components = {}
    for fp in value(board, 'footprints', None) or value(board, 'get_footprints', []):
        ref = value(fp, 'reference_field', None)
        text = value(ref, 'text', '')
        ref = text if isinstance(text, str) else value(text, 'value', '')
        ref = ref or value(fp, 'reference', '')
        if not isinstance(ref, str) or not ref:
            continue
        pads = value(fp, 'pads', None)
        if pads is None:
            pads = value(value(fp, 'definition'), 'pads', [])
        terminals = sorted(set((str(value(p, 'number', '')), value(value(p, 'net'), 'name', '')) for p in pads), key=lambda item: natural_key(item[0]))
        terminals = [(p, n) for p, n in terminals if p and n]
        if len({n for _, n in terminals}) > 1:
            components[ref] = terminals
    return components


class SeriesDialog(wx.Dialog):
    def __init__(self, parent, board, active_net, link=None):
        super().__init__(parent, title="Series component", size=(520, 360))
        self.components = component_terminals(board)
        self.result = None
        self.active_net = active_net
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.ref = wx.Choice(self, choices=sorted(self.components, key=natural_key))
        self.a, self.b = wx.Choice(self), wx.Choice(self)
        self.kind = wx.Choice(self, choices=['Fuse', 'Resistor', 'Connector', 'Switch'])
        self.kind.SetSelection(0)
        self.resistance = wx.TextCtrl(self, value='0')
        self.closed = wx.CheckBox(self, label="Connected (clear for open circuit)")
        self.closed.SetValue(True)
        for label, control in [('Component', self.ref), ('Terminal A', self.a), ('Terminal B', self.b),
                               ('Type', self.kind), ('Resistance (mΩ; 0 = ideal link)', self.resistance)]:
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
            row.Add(control, 1, wx.EXPAND)
            sizer.Add(row, 0, wx.EXPAND | wx.ALL, 5)
        sizer.Add(self.closed, 0, wx.ALL, 5)
        sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizer(sizer)
        self.ref.Bind(wx.EVT_CHOICE, self.choose_component)
        self.Bind(wx.EVT_BUTTON, self.accept, id=wx.ID_OK)
        if self.ref.GetCount():
            self.ref.SetSelection(0)
            if link:
                self.ref.SetStringSelection(link.ref_des)
            self.choose_component(None)
        if link:
            self.a.SetStringSelection(f'{link.pad_a} ({link.net_a})')
            self.b.SetStringSelection(f'{link.pad_b} ({link.net_b})')
            self.kind.SetStringSelection(link.kind)
            self.resistance.SetValue(str(link.resistance_ohm*1000))
            self.closed.SetValue(link.closed)

    def choose_component(self, event):
        terminals = self.components[self.ref.GetStringSelection()]
        choices = [f'{p} ({n})' for p, n in terminals]
        self.a.Set(choices)
        self.b.Set(choices)
        a = next((i for i, (_, n) in enumerate(terminals) if n == self.active_net), 0)
        b = next(i for i, (_, n) in enumerate(terminals) if n != terminals[a][1])
        self.a.SetSelection(a)
        self.b.SetSelection(b)

    def accept(self, event):
        try:
            ref = self.ref.GetStringSelection()
            terminals = self.components[ref]
            pa, na = terminals[self.a.GetSelection()]
            pb, nb = terminals[self.b.GetSelection()]
            resistance = float(self.resistance.GetValue())/1000
            if na == nb or not math.isfinite(resistance) or resistance < 0:
                raise ValueError('Select different nets and enter a finite, nonnegative resistance.')
            self.result = SeriesComponent(ref, na, pa, nb, pb, resistance, self.kind.GetStringSelection(), self.closed.GetValue())
        except (ValueError, KeyError, IndexError) as exc:
            wx.MessageBox(str(exc) or 'Select a component and both pads.', 'Invalid series component', wx.OK | wx.ICON_ERROR)
            return
        self.EndModal(wx.ID_OK)
