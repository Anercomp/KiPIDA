"""Install the validated local runtime payload, preserving replaced files."""
from pathlib import Path
import zipfile
from datetime import datetime

root = Path(__file__).resolve().parent
target = Path(r'C:\Users\Anercomp\Documents\KiCad\9.0\3rdparty\plugins\com_github_kbralten_kipida').resolve()
package = root.parent / 'KiPIDA-PCM.zip'
backup = root / ('installed-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.zip')
with zipfile.ZipFile(package) as release:
    assert release.testzip() is None
    files = []
    for name in release.namelist():
        if not name.startswith('plugins/') or name.endswith('/'):
            continue
        relative = Path(name[len('plugins/'):])
        destination = (target / relative).resolve()
        assert destination.is_relative_to(target)
        files.append((name, relative, destination))
    with zipfile.ZipFile(backup, 'w', zipfile.ZIP_DEFLATED) as archive:
        for _, relative, destination in files:
            if destination.is_file():
                archive.write(destination, relative.as_posix())
    for name, _, destination in files:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(release.read(name))
    for name, _, destination in files:
        assert destination.read_bytes() == release.read(name)
print(f'Installed and verified {len(files)} runtime files in {target}')
print(f'Previous runtime backup: {backup}')
