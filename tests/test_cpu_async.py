# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import dataclasses
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bosun as fixtures
from bosun.control import Control
from bosun.core import STATE, BosunError, Change, Engine, plan


class AsyncCpuTests(unittest.TestCase):
    setUp = fixtures.CoreTests.setUp
    tearDown = fixtures.CoreTests.tearDown
    put = fixtures.CoreTests.put
    cpu = fixtures.CoreTests.cpu

    @contextlib.contextmanager
    def deferred_cpufreq(self):
        """Model QoS writes returning before the policy worker updates readback."""
        real_open = Path.open
        queued = []
        sleeps = []
        @contextlib.contextmanager
        def defer(path):
            stream = io.StringIO()
            yield stream
            queued.append((path, stream.getvalue()))
        def opened(path, mode='r', *args, **kwargs):
            if mode == 'w' and path.name in {'scaling_min_freq', 'scaling_max_freq'}:
                return defer(path)
            return real_open(path, mode, *args, **kwargs)
        def tick(seconds):
            sleeps.append(seconds)
            while queued:
                path, value = queued.pop(0)
                lower = int(path.with_name('scaling_min_freq').read_text())
                upper = int(path.with_name('scaling_max_freq').read_text())
                self.assertLessEqual(int(value), upper) if path.name == 'scaling_min_freq' else self.assertGreaterEqual(int(value), lower)
                with real_open(path, 'w') as stream:
                    stream.write(value)
        with patch.object(Path, 'open', opened), patch('bosun.core.time.sleep', tick):
            yield sleeps
        self.assertFalse(queued, 'Every queued policy update must be observed before return')

    def test_async_min_max_apply_raise_and_rollback_on_both_models(self):
        base = self.cpu()
        for board in ['Jupiter', 'Galileo']:
            self.put('/sys/class/dmi/id/board_name', board)
            for i in range(8):
                path = base.replace('policy0', f'policy{i}')
                for key, value in {'cpuinfo_min_freq': '400000', 'cpuinfo_max_freq': '3500000',
                                   'scaling_min_freq': '1700000', 'scaling_max_freq': '3500000'}.items():
                    self.put(path + key, value)
            with self.deferred_cpufreq() as sleeps:
                for cap in [1500, 2400]:
                    result = Control(self.s).apply_global('async', {'cpu_max_mhz': cap})
                    self.assertFalse(result['conflict'])
                    self.assertFalse(Engine(self.s).state['pending'])
                    for i in range(8):
                        self.assertEqual(self.s.read(base.replace('policy0', f'policy{i}') + 'scaling_max_freq'), str(cap * 1000))
                Control(self.s).rollback()
                self.assertTrue(sleeps)
            for i in range(8):
                path = base.replace('policy0', f'policy{i}')
                self.assertEqual(self.s.read(path + 'scaling_min_freq'), '1700000')
                self.assertEqual(self.s.read(path + 'scaling_max_freq'), '3500000')

    def test_discrete_driver_resolves_ceiling_down_before_writing(self):
        base = self.cpu()
        self.put(base + 'cpuinfo_min_freq', '1400000')
        self.put(base + 'scaling_min_freq', '1700000')
        self.put(base + 'scaling_available_frequencies', '3500000 1700000 1400000')
        before = self.s.read(base + 'scaling_max_freq')
        changes, notes = plan(self.s, {'cpu_max_mhz': 1500})
        self.assertTrue(any('1500 MHz -> supported 1400 MHz' in n for n in notes))
        self.assertTrue(all(c.value == '1400000' for c in changes))
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), before)
        with self.deferred_cpufreq():
            for request, effective in [(1500, 1400000), (2800, 1700000)]:
                Control(self.s).apply_global('discrete', {'cpu_max_mhz': request})
                self.assertEqual(self.s.read(base + 'scaling_max_freq'), str(effective))
                self.assertEqual(Engine(self.s).state['desired']['cpu_max_mhz'], request)
            Control(self.s).rollback()
        self.assertEqual(self.s.read(base + 'scaling_min_freq'), '1700000')

    def test_request_below_the_lowest_discrete_step_is_skipped(self):
        base = self.cpu()
        self.put(base + 'cpuinfo_min_freq', '400000')
        self.put(base + 'scaling_available_frequencies', '1400000 1700000 3500000')
        changes, notes = plan(self.s, {'cpu_max_mhz': 800, 'nmi_watchdog': 0})
        self.assertFalse(any(c.key == 'cpu_max_mhz' for c in changes))
        self.assertTrue(any('no supported frequency' in n for n in notes))
        self.assertTrue(any(c.key == 'nmi_watchdog' for c in changes))

    def test_unknown_readback_is_not_accepted_as_success(self):
        base = self.cpu()
        change = Change('cpu_max_mhz', base + 'scaling_max_freq', '1500000')
        with patch.object(Change, 'current', return_value='1700000'), patch('bosun.core.time.sleep') as sleep:
            with self.assertRaisesRegex(BosunError, "requested '1500000', read back '1700000'"):
                change.put(self.s, '1500000')
            self.assertEqual(sleep.call_count, 50)

    def pending_cpu_conflict(self):
        base = self.cpu()
        engine = Engine(self.s)
        engine.apply('before', {'cpu_max_mhz': 2800, 'nmi_watchdog': 0, 'swappiness': 100})
        old = json.loads(json.dumps(engine.state))
        writes = []
        for change, before in [(Change('cpu_max_mhz', base + 'scaling_max_freq', '1500000'), '2800000'),
                               (Change('swappiness', '/proc/sys/vm/swappiness', '150'), '100')]:
            writes.append({'change': dataclasses.asdict(change), 'before': before})
        engine.state['pending'] = {'state_before': old, 'writes': writes}
        engine.save()
        self.put(base + 'scaling_max_freq', '1700000')
        self.put('/proc/sys/vm/swappiness', '150')
        return base, engine

    def test_recovery_conflict_reports_before_intended_and_actual(self):
        _, engine = self.pending_cpu_conflict()
        with patch('bosun.core.time.sleep'), self.assertRaisesRegex(BosunError, "before='2800000', intended='1500000', actual='1700000'"):
            engine.recover()
        self.assertTrue(Engine(self.s).state['pending'])

    def test_explicit_resolution_archives_journal_keeps_cpu_and_recovers_other_writes(self):
        base, engine = self.pending_cpu_conflict()
        original = engine.file.read_bytes()
        report = Control(self.s).state()['report']
        self.assertEqual(report['cpu_recovery'][0]['actual'], '1700000')
        self.assertEqual(engine.file.read_bytes(), original, 'Status must not release or recover anything')
        result = Control(self.s).recover_cpu()
        self.assertTrue(any('Копия журнала' in n for n in result['notes']))
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '1700000')
        self.assertEqual(self.s.read('/proc/sys/vm/swappiness'), '100')
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')
        state = Engine(self.s).state
        self.assertFalse(state['pending'])
        self.assertNotIn('cpu_max_mhz', state['desired'])
        archives = list(self.s.path(STATE).glob('cpu-recovery-*.json'))
        self.assertEqual(len(archives), 1)
        archived = json.loads(archives[0].read_text())
        self.assertTrue(archived['journal']['pending'])
        self.assertEqual(archived['actual_cpu_limits'][base + 'scaling_max_freq'], '1700000')
        Control(self.s).apply_global('retry', {'cpu_max_mhz': 1500})
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '1500000')
        Control(self.s).rollback()
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '1700000')
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '1')
        self.assertEqual(self.s.read('/proc/sys/vm/swappiness'), '60')
        Control(self.s).recover_cpu()
        self.assertEqual(len(list(self.s.path(STATE).glob('cpu-recovery-*.json'))), 1)

    def test_resolution_does_not_release_an_unrelated_conflict(self):
        base, engine = self.pending_cpu_conflict()
        self.put('/proc/sys/vm/swappiness', '180')
        with self.assertRaisesRegex(BosunError, 'swappiness'):
            Control(self.s).recover_cpu()
        state = Engine(self.s).state
        self.assertTrue(state['pending'])
        self.assertTrue(all(w['change']['key'] != 'cpu_max_mhz' for w in state['pending']['writes']))
        self.assertEqual(self.s.read('/proc/sys/vm/swappiness'), '180')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '1700000')

    def test_archive_failure_leaves_the_original_journal_untouched(self):
        _, engine = self.pending_cpu_conflict()
        before = engine.file.read_bytes()
        with patch('bosun.core.atomic', side_effect=OSError('archive failure')), self.assertRaises(OSError):
            Control(self.s).recover_cpu()
        self.assertEqual(engine.file.read_bytes(), before)

    def test_explicit_resolution_releases_both_bounds_for_all_eight_policies(self):
        base = self.cpu()
        for i in range(8):
            path = base.replace('policy0', f'policy{i}')
            for key, value in {'cpuinfo_min_freq': '400000', 'cpuinfo_max_freq': '3500000',
                               'scaling_min_freq': '3500000', 'scaling_max_freq': '3500000'}.items():
                self.put(path + key, value)
        engine = Engine(self.s)
        engine.apply('old', {'cpu_max_mhz': 2800})
        old = json.loads(json.dumps(engine.state))
        writes = []
        for i in range(8):
            path = base.replace('policy0', f'policy{i}')
            for field in ['scaling_min_freq', 'scaling_max_freq']:
                change = Change('cpu_max_mhz', path + field, '1500000')
                writes.append({'change': dataclasses.asdict(change), 'before': '2800000'})
                self.put(path + field, '1700000')
        engine.state['pending'] = {'state_before': old, 'writes': writes}; engine.save()
        with patch.object(Change, 'put', side_effect=AssertionError('Resolution must not write CPU bounds')):
            Control(self.s).recover_cpu()
        self.assertFalse(Engine(self.s).state['entries'])
        self.assertFalse(Engine(self.s).state['pending'])
        for item in writes:
            self.assertEqual(self.s.read(item['change']['path']), '1700000')


if __name__ == '__main__':
    unittest.main()
