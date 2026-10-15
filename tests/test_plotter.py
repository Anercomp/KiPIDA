
import unittest
import sys
import os

# Add plugin root to path
plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

# Mock wx if strictly necessary, but let's try assuming it might be present or we can mock it
# For CI/headless correctness without wx installed, we often mock wx.
# Let's simple-mock wx to ensure logic runs even if system python doesn't have wx (though user python likely does)
import types
if 'wx' not in sys.modules:
    wx_mock = types.ModuleType('wx')
    wx_mock.Bitmap = lambda *args: "BITMAP_OBJECT"
    wx_mock.Image = lambda *args: "IMAGE_OBJECT"
    wx_mock.BITMAP_TYPE_PNG = 1
    sys.modules['wx'] = wx_mock

from plotter import Plotter, plot_samples
from compact_storage import Coordinates, Voltages
import numpy as np
from mesh import Mesh

class TestPlotter(unittest.TestCase):
    def setUp(self):
        self.plotter = Plotter(debug=True)
        self.mesh = Mesh()
        # Create a simple dummy mesh
        # Nodes 0,1 on layer 0
        # Nodes 2,3 on layer 1
        self.mesh.nodes = [0, 1, 2, 3]
        self.mesh.node_coords = {
            0: (0.0, 0.0, 0),
            1: (1.0, 1.0, 0),
            2: (0.0, 0.0, 1),
            3: (1.0, 1.0, 1)
        }
        self.mesh.results = {
            0: 3.3,
            1: 3.2,
            2: 3.3,
            3: 3.1
        }
        self.stackup = {
            'copper': {0: {}, 1: {}},
            'layer_order': [0, 1]
        }

    def test_plot_3d(self):
        # Should return a bitmap (or mock string)
        bmp = self.plotter.plot_3d_mesh(self.mesh, self.stackup)
        print(f"3D Plot result: {bmp}")
        self.assertIsNotNone(bmp)
        
    def test_plot_2d_layer(self):
        # Should return a bitmap for layer 0
        bmp = self.plotter.plot_layer_2d(self.mesh, 0, self.stackup, vmin=3.0, vmax=3.5, layer_name="F.Cu (Test)")
        print(f"2D Plot Layer 0 result: {bmp}")
        self.assertIsNotNone(bmp)
        
    def test_plot_2d_empty_layer(self):
        # Layer 99 empty
        bmp = self.plotter.plot_layer_2d(self.mesh, 99, self.stackup)
        self.assertIsNone(bmp)

    def test_compact_layer_samples_preserve_global_voltage_indices(self):
        self.mesh.node_coords = Coordinates()
        self.mesh.node_coords.append([[0, 0, 3], [1, 0, 3]])
        self.mesh.node_coords.append([[0, 0, 34], [1, 0, 34]])
        self.mesh.results = Voltages([12, 11, np.nan, 9])
        coords, values, total = plot_samples(self.mesh, 34)
        np.testing.assert_array_equal(coords, [[1, 0, 34]])
        np.testing.assert_array_equal(values, [9])
        self.assertEqual(total, 2)

    def test_render_sampling_retains_complete_solution(self):
        self.mesh.node_coords = Coordinates()
        self.mesh.node_coords.append(np.column_stack((np.arange(1000), np.zeros(1000), np.full(1000, 3))))
        self.mesh.results = Voltages(np.linspace(12, 11, 1000))
        coords, values, total = plot_samples(self.mesh, limit=100)
        self.assertEqual(len(coords), 100)
        self.assertEqual(total, 1000)
        self.assertEqual(len(self.mesh.results), 1000)
        np.testing.assert_array_equal(values, self.mesh.results.array[::10])

    def test_combined_layer_preserves_positions_gaps_and_shared_voltage_scale(self):
        from unittest.mock import patch
        other = Mesh()
        other.node_coords = {0: (10, 20, 0), 1: (11, 20, 1), 2: (12, 20, 0)}
        other.results = {0: 12, 1: 11.9}  # Unpowered node 2 is omitted.
        with patch.object(self.plotter, '_fig_to_bitmap', side_effect=lambda fig: fig):
            fig = self.plotter.plot_combined_layer({'VIN': self.mesh, 'VOUT': other}, 0, 'F.Cu')
        ax = fig.axes[0]
        self.assertEqual(len(ax.collections), 2)
        np.testing.assert_array_equal(ax.collections[0].get_offsets(), [[0, 0], [1, -1]])
        np.testing.assert_array_equal(ax.collections[1].get_offsets(), [[10, -20]])
        self.assertIs(ax.collections[0].norm, ax.collections[1].norm)
        self.assertEqual(ax.collections[0].norm.vmin, 3.2)
        self.assertEqual(ax.collections[0].norm.vmax, 12)
        self.assertEqual([t.get_text() for t in ax.texts], ['VIN', 'VOUT'])

    def test_combined_empty_layer_returns_no_plot(self):
        self.assertIsNone(self.plotter.plot_combined_layer({'VIN': self.mesh}, 99))

if __name__ == '__main__':
    unittest.main()
