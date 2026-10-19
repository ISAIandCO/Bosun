# SPDX-License-Identifier: GPL-3.0-or-later
"""One-shot privileged calls for the unprivileged Qt window. No daemon or shell."""
import importlib
import json
import os
import subprocess

from .core import APP, BosunError, Engine

METHODS = {'state', 'save_game', 'remove_game', 'automatic', 'activate', 'preview', 'apply_global',
           'release', 'rollback', 'cpu_check', 'measurement_tools', 'save_profile', 'remove_profile'}


def dispatch(control, payload):
    if not isinstance(payload, dict) or set(payload) - {'method', 'args'}:
        raise BosunError('Invalid Desktop request')
    method, args = payload.get('method'), payload.get('args', [])
    if not isinstance(method, str) or not isinstance(args, list) or len(args) > 4:
        raise BosunError('Invalid Desktop method/arguments')
    if method == 'ensure_gui' and not args:
        from .dependencies import ensure_desktop
        control.system.check_device()
        with Engine(control.system).locked():
            return {'notes': ensure_desktop(control.system)}
    if method == 'update' and not args:
        control.system.check_device()
        user = json.loads(control.system.path('/home/.bosun/install.json').read_text())['user']
        control.system.command('systemd-run', '--collect', '--unit=bosun-update', '/usr/bin/python3',
                               APP + '/install.py', '--update', '--user', user)
        return {'notes': ['Обновление запущено. После завершения откройте Bosun заново. Журнал: journalctl -u bosun-update']}
    if method not in METHODS:
        raise BosunError('Desktop method is not allowed')
    return getattr(control, method)(*args)


def call(method, args=()):
    command = ['/usr/bin/python3', APP + '/bosun.py', 'desktop-rpc']
    if os.geteuid() != 0:
        command = ['pkexec', '--disable-internal-agent', *command]
    result = subprocess.run(command, input=json.dumps({'method': method, 'args': list(args)}),
                            text=True, capture_output=True, timeout=300)
    if result.returncode:
        raise BosunError(result.stderr.strip() or 'Авторизация отменена или системная операция завершилась ошибкой')
    return json.loads(result.stdout)


def main():
    try:
        importlib.import_module('PySide6.QtWidgets')
    except (ImportError, OSError):
        try:
            call('ensure_gui')
            importlib.invalidate_caches()
        except (BosunError, OSError, subprocess.SubprocessError) as exc:
            message = 'Не удалось подготовить графический интерфейс Bosun:\n' + str(exc)
            if __import__('shutil').which('kdialog'):
                subprocess.run(['kdialog', '--error', message], check=False)
            raise BosunError(message) from exc
    from .gui import run
    return run()
