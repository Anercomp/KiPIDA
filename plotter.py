
import matplotlib
# Use Agg backend to avoid GUI requirement for matplotlib, since we just want images
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
import io
import wx
import numpy as np

def plot_samples(mesh, layer=None, limit=200000):
    """Limit rendering only; the electrical solution retains every mesh node."""
    has_results = hasattr(mesh, 'results') and bool(mesh.results)
    if hasattr(mesh.node_coords, 'parts'):
        total = sum(len(part) for part in mesh.node_coords.parts if len(part) and (layer is None or part[0, 2] == layer))
        stride = max(1, int(np.ceil(total/limit)))
        coordinates, voltages = [], []
        offset = 0
        for part in mesh.node_coords.parts:
            if len(part) and (layer is None or part[0, 2] == layer):
                indices = np.arange(0, len(part), stride)
                if has_results and hasattr(mesh.results, 'array'):
                    values = mesh.results.array[offset+indices]
                    valid = np.isfinite(values)
                    coordinates.append(part[indices[valid]])
                    voltages.append(values[valid])
                else:
                    selected = [int(offset+i) for i in indices if not has_results or int(offset+i) in mesh.results]
                    coordinates.append(mesh.node_coords.take(selected))
                    voltages.append(np.asarray([mesh.results[n] if has_results else 0 for n in selected]))
            offset += len(part)
        if not coordinates:
            return np.empty((0, 3)), np.empty(0), total
        return np.concatenate(coordinates), np.concatenate(voltages), total
    selected = [(n, coord) for n, coord in mesh.node_coords.items() if (layer is None or coord[2] == layer) and (not has_results or n in mesh.results)]
    total = len(selected)
    selected = selected[::max(1, int(np.ceil(total/limit)))]
    return (np.asarray([coord for _, coord in selected]).reshape((-1, 3)),
            np.asarray([mesh.results[n] if has_results else 0 for n, _ in selected]), total)


class Plotter:
    def __init__(self, debug=False):
        self.debug = debug

    def plot_temperature(self, result):
        fig, _ = self.temperature_figure(result)
        return self._fig_to_bitmap(fig)

    def temperature_figure(self, result, temperatures=None):
        from temperature_map import TemperatureMap
        from matplotlib.figure import Figure
        temperatures = temperatures or TemperatureMap(result)
        fig = Figure(figsize=(9, 6), constrained_layout=True)
        ax = fig.add_subplot(111)
        im = ax.imshow(temperatures.image, origin='upper', interpolation='nearest', cmap='inferno',
                       extent=temperatures.extent)
        x, y = temperatures.maximum_xy
        ax.plot(x, -y, marker='x', color='cyan', markersize=8, markeredgewidth=2, zorder=5)
        fig.colorbar(im, ax=ax, label='Board-average temperature (°C)')
        ax.set_title('Experimental steady-state temperature — selected copper + series losses')
        ax.set_xlabel('X (mm)'); ax.set_ylabel('Y (mm)')
        return fig, ax

    def plot_combined_layer(self, meshes, layer_id, layer_name=None, vmin=None, vmax=None):
        """Overlay solved rails in board coordinates with one absolute voltage scale."""
        counts = {}
        for name, mesh in meshes.items():
            if not getattr(mesh, 'results', None):
                continue
            if hasattr(mesh.node_coords, 'parts'):
                count = sum(len(p) for p in mesh.node_coords.parts if len(p) and p[0, 2] == layer_id)
            else:
                count = sum(c[2] == layer_id for c in mesh.node_coords.values())
            if count:
                counts[name] = count
        total = sum(counts.values())
        samples = []
        for name, count in counts.items():
            budget = max(1, int(200000*count/total))
            coords, volts, _ = plot_samples(meshes[name], layer_id, budget)
            if len(coords):
                samples.append((name, coords, volts))
        if not samples:
            return None
        if vmin is None:
            vmin = min(float(v.min()) for _, _, v in samples)
        if vmax is None:
            vmax = max(float(v.max()) for _, _, v in samples)
        norm = matplotlib.colors.Normalize(vmin=vmin, vmax=vmax)
        fig, ax = plt.subplots(figsize=(9, 6), constrained_layout=True)
        try:
            for name, coords, volts in samples:
                ax.scatter(coords[:, 0], -coords[:, 1], c=volts, cmap='viridis', norm=norm, s=4, linewidths=0)
                # Anchor the name to a sampled copper node, rather than an empty gap.
                center = coords[:, :2].mean(axis=0)
                index = np.argmin(np.sum((coords[:, :2]-center)**2, axis=1))
                ax.annotate(name, (coords[index, 0], -coords[index, 1]), fontsize=7,
                            xytext=(4, 4), textcoords='offset points',
                            bbox=dict(facecolor='white', alpha=.8, edgecolor='none', pad=1))
            fig.colorbar(matplotlib.cm.ScalarMappable(norm=norm, cmap='viridis'), ax=ax, label='Voltage (V)')
            title = f"All rails — {layer_name if layer_name is not None else layer_id}"
            displayed = sum(len(c) for _, c, _ in samples)
            if displayed < total:
                title += f'\nDisplay sampled: {displayed:,} of {total:,} nodes'
            ax.set_title(title)
            ax.set_xlabel('X (mm)'); ax.set_ylabel('Y (mm)')
            ax.set_aspect('equal', 'box')
            return self._fig_to_bitmap(fig)
        finally:
            plt.close(fig)

    def plot_3d_mesh(self, mesh, stackup=None, vmin=None, vmax=None):
        """
        Generates a 3D scatter plot of the mesh nodes.
        Returns a wx.Bitmap.
        """
        try:
            fig = plt.figure(figsize=(7, 5), constrained_layout=True)
            ax = fig.add_subplot(111, projection='3d')
            
            xs, ys, zs, c = [], [], [], []
            has_results = hasattr(mesh, 'results') and mesh.results
            
            layer_to_z = {}
            if stackup and 'layer_order' in stackup:
                for idx, layer_id in enumerate(stackup['layer_order']):
                    layer_to_z[layer_id] = 10.0 - idx
            else:
                copper_layers = sorted(stackup['copper'].keys()) if stackup and 'copper' in stackup else []
                for idx, layer_id in enumerate(copper_layers):
                    layer_to_z[layer_id] = 10.0 - idx
            
            # If no node_coords populated yet (edge case), return None
            if not mesh.node_coords:
                plt.close(fig)
                return None

            coordinates, c, total = plot_samples(mesh)
            if not len(coordinates):
                plt.close(fig)
                return None
            xs, ys = coordinates[:, 0], -coordinates[:, 1]
            zs = [layer_to_z.get(layer, 10-layer*.5) for layer in coordinates[:, 2]]
            if len(coordinates) < total:
                ax.set_title(f'Display sampled: {len(coordinates):,} of {total:,} nodes')

            sc = ax.scatter(xs, ys, zs, c=c, cmap='viridis', vmin=vmin, vmax=vmax)
            if has_results:
                plt.colorbar(sc, label='Voltage (V)', shrink=0.8)
            
            ax.set_xlabel('X (mm)'); ax.set_ylabel('Y (mm)'); ax.set_zlabel('L (pseudo)')
            
            # Equal aspect ratio
            x_limits = ax.get_xlim3d()
            y_limits = ax.get_ylim3d()
            x_range = x_limits[1] - x_limits[0]
            y_range = y_limits[1] - y_limits[0]
            max_range = max(x_range, y_range)
            x_mid = (x_limits[0] + x_limits[1]) / 2.0
            y_mid = (y_limits[0] + y_limits[1]) / 2.0
            ax.set_xlim3d([x_mid - max_range/2, x_mid + max_range/2])
            ax.set_ylim3d([y_mid - max_range/2, y_mid + max_range/2])
            
            return self._fig_to_bitmap(fig)
        except Exception as e:
            if self.debug: print(f"Plotter 3D Error: {e}")
            return None

    def plot_layer_2d(self, mesh, layer_id, stackup=None, vmin=None, vmax=None, layer_name=None):
        """
        Generates a 2D plot (heatmap) for a specific layer.
        Returns a wx.Bitmap.
        """
        try:
            fig, ax = plt.subplots(figsize=(7, 5), constrained_layout=True)
            
            xs, ys, vs = [], [], []
            has_results = hasattr(mesh, 'results') and mesh.results
            
            coordinates, vs, total = plot_samples(mesh, layer_id)
            if not len(coordinates):
                plt.close(fig)
                return None
            xs, ys = coordinates[:, 0], -coordinates[:, 1]
            if len(coordinates) < total:
                ax.set_title(f'Display sampled: {len(coordinates):,} of {total:,} nodes')

            # Scatter plot for now - tripcolor or imshow is better if we have regular grid, 
            # but scatter is robust for sparse nodes.
            # Using a fixed marker size might be tricky, let's try a reasonable default.
            # Ideally s should relate to grid_size, but scatter size is in points^2.
            # Let's just use a standard size for visibility.
            sc = ax.scatter(xs, ys, c=vs, cmap='viridis', vmin=vmin, vmax=vmax, s=20)
            
            if has_results:
                plt.colorbar(sc, label='Voltage (V)')
            
            if layer_name is None:
                layer_name = str(layer_id)
                if stackup and 'copper' in stackup and layer_id in stackup['copper']:
                     # Try to get layer name? currently stackup dict structure in test is simple
                     pass

            title = f"Layer: {layer_name}"
            if len(coordinates) < total:
                title += f'\nDisplay sampled: {len(coordinates):,} of {total:,} nodes'
            ax.set_title(title)
            ax.set_xlabel('X (mm)')
            ax.set_ylabel('Y (mm)')
            ax.set_aspect('equal', 'box')
            
            return self._fig_to_bitmap(fig)

        except Exception as e:
            if self.debug: print(f"Plotter 2D Error: {e}")
            return None

    def _fig_to_bitmap(self, fig):
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100)
        plt.close(fig)
        buf.seek(0)
        image = wx.Image(buf, wx.BITMAP_TYPE_PNG)
        return wx.Bitmap(image)
