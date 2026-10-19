# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from unittest.mock import patch

import test_bosun as fixtures
import test_cpu as cpu_fixtures
from bosun.control import Control
from bosun.core import Engine


class CpuCeilingTests(unittest.TestCase):
    setUp = fixtures.CoreTests.setUp
    tearDown = fixtures.CoreTests.tearDown
    put = fixtures.CoreTests.put
    cpu = fixtures.CoreTests.cpu
    strict_ranges = cpu_fixtures.CpuTests.strict_ranges

    def test_initial_floor_equal_to_ceiling_opens_a_range_and_restores_exact_baseline(self):
        base = self.cpu()
        self.put(base + 'cpuinfo_min_freq', '400000')
        self.put(base + 'scaling_min_freq', '1500000')
        self.put(base + 'scaling_max_freq', '1500000')
        with self.strict_ranges():
            self.engine.apply('cap', {'cpu_max_mhz': 1500})
            self.assertEqual(self.s.read(base + 'scaling_min_freq'), '400000')
            self.assertEqual(self.s.read(base + 'scaling_max_freq'), '1500000')
            self.engine.apply('stock', {})
        self.assertEqual(self.s.read(base + 'scaling_min_freq'), '1500000')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '1500000')

    def test_lower_custom_floor_is_preserved_and_only_released_when_it_pins_the_cap(self):
        base = self.cpu()
        self.put(base + 'cpuinfo_min_freq', '400000')
        self.put(base + 'scaling_min_freq', '1700000')
        with self.strict_ranges():
            for cap, floor in [(2800, 1700000), (1500, 400000), (2400, 1700000)]:
                self.engine.apply('cap', {'cpu_max_mhz': cap})
                self.assertEqual(self.s.read(base + 'scaling_min_freq'), str(floor))
                self.assertEqual(self.s.read(base + 'scaling_max_freq'), str(cap * 1000))
            self.engine.apply('stock', {})
        self.assertEqual(self.s.read(base + 'scaling_min_freq'), '1700000')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')

    def test_044_fixed_range_journal_is_corrected_on_next_automatic_game_activation(self):
        base = self.cpu()
        self.put(base + 'cpuinfo_min_freq', '400000')
        self.put(base + 'scaling_min_freq', '3500000')
        control = Control(self.s)
        control.save_game('123', 'Game', {'cpu_max_mhz': 1500})
        control.activate('123')
        engine = Engine(self.s)
        entry = engine.state['entries']['scalar:' + base + 'scaling_min_freq']
        entry['change']['value'] = entry['last'] = '1500000'
        engine.save(); self.put(base + 'scaling_min_freq', '1500000')
        with self.strict_ranges():
            control.activate('123')
            self.assertEqual(self.s.read(base + 'scaling_min_freq'), '400000')
            self.assertEqual(self.s.read(base + 'scaling_max_freq'), '1500000')
            control.activate('')
        self.assertEqual(self.s.read(base + 'scaling_min_freq'), '3500000')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')

    def test_steam_reset_of_both_bounds_does_not_turn_game_cap_into_a_fixed_clock(self):
        base = self.cpu()
        self.put(base + 'cpuinfo_min_freq', '400000')
        control = Control(self.s)
        control.save_game('123', 'Game', {'cpu_max_mhz': 1500})
        for _ in range(3):
            self.put(base + 'scaling_max_freq', '3500000')
            self.put(base + 'scaling_min_freq', '3500000')
            with self.strict_ranges():
                control.activate('123')
            self.assertEqual(self.s.read(base + 'scaling_min_freq'), '400000')
            self.assertEqual(self.s.read(base + 'scaling_max_freq'), '1500000')

    def test_cap_never_silently_switches_governor_on_either_driver(self):
        base = self.cpu()
        for driver in ['acpi-cpufreq', 'amd-pstate-epp']:
            self.put(base + 'scaling_driver', driver)
            notes, conflict = self.engine.apply('cap', {'cpu_max_mhz': 2400})
            self.assertFalse(conflict, notes)
            self.assertEqual(self.s.read(base + 'scaling_governor'), 'performance')
            self.assertEqual(self.s.read(base + 'energy_performance_preference'), 'performance')
            self.assertTrue(any(n.startswith('NOTE ') and 'governor=performance retained' in n for n in notes))
            self.assertFalse(any(e['change']['key'] in {'governor', 'epp'} for e in self.engine.state['entries'].values()))

    def test_cpu_check_distinguishes_a_fixed_range_from_performance_governor(self):
        base = self.cpu()
        self.put(base + 'scaling_driver', 'acpi-cpufreq')
        self.put(base + 'scaling_available_governors', 'performance powersave schedutil')
        self.put(base + 'scaling_min_freq', '1500000')
        self.put(base + 'scaling_max_freq', '1500000')
        with patch('bosun.control.time.sleep'):
            notes = '\n'.join(Control(self.s).cpu_check()['notes'])
        self.assertIn('min=max', notes)
        self.assertIn('performance requests the upper end', notes)
        self.assertIn('Load-based scaling available: schedutil', notes)
        self.put(base + 'scaling_driver', 'amd-pstate-epp')
        self.put(base + 'scaling_min_freq', '400000')
        with patch('bosun.control.time.sleep'):
            notes = '\n'.join(Control(self.s).cpu_check()['notes'])
        self.assertNotIn('min=max', notes)
        self.assertIn('Load-based scaling available: powersave + EPP', notes)


if __name__ == '__main__':
    unittest.main()
