# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared control plane for Decky: bounded, validated JSON; event-driven sessions."""
import json
from pathlib import Path
import re
import uuid

from .core import CATALOG, GLOBAL_KEYS, POWER, PROFILES, STATE, BosunError, Change, Engine, atomic, plan, validate
from .sessions import session_action
from .launch import account_key, compose

REBOOT_KEYS = GLOBAL_KEYS


class Control:
    def __init__(self, system):
        self.system = system
        self.file = system.path(STATE + '/games.json')

    def config(self):
        return json.loads(self.file.read_text()) if self.file.exists() else {'schema': 1, 'automatic': True, 'games': {}}

    def write_config(self, data):
        atomic(self.file, json.dumps(data, indent=2, ensure_ascii=False))

    def profiles(self):
        result = dict(PROFILES)
        for path in self.system.path(STATE + '/profiles').glob('*.json'):
            data = json.loads(path.read_text())
            result[path.stem] = validate(data['settings'])
        return result

    def state(self):
        engine = Engine(self.system)
        info = engine.report()
        return {'report': info, 'config': self.config(), 'profiles': self.profiles(), 'catalog': CATALOG,
                'runtime_keys': [key for key in CATALOG if key not in REBOOT_KEYS],
                'power_conflicts': self.system.power_conflicts()}

    def save_game(self, appid, name, settings, resume=True):
        if not isinstance(appid, str) or not re.fullmatch('[1-9][0-9]{0,19}', appid):
            raise BosunError('Choose a valid game AppID')
        if not isinstance(name, str) or len(name) > 160:
            raise BosunError('Game name is too long')
        validate(settings)
        if type(resume) is not bool:
            raise BosunError('resume must be a boolean')
        forbidden = REBOOT_KEYS.intersection(settings)
        if forbidden:
            raise BosunError('These settings must be global: ' + ', '.join(sorted(forbidden)))
        engine = Engine(self.system)
        with engine.locked():
            config = self.config()
            config['games'][appid] = {'name': name, 'settings': settings, 'resume': bool(resume)}
            self.write_config(config)
        return {'notes': ['Game profile saved']}

    def remove_game(self, appid):
        engine = Engine(self.system)
        with engine.locked():
            config = self.config()
            config['games'].pop(appid, None)
            self.write_config(config)
        return self.activate(appid)

    def automatic(self, enabled):
        if type(enabled) is not bool:
            raise BosunError('automatic must be a boolean')
        engine = Engine(self.system)
        with engine.locked():
            config = self.config()
            config['automatic'] = bool(enabled)
            self.write_config(config)
        return {'notes': ['Automatic game profiles ' + ('enabled' if enabled else 'disabled')]}

    def preview(self, settings, game=False):
        engine = Engine(self.system)
        with engine.locked():
            checked = validate(settings)
            if game:
                if REBOOT_KEYS.intersection(checked):
                    raise BosunError('Game profiles accept runtime settings only')
                checked = {**engine.state['desired'], **checked}
            notes, conflicts = engine.apply('preview', checked, dry=True)
        return {'notes': notes, 'conflict': conflicts}

    def apply_global(self, name, settings):
        engine = Engine(self.system)
        with engine.locked():
            stale = engine.state['boot_id'] != self.system.read('/proc/sys/kernel/random/boot_id', '')
            if engine.state.get('session') and not stale:
                raise BosunError('Close the game or stop its profile before changing the global profile')
            notes, conflict = engine.apply(name, validate(settings))
            if stale and not conflict:
                engine.state.pop('session', None)
                engine.save()
        return {'notes': notes, 'conflict': conflict}

    def release(self, key):
        if key not in CATALOG:
            raise BosunError('Unknown setting')
        engine = Engine(self.system)
        with engine.locked():
            engine.recover()
            removed = engine.release(key)
            keys = {'governor', 'epp'} if key in {'governor', 'epp'} else {key}
            if key in {'dirty_background_mib', 'dirty_mib'}:
                keys = {'dirty_background_mib', 'dirty_mib'}
            session = engine.state.get('session')
            if session and session.get('owner') == 'decky':
                for k in keys:
                    session['previous']['desired'].pop(k, None)
                    session['settings'].pop(k, None)
                config = self.config()
                if session['appid'] in config['games']:
                    for k in keys:
                        config['games'][session['appid']]['settings'].pop(k, None)
                self.write_config(config)
                engine.save()
        return {'notes': ['Фактические значения сохранены; владение снято: ' + ', '.join(removed)]}

    def rollback(self):
        engine = Engine(self.system)
        with engine.locked():
            self.check_launch_rollback()
            notes, conflict = engine.apply('stock', {})
            if not conflict:
                engine.state.pop('session', None)
                engine.save()
                config = self.config()
                config['automatic'] = False
                self.write_config(config)
        return {'notes': notes, 'conflict': conflict}

    def measurement_tools(self):
        from .dependencies import install_package
        engine = Engine(self.system)
        with engine.locked():
            self.system.check_device()
            note = ('MangoHud is already installed' if self.system.which('mangohud') else
                    install_package(self.system, ['mangohud'], 'mangohud'))
        return {'notes': [note, 'See docs/testing.md for per-frame logging and battery A/B measurement.']}

    def check_launch_rollback(self):
        if self.config().get('launch_options'):
            raise BosunError('Restore Steam launch options in Decky → Bosun first (all affected Steam accounts). System journal and application retained.')

    def preview_launch(self, account, appid, current, options):
        key = account_key(account, appid)
        if not isinstance(current, str) or len(current) > 8192 or '\x00' in current:
            raise BosunError('Invalid current launch text')
        saved = self.config().get('launch_options', {}).get(key)
        original = saved['original'] if saved else current
        if saved and current not in {saved['last'], original}:
            raise BosunError('Launch options changed outside Bosun; keep the external value or restore explicitly')
        return compose(self.system, original, options)

    def prepare_launch(self, account, appid, current, target, mode='write', options=None):
        key = account_key(account, appid)
        if not isinstance(mode, str) or mode not in {'write', 'restore', 'keep'}:
            raise BosunError('Invalid launch operation')
        if any(not isinstance(s, str) or len(s) > 8192 or '\x00' in s for s in [current, target]):
            raise BosunError('Invalid launch text')
        engine = Engine(self.system)
        with engine.locked():
            self.system.check_device()
            config = self.config()
            entries = config.setdefault('launch_options', {})
            saved = entries.get(key)
            if mode == 'keep':
                entries.pop(key, None)
                self.write_config(config)
                return {'target': current, 'notes': ['External Steam launch options retained; ownership released']}
            if saved and current not in {saved['last'], saved['original']}:
                raise BosunError('Launch options changed outside Bosun; refusing overwrite')
            original = saved['original'] if saved else current
            notes = []
            if mode == 'restore':
                target = original
            elif options is not None:
                result = compose(self.system, original, options, install=True)
                target, notes = result['target'], result['notes']
            # Save before Steam API mutation; an interrupted frontend can restore it later.
            entries[key] = {'account': account, 'appid': appid, 'original': original,
                            'last': target, 'restoring': mode == 'restore', 'pending': True}
            self.write_config(config)
        return {'target': target, 'notes': notes}

    def finish_launch(self, account, appid, actual):
        key = account_key(account, appid)
        engine = Engine(self.system)
        with engine.locked():
            config = self.config()
            entries = config.get('launch_options', {})
            saved = entries.get(key)
            if not saved or actual != saved['last']:
                raise BosunError('Steam did not confirm the expected launch options; recovery record retained')
            if saved['restoring'] or saved['original'] == actual:
                entries.pop(key)
            else:
                saved['pending'] = False
            self.write_config(config)
        return {'notes': ['Steam launch options verified']}

    def activate(self, appid, resume=False):
        appid = str(appid or '')
        if appid and not re.fullmatch('[1-9][0-9]{0,19}', appid):
            raise BosunError('Invalid AppID')
        engine = Engine(self.system)
        with engine.locked():
            engine.recover()
            config = self.config()
            selected = config['games'].get(appid) if config['automatic'] else None
            session = engine.state.get('session')
            notes = []
            if session and session.get('owner') != 'decky':
                if selected:
                    raise BosunError('CLI launcher owns the active profile; use either Decky or launch options')
                return {'notes': []}
            if session and engine.state['boot_id'] != self.system.read('/proc/sys/kernel/random/boot_id', ''):
                previous = session['previous']
                result, conflict = engine.apply(previous['profile'], previous['desired'], automatic=True)
                notes.extend(result)
                if conflict:
                    return {'notes': notes, 'conflict': True}
                engine.state.pop('session', None)
                engine.save()
                session = None
            if selected and engine.state['fingerprint'] != self.system.fingerprint():
                return {'notes': ['SteamOS/kernel changed: automatic game profiles paused. Preview and apply a global profile to accept the new system baseline.'], 'conflict': True}
            if session and session.get('appid') == appid and selected:
                previous_settings = session.get('settings')
                if previous_settings == selected['settings'] and not resume:
                    return {'notes': ['Game profile is already active']}
                if resume and selected.get('resume', True):
                    # One explicit resume event. Accept only a known original/reset value;
                    # never continually overwrite Steam or another power controller.
                    for entry in engine.state['entries'].values():
                        change = Change(**entry['change'])
                        if change.key in POWER and change.current(self.system) == entry['original']:
                            entry['last'] = entry['original']
                    desired = {**session['previous']['desired'], **selected['settings']}
                    result, conflict = engine.apply('decky-' + appid, desired)
                    if conflict:
                        raise BosunError('; '.join(result))
                    return {'notes': result}
                if resume:
                    return {'notes': ['Resume replay disabled for this game']}
            if session:
                notes.extend(session_action(engine, 'session-end', 'game', session['token']))
            if selected:
                token = uuid.uuid4().hex
                notes.extend(session_action(engine, 'session-start', 'decky-' + appid, token, selected['settings']))
                engine.state['session'].update(owner='decky', appid=appid, settings=selected['settings'])
                engine.save()
            return {'notes': notes}
