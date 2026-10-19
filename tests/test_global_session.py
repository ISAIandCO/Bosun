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

    def test_cli_launcher_session_is_still_protected(self):
        self.cpu()
        engine = Engine(self.s)
        session_action(engine, 'session-start', 'launcher', 'a' * 32, {'cpu_max_mhz': 2400})
        before = engine.file.read_bytes()
        for operation in [lambda: Control(self.s).preview({'nmi_watchdog': 0}),
                          lambda: Control(self.s).apply_global('new', {'nmi_watchdog': 0})]:
            with self.assertRaisesRegex(BosunError, 'CLI launcher'):
                operation()
            self.assertEqual(engine.file.read_bytes(), before)

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
