import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch


class TestToolbarBridge(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1]/'toolbar_bridge.py'
        spec = importlib.util.spec_from_file_location('tested_toolbar_bridge', path)
        self.bridge = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'pcbnew': SimpleNamespace(ActionPlugin=object)}):
            spec.loader.exec_module(self.bridge)

    def test_starts_managed_runtime_without_shell(self):
        directory = Path(__file__).resolve().parents[1]
        executable = directory/'pythonw.exe'
        with patch.object(self.bridge, 'runtime_python', return_value=executable), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(self.bridge.subprocess, 'Popen') as spawn:
            self.bridge.launch_runtime('9.0', directory)
        args, kwargs = spawn.call_args
        self.assertEqual(args[0], [str(executable), str(directory/'launch.py')])
        self.assertEqual(kwargs['cwd'], str(directory))
        self.assertNotIn('shell', kwargs)
        self.assertIn('env', kwargs)

    def test_missing_runtime_is_explained(self):
        with patch.object(self.bridge, 'runtime_python', return_value=Path('absent-pythonw.exe')):
            with self.assertRaisesRegex(RuntimeError, 'Python environment was not found'):
                self.bridge.launch_runtime('9.0', Path(__file__).resolve().parents[1])

    def test_toolbar_visible_with_correct_icon(self):
        action = self.bridge.KiPIDAToolbarBridge()
        action.defaults()
        self.assertTrue(action.show_toolbar_button)
        self.assertTrue(Path(action.icon_file_name).is_file())
        self.assertEqual(action.name, 'Ki-PIDA Simulation')
