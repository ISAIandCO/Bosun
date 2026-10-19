# SPDX-License-Identifier: GPL-3.0-or-later
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_bosun as fixtures
from bosun.control import Control
from bosun.core import BosunError, Engine
from bosun.sessions import session_action


class GlobalSessionTests(unittest.TestCase):
    setUp = fixtures.CoreTests.setUp
    tearDown = fixtures.CoreTests.tearDown
    put = fixtures.CoreTests.put
    cpu = fixtures.CoreTests.cpu

    def start(self):
        self.base = self.cpu()
        control = Control(self.s)
        control.apply_global('old-global', {'swappiness': 100})
        control.save_game('123', 'Game', {'cpu_max_mhz': 2400})
        control.activate('123')
        return Control(self.s)  # Desktop opens after exit notification was lost.

    def test_lost_exit_no_longer_blocks_preview_and_apply_in_same_boot(self):
        control = self.start()
        before = Engine(self.s).file.read_bytes()
        preview = control.preview({'nmi_watchdog': 0})
        self.assertTrue(any('Decky' in n for n in preview['notes']))
        self.assertEqual(Engine(self.s).file.read_bytes(), before)
        self.assertEqual(self.s.read(self.base + 'scaling_max_freq'), '2400000')
        result = control.apply_global('new-global', {'nmi_watchdog': 0})
        self.assertFalse(result['conflict'])
        self.assertNotIn('session', Engine(self.s).state)
        self.assertEqual(Engine(self.s).state['profile'], 'new-global')
        self.assertEqual(self.s.read(self.base + 'scaling_max_freq'), '3500000')
        self.assertEqual(self.s.read('/proc/sys/vm/swappiness'), '60')
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')
        self.assertEqual(control.config()['games']['123']['settings'], {'cpu_max_mhz': 2400})
        before = Engine(self.s).file.read_bytes()
        control.apply_global('new-global', {'nmi_watchdog': 0})
        self.assertEqual(Engine(self.s).file.read_bytes(), before)
        control.activate('')  # A delayed Decky exit cannot restore the old global profile.
        self.assertEqual(Engine(self.s).state['profile'], 'new-global')
        control.activate('123')
        control.activate('')
        self.assertEqual(Engine(self.s).state['profile'], 'new-global')
        control.rollback()
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '1')
        self.assertFalse(Engine(self.s).state['entries'])

    def test_invalid_target_keeps_temporary_profile_and_journal(self):
        control = self.start()
        before = Engine(self.s).file.read_bytes()
        with self.assertRaises(BosunError):
            control.apply_global('bad', {'cpu_max_mhz': 99999})
        self.assertEqual(Engine(self.s).file.read_bytes(), before)

    def test_external_frequency_change_is_not_overwritten_or_forgotten(self):
        control = self.start()
        self.put(self.base + 'scaling_max_freq', '2700000')
        result = control.apply_global('new-global', {'nmi_watchdog': 0})
        self.assertTrue(result['conflict'])
        self.assertEqual(self.s.read(self.base + 'scaling_max_freq'), '2700000')
        self.assertEqual(Engine(self.s).state['session']['appid'], '123')

    def test_failed_write_recovers_overlay_and_its_session(self):
        control = self.start()
        original = Engine.write
        def fail_once(engine, change, value):
            if change.key == 'nmi_watchdog' and value == '0':
                raise BosunError('simulated write failure')
            return original(engine, change, value)
        with patch.object(Engine, 'write', fail_once), self.assertRaisesRegex(BosunError, 'simulated'):
            control.apply_global('new-global', {'nmi_watchdog': 0})
        self.assertEqual(self.s.read(self.base + 'scaling_max_freq'), '2400000')
        self.assertEqual(Engine(self.s).state['session']['appid'], '123')
        self.assertEqual(Engine(self.s).state['profile'], 'decky-123')
        self.assertFalse(Engine(self.s).state['pending'])
        control.apply_global('retry', {'nmi_watchdog': 0})
        self.assertNotIn('session', Engine(self.s).state)

    def test_cli_launcher_can_be_superseded_and_late_exit_preserves_global(self):
        base = self.cpu()
        engine = Engine(self.s)
        session_action(engine, 'session-start', 'launcher', 'a' * 32, {'cpu_max_mhz': 2400})
        before = engine.file.read_bytes()
        Control(self.s).preview({'nmi_watchdog': 0})
        self.assertEqual(engine.file.read_bytes(), before)
        Control(self.s).apply_global('new', {'nmi_watchdog': 0})
        session_action(Engine(self.s), 'session-end', 'launcher', 'a' * 32)
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')
        self.assertEqual(Engine(self.s).state['profile'], 'new')
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')

    def test_late_end_of_superseded_launcher_cannot_end_a_new_session(self):
        self.cpu()
        session_action(Engine(self.s), 'session-start', 'first', 'a' * 32, {'cpu_max_mhz': 2400})
        Control(self.s).apply_global('new-global', {'nmi_watchdog': 0})
        session_action(Engine(self.s), 'session-start', 'second', 'b' * 32, {'cpu_max_mhz': 2800})
        before = Engine(self.s).file.read_bytes()
        for _ in range(2):
            session_action(Engine(self.s), 'session-end', 'first', 'a' * 32)
        self.assertEqual(Engine(self.s).file.read_bytes(), before)
        self.assertEqual(Engine(self.s).state['session']['token'], 'b' * 32)
        session_action(Engine(self.s), 'session-end', 'second', 'b' * 32)
        self.assertEqual(Engine(self.s).state['profile'], 'new-global')

    def test_profile_commit_and_session_retirement_are_atomic(self):
        self.cpu()
        session_action(Engine(self.s), 'session-start', 'launcher', 'a' * 32, {'cpu_max_mhz': 2400})
        saved = []
        original = Engine.save
        def record(engine):
            saved.append(json.loads(json.dumps(engine.state)))
            return original(engine)
        with patch.object(Engine, 'save', record):
            Control(self.s).apply_global('new-global', {'nmi_watchdog': 0})
        committed = [s for s in saved if s['profile'] == 'new-global' and not s['pending']]
        self.assertTrue(committed)
        for state in committed:
            self.assertNotIn('session', state)
            self.assertIn('a' * 32, state['retired_sessions'])

    def test_cli_failure_and_foreign_change_retain_session_without_retiring_token(self):
        base = self.cpu()
        session_action(Engine(self.s), 'session-start', 'launcher', 'a' * 32, {'cpu_max_mhz': 2400})
        self.s.fail_boot = True
        with self.assertRaisesRegex(BosunError, 'simulated'):
            Control(self.s).apply_global('bad', {'mitigations': 'off'})
        state = Engine(self.s).state
        self.assertEqual(state['session']['token'], 'a' * 32)
        self.assertNotIn('a' * 32, state.get('retired_sessions', []))
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2400000')
        self.put(base + 'scaling_max_freq', '2700000')
        result = Control(self.s).apply_global('conflict', {'nmi_watchdog': 0})
        self.assertTrue(result['conflict'])
        self.assertEqual(Engine(self.s).state['session']['token'], 'a' * 32)
        self.assertNotIn('a' * 32, Engine(self.s).state.get('retired_sessions', []))
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2700000')

    def test_unknown_end_token_is_rejected_and_retired_token_cannot_be_reused(self):
        self.cpu()
        session_action(Engine(self.s), 'session-start', 'launcher', 'a' * 32, {'cpu_max_mhz': 2400})
        Control(self.s).apply_global('new', {'nmi_watchdog': 0})
        with self.assertRaises(BosunError):
            session_action(Engine(self.s), 'session-end', 'unknown', 'c' * 32)
        with self.assertRaises(BosunError):
            session_action(Engine(self.s), 'session-start', 'reused', 'a' * 32, {})

    def test_real_cli_launcher_preserves_child_exit_code_after_global_takeover(self):
        from types import SimpleNamespace
        base = self.cpu()
        source = Path(__file__).resolve().parents[1]
        # A real child changes the global profile while game_run is waiting for it.
        child_code = ('import subprocess,sys; '
                      f'subprocess.run([sys.executable, {str(source / "bosun.py")!r}, "--root", {str(self.root)!r}, '
                      '"apply", "new-global", "--stdin"], input=\'{"nmi_watchdog":0}\', text=True, capture_output=True, check=True); '
                      'sys.exit(17)')
        calls = []
        def request(action, name, token, settings=None):
            calls.append(action)
            engine = Engine(self.s)
            with engine.locked():
                return session_action(engine, action, name, token, settings)
        with patch.object(fixtures.cli.os, 'geteuid', return_value=1000), \
             patch.object(fixtures.cli, 'profile', return_value={'cpu_max_mhz': 2400}), \
             patch.object(fixtures.cli, 'broker_request', request):
            result = fixtures.cli.game_run(SimpleNamespace(profile='launcher', command=[sys.executable, '-c', child_code]))
        self.assertEqual(result, 17)
        self.assertEqual(calls, ['session-start', 'session-end'])
        self.assertEqual(Engine(self.s).state['profile'], 'new-global')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')

    def test_retirement_history_is_bounded_and_retains_latest_token(self):
        engine = Engine(self.s)
        engine.state['retired_sessions'] = [f'{i:032x}' for i in range(64)]
        engine.save()
        session_action(Engine(self.s), 'session-start', 'launcher', 'a' * 32, {})
        Control(self.s).apply_global('new', {})  # Also retires an overlay with no writes.
        retired = Engine(self.s).state['retired_sessions']
        self.assertEqual(len(retired), 64)
        self.assertEqual(retired[-1], 'a' * 32)
        self.assertNotIn('0' * 32, retired)

    def test_interrupted_final_commit_recovers_session_without_retiring_it(self):
        base = self.cpu()
        session_action(Engine(self.s), 'session-start', 'launcher', 'a' * 32, {'cpu_max_mhz': 2400})
        original = Engine.save
        def interrupt_commit(engine):
            if engine.state['profile'] == 'new-global' and not engine.state['pending']:
                raise OSError('interrupted commit')
            return original(engine)
        with patch.object(Engine, 'save', interrupt_commit), self.assertRaisesRegex(OSError, 'interrupted'):
            Control(self.s).apply_global('new-global', {'nmi_watchdog': 0})
        engine = Engine(self.s)
        self.assertTrue(engine.state['pending'])
        engine.recover()
        self.assertEqual(engine.state['session']['token'], 'a' * 32)
        self.assertNotIn('a' * 32, engine.state.get('retired_sessions', []))
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2400000')
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '1')

    def test_cli_apply_uses_same_transition_and_dry_run_is_read_only(self):
        self.start()
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'bosun.py'), '--root', str(self.root), 'apply', 'test', '--stdin']
        before = Engine(self.s).file.read_bytes()
        for dry in [True, False]:
            result = subprocess.run(command + (['--dry-run'] if dry else []), input=json.dumps({'nmi_watchdog': 0}), text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            if dry:
                self.assertEqual(Engine(self.s).file.read_bytes(), before)
        self.assertNotIn('session', Engine(self.s).state)
        self.assertEqual(self.s.read(self.base + 'scaling_max_freq'), '3500000')

    def test_cli_disable_can_leave_a_decky_profile_without_losing_global_settings(self):
        self.start()
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'bosun.py'), '--root', str(self.root), 'disable', 'cpu_max_mhz']
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('session', Engine(self.s).state)
        self.assertEqual(self.s.read(self.base + 'scaling_max_freq'), '3500000')
        self.assertEqual(self.s.read('/proc/sys/vm/swappiness'), '100')


if __name__ == '__main__':
    unittest.main()
