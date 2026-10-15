"""Physics and regression checks independent of a running KiCad/wx session."""
import ast
import math
import time
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
import numpy as np
from shapely.geometry import box, Polygon, Point
from scipy.sparse import coo_matrix
from mesh import Mesh, Mesher
from solver import Solver
from copper_geometry import terminal_nodes, pad_geometry
from extractor import GeometryExtractor
from models import PowerRail, UnifiedSource, UnifiedLoad, ComponentRef, VoltageRegulator


def stackup():
    return {'copper': {3: {'thickness_mm': .035}, 34: {'thickness_mm': .035}},
            'layer_order': [3, 34], 'resistivity': 1.68e-5,
            'substrate': [{'between': [3, 34], 'thickness_mm': 1.6}], 'via_plating_mm': .025}


def matrix(mesh):
    return coo_matrix((mesh.G_coo_data, (mesh.G_coo_row, mesh.G_coo_col)),
                      shape=(len(mesh.nodes), len(mesh.nodes))).tocsr()


class TestElectricalAccuracy(unittest.TestCase):
    def test_rectangle_matches_analytic_resistance_and_refinement(self):
        for spacing in (.5, .25, .125):
            mesh = Mesher(NS(vias=[], footprints=[])).generate_mesh('PWR', {3: box(0, 0, 10, 1)}, stackup(), spacing)
            sources = [{'node_id': n, 'voltage': 5} for n, (x, y, l) in mesh.node_coords.items() if abs(x) < 1e-9]
            ends = [n for n, (x, y, l) in mesh.node_coords.items() if abs(x-10) < 1e-9]
            # Distributed end current proportional to each boundary cell width.
            weights = [min(1, mesh.node_coords[n][1]+spacing/2)-max(0, mesh.node_coords[n][1]-spacing/2) for n in ends]
            loads = [{'node_id': n, 'current': w/sum(weights)} for n, w in zip(ends, weights)]
            solver = Solver(); results = solver.solve(mesh, sources, loads)
            expected_r = 1.68e-5*10/(.035*1)
            self.assertAlmostEqual(5-min(results.values()), expected_r, places=9)
            self.assertAlmostEqual(solver.diagnostics['copper_loss_w'], expected_r, places=9)
            self.assertLess(solver.diagnostics['balance_a'], 1e-8)

    def test_polygon_holes_remain_empty(self):
        poly = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)], [[(1, 1), (3, 1), (3, 3), (1, 3)]])
        mesh = Mesher(NS(vias=[], footprints=[])).generate_mesh('PWR', {3: poly}, stackup(), .25)
        self.assertFalse(any(1 < x < 3 and 1 < y < 3 for x, y, l in mesh.node_coords.values()))

    def test_neighbor_nodes_do_not_bridge_small_gap(self):
        geo = box(0, 0, .2, 1).union(box(.3, 0, 1, 1))
        mesh = Mesher(NS(vias=[], footprints=[])).generate_mesh('PWR', {3: geo}, stackup(), .5)
        g = matrix(mesh).tocoo()
        for u, v, val in zip(g.row, g.col, g.data):
            if u != v and val:
                self.assertFalse(mesh.node_coords[u][0] <= .2 and mesh.node_coords[v][0] >= .3)

    def test_disconnected_load_rejected_and_unloaded_island_excluded(self):
        mesh = Mesh(); mesh.nodes = [0, 1, 2, 3]; mesh.edges = [(0, 1, 1), (2, 3, 1)]
        solver = Solver()
        with self.assertRaisesRegex(ValueError, 'disconnected'):
            solver.solve(mesh, [{'node_id': 0, 'voltage': 5}], [{'node_id': 3, 'current': 1}])
        result = solver.solve(mesh, [{'node_id': 0, 'voltage': 5}], [{'node_id': 1, 'current': 1}])
        self.assertEqual(result, {0: 5, 1: 4})
        self.assertEqual(solver.diagnostics['excluded_nodes'], 2)

    def test_sparse_nonsequential_ids_and_fixed_node_load(self):
        mesh = Mesh(); mesh.nodes = [10, 20]; mesh.add_edge_direct(10, 20, 1)
        solver = Solver()
        result = solver.solve(mesh, [{'node_id': 10, 'voltage': 5}],
                              [{'node_id': 10, 'current': 2}, {'node_id': 20, 'current': 1}])
        self.assertEqual(result, {10: 5, 20: 4})
        self.assertAlmostEqual(solver.diagnostics['source_current_a'], 3)

    def test_bad_boundaries_rejected(self):
        mesh = Mesh(); mesh.nodes = [0, 1]; mesh.edges = [(0, 1, 1)]
        for sources, loads in [([{'node_id': 0, 'voltage': float('nan')}], []),
                               ([{'node_id': 0, 'voltage': 5}], [{'node_id': 9, 'current': 1}]),
                               ([{'node_id': 0, 'voltage': 5}, {'node_id': 0, 'voltage': 6}], [])]:
            with self.assertRaises(ValueError): Solver().solve(mesh, sources, loads)

    def test_via_barrel_resistance_and_cross_layer_edge(self):
        via = NS(net=NS(name='PWR'), position=NS(x=0, y=0), diameter=600000,
                 drill_diameter=300000, padstack=NS(layers=[3, 34]))
        poly = Point(0, 0).buffer(.3).difference(Point(0, 0).buffer(.15))
        mesher = Mesher(NS(vias=[via], footprints=[]))
        mesh = mesher.generate_mesh('PWR', {3: poly, 34: poly}, stackup(), .05)
        g = matrix(mesh).tocoo()
        vertical = [(u, v, -val) for u, v, val in zip(g.row, g.col, g.data)
                    if u < v and mesh.node_coords[u][2] != mesh.node_coords[v][2]]
        self.assertGreater(len(vertical), 4)
        expected_g = math.pi*(.3*.025+.025**2)/(1.68e-5*1.635)
        self.assertAlmostEqual(sum(edge[2] for edge in vertical), expected_g)
        # A coarse mesh cannot silently attach the via to nearby unrelated copper.
        with self.assertRaisesRegex(ValueError, 'annular mesh'):
            mesher.generate_mesh('PWR', {3: poly, 34: poly}, stackup(), 1)

    def test_terminal_shape_and_layer_restrictions(self):
        mesh = Mesh(); mesh.node_coords = {0: (0, 0, 3), 1: (0, 0, 34), 2: (1, 0, 3)}
        self.assertEqual(terminal_nodes(mesh, {3: box(-.2, -.2, .2, .2)}), [0])

    def test_real_ipc_pad_polygon_used(self):
        from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode, Vector2
        from kipy.board_types import Pad
        pad = Pad(); pad.padstack.layers = [3]
        poly = PolygonWithHoles(); line = PolyLine()
        for x, y in [(0, 0), (1000000, 0), (1000000, 1000000), (0, 1000000)]:
            node = PolyLineNode(); node.point = Vector2.from_xy(x, y); line.append(node)
        poly.outline = line
        shapes = pad_geometry(NS(get_pad_shapes_as_polygons=lambda p, l: poly), pad, stackup()['copper'])
        self.assertEqual(list(shapes), [3]); self.assertEqual(shapes[3].area, 1)

    def test_track_extraction_keeps_actual_width(self):
        track = NS(net=NS(name='PWR'), layer=3, start=NS(x=0, y=0),
                   end=NS(x=10000000, y=0), width=100000)
        board = NS(tracks=[track], vias=[], footprints=[], zones=[])
        extractor = GeometryExtractor(board); extractor._stackup_cache = stackup()
        poly = extractor.get_net_geometry('PWR')[3]
        self.assertAlmostEqual(poly.bounds[3]-poly.bounds[1], .1)


def production_method(name):
    # Compile the actual UI method without importing wx; do not copy its implementation.
    path = Path(__file__).resolve().parents[1]/'ui'/'main_dialog.py'
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec')


class TestProductionRailSimulation(unittest.TestCase):
    def test_regulator_input_current_stamped_once(self):
        for reg_type, expected in [('LINEAR', 1.0), ('SWITCHING', (10-math.sqrt(75))/2)]:
            root = PowerRail('10V', 10, [UnifiedSource(ComponentRef('J1'), ['1'])])
            child = PowerRail('5V', 5, loads=[UnifiedLoad(ComponentRef('U2'), 1, ['1'])])
            root.child_regulators.append(VoltageRegulator('REG', '10V', 'U1', ['IN'], '5V', 'U1', ['OUT'], reg_type, .8))
            class Extractor:
                def __init__(self, *args, **kwargs): pass
                def get_board_stackup(self): return stackup()
                def get_net_geometry(self, net): return {3: box(0, 0, 1, 1)}
            class FixtureMesher:
                def __init__(self, *args, **kwargs): pass
                def generate_mesh(self, *args, **kwargs):
                    m = Mesh(); m.nodes = [0, 1]; m.edges = [(0, 1, 1)]; return m
            errors = []
            env = dict(math=math, GeometryExtractor=Extractor, Mesher=FixtureMesher, Solver=Solver,
                       wx=NS(MessageBox=lambda *args: errors.append(args), OK=1, ICON_ERROR=2))
            from simulation_cache import BoardSnapshot, MeshCache, rail_signature, signature
            from simulation_timing import TimingHistory
            env.update(time=time, BoardSnapshot=BoardSnapshot, rail_signature=rail_signature, signature=signature)
            exec(production_method('on_run'), env)
            dialog = NS(power_tree=NS(rails=[root, child]), board=None, notebook=NS(SetSelection=lambda *a: None),
                        chk_debug=NS(GetValue=lambda: False), chk_convergence=NS(GetValue=lambda: False),
                        txt_grid_size=NS(GetValue=lambda: '.1'), log=lambda *a: None,
                        _topological_sort_rails=lambda rails: [child, root], _update_results_ui=lambda: None,
                        _get_mesh_nodes=lambda mesh, ref, pads, debug: [0 if ref=='J1' or pads==['OUT'] else 1])
            dialog._mesh_cache = MeshCache()
            dialog._prepared_cache = None
            dialog._geometry_cache = {}
            dialog._board_token = None
            dialog._last_electrical = None
            dialog._last_thermal = None
            dialog._timing_history = NS(estimate=lambda f: None, record=lambda *a: None)
            dialog._eta_timer = NS(Start=lambda *a: None, Stop=lambda: None)
            dialog._memory_budget = lambda: 1
            dialog._clear_caches = lambda: None
            dialog._other_retained_bytes = lambda: 0
            dialog._retained_memory_bytes = lambda *a: 0
            dialog._update_eta = lambda: None
            dialog.chk_thermal = NS(GetValue=lambda: False)
            dialog.txt_thermal_step = NS(GetValue=lambda: '1')
            dialog.txt_cooling = NS(GetValue=lambda: '10')
            dialog.eta_label = NS(SetLabel=lambda *a: None)
            dialog.btn_thermal = NS(Enable=lambda *a: None)
            env['format_duration'] = lambda seconds: str(seconds)
            env['on_run'](dialog, None)
            self.assertEqual(errors, [])
            self.assertAlmostEqual(dialog.system_results['10V']['results'][1], 10-expected)
            self.assertAlmostEqual(dialog.system_results['10V']['diagnostics']['load_current_a'], expected)
            # Exercise the production refinement branch, including terminal remapping.
            dialog.chk_convergence = NS(GetValue=lambda: True)
            env['on_run'](dialog, None)
            self.assertEqual(errors, [])
            self.assertIn('passed', dialog.system_results['10V']['diagnostics']['mesh_check'])
            # A refinement that changes resistance by 50% must invalidate all results.
            class NonConvergedMesher(FixtureMesher):
                def generate_mesh(self, *args, **kwargs):
                    mesh = super().generate_mesh(*args, **kwargs)
                    mesh.edges = [(0, 1, 1 if kwargs['grid_size_mm'] >= .1 else 2)]
                    return mesh
            env['Mesher'] = NonConvergedMesher
            env['on_run'](dialog, None)
            self.assertEqual(dialog.system_results, {})
            self.assertIn('Mesh not converged', errors[-1][0])


if __name__ == '__main__': unittest.main()
