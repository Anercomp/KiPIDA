"""Run production electrical/thermal flow with analytical regulator fixtures."""
import sys
from types import SimpleNamespace as NS
from unittest.mock import patch
import wx
from shapely.geometry import box
from kipy.board_types import BoardRectangle
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BL_Edge_Cuts
from ui.main_dialog import KiPIDA_MainDialog
from tests.test_regulator_heat import fixture

app = wx.App(False)
streams = sys.stdout, sys.stderr
dialog = KiPIDA_MainDialog(None, None)
sys.stdout, sys.stderr = streams
app.ProcessPendingEvents()
rails, meshes, resolve, footprints = fixture()
outline = BoardRectangle(); outline.layer = BL_Edge_Cuts
outline.top_left = Vector2.from_xy(0, 0); outline.bottom_right = Vector2.from_xy(100000000, 50000000)
snapshot = NS(cache_token='regulator-thermal-fixture', get_shapes=lambda: [outline], footprints=footprints.footprints)
stackup = {'copper': {3: {'thickness_mm': .035, 'name': 'F.Cu'}},
           'substrate': [{'thickness_mm': 1.6}], 'layer_order': [3]}
class Extractor:
    def __init__(self, *a, **kw): pass
    def get_board_stackup(self): return stackup
    def get_net_geometry(self, net):
        return {3: box(45, 24.5, 50, 25.5) if net == 'VIN' else box(50, 24.5, 55, 25.5)}
class Mesher:
    def __init__(self, *a, **kw): pass
    def generate_mesh(self, net, *a, **kw):
        mesh = meshes[net]; mesh.copper_layers = {3}
        self.progress_callback(1)
        return mesh
dialog.power_tree.rails = rails
dialog._get_mesh_nodes = lambda mesh, ref, pads, debug=False: resolve(mesh, ref, pads)
dialog.chk_thermal.SetValue(True)
dialog.chk_convergence.SetValue(False)
dialog.txt_thermal_step.ChangeValue('5')
dialog.choice_backend.SetSelection(1)
dialog._timing_history = NS(estimate=lambda f: None, record=lambda *a: None)
try:
    with patch('ui.main_dialog.BoardSnapshot', return_value=snapshot), \
         patch('ui.main_dialog.GeometryExtractor', Extractor), patch('ui.main_dialog.Mesher', Mesher), \
         patch('wx.MessageBox') as messages:
        dialog.on_run(None)
        assert not messages.called, messages.call_args
        linear = dialog.thermal_result
        assert abs(linear['heat_sources_w']['regulator']-6.9) < 1e-9
        assert abs(linear['input_w']-7.1) < 1e-9
        assert abs(linear['rejected_w']-7.1) < 1e-8
        assert 'Total regulator loss: 6.9 W' in dialog.result_text.GetValue()
        assert 'regulators 6.9 W' in dialog.result_text.GetValue()
        rails[0].child_regulators[0].reg_type = 'SWITCHING'
        dialog.on_run(None)
        assert not messages.called, messages.call_args
        switching = dialog.thermal_result
        assert abs(switching['heat_sources_w']['regulator']-1.25) < 1e-7
        assert switching['temperature_c'].max() < linear['temperature_c'].max()
        assert abs(switching['balance_w']) < 1e-7
        before = switching['temperature_c'].copy()
        dialog.txt_ambient.ChangeValue('35')
        dialog.on_thermal_only(None)
        import numpy as np
        np.testing.assert_allclose(dialog.thermal_result['temperature_c'], before+10)
        assert abs(dialog.thermal_result['heat_sources_w']['regulator']-1.25) < 1e-7
        assert 'Total regulator loss: 1.25 W' in dialog.result_text.GetValue()
        print('Production regulator heat passed: LINEAR 6.9 W / total 7.1 W; SWITCHING 1.25 W; '
              'body-centre placement, thermal balance, summary and cached ambient shift.')
finally:
    dialog.Destroy()
    app.ProcessPendingEvents()
    sys.stdout, sys.stderr = streams
