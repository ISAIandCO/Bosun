# SPDX-License-Identifier: GPL-3.0-or-later
"""Install only requested tools; never replace an installed dependency or use foreign repos."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.request

from .core import BOOT_KEYS, BosunError, STATE, atomic


def fetch(url, limit=100 * 1024 * 1024):
    if not url.startswith('https://'):
        raise BosunError('Component downloads require HTTPS')
    req = urllib.request.Request(url, headers={'User-Agent': 'Bosun', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=120) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise BosunError('Component download exceeds size limit')
    return data


def install_package(system, candidates, executable, probe=()):
    if not system.which('pacman'):
        raise BosunError(f'Missing {executable}; SteamOS pacman unavailable')
    package = next((name for name in candidates if system.command('pacman', '-Si', name, check=False)), None)
    if package is None:
        raise BosunError(f'{executable}: no provider in configured SteamOS repositories ({", ".join(candidates)}). Update SteamOS repositories through the normal OS update, then retry.')
    with system.writable():
        # --print performs dependency solving without installation. Refuse upgrades/downgrades
        # of installed components instead of doing a partial SteamOS upgrade behind the user.
        output = system.command('pacman', '-Sp', '--print-format', '%n %v', package, timeout=120)
        rows = [line.split() for line in output.splitlines()]
        if not rows or any(len(row) != 2 or not re.fullmatch('[a-zA-Z0-9@+_.-]+', row[0]) for row in rows):
            raise BosunError(f'Cannot obtain a package-only transaction for {package}')
        names = [row[0] for row in rows]
        replacements = []
        for name, version in rows:
            installed = system.command('pacman', '-Q', name, check=False).split()
            if installed and not (name == package and installed == [name, version]):
                replacements.append(name)
        if replacements:
            raise BosunError('Dependency installation would replace installed packages: ' + ', '.join(replacements) + '. Update SteamOS first; no packages changed.')
        system.command('pacman', '-S', '--noconfirm', package, timeout=240)
    available = (system.command(*probe, check=False) == 'ready' if probe else system.which(executable)) if not system.fake else True
    if not available and not (executable == 'zram-generator' and system.path('/usr/lib/systemd/system-generators/zram-generator').exists()):
        raise BosunError(f'{package} installed but {executable} is not available')
    history = system.path(STATE + '/dependencies.json')
    data = json.loads(history.read_text()) if history.exists() else []
    data.append({'package': package, 'transaction': names})
    atomic(history, json.dumps(data, indent=2))
    return f'Installed {package} from configured SteamOS repository'


def ensure_for(system, settings):
    messages = []
    if settings.get('home_noatime') is not None and (not system.which('mount') or not system.which('findmnt')):
        # In fixtures there is no host mount tool: never install into a synthetic /sys.
        if not system.fake:
            messages.append(install_package(system, ['util-linux'], 'findmnt'))
    if settings.get('lavd') and not system.which('scx_lavd') and system.path('/sys/kernel/sched_ext/state').exists() and system.path('/sys/kernel/btf/vmlinux').exists():
        messages.append(install_package(system, ['scx-scheds', 'scx'], 'scx_lavd'))
    if any(k in settings for k in ['zram_algorithm', 'zram_size_percent']):
        generator = system.path('/usr/lib/systemd/system-generators/zram-generator')
        if not generator.exists() and not system.which('zram-generator'):
            owner = system.command('systemctl', 'is-active', 'systemd-zram-setup@zram0.service', check=False) == 'active'
            if '/dev/zram' in system.read('/proc/swaps', '') and not owner:
                messages.append('SKIP zram dependency: active zram uses another implementation; no live replacement')
            else:
                messages.append(install_package(system, ['zram-generator'], 'zram-generator'))
    if (BOOT_KEYS - {'lbpw'}).intersection(settings):
        if not system.which('grub-mkconfig') or not system.which('grub-script-check'):
            messages.append(install_package(system, ['grub'], 'grub-mkconfig'))
    return messages


def ensure_desktop(system):
    probe = ('/usr/bin/python3', '-c', 'from PySide6 import QtWidgets; print("ready")')
    notes = []
    if system.command(*probe, check=False) != 'ready':
        # PySide tooling may be a separate package; only the actual QtWidgets import matters.
        notes.append(install_package(system, ['pyside6', 'python-pyside6'], 'PySide6.QtWidgets', probe=probe))
    if not system.which('pkexec'):
        notes.append(install_package(system, ['polkit'], 'pkexec'))
    return notes


def decky_home(system, user_home):
    text = system.read('/etc/systemd/system/plugin_loader.service', '')
    match = re.search(r'^Environment=["\']?UNPRIVILEGED_PATH=([^\n"\']+)', text, re.M)
    return Path(match[1]) if match else Path(user_home) / 'homebrew'


def ensure_decky(system, user, start=False):
    """Official release binary and same service contract as upstream installer; preserve plugins."""
    homebrew = decky_home(system, user.pw_dir)
    loader = homebrew / 'services/PluginLoader'
    unit = system.path('/etc/systemd/system/plugin_loader.service')
    messages = []
    if not loader.exists():
        if system.fake:
            raise BosunError('Decky download disabled in test root; mock installer transport')
        release = json.loads(fetch('https://api.github.com/repos/SteamDeckHomebrew/decky-loader/releases/latest', 2 * 1024 * 1024))
        asset = next((a for a in release.get('assets', []) if a['name'] == 'PluginLoader'), None)
        if not asset:
            raise BosunError('Official Decky release has no x86_64 PluginLoader asset')
        data = fetch(asset['browser_download_url'])
        if data[:4] != b'\x7fELF' or data[4:6] != b'\x02\x01' or data[18:20] != b'\x3e\x00':
            raise BosunError('Decky asset is not an x86_64 ELF executable')
        if (asset.get('digest') or '').startswith('sha256:') and hashlib.sha256(data).hexdigest() != asset['digest'][7:]:
            raise BosunError('Decky release digest mismatch')
        loader.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix='.bosun-loader-', dir=loader.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, 0o755)
            os.replace(temp, loader)
        finally:
            Path(temp).unlink(missing_ok=True)
        atomic(loader.parent / '.loader.version', release['tag_name'] + '\n', 0o644)
        messages.append('Installed official Decky ' + release['tag_name'])
    if not unit.exists():
        quoted = str(homebrew).replace('%', '%%').replace('"', '\\"')
        text = ('[Unit]\nDescription=SteamDeck Plugin Loader\nAfter=network.target\n'
                '[Service]\nType=simple\nUser=root\nRestart=always\nKillMode=process\nTimeoutStopSec=15\n'
                f'ExecStart="{quoted}/services/PluginLoader"\nWorkingDirectory="{quoted}/services"\n'
                f'Environment="UNPRIVILEGED_PATH={quoted}"\nEnvironment="PRIVILEGED_PATH={quoted}"\nEnvironment=LOG_LEVEL=INFO\n'
                '[Install]\nWantedBy=multi-user.target\n')
        with system.writable():
            atomic(unit, text, 0o644)
    for directory in [homebrew, homebrew / 'plugins', homebrew / 'services']:
        existed = directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        if not existed:
            os.chown(directory, user.pw_uid, user.pw_gid)
    # Same Steam CEF opt-in used by upstream Decky installer. Existing flags are retained.
    flag = Path(user.pw_dir) / '.steam/steam/.cef-enable-remote-debugging'
    flag.parent.mkdir(parents=True, exist_ok=True)
    if not flag.exists():
        flag.touch()
        os.chown(flag, user.pw_uid, user.pw_gid)
    if start:
        system.command('systemctl', 'daemon-reload')
        with system.writable():
            system.command('systemctl', 'enable', 'plugin_loader.service')
        system.command('systemctl', 'restart', 'plugin_loader.service', timeout=60)
    return homebrew, messages
