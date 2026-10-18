#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Package tracked source and the checked-in Decky bundle; no npm needed on Deck."""
import argparse
import gzip
import hashlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bosun.core import VERSION
from install import FILES


def package(output):
    output.mkdir(parents=True, exist_ok=True)
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    paths = sorted(p for p in paths if p)
    if set(FILES) - set(paths):
        raise RuntimeError('Installation files must be tracked before packaging')
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip()
    archive = output / f'Bosun-{VERSION}.tar.gz'
    with archive.open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as tar:
            for name in paths + ['COMMIT']:
                source = ROOT / name
                if name != 'COMMIT' and (not source.is_file() or source.is_symlink()):
                    raise RuntimeError('Only regular tracked files can be released: ' + name)
                data = (commit + '\n').encode() if name == 'COMMIT' else source.read_bytes()
                info = tarfile.TarInfo(f'Bosun-{VERSION}/' + name)
                info.size = len(data)
                info.mode = 0o755 if name == 'Bosun-installer.desktop' or (name != 'COMMIT' and source.stat().st_mode & 0o111) else 0o644
                tar.addfile(info, io.BytesIO(data))
    # Verify the same allow-list consumed by install.py before publishing an asset.
    with tarfile.open(archive) as tar:
        for name in FILES:
            if tar.extractfile(f'Bosun-{VERSION}/' + name).read() != (ROOT / name).read_bytes():
                raise RuntimeError('Package verification failed: ' + name)
    launcher = output / 'Bosun-installer.desktop'
    shutil.copyfile(ROOT / launcher.name, launcher)
    launcher.chmod(0o755)
    sums = output / 'SHA256SUMS'
    sums.write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' for p in [archive, launcher]))
    print(f'Bosun {VERSION}: source + built plugin verified; {archive}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'release')
    package(parser.parse_args().output.resolve())
