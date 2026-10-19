# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared control plane for Decky: bounded, validated JSON; event-driven sessions."""
import json
from pathlib import Path
import re
import time
import uuid
from .presentation import LABELS, group

from .core import BOOT_KEYS, CATALOG, GLOBAL_KEYS, PROFILES, STATE, BosunError, Engine, atomic, plan, validate
from .sessions import apply_global as apply_global_profile, session_action
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
        profiles = self.profiles()
        runtime = [key for key in CATALOG if key not in REBOOT_KEYS]
        games = {name: {k: v for k, v in settings.items() if k in runtime} for name, settings in profiles.items()}
        return {'report': info, 'config': self.config(), 'profiles': profiles,
                'game_profiles': {name: settings for name, settings in games.items() if settings or name == 'stock'},
                'catalog': CATALOG, 'labels': LABELS, 'groups': {key: group(key) for key in CATALOG},
                'reboot_keys': sorted(BOOT_KEYS | {'zram_algorithm', 'zram_size_percent', 'memlock_kb'}),
                'runtime_keys': runtime,
                'power_conflicts': self.system.power_conflicts()}

    def save_profile(self, name, settings):
        if not isinstance(name, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}', name) or name in PROFILES:
            raise BosunError('Имя своего профиля: 1–64 латинских букв, цифр, _ или -; встроенные имена заняты')
        checked = validate(settings)
        with Engine(self.system).locked():
            atomic(self.system.path(STATE + '/profiles/' + name + '.json'), json.dumps({'schema': 1, 'settings': checked}, indent=2))
        return {'notes': ['Профиль сохранён: ' + name]}

    def remove_profile(self, name):
        if not isinstance(name, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}', name) or name in PROFILES:
            raise BosunError('Можно удалить только свой профиль')
        with Engine(self.system).locked():
            self.system.path(STATE + '/profiles/' + name + '.json').unlink(missing_ok=True)
        return {'notes': ['Профиль удалён: ' + name]}

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
            active = (engine.state.get('session') or {}).get('appid') == appid
        return self.activate(appid) if active else {'notes': ['Назначение игре удалено']}

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
                session = engine.state.get('session') or {}
                base = session.get('previous', {}).get('desired', engine.state['desired'])
                priority = set(session.get('settings', {})) | set(checked)
                checked = {**base, **checked}
            notes, conflicts = (engine.apply('preview', checked, dry=True, priority_keys=priority) if game else
                                apply_global_profile(engine, 'preview', checked, dry=True))
        return {'notes': notes, 'conflict': conflicts}

    def apply_global(self, name, settings):
        engine = Engine(self.system)
        with engine.locked():
            notes, conflict = apply_global_profile(engine, name, settings)
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

    def cpu_check(self):
        """Bounded observation; no writes, stress load, packages or background polling."""
        info = self.system.info()
        policies = self.system.glob('/sys/devices/system/cpu/cpufreq/policy[0-9]*')
        engine = Engine(self.system)
        notes = [f"CPU CHECK: Bosun {info['bosun']} / {info['model']} / {info['kernel']} / SteamOS {info['os'].get('VERSION_ID', '?')}",
                 f"profile={engine.state['profile']}; game={(engine.state.get('session') or {}).get('appid', 'none')}; "
                 f"automatic={self.config()['automatic']}; requested_cpu_max_mhz={engine.state['desired'].get('cpu_max_mhz', 'inherit')}"]
        conflicts = self.system.power_conflicts()
        if conflicts:
            notes.append('SKIP power settings: ' + ', '.join(conflicts))
        readings = {}
        for p in policies:
            source = next((k for k in ['cpuinfo_avg_freq', 'cpuinfo_cur_freq', 'scaling_cur_freq']
                           if self.system.read(p + '/' + k, '').isdigit()), None)
            readings[p] = {'source': source, 'peak': 0, 'limits': set()}
        for sample in range(10):
            for p, reading in readings.items():
                reading['limits'].add(self.system.read(p + '/scaling_max_freq', '?'))
                raw = self.system.read(p + '/' + reading['source'], '') if reading['source'] else ''
                if raw.isdigit():
                    reading['peak'] = max(reading['peak'], int(raw))
            if sample < 9:
                time.sleep(0.2)
        for p, reading in readings.items():
            driver = self.system.read(p + '/scaling_driver', '?')
            floor = self.system.read(p + '/scaling_min_freq', '?')
            ceiling = self.system.read(p + '/scaling_max_freq', '?')
            source = reading['source'] or 'unavailable'
            notes.append(f"{Path(p).name}: driver={driver}; governor={self.system.read(p + '/scaling_governor', '?')}; "
                         f"EPP={self.system.read(p + '/energy_performance_preference', '?')}; "
                         f"boost={self.system.read(p + '/boost', self.system.read('/sys/devices/system/cpu/cpufreq/boost', '?'))}")
            notes.append(f"min/max={floor}/{ceiling} kHz; peak={reading['peak']} kHz ({source})")
            notes.append(f"available_kHz={self.system.read(p + '/scaling_available_frequencies', 'continuous/not exported')}; "
                         f"related_cpus={self.system.read(p + '/related_cpus', '?')}")
            if len(reading['limits']) > 1:
                notes.append('WARNING: scaling_max_freq changed during observation: ' + ', '.join(sorted(reading['limits'])))
            if ceiling.isdigit() and reading['peak'] > int(ceiling):
                notes.append('WARNING: observed frequency exceeds policy ceiling; hardware enforcement is NOT confirmed')
            if source == 'scaling_cur_freq':
                notes.append('scaling_cur_freq may report a driver request, not a measured hardware clock')
            if driver == 'acpi-cpufreq':
                notes.append('Legacy ACPI P-states: a policy limit alone does not prove a hard boost/SMU limit')
        if not policies:
            notes.append('SKIP: no CPUFreq policies')
        notes.append('Observation only (~2 s), without added load. No exceedance in this short sample is not proof of enforcement; compare under game load.')
        return {'notes': notes}

    def recover_cpu(self):
        engine = Engine(self.system)
        with engine.locked():
            return {'notes': engine.release_cpu_recovery()}

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
            config = self.config()
            selected = config['games'].get(appid) if config['automatic'] else None
            notes = []
            try:
                notes.extend(engine.recover())
            except BosunError:
                # The configured game owns its CPU limits at this event. Archive a
                # stale CPU transaction, keep actual bounds and retry with a fresh
                # baseline; unrelated recovery conflicts still block safely.
                if not selected or 'cpu_max_mhz' not in selected['settings'] or not engine.report()['cpu_recovery']:
                    raise
                notes.extend(engine.release_cpu_recovery(preserve_desired=True))
            session = engine.state.get('session')
            if session and engine.state['boot_id'] != self.system.read('/proc/sys/kernel/random/boot_id', ''):
                previous = session['previous']
                result, conflict = engine.apply(previous['profile'], previous['desired'], automatic=not bool(selected), supersede_session=True)
                notes.extend(result)
                if conflict:
                    return {'notes': notes, 'conflict': True}
                session = None
            if session and session.get('owner') != 'decky' and not selected:
                return {'notes': notes}
            if selected and engine.state['fingerprint'] != self.system.fingerprint():
                notes.append('SteamOS/kernel changed: rechecking current interfaces before applying the saved game profile.')
            if session and session.get('appid') == appid and selected:
                if resume and not selected.get('resume', True):
                    return {'notes': notes + ['Resume replay disabled for this game']}
                desired = {**session['previous']['desired'], **selected['settings']}
                priority = set(session.get('settings', {})) | set(selected['settings'])
                result, conflict = engine.apply('decky-' + appid, desired, priority_keys=priority)
                notes.extend(result)
                if not conflict:
                    engine.state['session']['settings'] = selected['settings']
                    engine.save()
                return {'notes': notes, 'conflict': conflict}
            if session:
                notes.extend(session_action(engine, 'session-end', 'game', session['token']))
            if selected:
                token = uuid.uuid4().hex
                notes.extend(session_action(engine, 'session-start', 'decky-' + appid, token,
                                            selected['settings'], decky_appid=appid))
            return {'notes': notes}
