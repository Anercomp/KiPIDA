"""Exercise removal and undo through a real wx ListCtrl, without editing a PCB."""
import wx
from models import (PowerRail, ComponentRef, UnifiedSource, UnifiedLoad,
                    VoltageRegulator, SeriesComponent)
from ui.power_tree_panel import PowerTreePanel

app = wx.App(False)
frame = wx.Frame(None)
panel = PowerTreePanel(frame, None)
try:
    source = UnifiedSource(ComponentRef('J1'), ['1'])
    load1 = UnifiedLoad(ComponentRef('J2'), 1, ['1'])
    load2 = UnifiedLoad(ComponentRef('J2'), 1, ['1'])  # Equal, distinct objects.
    regulator = VoltageRegulator('U1', 'A', 'U1', ['IN'], 'C', 'U1', ['OUT'])
    first = SeriesComponent('F1', 'A', '1', 'B', '2')
    second = SeriesComponent('F2', 'B', '1', 'D', '2')
    a = PowerRail('A', 12, sources=[source], loads=[load1, load2],
                  child_regulators=[regulator], series_components=[first])
    b = PowerRail('B', series_components=[second])
    c, d = PowerRail('C', 5), PowerRail('D')
    panel.rails = [a, b, c, d]
    for rail in panel.rails:
        panel.rail_list.Append(rail.net_name)
    panel.rail_list.SetSelection(0)
    panel.on_rail_select(None)
    assert panel.comp_list.HasFlag(wx.LC_SINGLE_SEL)
    assert not panel.btn_del_comp.IsEnabled()

    def select(component):
        index = next(i for i, (_, _, entry) in panel.component_rows.items() if entry is component)
        panel.comp_list.Select(index)
        panel.comp_list.Focus(index)
        app.ProcessPendingEvents()
        panel.on_component_selection(None)
        assert panel.comp_list.GetSelectedItemCount() == 1
        assert panel.btn_del_comp.IsEnabled()
        return index

    # Selecting a different row must deselect the source, including Ctrl-selection.
    select(source)
    index = select(load2)
    assert panel.comp_list.GetFirstSelected() == index
    assert 'LOAD J2' in panel.removal_target.GetLabel()
    panel.on_del_component(None)
    assert len(a.loads) == 1 and a.loads[0] is load1
    assert a.sources[0] is source
    assert panel.comp_list.GetFirstSelected() == -1
    assert not panel.btn_del_comp.IsEnabled()
    panel.on_del_component(None)  # Repeated click with no selection is a no-op.
    assert len(a.loads) == 1 and len(panel.removal_history) == 1
    panel.on_undo_remove(None)
    assert len(a.loads) == 2 and a.loads[1] is load2

    # Incoming regulator row removes the actual upstream object, and undo restores it.
    panel.rail_list.SetSelection(2)
    panel.on_rail_select(None)
    index = select(regulator)
    assert panel.comp_list.GetItemText(index).startswith('REG FROM')
    panel.on_del_component(None)
    assert not a.child_regulators and not c.sources
    panel.on_undo_remove(None)
    assert a.child_regulators[0] is regulator

    # Removing a bridge hides the detached chain; it must not delete other links.
    panel.rail_list.SetSelection(0)
    panel.on_rail_select(None)
    select(first)
    panel.on_del_component(None)
    assert not a.series_components and b.series_components[0] is second
    assert not panel.series_rows
    panel.on_undo_remove(None)
    assert a.series_components[0] is first and len(panel.series_rows) == 2

    # Several deletions undo in reverse order, retaining exact source/load identities.
    select(source)
    panel.on_del_component(None)
    select(load1)
    panel.on_del_component(None)
    panel.on_undo_remove(None)
    panel.on_undo_remove(None)
    assert a.sources[0] is source and a.loads[0] is load1
    assert not panel.removal_history and not panel.btn_undo_remove.IsEnabled()
    print('Real wx removal checks passed: single selection, exact identity, incoming regulator, '
          'detached chain retained, repeated-click protection, multi-step undo.')
finally:
    frame.Destroy()
    app.ProcessPendingEvents()
