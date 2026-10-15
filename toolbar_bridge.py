"""KiCad 9 toolbar bridge; analysis still runs in the isolated IPC runtime."""
import logging
import os
from pathlib import Path
import subprocess
import tempfile

import pcbnew


def runtime_python(version):
    if os.name != 'nt':
        raise RuntimeError('This toolbar bridge currently supports Windows only. Use the IPC action on other platforms.')
    return Path(os.environ['LOCALAPPDATA'])/'KiCad'/version/'python-environments'/'com.github.kbralten.kipida'/'Scripts'/'pythonw.exe'


def launch_runtime(version, plugin_dir):
    """Use an argument list, without a shell, and report launch failures to KiCad."""
    directory = Path(plugin_dir).resolve()
    executable = runtime_python(version)
    entrypoint = directory/'launch.py'
    if not executable.is_file():
        raise RuntimeError(f'KiPIDA Python environment was not found:\n{executable}\nInstall KiPIDA using Plugin and Content Manager first.')
    if not entrypoint.is_file():
        raise RuntimeError(f'KiPIDA launcher was not found:\n{entrypoint}')
    return subprocess.Popen([str(executable), str(entrypoint)], cwd=str(directory),
                            env=os.environ.copy(), creationflags=subprocess.CREATE_NO_WINDOW)


class KiPIDAToolbarBridge(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = 'Ki-PIDA Simulation'
        self.category = 'Power integrity'
        self.description = 'Open KiPIDA (KiCad 9 toolbar bridge)'
        self.show_toolbar_button = True
        self.icon_file_name = str(Path(__file__).resolve().parent/'resources'/'kipida.png')

    def Run(self):
        path = Path(tempfile.gettempdir())/'kipida_toolbar.log'
        logger = logging.getLogger('kipida.toolbar')
        handler = logging.FileHandler(path, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            logger.info('Toolbar bridge invoked')
            process = launch_runtime(pcbnew.GetMajorMinorVersion(), Path(__file__).resolve().parent)
            logger.info('Started KiPIDA runtime, process %s', process.pid)
        except Exception as exc:
            logger.exception('Toolbar launch failed')
            import wx
            wx.MessageBox(f'{exc}\n\nStartup log: {path}', 'KiPIDA launch failed', wx.OK | wx.ICON_ERROR)
        finally:
            logger.removeHandler(handler)
            handler.close()
