#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""SteamOS installer/update/uninstall. Uses only Python, sudo and systemd."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

REPO = 'ISAIandCO/Bosun'
APP = Path('/home/.bosun/app')
BASE = APP.parent
FILES = ['bosun.py', 'bosun/__init__.py', 'bosun/core.py', 'bosun/measure.py', 'bosun/sessions.py', 'bosun/control.py', 'bosun/dependencies.py', 'bosun/tweaks.py', 'bosun/launch.py',
         'install.py', 'README.md', 'LICENSE', 'docs/tweaks.md', 'docs/testing.md', 'docs/benchmark-metadata.json', 'docs/hardware-and-tweaks.md',
         'docs/global-analysis.md', 'docs/catalog.md', 'docs/releases/0.3.0.md',
         'decky/main.py', 'decky/plugin.json', 'decky/package.json', 'decky/dist/index.js',
         'decky/NOTICE', 'decky/LICENSES/Decky-LGPL-2.1.txt',
         'decky/third-party/loader-api/src/index.ts', 'decky/third-party/loader-api/src/types.ts',
         'decky/third-party/loader-api/src/types.d.ts', 'decky/third-party/loader-api/package.json']


def download(revision=None):
    headers = {'User-Agent': 'Bosun-installer', 'Accept': 'application/vnd.github+json'}
    sha = revision
    if sha is None:
        req = urllib.request.Request(f'https://api.github.com/repos/{REPO}/commits/main', headers=headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            sha = json.load(response)['sha']
    if len(sha) != 40 or any(c not in '0123456789abcdef' for c in sha):
        raise RuntimeError('Invalid GitHub commit ID')
    req = urllib.request.Request(f'https://api.github.com/repos/{REPO}/tarball/{sha}', headers=headers)
    with urllib.request.urlopen(req, timeout=120) as response:
        archive = response.read(20 * 1024 * 1024 + 1)
    if len(archive) > 20 * 1024 * 1024:
        raise RuntimeError('Archive exceeds expected size')
    temp = tempfile.TemporaryDirectory(prefix='bosun-download-')
    stage = Path(temp.name)
    # Read only explicit regular files, no archive symlinks, traversal or device nodes.
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        for file in FILES:
            matches = [m for m in tar.getmembers() if '/'.join(Path(m.name).parts[1:]) == file]
            if len(matches) != 1 or not matches[0].isfile() or matches[0].size > 2 * 1024 * 1024:
                raise RuntimeError(f'Missing/invalid archive file: {file}')
            target = stage / file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(matches[0]).read())
    (stage / 'COMMIT').write_text(sha + '\n')
    return temp, stage


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def desktop_exec(args):
    # Desktop Entry string unescaping occurs before Exec argument unquoting.
    def quote(arg):
        out = '"'
        for char in arg.replace('%', '%%'):
            if char in '\\"`$':
                out += '\\'
            out += char
        return (out + '"').replace('\\', '\\\\').replace('\n', '\\n').replace('\t', '\\t')
    return ' '.join(quote(arg) for arg in args)


def desktop_entry(args, name='Bosun'):
    return ('[Desktop Entry]\nType=Application\nName=' + name + '\nComment=Steam Deck performance per watt\n'
            'Icon=steamdeck-gaming-return\nTerminal=true\nStartupNotify=false\nCategories=Game;Settings;\nExec='
            + desktop_exec(args) + '\n')


def unit_files():
    return {
        '/etc/systemd/system/bosun-session.path':
        '[Unit]\nDescription=Watch for Bosun game profile requests\n'
        '[Path]\nPathChanged=/home/.bosun/requests/request.json\nUnit=bosun-session.service\n'
        '[Install]\nWantedBy=multi-user.target\n',
        '/etc/systemd/system/bosun-session.service':
        '[Unit]\nDescription=Apply or restore one validated Bosun game profile\nStartLimitIntervalSec=10\nStartLimitBurst=30\n'
        '[Service]\nType=oneshot\nExecStart=/usr/bin/python3 /home/.bosun/app/bosun.py broker\n'
        'TimeoutStartSec=45\n',
        '/etc/systemd/system/bosun-restore.service':
        '[Unit]\nDescription=Restore Bosun system tuning after capability check\n'
        'After=local-fs.target systemd-sysctl.service\nConditionPathExists=/home/.bosun/state/state.json\n'
        '[Service]\nType=oneshot\nExecStart=/usr/bin/python3 /home/.bosun/app/bosun.py reconcile\n'
        '[Install]\nWantedBy=multi-user.target\n',
        '/etc/systemd/system/bosun-resume.service':
        '[Unit]\nDescription=Verify Bosun tuning after sleep\nBefore=sleep.target\nStopWhenUnneeded=yes\n'
        '[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/bin/true\n'
        'ExecStop=/usr/bin/python3 /home/.bosun/app/bosun.py reconcile\n'
        '[Install]\nWantedBy=sleep.target\n',
    }


def install(source, username, decky_enabled=True):
    sys.path.insert(0, str(source))
    from bosun.core import System, atomic
    system = System()
    system.check_device()
    user = pwd.getpwnam(username)
    if user.pw_uid == 0:
        raise RuntimeError('Specify the desktop user, not root')
    homedir = Path(user.pw_dir)
    manifestfile = BASE / 'install.json'
    manifest = json.loads(manifestfile.read_text()) if manifestfile.exists() else {'files': {}}
    if manifest.get('user', username) != username:
        raise RuntimeError('Installation belongs to another desktop user')
    wrappers = {
        str(homedir / '.local/bin/bosun'): '#!/bin/sh\nexec /usr/bin/python3 /home/.bosun/app/bosun.py "$@"\n',
        str(homedir / '.local/share/applications/bosun.desktop'): desktop_entry(['/usr/bin/python3', str(APP / 'bosun.py'), 'menu']),
        str(homedir / 'Desktop/Bosun.desktop'): desktop_entry(['/usr/bin/python3', str(APP / 'bosun.py'), 'menu']),
    }
    content = {**unit_files(), **wrappers}
    for file, text in content.items():
        p = Path(file)
        expected = manifest['files'].get(file)
        if p.is_symlink():
            raise RuntimeError('Refusing symlink: ' + file)
        if p.exists() and (expected is None or digest(p) != expected):
            raise RuntimeError('File changed outside Bosun; retain it and resolve manually: ' + file)
    BASE.mkdir(mode=0o755, parents=True, exist_ok=True)
    if BASE.is_symlink() or BASE.stat().st_uid != 0 or BASE.stat().st_mode & 0o022:
        raise RuntimeError('/home/.bosun must be root-owned and not writable by other users')
    (BASE / 'state').mkdir(mode=0o700, exist_ok=True)
    requests = BASE / 'requests'
    requests.mkdir(mode=0o700, exist_ok=True)
    os.chown(requests, user.pw_uid, user.pw_gid)
    (BASE / 'responses').mkdir(mode=0o755, exist_ok=True)
    # Updates and tuning transactions share the same lock.
    import fcntl
    with (BASE / 'state/lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        system.restore_readonly()
        if decky_enabled:
            from bosun.dependencies import ensure_decky
            homebrew, messages = ensure_decky(system, user)
            print('\n'.join(messages))
            for rel in ['main.py', 'plugin.json', 'package.json', 'dist/index.js', 'NOTICE', 'LICENSES/Decky-LGPL-2.1.txt', 'third-party/loader-api/src/index.ts', 'third-party/loader-api/src/types.ts', 'third-party/loader-api/src/types.d.ts', 'third-party/loader-api/package.json']:
                file = str(homebrew / 'plugins/Bosun' / rel)
                p = Path(file)
                if p.is_symlink() or (p.exists() and digest(p) != manifest['files'].get(file)):
                    raise RuntimeError('Decky plugin file changed externally: ' + file)
                content[file] = (source / 'decky' / rel).read_text()
        previous = BASE / 'previous'
        nextdir = BASE / 'next'
        if nextdir.exists():
            shutil.rmtree(nextdir)
        nextdir.mkdir(mode=0o755)
        for file in FILES:
            target = nextdir / file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source / file).read_bytes())
            target.chmod(0o644)
        if (source / 'COMMIT').exists():
            shutil.copyfile(source / 'COMMIT', nextdir / 'COMMIT')
        subprocess.run(['/usr/bin/python3', '-m', 'py_compile', str(nextdir / 'bosun.py'), str(nextdir / 'bosun/core.py')], check=True)
        backups = {file: (Path(file).read_text(), Path(file).stat()) if Path(file).exists() else None for file in content}
        swapped = False
        try:
            if previous.exists():
                shutil.rmtree(previous)
            if APP.exists():
                APP.rename(previous)
            nextdir.rename(APP)
            swapped = True
            with system.writable():
                for file, text in content.items():
                    atomic(file, text, 0o755 if file in wrappers else 0o644)
            for file in wrappers:
                p = Path(file)
                os.chown(p, user.pw_uid, user.pw_gid)
                for parent in p.parents:
                    if parent == homedir:
                        break
                    os.chown(parent, user.pw_uid, user.pw_gid)
            system.command('systemctl', 'daemon-reload')
            with system.writable():
                system.command('systemctl', 'enable', 'bosun-restore.service', 'bosun-resume.service', 'bosun-session.path')
            system.command('systemctl', 'start', 'bosun-session.path')
            atomic(manifestfile, json.dumps({'user': username, 'files': {**manifest['files'], **{f: digest(Path(f)) for f in content}}}, indent=2))
        except BaseException:
            if swapped:
                shutil.rmtree(APP)
                if previous.exists():
                    previous.rename(APP)
            with system.writable():
                for file, backup in backups.items():
                    if backup is None:
                        Path(file).unlink(missing_ok=True)
                    else:
                        text, info = backup
                        atomic(file, text, info.st_mode & 0o777)
                        os.chown(file, info.st_uid, info.st_gid)
            if not manifestfile.exists():
                system.command('systemctl', 'disable', 'bosun-restore.service', 'bosun-resume.service', 'bosun-session.path', check=False)
                system.command('systemctl', 'stop', 'bosun-session.path', check=False)
            system.command('systemctl', 'daemon-reload', check=False)
            raise
    if decky_enabled:
        ensure_decky(system, user, start=True)
    print('Bosun installed. Open Desktop/Bosun.desktop or ~/.local/bin/bosun. No tuning applied by installation.')


def uninstall():
    sys.path.insert(0, str(APP))
    from bosun.core import System, Engine
    system = System()
    engine = Engine(system)
    with engine.locked():
        from bosun.control import Control
        Control(system).check_launch_rollback()
        notes, conflict = engine.apply('stock', {})
        print('\n'.join(notes))
        if conflict or engine.state['entries']:
            raise RuntimeError('Rollback has conflicts. Resolve them before uninstall; application retained.')
        # Decky's unload callback must not restore the pre-game profile after uninstall.
        engine.state.pop('session', None)
        engine.save()
        from bosun.control import Control
        control = Control(system)
        config = control.config()
        config['automatic'] = False
        control.write_config(config)
        manifest = json.loads((BASE / 'install.json').read_text())
        conflicts = [file for file, sha in manifest['files'].items() if Path(file).exists() and digest(Path(file)) != sha]
        if conflicts:
            raise RuntimeError('Installer files changed externally; retained: ' + ', '.join(conflicts))
        with system.writable():
            system.command('systemctl', 'disable', 'bosun-restore.service', 'bosun-resume.service', 'bosun-session.path')
        system.command('systemctl', 'stop', 'bosun-session.path')
        with system.writable():
            for file in manifest['files']:
                Path(file).unlink(missing_ok=True)
        system.command('systemctl', 'daemon-reload')
        shutil.rmtree(APP)
        had_plugin = any('/plugins/Bosun/' in f for f in manifest['files'])
        (BASE / 'install.json').unlink()
    if had_plugin:
        system.command('systemctl', 'restart', 'plugin_loader.service', check=False, timeout=60)
    print('Bosun removed. Original tuning restored where owned; journal/recovery copy retained in /home/.bosun/state. Reboot if boot settings changed.')


def main():
    p = argparse.ArgumentParser(description='Install/update Bosun for SteamOS')
    p.add_argument('--source', type=Path)
    p.add_argument('--user', default=os.environ.get('SUDO_USER') or os.environ.get('USER'))
    p.add_argument('--update', action='store_true')
    p.add_argument('--uninstall', action='store_true')
    p.add_argument('--no-decky', action='store_true', help='Skip Decky install/update; an existing Bosun plugin is retained')
    args = p.parse_args()
    temp = None
    if args.uninstall:
        if os.geteuid() != 0:
            return subprocess.call(['sudo', '--', '/usr/bin/python3', str(APP / 'install.py'), '--uninstall', '--user', args.user])
        uninstall()
        return 0
    source = args.source
    if source is None:
        local = Path(__file__).resolve().parent
        if not args.update and (local / 'bosun.py').is_file():
            source = local
        else:
            temp, source = download()
    try:
        if any(not (source / file).is_file() for file in FILES):
            # A pre-0.2 downloader extracted its older file list. Obtain the new files
            # from that same commit, never mix the staged update with a newer main.
            if not (source / 'COMMIT').is_file():
                raise RuntimeError('Incomplete installation source; rerun the current desktop installer')
            revision = (source / 'COMMIT').read_text().strip()
            temp, source = download(revision=revision)
        if os.geteuid() != 0 or (source / 'install.py').resolve() != Path(__file__).resolve():
            prefix = ['sudo', '--'] if os.geteuid() != 0 else []
            return subprocess.call([*prefix, '/usr/bin/python3', str(source / 'install.py'), '--source', str(source), '--user', args.user, *(['--no-decky'] if args.no_decky else [])])
        install(source, args.user, decky_enabled=not args.no_decky)
    finally:
        if temp:
            temp.cleanup()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print('Bosun installer:', exc, file=sys.stderr)
        sys.exit(1)
