# SPDX-License-Identifier: GPL-3.0-or-later
import dataclasses
import json
import unittest
from unittest.mock import patch

import test_bosun as fixtures
from bosun.control import Control
from bosun.core import BosunError, Change


class CpuTests(unittest.TestCase):
    setUp = fixtures.CoreTests.setUp
    tearDown = fixtures.CoreTests.tearDown
    put = fixtures.CoreTests.put
    cpu = fixtures.CoreTests.cpu

    def strict_ranges(self):
        real = Change.put
        def write(change, system, value):
            parent = change.path.rsplit('/', 1)[0]
            if change.path.endswith('/scaling_min_freq'):
                if int(value) > int(system.read(parent + '/scaling_max_freq')):
                    raise BosunError('minimum exceeds current maximum')
            if change.path.endswith('/scaling_max_freq'):
                if int(value) < int(system.read(parent + '/scaling_min_freq')):
                    raise BosunError('maximum below current minimum')
            return real(change, system, value)
        return patch.object(Change, 'put', write)

    def test_ceiling_below_current_floor_and_raising_it_restore_the_exact_pair(self):
        base = self.cpu()
        self.put(base + 'cpuinfo_min_freq', '400000')
        self.put(base + 'scaling_min_freq', '3500000')
        with self.strict_ranges():
            for cap in [2800, 2400, 3000]:
                notes, conflict = self.engine.apply('cap', {'cpu_max_mhz': cap})
                self.assertFalse(conflict, notes)
                self.assertEqual(self.s.read(base + 'scaling_max_freq'), str(cap * 1000))
                self.assertEqual(self.s.read(base + 'scaling_min_freq'), '400000')
            self.engine.apply('stock', {})
        self.assertEqual(self.s.read(base + 'scaling_min_freq'), '3500000')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')

    def test_partial_failure_restores_both_bounds(self):
        base = self.cpu()
        self.put(base + 'cpuinfo_min_freq', '400000')
        self.put(base + 'scaling_min_freq', '3500000')
        real = Change.put
        def fail_max(change, system, value):
            if change.path == base + 'scaling_max_freq' and value == '2800000':
                raise OSError('injected driver failure')
            return real(change, system, value)
        with patch.object(Change, 'put', fail_max), self.assertRaises(OSError):
            self.engine.apply('cap', {'cpu_max_mhz': 2800})
        self.assertEqual(self.s.read(base + 'scaling_min_freq'), '3500000')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')
        self.assertFalse(self.engine.state['entries'])

    def test_new_boot_uses_the_new_floor_as_baseline(self):
        base = self.cpu()
        self.put(base + 'cpuinfo_min_freq', '400000')
        self.put(base + 'scaling_min_freq', '3500000')
        self.engine.apply('cap', {'cpu_max_mhz': 2800})
        self.put('/proc/sys/kernel/random/boot_id', 'boot2')
        self.put(base + 'scaling_max_freq', '3500000')
        with self.strict_ranges():
            self.engine.apply('cap', {'cpu_max_mhz': 3000})
            self.assertEqual(self.s.read(base + 'scaling_min_freq'), '400000')
            self.engine.apply('stock', {})
        self.assertEqual(self.s.read(base + 'scaling_min_freq'), '400000')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')

    def test_all_policies_apply_and_restore_on_lcd_and_oled(self):
        base = self.cpu()
        self.put(base + 'cpuinfo_min_freq', '400000')
        for board in ['Jupiter', 'Galileo']:
            with self.subTest(board=board):
                self.put('/sys/class/dmi/id/board_name', board)
                for i in range(8):
                    for field, value in {'scaling_min_freq': '3500000', 'scaling_max_freq': '3500000',
                                         'cpuinfo_min_freq': '400000', 'cpuinfo_max_freq': '3500000'}.items():
                        self.put(base.replace('policy0', 'policy' + str(i)) + field, value)
                with self.strict_ranges():
                    self.engine.apply('cap', {'cpu_max_mhz': 2400})
                    for i in range(8):
                        self.assertEqual(self.s.read(base.replace('policy0', 'policy' + str(i)) + 'scaling_max_freq'), '2400000')
                        self.assertEqual(self.s.read(base.replace('policy0', 'policy' + str(i)) + 'scaling_min_freq'), '400000')
                    self.engine.apply('stock', {})
                for i in range(8):
                    self.assertEqual(self.s.read(base.replace('policy0', 'policy' + str(i)) + 'scaling_min_freq'), '3500000')

    def test_external_floor_change_blocks_the_entire_owned_pair(self):
        base = self.cpu()
        self.put(base + 'cpuinfo_min_freq', '400000')
        self.put(base + 'scaling_min_freq', '3500000')
        self.engine.apply('cap', {'cpu_max_mhz': 2800})
        self.put(base + 'scaling_min_freq', '2000000')
        notes, conflict = self.engine.apply('stock', {})
        self.assertTrue(conflict, notes)
        self.assertEqual(self.s.read(base + 'scaling_min_freq'), '2000000')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2800000')

    def test_interrupted_raising_of_a_range_recovers_without_crossing_bounds(self):
        base = self.cpu()
        self.put(base + 'cpuinfo_min_freq', '400000')
        self.put(base + 'scaling_min_freq', '3500000')
        self.engine.apply('cap', {'cpu_max_mhz': 2800})
        before = json.loads(json.dumps(self.engine.state))
        writes = []
        for field in ['scaling_max_freq', 'scaling_min_freq']:
            change = Change('cpu_max_mhz', base + field, '3000000')
            writes.append({'change': dataclasses.asdict(change), 'before': self.s.read(base + field)})
            self.put(base + field, '3000000')
        self.engine.state['pending'] = {'state_before': before, 'writes': writes}
        self.engine.save()
        with self.strict_ranges():
            self.engine.recover()
        self.assertEqual(self.s.read(base + 'scaling_min_freq'), '400000')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2800000')

    def test_cap_below_hardware_minimum_is_skipped_before_a_driver_write(self):
        base = self.cpu()
        self.put(base + 'cpuinfo_min_freq', '1400000')
        self.put(base + 'scaling_min_freq', '1400000')
        notes, _ = self.engine.apply('cap', {'cpu_max_mhz': 800, 'nmi_watchdog': 0})
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')
        self.assertTrue(any('1400000' in n for n in notes))
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')

    def test_same_game_retries_a_previously_skipped_cpu_setting(self):
        base = self.cpu()
        c = Control(self.s)
        c.save_game('123', 'Game', {'cpu_max_mhz': 2400})
        self.s.services['tlp.service'] = 'active'
        self.assertTrue(any('SKIP cpu_max_mhz' in n for n in c.activate('123')['notes']))
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')
        self.s.services['tlp.service'] = 'inactive'
        c.activate('123')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2400000')

    def test_same_game_profile_reclaims_its_limit_after_a_steam_reset(self):
        base = self.cpu()
        c = Control(self.s)
        c.save_game('123', 'Game', {'cpu_max_mhz': 2400})
        c.activate('123')
        self.put(base + 'scaling_max_freq', '3500000')
        result = c.activate('123')
        self.assertFalse(result.get('conflict'), result)
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2400000')

    def test_cpu_check_observes_exceedance_without_writing(self):
        base = self.cpu()
        self.put(base + 'scaling_max_freq', '2400000')
        self.put(base + 'cpuinfo_cur_freq', '3000000')
        original = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('bosun.control.time.sleep'):
            result = Control(self.s).cpu_check()
        notes = '\n'.join(result['notes'])
        self.assertIn('peak=3000000', notes)
        self.assertIn('hardware enforcement is NOT confirmed', notes)
        self.assertIn('cpuinfo_cur_freq', notes)
        self.assertEqual(original, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertFalse(self.engine.file.exists())

    def test_cpu_check_exposes_a_limit_replaced_during_sampling(self):
        base = self.cpu()
        self.put(base + 'scaling_max_freq', '2400000')
        self.put(base + 'scaling_cur_freq', '2000000')
        with patch('bosun.control.time.sleep', side_effect=lambda _: self.put(base + 'scaling_max_freq', '3500000')):
            notes = '\n'.join(Control(self.s).cpu_check()['notes'])
        self.assertIn('changed during observation', notes)
        self.assertIn('not a measured hardware clock', notes)
