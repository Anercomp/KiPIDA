"""Connect to the running KiCad PCB editor and show the analysis dialog."""
import logging
import os
import sys
from types import SimpleNamespace
import wx
import kipy

plugin_dir = os.path.dirname(os.path.abspath(__file__))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)
from ui.main_dialog import KiPIDA_MainDialog


def main():
    logger = logging.getLogger('KiPIDA')
    app = wx.App(False)
    logger.info('Connecting to KiCad PCB editor')
    # kipy reads KiCad's socket/token environment and handles Windows defaults.
    client = kipy.KiCad(timeout_ms=3000)
    try:
        board = client.get_board()
        if board is None:
            raise RuntimeError('No PCB board returned by KiCad')
    except Exception as exc:
        logger.exception('Could not connect to the PCB editor')
        raise RuntimeError('Cannot connect to a PCB board. Open your board in the PCB Editor and enable the API.\n\n' + str(exc)) from exc
    project = None
    document = getattr(board, 'document', None)
    info = getattr(document, 'project', None)
    path = getattr(info, 'path', '')
    if path:
        project = SimpleNamespace(path=path, name=getattr(info, 'name', '') or os.path.basename(path))
    logger.info('Connected; constructing analysis dialog')
    old_streams = sys.stdout, sys.stderr
    dialog = None
    try:
        dialog = KiPIDA_MainDialog(None, board_adapter=board, project=project)
        dialog.log('[INIT] Connected to the KiCad PCB Editor.')
        logger.info('Showing analysis dialog')
        # KiCad launches Python without a console. Explicitly show the dialog once
        # its modal loop starts, including when Windows inherited a hidden state.
        def show_dialog():
            if os.name == 'nt':
                import ctypes
                user32 = ctypes.windll.user32
                user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
                user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
                user32.ShowWindow(dialog.GetHandle(), 5)
                user32.SetForegroundWindow(dialog.GetHandle())
            dialog.Raise()
            dialog.SetFocus()
            logger.info('Analysis dialog visible: %s', dialog.IsShown())
        wx.CallAfter(show_dialog)
        dialog.ShowModal()
    finally:
        sys.stdout, sys.stderr = old_streams
        if dialog is not None:
            dialog.Destroy()
    # ShowModal supplies the event loop; no second MainLoop after closing.


if __name__ == '__main__':
    from launch import run
    sys.exit(run())
