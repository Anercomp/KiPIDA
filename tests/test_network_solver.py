import unittest
import tempfile
from pathlib import Path
from mesh import Mesh
from models import PowerRail, UnifiedSource, UnifiedLoad, ComponentRef, SeriesComponent, VoltageRegulator
from network_solver import solve_system
from config_manager import save_config, load_config


class TestStitchedNetwork(unittest.TestCase):
    def fixture(self, resistance=.02, closed=True):
        a = PowerRail('VIN', 12, sources=[UnifiedSource(ComponentRef('J1'), ['1'])])
        b = PowerRail('VOUT', 0, loads=[UnifiedLoad(ComponentRef('J2'), 2, ['1'])])
        a.series_components.append(SeriesComponent('F1', 'VIN', '1', 'VOUT', '2', resistance, closed=closed))
        meshes = {}
        for rail in (a, b):
            mesh = Mesh(); mesh.nodes = [10, 20]; mesh.net_name = rail.net_name
            mesh.add_edge_direct(10, 20, 10)  # 100 mOhm copper on each side
            meshes[rail.net_name] = mesh
        def resolve(mesh, ref, pads):
            return [10 if ref == 'J1' or (ref == 'F1' and pads == ['2']) else 20]
        return [a, b], meshes, resolve

    def test_fuse_current_voltage_loss(self):
        results, parts = solve_system(*self.fixture())
        self.assertAlmostEqual(results['VOUT']['results'][20], 11.56)
        self.assertAlmostEqual(parts[0]['current_a'], 2)
        self.assertAlmostEqual(parts[0]['drop_v'], .04)
        self.assertAlmostEqual(parts[0]['loss_w'], .08)
        self.assertAlmostEqual(sum(d['diagnostics']['copper_loss_w'] for d in results.values()), .8)

    def test_open_fuse_rejects_downstream_load(self):
        with self.assertRaisesRegex(ValueError, 'disconnected'):
            solve_system(*self.fixture(closed=False))

    def test_removed_source_names_unpowered_network(self):
        rails, meshes, resolve = self.fixture()
        rails[0].sources.clear()
        def unexpected_mapping(*args):
            self.fail('Missing feed should fail before terminal mapping or solving.')
        with self.assertRaisesRegex(ValueError, 'network: VIN, VOUT.*Undo Remove'):
            solve_system(rails, meshes, unexpected_mapping)

    def test_removed_series_link_names_downstream_network(self):
        rails, meshes, resolve = self.fixture()
        rails[0].series_components.clear()
        with self.assertRaisesRegex(ValueError, 'network: VOUT'):
            solve_system(rails, meshes, resolve)

    def test_ideal_fuse_merges_nodes_and_recovers_current(self):
        results, parts = solve_system(*self.fixture(resistance=0))
        self.assertEqual(parts[0]['drop_v'], 0)
        self.assertAlmostEqual(parts[0]['current_a'], 2)
        self.assertEqual(parts[0]['loss_w'], 0)
        self.assertAlmostEqual(results['VOUT']['results'][20], 11.6)

    def test_reverse_flow(self):
        rails, meshes, resolve = self.fixture()
        rails[0].sources, rails[1].loads = [], []
        rails[1].sources = [UnifiedSource(ComponentRef('F1'), ['2'])]
        rails[1].nominal_voltage = 12
        rails[0].loads = [UnifiedLoad(ComponentRef('J1'), 2, ['1'])]
        _, parts = solve_system(rails, meshes, resolve)
        self.assertAlmostEqual(parts[0]['current_a'], -2)
        self.assertAlmostEqual(parts[0]['loss_w'], .08)

    def test_regulator_after_fuse(self):
        rails, meshes, resolve = self.fixture()
        rails[1].loads = []
        child = PowerRail('5V', 5, loads=[UnifiedLoad(ComponentRef('U2'), 1, ['1'])])
        rails[1].child_regulators.append(VoltageRegulator('U1', 'VOUT', 'U1', ['IN'], '5V', 'U1', ['OUT'], 'SWITCHING', .8))
        rails.append(child)
        mesh = Mesh(); mesh.nodes = [10, 20]; mesh.net_name = '5V'; mesh.add_edge_direct(10, 20, 10)
        meshes['5V'] = mesh
        base_resolve = resolve
        def mapper(mesh, ref, pads):
            return [10 if pads == ['OUT'] else 20] if mesh.net_name == '5V' else base_resolve(mesh, ref, pads)
        results, parts = solve_system(rails, meshes, mapper)
        current = parts[0]['current_a']
        vin = results['VOUT']['results'][20]
        self.assertAlmostEqual(vin*current, 6.25, places=6)
        self.assertAlmostEqual(results['5V']['results'][20], 4.9)

    def test_persistence_and_old_config(self):
        rails, _, _ = self.fixture(closed=False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.json'
            save_config(rails, path)
            self.assertEqual(load_config(path)[0].series_components, rails[0].series_components)
            path.write_text('{"version":"1.0","rails":[{"net_name":"VIN"}]}')
            self.assertEqual(load_config(path)[0].series_components, [])

    def test_invalid_and_duplicate_links(self):
        for r in (-1, float('nan'), float('inf')):
            with self.assertRaisesRegex(ValueError, 'resistance'):
                solve_system(*self.fixture(resistance=r))
        rails, meshes, resolve = self.fixture()
        rails[0].series_components *= 2
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            solve_system(rails, meshes, resolve)


if __name__ == '__main__':
    unittest.main()
