"""Exercise config file actions with real wx controls without changing a PCB."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import wx

from config_manager import load_config, save_config
from models import PowerRail, ComponentRef, UnifiedSource
from ui.power_tree_panel import PowerTreePanel


app = wx.App(False)
frame = wx.Frame(None)
panel = PowerTreePanel(frame, None)
try:
    with tempfile.TemporaryDirectory() as directory:
        default = Path(directory) / "board.kipida.json"
        named = Path(directory) / "full load.kipida.json"
        panel.project = SimpleNamespace(path=directory, name="board")
        panel.rails = [PowerRail("12V", 12, sources=[UnifiedSource(ComponentRef("J1"), ["1"])])]
        panel.rail_list.Append("12V")
        panel.rail_list.SetSelection(0)
        panel.on_rail_select(None)
        panel.on_save_config(None)
        assert default.exists() and panel.config_path == default
        assert panel.config_name.GetLabel() == "Config: board.kipida.json"
        assert panel.btn_load_config.GetLabel() == "Open Config..."
        assert panel.btn_save_config_as.GetLabel() == "Save As..."

        # Construct the actual platform file dialog once to verify supported flags.
        native = wx.FileDialog(panel, "Save config", defaultDir=directory, defaultFile=named.name,
                               wildcard="KiPIDA configurations (*.kipida.json)|*.kipida.json",
                               style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        assert native.GetDirectory() == directory
        assert native.GetFilename() == named.name
        native.Destroy()

        def choose(path, result=wx.ID_OK):
            return SimpleNamespace(ShowModal=lambda: result, GetPath=lambda: str(path), Destroy=lambda: None)

        with patch.object(wx, "FileDialog", return_value=choose(named)):
            panel.on_save_config_as(None)
        assert panel.config_path == named and named.exists()
        panel.rails[0].nominal_voltage = 11.5
        panel.on_save_config(None)
        assert load_config(str(named))[0].nominal_voltage == 11.5
        assert load_config(str(default))[0].nominal_voltage == 12

        # Cancel leaves all unsaved state and the removal undo stack intact.
        panel.removal_history.append("marker")
        old_rails = panel.rails
        with patch.object(wx, "FileDialog", return_value=choose(default, wx.ID_CANCEL)):
            panel.on_load_config(None)
            panel.on_save_config_as(None)
        assert panel.rails is old_rails and panel.removal_history == ["marker"]
        assert panel.config_path == named

        broken = Path(directory) / "broken.json"
        broken.write_text('{"version":"1.1","rails":[{"net_name":"OK"},{"missing":"name"}]}')
        with patch.object(wx, "FileDialog", return_value=choose(broken)), patch.object(wx, "MessageBox"):
            panel.on_load_config(None)
        assert panel.rails is old_rails and panel.removal_history == ["marker"]
        assert panel.config_path == named

        with patch.object(wx, "FileDialog", return_value=choose(default)):
            panel.on_load_config(None)
        assert panel.config_path == default and panel.active_rail.net_name == "12V"
        assert panel.active_rail.nominal_voltage == 12 and not panel.removal_history
        assert panel.comp_list.GetItemCount() == 1 and not panel.btn_undo_remove.IsEnabled()

        empty = Path(directory) / "empty.kipida.json"
        save_config([], str(empty))
        with patch.object(wx, "FileDialog", return_value=choose(empty)):
            panel.on_load_config(None)
        assert panel.rails == [] and panel.active_rail is None
        assert not panel.detail_panel.IsEnabled() and panel.comp_list.GetItemCount() == 0
        print("Real wx config file checks passed: native dialog flags, named saves, current-file Save, "
              "cancel/error preservation, undo reset, and empty configuration handling.")
finally:
    frame.Destroy()
    app.ProcessPendingEvents()
