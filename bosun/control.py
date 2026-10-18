# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared control plane for Decky: bounded, validated JSON; event-driven sessions."""
import json
from pathlib import Path
import re
import uuid

from .core import CATALOG, POWER, PROFILES, STATE, BosunError, Change, Engine, atomic, plan, validate
from .sessions import session_action

REBOOT_KEYS = {'mitigations', 'moverate', 'lbpw', 'workqueue_power_efficient', 'zram_algorithm', 'zram_size_percent', 'lavd', 'smt'}


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
            notes, conflict = engine.apply('stock', {})
            if not conflict:
                engine.state.pop('session', None)
                engine.save()
                config = self.config()
                config['automatic'] = False
                self.write_config(config)
        return {'notes': notes, 'conflict': conflict}

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
