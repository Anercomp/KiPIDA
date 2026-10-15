"""Config picker and failure handling tests without a wx dependency."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import patch

from config_manager import load_config, save_config
from models import PowerRail, UnifiedSource, ComponentRef, VoltageRegulator


def panel_class():
    source = Path(__file__).resolve().parents[1] / "ui" / "power_tree_panel.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PowerTreePanel")
    names = {"_read_config_file", "_save_config_file", "on_save_config", "on_save_config_as",
             "on_load_config", "_choose_config_file", "_update_config_label"}
    selected = [node for node in original.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ast.Module(body=[ast.ClassDef(name="Panel", bases=[], keywords=[], body=selected, decorator_list=[])], type_ignores=[])
    ast.fix_missing_locations(module)
    messages = []
    wx = NS(ID_OK=1, FD_SAVE=2, FD_OVERWRITE_PROMPT=4, FD_OPEN=8, FD_FILE_MUST_EXIST=16,
            OK=32, ICON_ERROR=64, YES_NO=128, NO_DEFAULT=256, ICON_QUESTION=512, YES=2,
            MessageBox=lambda *args: messages.append(args))
    namespace = {"Path": Path, "save_config": save_config, "load_config": load_config, "wx": wx}
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["Panel"], namespace, messages


class TestConfigFiles(unittest.TestCase):
    def setUp(self):
        self.Panel, self.namespace, self.messages = panel_class()
        self.panel = self.Panel()
        self.panel.config_path = None
        self.panel.rails = [PowerRail("12V", 12, sources=[UnifiedSource(ComponentRef("J1"), ["1"])])]
        self.panel.removal_history = ["earlier removal"]
        self.panel.logs = []
        self.panel.log = self.panel.logs.append
        self.panel._update_config_label = lambda: None
        self.panel._get_config_path = lambda: None
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "named.kipida.json"
        self.old_rails = self.panel.rails
        self.old_history = self.panel.removal_history.copy()

    def select(self, path):
        self.panel._choose_config_file = lambda save=False: path

    def test_save_defaults_then_targets_the_selected_file(self):
        self.panel._get_config_path = lambda: self.path
        self.panel.on_save_config(None)
        self.assertEqual(load_config(str(self.path)), self.panel.rails)
        named = self.path.with_name("alternative.kipida.json")
        self.select(named)
        self.panel.on_save_config_as(None)
        self.assertEqual(self.panel.config_path, named)
        self.panel.rails[0].nominal_voltage = 11
        self.panel.on_save_config(None)
        self.assertEqual(load_config(str(named))[0].nominal_voltage, 11)
        self.assertEqual(load_config(str(self.path))[0].nominal_voltage, 12)
        self.assertEqual(self.panel.removal_history, self.old_history)

    def test_cancelled_open_and_save_preserve_current_edits(self):
        self.select(None)
        self.panel.on_load_config(None)
        self.panel.on_save_config_as(None)
        self.assertIs(self.panel.rails, self.old_rails)
        self.assertEqual(self.panel.removal_history, self.old_history)
        self.assertIsNone(self.panel.config_path)
        self.assertEqual(self.messages, [])
        self.assertFalse(self.path.exists())

    def test_invalid_import_preserves_rails_path_and_undo(self):
        self.path.write_text('{"version":"1.1", "rails":[{"net_name":"NEW"}, {"bad":"second"}]}')
        self.panel.config_path = self.path.with_name("previous.kipida.json")
        old_path = self.panel.config_path
        self.select(self.path)
        self.panel.on_load_config(None)
        self.assertIs(self.panel.rails, self.old_rails)
        self.assertEqual(self.panel.removal_history, self.old_history)
        self.assertEqual(self.panel.config_path, old_path)
        self.assertTrue(self.messages)

    def test_successful_open_replaces_state_and_resets_undo(self):
        save_config([PowerRail("3V3", 3.3)], str(self.path))
        self.select(self.path)
        labels, selections = [], []
        self.panel.rail_list = NS(Clear=labels.clear, Append=labels.append, SetSelection=selections.append)
        self.panel.on_rail_select = lambda event: setattr(self.panel, "active_rail", self.panel.rails[0])
        self.panel.on_load_config(None)
        self.assertEqual(self.panel.active_rail.net_name, "3V3")
        self.assertEqual(self.panel.config_path, self.path)
        self.assertEqual(self.panel.removal_history, [])
        self.assertEqual(labels, ["3V3 (3.3V)"])
        self.assertEqual(selections, [0])

    def test_failed_save_does_not_damage_existing_file(self):
        original = b'{"version":"1.1","rails":[]}'
        self.path.write_bytes(original)
        def partial_save(rails, path):
            Path(path).write_text("partially written")
            raise OSError("Simulated write failure")
        with patch.dict(self.namespace, {"save_config": partial_save}):
            self.assertFalse(self.panel._save_config_file(self.path))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIsNone(self.panel.config_path)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_validation_accepts_old_version_but_rejects_bad_shape_and_values(self):
        documents = [
            {"version": "1.1"},
            {"version": "1.1", "rails": [{"net_name": "A"}, {"net_name": "A"}]},
            {"version": "1.1", "rails": [{"net_name": "A", "nominal_voltage": "12"}]},
            {"version": "1.1", "rails": [{"net_name": "A", "nominal_voltage": float("nan")}]},
            {"version": "1.1", "rails": [{"net_name": "A", "sources": [{"ref_des": "J1", "pad_names": "1"}]}]},
        ]
        for document in documents:
            with self.subTest(document=document):
                self.path.write_text(json.dumps(document))
                with self.assertRaises(ValueError):
                    self.Panel._read_config_file(self.path)
        self.path.write_text('{"version":"1.0","rails":[{"net_name":"A"}]}')
        self.assertEqual(self.Panel._read_config_file(self.path), [PowerRail("A")])

    def test_missing_regulator_output_is_rejected_before_replacing_edits(self):
        rail = PowerRail("A", 12, child_regulators=[VoltageRegulator("U1", "A", "U1", ["1"], "MISSING", "U1", ["2"])])
        save_config([rail], str(self.path))
        with self.assertRaisesRegex(ValueError, "missing or incorrect rail"):
            self.Panel._read_config_file(self.path)

    def test_import_accepts_literal_unicode_with_optional_utf8_bom(self):
        document = {"version": "1.1", "rails": [{"net_name": "Versorgung µ", "nominal_voltage": 5}]}
        self.path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8-sig")
        self.assertEqual(self.Panel._read_config_file(self.path)[0].net_name, "Versorgung µ")

    def test_file_dialog_styles_extension_and_destruction(self):
        calls, destroyed = [], []
        def dialog(parent, title, **kwargs):
            calls.append((title, kwargs))
            return NS(ShowModal=lambda: self.namespace["wx"].ID_OK, GetPath=lambda: str(self.path.with_name("custom")),
                      Destroy=lambda: destroyed.append(True))
        self.namespace["wx"].FileDialog = dialog
        self.panel._get_config_path = lambda: self.path
        selected = self.panel._choose_config_file(save=True)
        self.assertEqual(selected.name, "custom.kipida.json")
        self.assertEqual(calls[-1][1]["style"], 6)
        self.assertEqual(calls[-1][1]["defaultDir"], str(self.path.parent))
        self.assertEqual(calls[-1][1]["defaultFile"], self.path.name)
        self.panel._choose_config_file()
        self.assertEqual(calls[-1][1]["style"], 24)
        self.assertEqual(destroyed, [True, True])

    def test_extension_completion_does_not_silently_overwrite_an_existing_file(self):
        self.path.write_text("existing")
        self.namespace["wx"].FileDialog = lambda *args, **kwargs: NS(
            ShowModal=lambda: self.namespace["wx"].ID_OK,
            GetPath=lambda: str(self.path.with_name("named")), Destroy=lambda: None)
        self.assertIsNone(self.panel._choose_config_file(save=True))
        self.assertEqual(self.path.read_text(), "existing")
        self.assertIn("Replace the existing configuration", self.messages[-1][0])


if __name__ == "__main__":
    unittest.main()
