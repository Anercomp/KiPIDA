import unittest
from types import SimpleNamespace as NS
import math
import numpy as np
from shapely.geometry import Point, box
from mesh import Mesher
from network_solver import copper_edges
from electrical_solver import Solver


class TestDistributedBarrel(unittest.TestCase):
    def test_total_barrel_conductance_and_contact_area_are_preserved_on_refinement(self):
        via = NS(net=NS(name='PWR'), position=NS(x=0, y=0), diameter=600000,
                 drill_diameter=300000, padstack=NS(layers=[3, 34]))
        stackup = {'copper': {3: {'thickness_mm': .035}, 34: {'thickness_mm': .035}},
                   'layer_order': [3, 34], 'resistivity': 1.68e-5,
                   'substrate': [{'between': [3, 34], 'thickness_mm': 1.6}]}
        plane = box(-1, -1, 1, 1).difference(Point(0, 0).buffer(.15))
        expected = math.pi*(.3*.025+.025**2)/(1.68e-5*1.635)
        drops = []
        for spacing in (.1, .05, .025):
            mesh = Mesher(NS(vias=[via], footprints=[])).generate_mesh('PWR', {3: plane, 34: plane}, stackup, spacing)
            edges = copper_edges(mesh)
            a, b = mesh.node_coords.take(edges.u), mesh.node_coords.take(edges.v)
            vertical = a[:, 2] != b[:, 2]
            self.assertGreater(np.count_nonzero(vertical), 4)
            self.assertAlmostEqual(float(edges.g[vertical].sum()), expected, places=8)
            for coords in (a[vertical], b[vertical]):
                self.assertTrue(np.all(np.hypot(coords[:, 0], coords[:, 1]) >= .15-1e-8))
                self.assertTrue(np.all(np.hypot(coords[:, 0], coords[:, 1]) <= .3+1e-8))
            sources = [dict(node_id=n, voltage=1) for n, (x, y, layer) in mesh.node_coords.items() if layer == 3 and abs(x+1) < 1e-8]
            ends = [n for n, (x, y, layer) in mesh.node_coords.items() if layer == 34 and abs(x-1) < 1e-8]
            solver = Solver(backend='cpu')
            values = solver.solve(mesh, sources, [dict(node_id=n, current=1/len(ends)) for n in ends])
            drops.append(1-min(values[n] for n in ends))
            solver.close()
        # Distributed finite contact avoids a point-injection singularity.
        self.assertLess(abs(drops[-1]-drops[-2])/drops[-1], .05)
