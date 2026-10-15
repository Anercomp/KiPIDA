"""Run production simulation control flow with deterministic electrical fixtures."""
import sys
from types import SimpleNamespace as NS
from unittest.mock import patch
import wx
from shapely.geometry import box
from ui.main_dialog import KiPIDA_MainDialog
from models import PowerRail, UnifiedSource, UnifiedLoad, ComponentRef
from mesh import Mesh
from compact_storage import Coordinates

app = wx.App(False)
streams = sys.stdout, sys.stderr
dialog = None
try:
    dialog = KiPIDA_MainDialog(None, None)
    dialog._timing_history = NS(estimate=lambda f: None, record=lambda *a, **kw: None)
    sys.stdout, sys.stderr = streams
    app.ProcessPendingEvents()
    rail = PowerRail('VIN', 12, sources=[UnifiedSource(ComponentRef('J1'), ['1'])],
                     loads=[UnifiedLoad(ComponentRef('J2'), 1, ['1'])])
    dialog.power_tree.rails = [rail]
    dialog.chk_thermal.SetValue(False)
    dialog.chk_convergence.SetValue(True)
    dialog._get_mesh_nodes = lambda mesh, ref, pads, debug=False: [0 if ref == 'J1' else 1]
    stackup = {'copper': {3: {'thickness_mm': .035}}}
    class FixtureExtractor:
        def __init__(self, *args, **kwargs): pass
        def get_board_stackup(self): return stackup
        def get_net_geometry(self, net): return {3: box(0, 0, 1, 1)}
    class FixtureMesher:
        def __init__(self, *args, **kwargs): pass
        def generate_mesh(self, net, *args, **kwargs):
            mesh = Mesh(); mesh.nodes = range(2); mesh.net_name = net
            mesh.node_coords = Coordinates(); mesh.node_coords.append([[0, 0, 3], [1, 0, 3]])
            mesh.add_edge_direct(0, 1, 10)
            for fraction in [0, .3, .8, 1]: self.progress_callback(fraction)
            return mesh
    values = []
    original = dialog._set_progress
    def record(value=None, *args, **kwargs):
        original(value, *args, **kwargs)
        values.append(dialog.progress_bar.GetValue())
    dialog._set_progress = record
    with patch('ui.main_dialog.GeometryExtractor', FixtureExtractor), patch('ui.main_dialog.Mesher', FixtureMesher):
        dialog.on_run(None)
        assert values[0] == 0 and values[-1] == 100 and values == sorted(values)
        assert dialog.progress_percent.GetLabel() == '100%'
        assert dialog.progress_stage.GetLabel() == 'Completed'
        assert len(dialog.system_results) == 1
        from models import VoltageRegulator
        downstream = PowerRail('5V', 5, loads=[UnifiedLoad(ComponentRef('L1'), 1, ['1'])])
        rail.loads = []
        rail.child_regulators = [VoltageRegulator('U1', 'VIN', 'U1', ['IN'], '5V', 'U1', ['OUT'], 'SWITCHING', .8)]
        dialog.power_tree.rails = [rail, downstream]
        dialog._get_mesh_nodes = lambda mesh, ref, pads, debug=False: [0 if ref == 'J1' or pads == ['OUT'] else 1]
        dialog.on_run(None)
        assert dialog.system_results['VIN']['diagnostics']['linear_rhs_solves'] == 3
        assert dialog.system_results['VIN']['diagnostics']['regulator_port_steps'] == 0
        assert dialog.system_results['VIN']['diagnostics']['regulator_power_error_w'] < 1e-8
        assert dialog.progress_percent.GetLabel() == '100%'
        print('Production coarse/refined regulator-port model uses three reusable RHS solves per upstream mesh.')
        values.clear()
        dialog.power_tree.rails = []
        with patch('wx.MessageBox'):
            dialog.on_run(None)
        assert values[0] == 0 and values[-1] < 100
        assert dialog.progress_stage.GetLabel() == 'Failed — see log'
        assert dialog.btn_run.IsEnabled()
        dialog.power_tree.rails = [PowerRail('UNPOWERED', 12, loads=[UnifiedLoad(ComponentRef('J2'), 1, ['1'])])]
        with patch('wx.MessageBox'), patch('ui.main_dialog.GeometryExtractor') as unused_extractor:
            dialog.on_run(None)
        unused_extractor.assert_not_called()
        assert 'No voltage source configured for network: UNPOWERED' in dialog.log_ctrl.GetValue()
        assert dialog.progress_stage.GetLabel() == 'Failed — see log'
    dialog._set_progress(40, 'Solving', reset=True)
    dialog._set_progress(20, 'Solving')
    assert dialog.progress_bar.GetValue() == 40
    dialog._cancel_requested = True
    try:
        dialog._set_progress(50)
        raise AssertionError('Cancellation must stop progress')
    except InterruptedError:
        pass
    dialog._cancel_requested = False
    print('Real wx progress: coarse/refined simulation, 100% success, reset, failure, monotonic value and cancellation passed.')
finally:
    sys.stdout, sys.stderr = streams
    app.ProcessPendingEvents()
    if dialog:
        dialog.Destroy()
    app.ProcessPendingEvents()
