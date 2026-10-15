"""Log startup before importing optional libraries; surface launch failures."""
import ctypes
import logging
import os
from pathlib import Path
import sys
import tempfile


def run():
    log_path = Path(tempfile.gettempdir()) / 'kipida_entry.log'
    original_streams = sys.stdout, sys.stderr
    try:
        logging.basicConfig(filename=str(log_path), level=logging.INFO,
                            format='%(asctime)s %(levelname)s %(message)s',
                            filemode='a', force=True)
        logging.info('KiPIDA launcher started; Python: %s', sys.executable)
        import ipc_entry
        logging.info('Dependencies imported successfully')
        ipc_entry.main()
        logging.info('KiPIDA closed')
    except Exception as exc:
        sys.stdout, sys.stderr = original_streams
        logging.exception('KiPIDA startup failed')
        message = f'KiPIDA could not start.\n\n{exc}\n\nDetails are in:\n{log_path}'
        if os.name == 'nt':
            ctypes.windll.user32.MessageBoxW(None, message, 'KiPIDA startup error', 0x40010)
        else:
            print(message, file=sys.stderr)
        return 1
    finally:
        sys.stdout, sys.stderr = original_streams
    return 0


if __name__ == '__main__':
    sys.exit(run())
