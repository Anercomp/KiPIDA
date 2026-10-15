"""Production wx control-flow checks with a synthetic physical-RAM ledger.

The board and numerics are tiny real fixtures. A separate ledger models GiB-scale
native allocations so these checks exercise memory admission without allocating
large buffers or touching the user's live board/configuration.
"""
import sys
from types import SimpleNamespace as NS
from unittest.mock import patch

import wx
from shapely.geometry import box

from compact_storage import Coordinates
from mesh import Mesh
from models import PowerRail, UnifiedLoad, UnifiedSource, ComponentRef
from resource_budget import GIB, check_budget
from ui.main_dialog import KiPIDA_MainDialog


app = wx.App(False)
streams = sys.stdout, sys.stderr
stackup = {'copper': {3: {'thickness_mm': .035, 'name': 'F.Cu'}},
           'substrate': [{'thickness_mm': 1.6}], 'layer_order': [3]}


class Ledger:
    def __init__(self, free_gib=10, coarse_gib=6, fine_gib=2):
        self.free = free_gib*GIB
        self.allocations = {.1: coarse_gib*GIB, .05: fine_gib*GIB}
        self.meshes = []

    def estimate(self, geometry, step):
        if step in self.allocations:
            amount = self.allocations[step]
        elif step >= .1:
            amount = self.allocations[.1]*(.1/step)**2
        else:
            amount = self.allocations[.05]*(.05/step)**2
        return 100, amount

    def allocate(self, mesh, step):
        amount = self.allocations[round(step, 2)]
        assert amount <= self.free, 'Fixture would exhaust physical RAM'
        mesh.ledger_bytes = amount
        self.free -= amount
        self.meshes.append(mesh)


class Extractor:
    def __init__(self, *args, **kwargs):
        pass

    def get_board_stackup(self):
        return stackup

    def get_net_geometry(self, net):
        return {3: box(0, 0, 2, 1)}


def create_dialog():
    dialog = KiPIDA_MainDialog(None, None)
    sys.stdout, sys.stderr = streams
    app.ProcessPendingEvents()
    dialog._timing_history = NS(estimate=lambda features: None,
                                record=lambda *args, **kwargs: None)
    dialog._update_results_ui = lambda: None
    dialog._get_mesh_nodes = lambda mesh, ref, pads, debug=False: [0 if ref == 'J1' else 1]
    dialog.power_tree.rails = [PowerRail('VIN', 12,
        sources=[UnifiedSource(ComponentRef('J1'), ['1'])],
        loads=[UnifiedLoad(ComponentRef('J2'), 1, ['1'])])]
    dialog.txt_grid_size.ChangeValue('.1')
    dialog.choice_memory.SetSelection(0)
    dialog.choice_backend.SetSelection(1)
    dialog.chk_convergence.SetValue(True)
    dialog.chk_thermal.SetValue(False)
    return dialog


def run_fixture(ledger, action):
    dialog = create_dialog()

    class FixtureMesher:
        def __init__(self, *args, memory_budget_gb=None, **kwargs):
            self.budget = memory_budget_gb

        def generate_mesh(self, net, geometry, stackup, grid_size_mm=.1):
            # Preserve the mesher's own allocation check in addition to the UI
            # whole-pass admission check.
            check_budget(ledger.estimate(geometry, grid_size_mm)[1], self.budget)
            mesh = Mesh()
            mesh.net_name, mesh.grid_step = net, grid_size_mm
            mesh.nodes = range(2)
            mesh.node_coords = Coordinates()
            mesh.node_coords.append([[.5, .5, 3], [1.5, .5, 3]])
            mesh.add_edge_direct(0, 1, 10)
            ledger.allocate(mesh, grid_size_mm)
            self.progress_callback(1)
            return mesh

    try:
        with patch('resource_budget.available_ram', side_effect=lambda: ledger.free), \
             patch('resource_budget.grid_estimate', side_effect=ledger.estimate), \
             patch('ui.main_dialog.grid_estimate', side_effect=ledger.estimate), \
             patch('ui.main_dialog.BoardSnapshot', return_value=NS(cache_token='ledger-board')), \
             patch('ui.main_dialog.GeometryExtractor', Extractor), \
             patch('ui.main_dialog.Mesher', FixtureMesher), \
             patch('simulation_cache.mesh_bytes', side_effect=lambda mesh: mesh.ledger_bytes), \
             patch('ui.main_dialog.mesh_bytes', side_effect=lambda mesh: mesh.ledger_bytes), \
             patch('wx.MessageBox') as messages:
            action(dialog, messages)
    finally:
        sys.stdout, sys.stderr = streams
        dialog.Destroy()
        app.ProcessPendingEvents()


def fresh_refinement(dialog, messages):
    dialog.on_run(None)
    assert not messages.called, (messages.call_args, dialog.log_ctrl.GetValue())
    assert len(large.meshes) == 2
    assert dialog.progress_percent.GetLabel() == '100%'
    assert dialog.system_results['VIN']['diagnostics']['mesh_check'].startswith('passed')
    assert large.free == 2*GIB

    # Eight GiB of electrical allocations remain resident. One additional GiB
    # still fits within the current two GiB physical headroom.
    from thermal_solver import ThermalGrid
    class LedgerThermalGrid(ThermalGrid):
        def __init__(self, region, settings, budget_gb=8, retained_bytes=0, progress=None):
            check_budget(GIB, budget_gb, retained_bytes)
            super().__init__(region, settings, budget_gb, retained_bytes, progress)
            large.free -= GIB
    dialog.chk_convergence.SetValue(False)
    with patch('thermal_solver.board_region', return_value=box(0, 0, 2, 1)), \
         patch('thermal_solver.ThermalGrid', LedgerThermalGrid):
        dialog._run_thermal(stackup, {'VIN': {3: box(0, 0, 2, 1)}}, 'cpu', dialog._memory_budget())
    assert dialog.thermal_result is not None
    assert large.free == GIB
    print('Dynamic RAM: fresh coarse/refined electrical and thermal allocations passed without counting resident data twice.')


def cached_changed_load(dialog, messages):
    dialog.on_run(None)
    assert not messages.called, messages.call_args
    count = len(small.meshes)
    dialog.power_tree.rails[0].loads[0].total_current = 2
    dialog.on_run(None)
    assert not messages.called, (messages.call_args, dialog.log_ctrl.GetValue())
    assert len(small.meshes) == count == 2
    assert dialog.system_results['VIN']['diagnostics']['prepared_cache_hit']
    assert abs(dialog.system_results['VIN']['results'][1]-11.8) < 1e-9
    print('Dynamic RAM: changed-load run retained cached meshes and prepared solvers as physical free RAM decreased.')


def insufficient_refinement(dialog, messages):
    dialog.on_run(None)
    assert messages.called
    assert not dialog.system_results
    assert len(insufficient.meshes) == 1
    assert dialog.txt_grid_size.GetValue() == '.1'
    assert 'RAM' in dialog.log_ctrl.GetValue()
    assert 'Refined electrical pass at 0.05 mm' in dialog.log_ctrl.GetValue()
    assert 'Try Mesh Resolution' in dialog.log_ctrl.GetValue()
    print('Dynamic RAM: genuinely oversized refinement was refused before allocation and mesh spacing stayed unchanged.')


def pressure_releases_coarse(dialog, messages):
    from mesh_convergence import release_coarse_arrays
    def release_with_ledger(coarse):
        release_coarse_arrays(coarse)
        # Model the physical memory reclaimed when the large native arrays die.
        compact.free += compact.allocations[.1]
    with patch('mesh_convergence.release_coarse_arrays', side_effect=release_with_ledger) as release:
        dialog.on_run(None)
    assert not messages.called, (messages.call_args, dialog.log_ctrl.GetValue())
    assert release.call_count == 1
    assert dialog.system_results['VIN']['diagnostics']['mesh_check'].startswith('passed')
    assert dialog.txt_grid_size.GetValue() == '.1'
    assert compact.free == 6*GIB
    print('Dynamic RAM: releasing coarse arrays admits the fine pass and preserves the full convergence comparison.')


try:
    large = Ledger()
    run_fixture(large, fresh_refinement)
    small = Ledger(free_gib=6, coarse_gib=.125, fine_gib=.25)
    run_fixture(small, cached_changed_load)
    compact = Ledger(fine_gib=4)
    run_fixture(compact, pressure_releases_coarse)
    insufficient = Ledger(fine_gib=8)
    run_fixture(insufficient, insufficient_refinement)
finally:
    sys.stdout, sys.stderr = streams
