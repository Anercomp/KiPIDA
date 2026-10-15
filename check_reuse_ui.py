"""Exercise repeated production runs, cache invalidation and thermal-only UI."""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import wx
from shapely.geometry import box
from compact_storage import Coordinates
from mesh import Mesh
from models import PowerRail, UnifiedSource, UnifiedLoad, ComponentRef
from ui.main_dialog import KiPIDA_MainDialog
from simulation_timing import TimingHistory
from kipy.board_types import BoardRectangle
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BL_Edge_Cuts

app = wx.App(False)
streams = sys.stdout, sys.stderr
dialog = KiPIDA_MainDialog(None, None)
timing_directory = tempfile.TemporaryDirectory()
dialog._timing_history = TimingHistory(Path(timing_directory.name)/'timings.json')
sys.stdout, sys.stderr = streams
app.ProcessPendingEvents()
shape = BoardRectangle(); shape.layer = BL_Edge_Cuts
shape.top_left = Vector2.from_xy(0, 0); shape.bottom_right = Vector2.from_xy(2000000, 1000000)
revision = ['one']
board = NS(get_shapes=lambda: [shape])
counts = dict(geometry=0, mesh=0)
stackup = {'copper': {3: {'thickness_mm': .035, 'name': 'F.Cu'}},
           'substrate': [{'thickness_mm': 1.6}], 'layer_order': [3]}

class Extractor:
    def __init__(self, *a, **kw): pass
    def get_board_stackup(self): return stackup
    def get_net_geometry(self, net):
        counts['geometry'] += 1
        return {3: box(0, 0, 2, 1)}

class Mesher:
    def __init__(self, *a, **kw): pass
    def generate_mesh(self, net, *a, grid_size_mm=.1, **kw):
        counts['mesh'] += 1
        mesh = Mesh(); mesh.net_name = net
        mesh.grid_step = grid_size_mm; mesh.copper_layers = {3}
        mesh.node_coords = Coordinates()
        if grid_size_mm >= .1:
            mesh.nodes = range(2)
            mesh.node_coords.append([[.5, .5, 3], [1.5, .5, 3]])
            mesh.add_edge_direct(0, 1, 10)
        else:
            mesh.nodes = range(3)
            mesh.node_coords.append([[.5, .5, 3], [1, .5, 3], [1.5, .5, 3]])
            mesh.add_edge_direct(0, 1, 20); mesh.add_edge_direct(1, 2, 20)
        self.progress_callback(1)
        return mesh

rail = PowerRail('VIN', 12, sources=[UnifiedSource(ComponentRef('J1'), ['1'])],
                 loads=[UnifiedLoad(ComponentRef('J2'), 1, ['1'])])
dialog.power_tree.rails = [rail]
dialog._get_mesh_nodes = lambda mesh, ref, pads, debug: [0 if ref == 'J1' else len(mesh.nodes)-1]
dialog.chk_thermal.SetValue(False)
dialog.chk_convergence.SetValue(True)
dialog.choice_backend.SetSelection(1)
try:
    with patch('ui.main_dialog.BoardSnapshot', side_effect=lambda b: NS(cache_token=revision[0], get_shapes=board.get_shapes)), \
         patch('ui.main_dialog.GeometryExtractor', Extractor), patch('ui.main_dialog.Mesher', Mesher), patch('wx.MessageBox') as messages:
        dialog.on_run(None)
        assert not messages.called, messages.call_args
        assert counts == dict(geometry=1, mesh=2), counts
        first = dialog.system_results['VIN']['results'][2]
        dialog.on_run(None)
        assert counts == dict(geometry=1, mesh=2), counts
        assert dialog.system_results['VIN']['results'][2] == first
        assert 'Reusing validated electrical results' in dialog.log_ctrl.GetValue()
        rail.loads[0].total_current = 2
        dialog.on_run(None)
        assert not messages.called, messages.call_args
        assert counts == dict(geometry=1, mesh=2), counts
        assert dialog.system_results['VIN']['diagnostics']['prepared_cache_hit']
        assert abs(dialog.system_results['VIN']['results'][2]-11.8) < 1e-9
        revision[0] = 'two'
        dialog.on_run(None)
        assert counts == dict(geometry=2, mesh=4), counts
        # Thermal-only must reject changed electrical inputs before extraction/meshing.
        rail.loads[0].total_current = 3
        dialog.on_thermal_only(None)
        assert messages.called
        assert counts == dict(geometry=2, mesh=4), counts
        messages.reset_mock()
        dialog.chk_convergence.SetValue(False)
        rail.loads[0].total_current = 1
        dialog.on_run(None)
        assert not messages.called, messages.call_args
        assert dialog.thermal_result is not None
        temperature = dialog.thermal_result['temperature_c'].copy()
        counts_before = counts.copy()
        dialog.txt_ambient.ChangeValue('35')
        dialog.on_thermal_only(None)
        assert not messages.called, messages.call_args
        assert counts == counts_before
        import numpy as np
        np.testing.assert_allclose(dialog.thermal_result['temperature_c'], temperature+10)
        assert dialog.thermal_result['diagnostics']['ambient_shift_only']
        dialog.txt_cooling.ChangeValue('20')
        dialog.on_thermal_only(None)
        assert not messages.called, messages.call_args
        assert counts == counts_before
        assert not dialog.thermal_result['diagnostics']['thermal_matrix_reused']
        assert dialog.choice_memory.GetSelection() == 0 and not dialog.txt_memory_gb.IsEnabled()
        assert 'New RAM allowance' in dialog.memory_label.GetLabel()
        print('Production reuse checks passed: mesh/geometry cache, changed-load solver reuse, '
              'board invalidation, convergence reuse, thermal-only, ambient shift, cooling rebuild, Auto RAM.')
finally:
    dialog.Destroy()
    app.ProcessPendingEvents()
    sys.stdout, sys.stderr = streams
    timing_directory.cleanup()
