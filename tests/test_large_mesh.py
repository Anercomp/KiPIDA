import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS
import numpy as np
from shapely.geometry import box
from compact_storage import Coordinates, Voltages
from mesh import Mesher, Mesh
from electrical_solver import Solver
from resource_budget import grid_estimate, check_budget, GIB


class TestLargeMesh(unittest.TestCase):
    def test_sparse_copper_over_two_million_candidate_points(self):
        # Widely separated copper patches: old rectangular-grid guard rejected it.
        geometry = {3: box(0, 0, .4, .4).union(box(150, 150, 150.4, 150.4))}
        count, estimate = grid_estimate(geometry, .1)
        self.assertGreater(count, 2_000_000)
        mesh = Mesher(NS(footprints=[], vias=[]), memory_budget_gb=1).generate_mesh(
            'PWR', geometry, {'copper': {3: {'thickness_mm': .035}}, 'resistivity': 1.68e-5}, .1)
        self.assertIsInstance(mesh.nodes, range)
        self.assertLess(len(mesh.nodes), 100)
        self.assertEqual(mesh.node_coords.__class__, Coordinates)

    def test_budget_counts_retained_refinement_memory(self):
        with patch('resource_budget.available_ram', return_value=16*GIB):
            self.assertEqual(check_budget(GIB, 2), GIB)
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                check_budget(GIB, 2, retained_bytes=2*GIB)
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                check_budget(12*GIB, 16)

    def test_coordinates_and_unpowered_voltage_mapping(self):
        coordinates = Coordinates()
        coordinates.append([[1, 2, 3], [4, 5, 3]])
        coordinates.append([[6, 7, 34]])
        np.testing.assert_array_equal(coordinates.take([2, 0]), [[6, 7, 34], [1, 2, 3]])
        values = Voltages([12, np.nan, 11])
        self.assertEqual(dict(values), {0: 12, 2: 11})
        self.assertNotIn(1, values)

    def test_compact_solver_boundary_load_and_unpowered_island(self):
        mesh = Mesh(); mesh.nodes = range(3); mesh.add_edge_direct(0, 1, 2)
        solver = Solver(backend='cpu')
        result = solver.solve(mesh, [dict(node_id=0, voltage=12)],
                              [dict(node_id=0, current=1), dict(node_id=1, current=2)])
        self.assertEqual(dict(result), {0: 12, 1: 11})
        self.assertEqual(solver.diagnostics['source_current_a'], 3)
        self.assertEqual(solver.diagnostics['copper_loss_w'], 2)
        self.assertEqual(solver.diagnostics['excluded_nodes'], 1)

    def test_refinement_grid_estimate_scales_fourfold(self):
        coarse = grid_estimate({3: box(0, 0, 100, 100)}, .1)[0]
        refined = grid_estimate({3: box(0, 0, 100, 100)}, .05)[0]
        self.assertAlmostEqual(refined/coarse, 4, delta=.02)

    def test_pth_contact_uses_outer_pad_not_drill_radius(self):
        from tests.test_review_fixes import stackup
        pad = NS(net=NS(name='PWR'), number='1', pad_type=1, position=NS(x=0, y=0),
                 size=NS(x=800000, y=800000), shape=1,
                 padstack=NS(layers=[3, 34], angle=NS(degrees=0), drill=NS(diameter=NS(x=300000, y=300000))))
        board = NS(vias=[], footprints=[NS(pads=[pad])])
        geometry = {3: box(-.4, -.4, .4, .4).difference(box(-.15, -.15, .15, .15)),
                    34: box(-.4, -.4, .4, .4).difference(box(-.15, -.15, .15, .15))}
        mesh = Mesher(board).generate_mesh('PWR', geometry, stackup(), .2)
        self.assertTrue(any(mesh.node_coords[u][2] != mesh.node_coords[v][2]
                            for u, v, g in zip(mesh.G_coo_row, mesh.G_coo_col, mesh.G_coo_data) if u != v))
