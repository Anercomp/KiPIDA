"""Build and validate a local PCM installer (no publication or installation)."""
import json
import struct
import zipfile
from pathlib import Path
import jsonschema

ROOT = Path(__file__).resolve().parent


def validate():
    for document, schema in [('metadata.json', 'pcm-v1.schema.json'), ('plugin.json', 'api-v1.schema.json')]:
        jsonschema.Draft7Validator(json.loads((ROOT/schema).read_text(encoding='utf-8'))).validate(
            json.loads((ROOT/document).read_text(encoding='utf-8')))


def build():
    validate()
    output = ROOT.parent/'KiPIDA-PCM.zip'
    runtime = ['__init__.py', 'config_manager.py', 'copper_geometry.py', 'discovery.py',
               'electrical_solver.py', 'extractor.py', 'ipc_entry.py', 'launch.py', 'mesh.py',
               'models.py', 'network_solver.py', 'compact_storage.py', 'resource_budget.py',
               'solver_backend.py', 'prepared_backend.py', 'regulator_ports.py', 'mesh_convergence.py',
               'network_cache.py', 'refined_start.py', 'simulation_cache.py', 'simulation_timing.py',
               'regulator_heat.py',
               'thermal_solver.py', 'temperature_map.py', 'pcb_overlay.py', 'requirements-gpu.txt', 'toolbar_bridge.py', 'plotter.py', 'solver.py', 'plugin.json', 'requirements.txt',
               'LICENSE', 'TESTING.md', 'ADAPTIVE_MESHING.md']
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.write(ROOT/'metadata.json', 'metadata.json')
        for name in runtime:
            archive.write(ROOT/name, 'plugins/'+name)
        for folder in ['ui', 'resources']:
            for path in sorted((ROOT/folder).rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts:
                    archive.write(path, 'plugins/'+path.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        assert 'metadata.json' in names and 'plugins/plugin.json' in names
        assert all(name == 'metadata.json' or name.startswith('plugins/') for name in names)
        plugin = json.loads(archive.read('plugins/plugin.json'))
        for action in plugin['actions']:
            assert 'plugins/'+action['entrypoint'] in names
            for key in ['icons-light', 'icons-dark']:
                for image in action.get(key, []):
                    assert 'plugins/'+image in names
        # These two chunks are the source of the reported libpng warnings.
        for name in names:
            if not name.endswith('.png'):
                continue
            data = archive.read(name)
            assert data[:8] == b'\x89PNG\r\n\x1a\n'
            offset = 8
            while offset < len(data):
                length = struct.unpack('>I', data[offset:offset+4])[0]
                kind = data[offset+4:offset+8]
                assert kind not in (b'iCCP', b'cHRM'), (name, kind)
                offset += length+12
    print(f'Validated PCM installer: {output}')


if __name__ == '__main__':
    build()
