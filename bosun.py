#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bosun command line and desktop terminal menu."""
import argparse
import json
import os
import pwd
import stat
import time
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid

from bosun.core import APP, STATE, CATALOG, PROFILES, POWER, VERSION, BosunError, Change, Engine, System, atomic, validate
from bosun.measure import capture, frame_metrics


def profile_dir():
    # Root only uses its own protected profiles, never executes user profile content.
    return Path(STATE + '/profiles') if os.geteuid() == 0 else Path.home() / '.config/bosun/profiles'


def profile(name):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', name):
        raise BosunError('Profile name must contain 1..64 letters, digits, _ or -')
    custom = profile_dir() / (name + '.json')
    if custom.exists():
        data = json.loads(custom.read_text())
        return validate(data['settings'])
    if name not in PROFILES:
        raise BosunError('Unknown profile: ' + name)
    return dict(PROFILES[name])


def root_call(*args, data=None, capture_output=False):
    command = ['/usr/bin/python3', APP + '/bosun.py', *args]
    if os.geteuid() != 0:
        command = ['sudo', '--', *command]
    result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                            text=True, capture_output=capture_output)
    if result.returncode:
        raise BosunError(result.stderr or f'Bosun action failed ({result.returncode})')
    return result.stdout if capture_output else ''


def emit(notes):
    for note in notes:
        print(note)


def menu():
    while True:
        print('\nBosun — One tool to tweak them all\n'
              '1  Фактическое состояние\n2  Предпросмотр рекомендуемого профиля\n'
              '3  Применить рекомендуемый профиль\n4  Профили игр / пользовательские\n'
              '5  Отключить отдельный твик\n6  Полный откат\n'
              '7  Обновить Bosun\n8  Список настроек / создание профиля\n9  Удалить Bosun\n0  Выход')
        try:
            choice = input('> ').strip()
            if choice == '0':
                return
            if choice == '1':
                root_call('status')
            elif choice in {'2', '3'}:
                root_call('apply', 'recommended', *(['--dry-run'] if choice == '2' else []))
            elif choice == '4':
                print('Встроенные:', ', '.join(PROFILES))
                print('Пользовательские:', ', '.join(p.stem for p in profile_dir().glob('*.json')))
                name = input('Профиль: ').strip()
                settings = profile(name)
                root_call('apply', name, '--stdin', '--dry-run', data=settings)
                if input('Применить? [y/N] ').strip().lower() == 'y':
                    root_call('apply', name, '--stdin', data=settings)
            elif choice == '5':
                root_call('disable', input('Имя настройки (см. пункт 8): ').strip())
            elif choice == '6':
                root_call('rollback')
            elif choice == '7':
                subprocess.run(['/usr/bin/python3', APP + '/install.py', '--update'], check=True)
            elif choice == '8':
                for k, (values, helptext) in CATALOG.items():
                    print(f'{k}: {values}\n  {helptext}')
                name = input('Имя нового профиля (Enter — назад): ').strip()
                if name:
                    base = input('Основа [recommended]: ').strip() or 'recommended'
                    settings = profile(base)
                    while True:
                        item = input('key=value; key=stock удаляет настройку; Enter — сохранить: ').strip()
                        if not item:
                            break
                        key, value = item.split('=', 1)
                        if value == 'stock':
                            settings.pop(key, None)
                        else:
                            settings[key] = parse_value(value)
                    save_profile(name, settings)
            elif choice == '9':
                subprocess.run(['/usr/bin/python3', APP + '/install.py', '--uninstall'], check=True)
                return
        except (BosunError, ValueError, OSError, subprocess.CalledProcessError) as exc:
            print('Ошибка:', exc)


def parse_value(value):
    return int(value) if re.fullmatch('-?[0-9]+', value) else value


def save_profile(name, settings):
    validate(settings)
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', name) or name in PROFILES:
        raise BosunError('Choose a custom profile name (letters, digits, _ and -)')
    path = profile_dir() / (name + '.json')
    atomic(path, json.dumps({'schema': 1, 'settings': settings}, indent=2) + '\n')
    print('Saved:', path)


def session_action(engine, action, name, token, overlay=None):
    if not re.fullmatch('[0-9a-f]{32}', token):
        raise BosunError('Invalid session token')
    engine.recover()
    if action == 'session-start':
        if engine.state.get('session'):
            raise BosunError('Another game session exists; close it or recover using token from status')
        overlay = validate(overlay)
        current = dict(engine.state['desired'])
        for key in ['mitigations', 'moverate', 'lbpw', 'lavd', 'smt']:
            if key in overlay and overlay[key] != current.get(key):
                raise BosunError('Game launch cannot change boot, scheduler or SMT. Use runtime-only game profiles.')
        previous = {'profile': engine.state['profile'], 'desired': current}
        engine.state['session'] = {'token': token, 'previous': previous}
        engine.save()
        settings = {**current, **overlay}
    else:
        session = engine.state.get('session')
        if not session or session['token'] != token:
            raise BosunError('Session token mismatch (see status)')
        settings, name = session['previous']['desired'], session['previous']['profile']
    try:
        notes, conflict = engine.apply(name, settings)
    except BaseException:
        if action == 'session-start' and not engine.state.get('pending'):
            engine.state.pop('session', None)
            engine.save()
        raise
    if conflict:
        raise BosunError('Session conflict; settings retained for explicit resolution: ' + '; '.join(notes))
    if action == 'session-end':
        engine.state.pop('session', None)
        engine.save()
    return notes


def broker_request(action, name, token, settings=None):
    request = Path('/home/.bosun/requests/request.json')
    response = Path('/home/.bosun/responses') / (token + '-' + action + '.json')
    # Only the fixed actions and validated settings cross this boundary; no game command.
    atomic(request, json.dumps({'action': action, 'profile': name, 'token': token, 'settings': settings}))
    deadline = time.monotonic() + 45
    while not response.exists():
        if time.monotonic() >= deadline:
            raise BosunError('Session helper timed out. Check systemctl status bosun-session.path and journalctl -u bosun-session.service; inspect status before retry.')
        time.sleep(.1)
    result = json.loads(response.read_text())
    if not result['ok']:
        raise BosunError(result['error'])
    emit(result['notes'])


def broker(system):
    manifest = json.loads(system.path('/home/.bosun/install.json').read_text())
    uid = pwd.getpwnam(manifest['user']).pw_uid
    path = system.path('/home/.bosun/requests/request.json')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != uid or not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise BosunError('Invalid session request owner/type/size')
        data = json.loads(stream.read(65537))
    token, action = data.get('token', ''), data.get('action', '')
    if not re.fullmatch('[0-9a-f]{32}', token) or action not in {'session-start', 'session-end'}:
        raise BosunError('Invalid session request token/action')
    response_path = system.path('/home/.bosun/responses/' + token + '-' + action + '.json')
    if response_path.exists():
        return  # inotify may coalesce/repeat events; each token/action runs at most once.
    result = {'ok': False}
    try:
        name = data.get('profile', 'game')
        if not isinstance(name, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}', name):
            raise BosunError('Invalid profile name')
        engine = Engine(system)
        with engine.locked():
            result = {'ok': True, 'notes': session_action(engine, action, name, token, data.get('settings'))}
    except (BosunError, OSError, ValueError) as exc:
        result['error'] = str(exc)
    atomic(system.path('/home/.bosun/responses/' + token + '-' + action + '.json'), json.dumps(result), 0o644)
    # Bounded response cache; no ongoing cleanup process.
    responses = sorted(system.path('/home/.bosun/responses').glob('*.json'), key=lambda p: p.stat().st_mtime)
    for old in responses[:-64]:
        old.unlink()


def game_run(args):
    if os.geteuid() == 0:
        raise BosunError('Run games as the desktop user, never with sudo')
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        raise BosunError('Usage: bosun run PROFILE -- GAME_COMMAND ...')
    settings = profile(args.profile)
    # Game sessions overlay current global settings. Boot changes are never made per game.
    token = uuid.uuid4().hex
    broker_request('session-start', args.profile, token, settings)
    try:
        child = subprocess.Popen(command)
        def forward(signum, _frame):
            child.send_signal(signum)
        old = {sig: signal.signal(sig, forward) for sig in [signal.SIGINT, signal.SIGTERM]}
        try:
            return child.wait()
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
    finally:
        broker_request('session-end', args.profile, token)


def main():
    parser = argparse.ArgumentParser(description='Bosun ' + VERSION)
    parser.add_argument('--root', default='/', help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest='action')
    sub.add_parser('menu')
    sub.add_parser('status')
    sub.add_parser('catalog')
    sub.add_parser('profiles')
    sub.add_parser('reconcile')
    sub.add_parser('recover')
    sub.add_parser('broker')
    for verb in ['apply', 'session-start']:
        p = sub.add_parser(verb)
        p.add_argument('profile')
        p.add_argument('--stdin', action='store_true')
        p.add_argument('--dry-run', action='store_true')
        if verb == 'session-start':
            p.add_argument('--token', required=True)
    p = sub.add_parser('session-end')
    p.add_argument('--token', required=True)
    p = sub.add_parser('rollback')
    p.add_argument('--dry-run', action='store_true')
    for verb in ['disable', 'release']:
        sub.add_parser(verb).add_argument('key', choices=list(CATALOG))
    p = sub.add_parser('profile-save')
    p.add_argument('name')
    p.add_argument('--base', default='recommended')
    p.add_argument('--set', action='append', default=[])
    p = sub.add_parser('run')
    p.add_argument('profile')
    p.add_argument('command', nargs=argparse.REMAINDER)
    p = sub.add_parser('capture')
    p.add_argument('--output', required=True)
    p.add_argument('--seconds', type=int, default=300)
    p.add_argument('--metadata', required=True, help='JSON file with test conditions')
    p = sub.add_parser('analyze')
    p.add_argument('csv')
    args = parser.parse_args()
    if args.action in {None, 'menu'}:
        menu()
        return 0
    if args.action == 'catalog':
        print(json.dumps(CATALOG, indent=2, ensure_ascii=False))
        return 0
    if args.action == 'profiles':
        print('\n'.join(list(PROFILES) + [p.stem for p in profile_dir().glob('*.json')]))
        return 0
    if args.action == 'profile-save':
        settings = profile(args.base)
        for item in args.set:
            key, val = item.split('=', 1)
            if val == 'stock':
                settings.pop(key, None)
            else:
                settings[key] = parse_value(val)
        save_profile(args.name, settings)
        return 0
    if args.action == 'run':
        return game_run(args)
    if args.action == 'analyze':
        print(json.dumps(frame_metrics(args.csv), indent=2))
        return 0
    # Reading a root-only journal goes through sudo as well; no passwordless sudo rule.
    if args.root == '/' and os.geteuid() != 0 and args.action not in {'capture'}:
        data = None
        if args.action == 'apply':
            data = profile(args.profile) if not args.stdin else json.load(sys.stdin)
            argv = ['apply', args.profile, '--stdin'] + (['--dry-run'] if args.dry_run else [])
        else:
            argv = sys.argv[1:]
        root_call(*argv, data=data)
        return 0
    system = System(args.root)
    if args.action == 'capture' and os.geteuid() != 0 and args.root == '/':
        from types import SimpleNamespace
        engine = SimpleNamespace()
    else:
        engine = Engine(system)
    if args.action == 'capture':
        # Unprivileged benchmark may not read protected state; ask for a read-only report first.
        if os.geteuid() != 0 and args.root == '/':
            report = json.loads(root_call('status', capture_output=True))
            def live_report():
                fresh = {**report, 'system': system.info(), 'journal_captured_at_start': True}
                fresh['managed'] = [{**entry, 'actual': Change(**entry['change']).current(system)} for entry in report['managed']]
                return fresh
            engine.report = live_report
        print(json.dumps(capture(system, engine, args.output, args.seconds,
                                 json.loads(Path(args.metadata).read_text())), indent=2))
        return 0
    if args.action == 'broker':
        if os.geteuid() != 0:
            raise BosunError('Broker is only invoked by systemd')
        broker(system)
        return 0
    if args.action == 'status':
        print(json.dumps(engine.report(), indent=2, ensure_ascii=False))
        return 0
    with engine.locked():
        if args.action == 'recover':
            emit(engine.recover())
            return 0
        if args.action == 'release':
            engine.recover()
            emit(engine.release(args.key))
            return 0
        settings, name = {}, 'stock'
        if args.action == 'apply':
            settings = validate(json.load(sys.stdin)) if args.stdin else profile(args.profile)
            name = args.profile
        elif args.action == 'disable':
            settings = dict(engine.state['desired'])
            settings.pop(args.key, None)
            name = engine.state['profile'] + ':custom'
        elif args.action == 'reconcile':
            settings, name = engine.state['desired'], engine.state['profile']
        elif args.action in {'session-start', 'session-end'}:
            overlay = validate(json.load(sys.stdin)) if args.action == 'session-start' else None
            emit(session_action(engine, args.action, getattr(args, 'profile', 'game'), args.token, overlay))
            return 0
        stale_session = bool(engine.state.get('session')) and engine.state['boot_id'] != system.read('/proc/sys/kernel/random/boot_id', '')
        if args.action == 'reconcile' and stale_session:
            settings = engine.state['session']['previous']['desired']
            name = engine.state['session']['previous']['profile']
        if args.action in {'apply', 'disable'} and engine.state.get('session') and not stale_session:
            raise BosunError('A game session is active; close it before switching global profiles')
        notes, conflict = engine.apply(name, settings, dry=getattr(args, 'dry_run', False),
                                       automatic=args.action == 'reconcile')
        emit(notes)
        if stale_session and not conflict and not getattr(args, 'dry_run', False):
            engine.state.pop('session', None)
            engine.save()
        if args.action == 'rollback' and not conflict and not args.dry_run:
            engine.state.pop('session', None)
            engine.save()
        return 2 if conflict else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (BosunError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print('Bosun:', exc, file=sys.stderr)
        sys.exit(1)
