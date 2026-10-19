# SPDX-License-Identifier: GPL-3.0-or-later
import dataclasses
import json
import unittest
from unittest.mock import patch

import test_bosun as fixtures
from bosun.control import Control
from bosun.core import STATE, BosunError, Change, Engine
from bosun.sessions import session_action


class GamePriorityTests(unittest.TestCase):
    setUp = fixtures.CoreTests.setUp
    tearDown = fixtures.CoreTests.tearDown
    put = fixtures.CoreTests.put
    cpu = fixtures.CoreTests.cpu

    def test_relaunch_after_steam_reset_reapplies_profile_without_manual_action(self):
        base = self.cpu()
        control = Control(self.s)
        control.apply_global('global', {'cpu_max_mhz': 3000})
        control.save_game('123', 'Game', {'cpu_max_mhz': 1500})
        for _ in range(3):
            control.activate('123')
            self.assertEqual(self.s.read(base + 'scaling_max_freq'), '1500000')
            self.put(base + 'scaling_max_freq', '3500000')  # Steam's exit reset precedes notification.
            control.activate('')
            self.assertNotIn('session', Engine(self.s).state)
            self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3000000')
            self.put(base + 'scaling_max_freq', '3500000')  # Next startup reset.
        control.activate('123')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '1500000')

    def test_late_reset_of_same_game_and_resume_have_priority_with_exact_new_baseline(self):
        base = self.cpu()
        control = Control(self.s)
        control.save_game('123', 'Game', {'cpu_max_mhz': 1500})
        control.activate('123')
        for resume, value in [(False, '3500000'), (True, '3100000')]:
            self.put(base + 'scaling_max_freq', value)
            result = control.activate('123', resume=resume)
            self.assertFalse(result.get('conflict'), result)
            self.assertEqual(self.s.read(base + 'scaling_max_freq'), '1500000')
        control.activate('')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3100000')

    def test_governor_and_epp_are_reclaimed_together_and_restore_latest_steam_values(self):
        base = self.cpu()
        control = Control(self.s)
        control.save_game('123', 'Game', {'governor': 'powersave', 'epp': 'balance_power'})
        control.activate('123')
        self.put(base + 'scaling_governor', 'performance')
        self.put(base + 'energy_performance_preference', 'balance_performance')
        control.activate('123')
        self.assertEqual(self.s.read(base + 'scaling_governor'), 'powersave')
        self.assertEqual(self.s.read(base + 'energy_performance_preference'), 'balance_power')
        control.activate('')
        self.assertEqual(self.s.read(base + 'scaling_governor'), 'performance')
        self.assertEqual(self.s.read(base + 'energy_performance_preference'), 'balance_performance')

    def test_switch_between_assigned_games_never_leaves_the_previous_game_cap(self):
        base = self.cpu()
        control = Control(self.s)
        control.apply_global('global', {'cpu_max_mhz': 3000})
        control.save_game('123', 'First', {'cpu_max_mhz': 1500})
        control.save_game('456', 'Second', {'cpu_max_mhz': 2200})
        control.activate('123')
        self.put(base + 'scaling_max_freq', '3500000')
        control.activate('456')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2200000')
        self.assertEqual(Engine(self.s).state['session']['appid'], '456')
        control.activate('')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3000000')

    def test_pending_cpu_conflict_is_archived_and_retried_automatically(self):
        base = self.cpu()
        control = Control(self.s)
        control.apply_global('global', {'cpu_max_mhz': 3000})
        control.save_game('123', 'Game', {'cpu_max_mhz': 1500})
        control.activate('123')
        engine = Engine(self.s)
        before = json.loads(json.dumps(engine.state))
        engine.state['pending'] = {'state_before': before, 'writes': [
            {'change': dataclasses.asdict(Change('cpu_max_mhz', base + 'scaling_max_freq', '1400000')), 'before': '1500000'}]}
        engine.save(); self.put(base + 'scaling_max_freq', '1700000')
        with patch('bosun.core.time.sleep'):
            control.activate('123')
        self.assertFalse(Engine(self.s).state['pending'])
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '1500000')
        self.assertTrue(list(self.s.path(STATE).glob('cpu-recovery-*.json')))
        control.activate('')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3000000')

    def test_unrelated_global_external_change_is_not_claimed_by_game_priority(self):
        base = self.cpu()
        control = Control(self.s)
        control.apply_global('global', {'swappiness': 100})
        self.put('/proc/sys/vm/swappiness', '120')
        control.save_game('123', 'Game', {'cpu_max_mhz': 1500})
        with self.assertRaisesRegex(BosunError, 'Session conflict'):
            control.activate('123')
        self.assertEqual(self.s.read('/proc/sys/vm/swappiness'), '120')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '1500000')
        # A partial failure must still identify the session as Decky, never orphan CLI.
        state = Engine(self.s).state
        self.assertEqual(state['session']['owner'], 'decky')
        self.assertEqual(state['session']['appid'], '123')

    def test_game_preview_has_the_same_priority_without_mutating_the_journal(self):
        base = self.cpu()
        control = Control(self.s)
        control.save_game('123', 'Game', {'cpu_max_mhz': 1500})
        control.activate('123')
        self.put(base + 'scaling_max_freq', '3500000')
        before = Engine(self.s).file.read_bytes()
        result = control.preview({'cpu_max_mhz': 1500}, game=True)
        self.assertFalse(result['conflict'], result)
        self.assertEqual(Engine(self.s).file.read_bytes(), before)
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')

    def test_removing_a_game_override_inherits_global_even_after_a_reset(self):
        base = self.cpu()
        control = Control(self.s)
        control.apply_global('global', {'cpu_max_mhz': 3000})
        control.save_game('123', 'Game', {'cpu_max_mhz': 1500})
        control.activate('123')
        self.put(base + 'scaling_max_freq', '3500000')
        control.save_game('123', 'Game', {})
        control.activate('123')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3000000')

    def test_reboot_and_changed_kernel_recheck_capabilities_without_manual_global_apply(self):
        base = self.cpu()
        control = Control(self.s)
        control.save_game('123', 'Game', {'cpu_max_mhz': 1500})
        control.activate('123')
        self.put('/proc/sys/kernel/random/boot_id', 'boot2')
        self.put('/proc/sys/kernel/osrelease', 'new-kernel')
        self.put(base + 'scaling_max_freq', '3500000')
        control.activate('123')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '1500000')
        self.assertEqual(Engine(self.s).state['fingerprint'], self.s.fingerprint())

    def test_known_cli_or_legacy_session_does_not_block_the_assigned_game(self):
        base = self.cpu()
        control = Control(self.s)
        control.save_game('123', 'Game', {'cpu_max_mhz': 1500})
        session_action(Engine(self.s), 'session-start', 'old-launcher', 'a' * 32, {'cpu_max_mhz': 2800})
        engine = Engine(self.s); engine.state['session'].pop('owner', None); engine.state['session'].pop('settings', None); engine.save()
        self.put(base + 'scaling_max_freq', '3500000')
        control.activate('123')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '1500000')
        before = Engine(self.s).file.read_bytes()
        session_action(Engine(self.s), 'session-end', 'old-launcher', 'a' * 32)
        self.assertEqual(Engine(self.s).file.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
