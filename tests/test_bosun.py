# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import dataclasses
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bosun.core import BOOT, STATE, BosunError, Change, Engine, PROFILES, System, boot_content, plan, validate
from bosun.measure import energy_metrics, frame_metrics


class FakeSystem(System):
    def __init__(self, root):
        super().__init__(root)
        self.services = {}
        self.fail_path = None
        self.fail_boot = False
        self.readonly = 'enabled'

    def command(self, *args, **kwargs):
        self.commands.append(list(args))
        if args[0] == 'steamos-readonly':
            if args[1] == 'status':
                return self.readonly
            self.readonly = 'enabled' if args[1] == 'enable' else 'disabled'
        if args[0] == 'systemctl':
            if args[1] == 'is-active':
                return self.services.get(args[2], 'inactive')
            if args[1] in {'start', 'restart', 'stop'}:
                self.services[args[2]] = 'inactive' if args[1] == 'stop' else 'active'
        return ''

    def regenerate_boot(self, expected):
        if self.fail_boot:
            raise BosunError('simulated generator error')
        super().regenerate_boot(expected)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / '.bosun-test-root').touch()
        self.s = FakeSystem(self.root)
        self.put('/etc/os-release', 'ID=steamos\nVERSION_ID=3.8\nBUILD_ID=test1')
        self.put('/sys/class/dmi/id/board_name', 'Jupiter')
        self.put('/proc/sys/kernel/osrelease', '6.18-fixture')
        self.put('/proc/sys/kernel/random/boot_id', 'boot1')
        self.put('/proc/swaps', 'Filename Type Size Used Priority\n/dev/zram0 partition 8000 0 100')
        self.put('/proc/sys/kernel/nmi_watchdog', '1')
        self.put('/proc/sys/vm/page-cluster', '3')
        self.put('/proc/sys/vm/swappiness', '60')
        self.put('/sys/kernel/mm/lru_gen/enabled', '0x0006')
        self.put('/sys/kernel/mm/transparent_hugepage/defrag', 'always defer defer+madvise [madvise] never')
        self.put('/usr/bin/update-grub', '#!/bin/bash\nfile=/efi/EFI/steamos/grub.cfg\n')
        self.put('/efi/EFI/steamos/grub.cfg', 'original config')
        self.put('/usr/bin/grub-mkconfig', '# load /etc/default/grub.d/*.cfg')
        self.put('/usr/bin/grub-script-check', '')
        self.put('/usr/bin/steamos-readonly', '')
        self.engine = Engine(self.s)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, path, value):
        p = self.s.path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value)

    def apply(self, settings=None):
        return self.engine.apply('test', PROFILES['recommended'] if settings is None else settings)

    def cpu(self):
        base = '/sys/devices/system/cpu/cpufreq/policy0/'
        for k, v in {'scaling_driver': 'amd-pstate-epp', 'scaling_governor': 'performance',
                     'scaling_available_governors': 'powersave performance',
                     'energy_performance_preference': 'performance',
                     'energy_performance_available_preferences': 'performance balance_performance balance_power power',
                     'scaling_max_freq': '3500000', 'scaling_min_freq': '400000', 'cpuinfo_max_freq': '3500000'}.items():
            self.put(base + k, v)
        return base

    def test_recommended_changes_and_exact_rollback(self):
        notes, conflict = self.apply()
        self.assertFalse(conflict)
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')
        self.assertEqual(self.s.read('/proc/sys/vm/page-cluster'), '0')
        self.assertEqual(self.s.read('/sys/kernel/mm/lru_gen/enabled'), '0x7')
        self.assertIn('mitigations=off', self.s.read(BOOT))
        self.assertTrue(any('REBOOT' in n for n in notes))
        self.engine.apply('stock', {})
        self.assertEqual(self.s.read('/sys/kernel/mm/lru_gen/enabled'), '0x6')
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '1')
        self.assertEqual(self.s.read('/proc/sys/vm/page-cluster'), '3')
        self.assertEqual(self.s.read('/sys/kernel/mm/transparent_hugepage/defrag'), 'madvise')
        self.assertIsNone(self.s.read(BOOT))
        self.assertEqual(self.s.readonly, 'enabled')
        self.assertFalse(self.engine.state['entries'])

    def test_dry_run_is_read_only(self):
        self.apply_dry()
        self.assertFalse(self.engine.file.exists())
        self.assertIsNone(self.s.read(BOOT))
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '1')

    def apply_dry(self):
        return self.engine.apply('test', PROFILES['recommended'], dry=True)

    def test_idempotent(self):
        self.apply()
        before = json.dumps(self.engine.state, sort_keys=True)
        self.apply()
        self.assertEqual(before, json.dumps(self.engine.state, sort_keys=True))

    def test_conflict_never_silently_overwrites(self):
        self.apply({'swappiness': 100})
        self.put('/proc/sys/vm/swappiness', '120')
        notes, conflict = self.engine.apply('stock', {})
        self.assertTrue(conflict)
        self.assertEqual(self.s.read('/proc/sys/vm/swappiness'), '120')
        self.assertTrue(self.engine.state['entries'])
        self.engine.release('swappiness')
        self.assertFalse(self.engine.state['entries'])

    def test_mixed_swap_skips_zram_only_knobs(self):
        self.put('/proc/swaps', 'Filename Type Size Used Priority\n/swapfile file 8000 0 100')
        changes, notes = plan(self.s, {'swappiness': 150, 'zram_page_cluster': 0})
        self.assertFalse(changes)
        self.assertEqual(len(notes), 2)

    def test_lower_priority_disk_swap_allows_zram(self):
        self.put('/proc/swaps', 'Filename Type Size Used Priority\n/dev/zram0 partition 8000 0 100\n/swapfile file 8000 0 -2')
        changes, _ = plan(self.s, {'swappiness': 150})
        self.assertEqual(len(changes), 1)

    def test_unavailable_knob_does_not_disable_rest(self):
        self.s.path('/sys/kernel/mm/lru_gen/enabled').unlink()
        self.apply()
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')

    def test_boot_failure_rolls_back_entire_transaction_and_readonly(self):
        self.s.fail_boot = True
        with self.assertRaises(BosunError):
            self.apply()
        self.assertIsNone(self.s.read(BOOT))
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '1')
        self.assertFalse(self.engine.state['entries'])
        self.assertEqual(self.s.readonly, 'enabled')

    def test_preserve_initial_writable_mode(self):
        self.s.readonly = 'disabled'
        self.apply({'mitigations': 'off'})
        self.assertEqual(self.s.readonly, 'disabled')

    def test_foreign_boot_file_preserved(self):
        self.put(BOOT, '# external config')
        _, conflict = self.apply()
        self.assertTrue(conflict)
        self.assertEqual(self.s.read(BOOT), '# external config')

    def test_unsupported_boot_layout_skips_boot_only(self):
        self.put('/usr/bin/update-grub', 'unknown layout')
        self.apply()
        self.assertIsNone(self.s.read(BOOT))
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')

    def test_os_update_pauses_replay_then_rebaselines(self):
        self.apply({'nmi_watchdog': 0})
        self.put('/etc/os-release', 'ID=steamos\nBUILD_ID=test2')
        self.put('/proc/sys/kernel/nmi_watchdog', '2')
        notes, paused = self.engine.apply('test', {'nmi_watchdog': 0}, automatic=True)
        self.assertTrue(paused)
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '2')
        self.apply({'nmi_watchdog': 0})
        self.engine.apply('stock', {})
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '2')

    def test_reboot_rebaselines_runtime_rollback(self):
        self.apply({'swappiness': 100})
        self.put('/proc/sys/kernel/random/boot_id', 'boot2')
        self.put('/proc/sys/vm/swappiness', '80')
        self.apply({'swappiness': 100})
        self.engine.apply('stock', {})
        self.assertEqual(self.s.read('/proc/sys/vm/swappiness'), '80')

    def test_resume_static_known_reset_and_unknown_conflict(self):
        self.apply({'swappiness': 100})
        self.put('/proc/sys/vm/swappiness', '60')
        self.engine.apply('test', {'swappiness': 100}, automatic=True)
        self.assertEqual(self.s.read('/proc/sys/vm/swappiness'), '100')
        self.put('/proc/sys/vm/swappiness', '110')
        _, conflict = self.engine.apply('test', {'swappiness': 100}, automatic=True)
        self.assertTrue(conflict)
        self.assertEqual(self.s.read('/proc/sys/vm/swappiness'), '110')

    def test_noop_takes_no_ownership_or_auto_replay(self):
        self.apply({'nmi_watchdog': 1})
        self.assertFalse(self.engine.state['entries'])
        self.put('/proc/sys/kernel/nmi_watchdog', '0')
        self.engine.apply('test', {'nmi_watchdog': 1}, automatic=True)
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')

    def test_cpu_model_and_capability_validation(self):
        base = self.cpu()
        self.apply(PROFILES['gpu-game'])
        self.assertEqual(self.s.read(base + 'energy_performance_preference'), 'balance_power')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2800000')
        self.engine.apply('stock', {})
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')
        self.assertEqual(self.s.read(base + 'scaling_governor'), 'performance')
        self.assertEqual(self.s.read(base + 'energy_performance_preference'), 'performance')

    def test_power_manager_conflict(self):
        self.cpu()
        self.s.services['power-profiles-daemon.service'] = 'active'
        changes, notes = plan(self.s, {**PROFILES['gpu-game'], 'nmi_watchdog': 0})
        self.assertEqual([c.key for c in changes], ['nmi_watchdog'])
        self.assertTrue(any('another power manager' in n for n in notes))

    def test_resume_never_fights_steam_power(self):
        base = self.cpu()
        self.apply({'cpu_max_mhz': 2800})
        self.put(base + 'scaling_max_freq', '3000000')
        self.engine.apply('test', {'cpu_max_mhz': 2800}, automatic=True)
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3000000')

    def test_oled_supported_unknown_model_rejected(self):
        self.put('/sys/class/dmi/id/board_name', 'Galileo')
        self.assertEqual(self.s.info()['model'], 'OLED')
        self.apply({'nmi_watchdog': 0})
        self.put('/sys/class/dmi/id/board_name', 'Other handheld')
        with self.assertRaises(BosunError):
            self.apply()

    def test_recovery_after_interrupted_write(self):
        change = Change('swappiness', '/proc/sys/vm/swappiness', '150')
        old = json.loads(json.dumps(self.engine.state))
        self.engine.state['pending'] = {'state_before': old, 'writes': [{'change': dataclasses.asdict(change), 'before': '60'}]}
        self.engine.save()
        self.put(change.path, '150')
        Engine(self.s).recover()
        self.assertEqual(self.s.read(change.path), '60')
        self.assertIsNone(Engine(self.s).state['pending'])

    def test_recovery_conflict_retains_journal(self):
        change = Change('swappiness', '/proc/sys/vm/swappiness', '150')
        old = json.loads(json.dumps(self.engine.state))
        self.engine.state['pending'] = {'state_before': old, 'writes': [{'change': dataclasses.asdict(change), 'before': '60'}]}
        self.engine.save()
        self.put(change.path, '90')
        with self.assertRaises(BosunError):
            self.engine.recover()
        self.assertIsNotNone(Engine(self.s).state['pending'])

    def test_scalar_failure_restores_previous_writes(self):
        real = Change.put
        def fail(c, s, value):
            if c.key == 'swappiness' and value == '100':
                raise OSError('fixture write failure')
            return real(c, s, value)
        with patch.object(Change, 'put', fail), self.assertRaises(OSError):
            self.apply({'nmi_watchdog': 0, 'swappiness': 100})
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '1')
        self.assertFalse(self.engine.state['entries'])

    def test_tdp_limits_only_recognized_hwmon(self):
        self.put('/sys/class/hwmon/hwmon2/name', 'amdgpu')
        self.put('/sys/class/hwmon/hwmon2/power1_cap', '15000000')
        self.put('/sys/class/hwmon/hwmon3/name', 'unrelated')
        self.put('/sys/class/hwmon/hwmon3/power1_cap', '80000000')
        self.apply({'tdp_w': 10})
        self.assertEqual(self.s.read('/sys/class/hwmon/hwmon2/power1_cap'), '10000000')
        self.assertEqual(self.s.read('/sys/class/hwmon/hwmon3/power1_cap'), '80000000')
        self.engine.apply('stock', {})
        self.assertEqual(self.s.read('/sys/class/hwmon/hwmon2/power1_cap'), '15000000')

    def test_gpu_policy_and_storage_available_only(self):
        p = '/sys/class/drm/card1/device/'
        self.put(p + 'vendor', '0x1002')
        self.put(p + 'power_dpm_force_performance_level', 'auto')
        self.put('/sys/block/mmcblk0/queue/scheduler', '[mq-deadline] bfq none')
        self.put('/sys/block/mmcblk0/device/type', 'SD')
        self.apply({'gpu_mode': 'low', 'sd_scheduler': 'bfq'})
        self.assertEqual(self.s.read(p + 'power_dpm_force_performance_level'), 'low')
        self.engine.apply('stock', {})
        self.assertEqual(self.s.read(p + 'power_dpm_force_performance_level'), 'auto')

    def test_lavd_optional_no_kernel(self):
        changes, notes = plan(self.s, {'lavd': 'autopilot'})
        self.assertFalse(changes)
        self.assertTrue(notes)

    def test_lavd_own_service_lifecycle(self):
        self.put('/usr/bin/scx_lavd', '')
        self.put('/sys/kernel/sched_ext/state', 'disabled')
        self.put('/sys/kernel/btf/vmlinux', 'fixture')
        self.apply({'lavd': 'autopilot'})
        self.assertEqual(self.s.services['bosun-lavd.service'], 'active')
        self.engine.apply('stock', {})
        self.assertEqual(self.s.services['bosun-lavd.service'], 'inactive')
        self.assertIsNone(self.s.read('/etc/systemd/system/bosun-lavd.service'))

    def test_boot_snippet_preserves_unrelated_parameters_and_replaces_only_owned(self):
        snippet = boot_content(['mitigations=off', 'amdgpu.moverate=256'])
        result = subprocess.run(['bash', '-c', 'GRUB_CMDLINE_LINUX_DEFAULT="quiet mitigations=auto splash amdgpu.moverate=8"\n' + snippet + 'printf "%s" "$GRUB_CMDLINE_LINUX_DEFAULT"'], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.split(), ['quiet', 'splash', 'mitigations=off', 'amdgpu.moverate=256'])

    def test_validation_rejects_dangerous_limits_and_shell_strings(self):
        for settings in [{'tdp_w': 30}, {'cpu_max_mhz': 4200}, {'epp': 'power; reboot'}, {'smt': 'forceoff'}, {'lbpw': 2}, {'bad': 1}]:
            with self.assertRaises(BosunError):
                validate(settings)

    def test_cli_preview_apply_rollback_in_isolation(self):
        script = str(Path(__file__).resolve().parents[1] / 'bosun.py')
        for args in [['apply', 'recommended', '--dry-run'], ['apply', 'recommended'], ['status'], ['rollback']]:
            result = subprocess.run([sys.executable, script, '--root', str(self.root), *args], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '1')


class MeasurementTests(unittest.TestCase):
    def test_energy_units_and_integration(self):
        samples = [{'monotonic_s': 0, 'energy_wh': 40, 'battery_w': 12},
                   {'monotonic_s': 300, 'energy_wh': 39, 'battery_w': 12}]
        result = energy_metrics(samples)
        self.assertAlmostEqual(result['average_total_device_w'], 12)
        self.assertAlmostEqual(result['integrated_battery_wh'], 1)
        samples[1]['energy_wh'] = 40
        self.assertAlmostEqual(energy_metrics(samples)['average_total_device_w'], 12)

    def test_mangohud_headers_and_tail_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'raw.csv'
            p.write_text('os,cpu,gpu\nSteamOS,AMD,AMD\nfps,frametime,elapsed\n' + '60,16,1\n' * 99 + '10,100,2\n')
            result = frame_metrics(p)
            self.assertEqual(result['one_percent_low_fps'], 10)
            self.assertEqual(result['p99_ms'], 16)
            self.assertEqual(result['spikes_over_50ms'], 1)



# Load CLI/installer as modules so sessions and installation can run against fixtures.
def load_module(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = load_module('bosun_cli', 'bosun.py')
installer = load_module('bosun_installer', 'install.py')


class SessionTests(unittest.TestCase):
    setUp = CoreTests.setUp
    tearDown = CoreTests.tearDown
    put = CoreTests.put
    cpu = CoreTests.cpu
    apply = CoreTests.apply

    def test_overlay_session_restores_global_profile(self):
        base = self.cpu()
        self.apply({'nmi_watchdog': 0})
        token = 'a' * 32
        cli.session_action(self.engine, 'session-start', 'my-game', token, {'cpu_max_mhz': 2800})
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2800000')
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')
        cli.session_action(self.engine, 'session-end', 'my-game', token)
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')
        self.assertNotIn('session', self.engine.state)

    def test_session_refuses_second_session_and_boot_overlay(self):
        token = 'a' * 32
        with self.assertRaises(BosunError):
            cli.session_action(self.engine, 'session-start', 'unsafe', token, {'mitigations': 'off'})
        cli.session_action(self.engine, 'session-start', 'first', token, {})
        with self.assertRaises(BosunError):
            cli.session_action(self.engine, 'session-start', 'second', 'b' * 32, {})
        with self.assertRaises(BosunError):
            cli.session_action(self.engine, 'session-end', 'first', 'b' * 32)

    def test_broker_validated_request_and_duplicate_event(self):
        base = self.cpu()
        self.put('/home/.bosun/install.json', json.dumps({'user': 'fixture'}))
        token = 'a' * 32
        request = {'action': 'session-start', 'profile': 'my-game', 'token': token, 'settings': {'cpu_max_mhz': 2800}}
        self.put('/home/.bosun/requests/request.json', json.dumps(request))
        from types import SimpleNamespace
        with patch.object(cli.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid())):
            cli.broker(self.s)
            cli.broker(self.s)  # repeated inotify event must return the successful cached response
            result = json.loads(self.s.read('/home/.bosun/responses/' + token + '-session-start.json'))
            self.assertTrue(result['ok'], result)
            self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2800000')
            request.update(action='session-end', settings=None)
            self.put('/home/.bosun/requests/request.json', json.dumps(request))
            cli.broker(self.s)
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')

    def test_broker_rejects_symlink_or_invalid_payload(self):
        self.put('/home/.bosun/install.json', json.dumps({'user': 'fixture'}))
        path = self.s.path('/home/.bosun/requests/request.json')
        path.parent.mkdir(parents=True)
        path.symlink_to(self.s.path('/etc/os-release'))
        from types import SimpleNamespace
        with patch.object(cli.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid())):
            with self.assertRaises(OSError):
                cli.broker(self.s)
            path.unlink()
            path.write_text(json.dumps({'token': 'a' * 32, 'action': 'run-shell', 'command': 'reboot'}))
            with self.assertRaises(BosunError):
                cli.broker(self.s)

    def test_governor_side_effect_does_not_lose_epp_on_rollback(self):
        base = self.cpu()
        self.put(base + 'energy_performance_preference', 'balance_power')
        real = Change.put
        def side_effect(c, s, value):
            real(c, s, value)
            if c.key == 'governor':
                self.put(base + 'energy_performance_preference', 'performance')
        with patch.object(Change, 'put', side_effect):
            self.apply({'governor': 'powersave', 'epp': 'balance_power'})
            self.assertEqual(self.s.read(base + 'energy_performance_preference'), 'balance_power')
            self.engine.apply('stock', {})
        self.assertEqual(self.s.read(base + 'energy_performance_preference'), 'balance_power')

    def test_governor_not_changed_when_epp_has_foreign_value(self):
        base = self.cpu()
        self.apply({'governor': 'powersave', 'epp': 'balance_power'})
        self.put(base + 'energy_performance_preference', 'power')
        _, conflict = self.engine.apply('stock', {})
        self.assertTrue(conflict)
        self.assertEqual(self.s.read(base + 'scaling_governor'), 'powersave')
        self.assertEqual(self.s.read(base + 'energy_performance_preference'), 'power')

    def test_recovery_checks_all_values_before_governor_mutation(self):
        base = self.cpu()
        changes = [Change('governor', base + 'scaling_governor', 'powersave'),
                   Change('epp', base + 'energy_performance_preference', 'balance_power')]
        old = json.loads(json.dumps(self.engine.state))
        self.engine.state['pending'] = {'state_before': old, 'writes': [
            {'change': dataclasses.asdict(changes[0]), 'before': 'performance'},
            {'change': dataclasses.asdict(changes[1]), 'before': 'performance'}]}
        self.put(changes[0].path, 'powersave')
        self.put(changes[1].path, 'power')
        with self.assertRaises(BosunError):
            self.engine.recover()
        self.assertEqual(self.s.read(changes[0].path), 'powersave')
        self.assertEqual(self.s.read(changes[1].path), 'power')

    def test_reboot_does_not_restore_previous_boot_cpu_value(self):
        base = self.cpu()
        self.apply({'cpu_max_mhz': 2800})
        self.put('/proc/sys/kernel/random/boot_id', 'boot2')
        self.put(base + 'scaling_max_freq', '2800000')
        self.engine.apply('test', {'cpu_max_mhz': 2800}, automatic=True)
        self.engine.apply('stock', {})
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2800000')

    def test_missing_requested_interface_retains_other_ownership(self):
        base = self.cpu()
        self.apply({'cpu_max_mhz': 2800})
        self.s.services['tlp.service'] = 'active'
        self.apply({'cpu_max_mhz': 2800, 'nmi_watchdog': 0})
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2800000')
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')
        self.assertTrue(any(e['change']['key'] == 'cpu_max_mhz' for e in self.engine.state['entries'].values()))

    def test_external_scheduler_unit_change_does_not_stop_foreign_service(self):
        self.put('/usr/bin/scx_lavd', '')
        self.put('/sys/kernel/sched_ext/state', 'disabled')
        self.put('/sys/kernel/btf/vmlinux', 'fixture')
        self.apply({'lavd': 'autopilot'})
        self.put('/etc/systemd/system/bosun-lavd.service', '# changed by someone else')
        _, conflict = self.engine.apply('stock', {})
        self.assertTrue(conflict)
        self.assertEqual(self.s.services['bosun-lavd.service'], 'active')

    def test_dynamic_hwmon_identity_prevents_wrong_device_rollback(self):
        self.put('/sys/class/hwmon/hwmon2/name', 'amdgpu')
        self.put('/sys/class/hwmon/hwmon2/power1_cap', '15000000')
        self.apply({'tdp_w': 10})
        self.put('/sys/class/hwmon/hwmon2/name', 'unrelated')
        _, conflict = self.engine.apply('stock', {})
        self.assertTrue(conflict)
        self.assertEqual(self.s.read('/sys/class/hwmon/hwmon2/power1_cap'), '10000000')

    def test_release_epp_keeps_foreign_value_through_later_rollback(self):
        base = self.cpu()
        self.apply({'governor': 'powersave', 'epp': 'balance_power'})
        self.put(base + 'energy_performance_preference', 'power')
        released = self.engine.release('epp')
        self.assertIn(base + 'scaling_governor', released)
        self.engine.apply('stock', {})
        self.assertEqual(self.s.read(base + 'scaling_governor'), 'powersave')
        self.assertEqual(self.s.read(base + 'energy_performance_preference'), 'power')

    def test_removing_epp_restores_baseline_with_governor_retained(self):
        base = self.cpu()
        self.apply({'governor': 'powersave', 'epp': 'balance_power'})
        self.apply({'governor': 'powersave'})
        self.assertEqual(self.s.read(base + 'scaling_governor'), 'powersave')
        self.assertEqual(self.s.read(base + 'energy_performance_preference'), 'performance')


@unittest.skipUnless(os.geteuid() == 0, 'Installer fixture needs chown, never writes outside test root')
class InstallerTests(unittest.TestCase):
    setUp = CoreTests.setUp
    tearDown = CoreTests.tearDown
    put = CoreTests.put

    @contextlib.contextmanager
    def environment(self):
        from types import SimpleNamespace
        source = Path(__file__).resolve().parents[1]
        homedir = self.root / 'home/deck'
        homedir.mkdir(parents=True)
        base = self.root / 'home/.bosun'
        original_units = installer.unit_files()
        units = {str(self.s.path(k)): v for k, v in original_units.items()}
        user = SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_dir=str(homedir))
        with patch.object(installer, 'BASE', base), patch.object(installer, 'APP', base / 'app'), \
             patch.object(installer, 'unit_files', return_value=units), \
             patch.object(installer.pwd, 'getpwnam', return_value=user), \
             patch.object(installer.os, 'chown'), \
             patch('bosun.core.System', return_value=self.s):
            with contextlib.redirect_stdout(io.StringIO()):
                yield source, homedir, base, units

    def test_install_update_uninstall_no_system_packages(self):
        with self.environment() as (source, home, base, units):
            installer.install(source, 'deck', decky_enabled=False)
            self.assertTrue((base / 'app/bosun.py').exists())
            self.assertTrue((home / 'Desktop/Bosun.desktop').exists())
            self.assertFalse((base / 'state/state.json').exists())
            (base / 'state/profiles').mkdir()
            (base / 'state/profiles/retained.json').write_text('{}')
            installer.install(source, 'deck', decky_enabled=False)
            self.assertTrue((base / 'state/profiles/retained.json').exists())
            installer.uninstall()
            self.assertFalse((base / 'app').exists())
            self.assertFalse((home / 'Desktop/Bosun.desktop').exists())
            self.assertTrue(all(not Path(p).exists() for p in units))
            self.assertEqual(self.s.readonly, 'enabled')
            self.assertFalse(any(cmd[0] == 'pacman' and '-Q' not in cmd for cmd in self.s.commands))

    def test_install_refuses_preexisting_files(self):
        with self.environment() as (source, home, base, units):
            p = home / 'Desktop/Bosun.desktop'
            p.parent.mkdir()
            p.write_text('user file')
            with self.assertRaises(RuntimeError):
                installer.install(source, 'deck', decky_enabled=False)
            self.assertEqual(p.read_text(), 'user file')

    def test_failed_update_restores_application_and_files(self):
        with self.environment() as (source, home, base, units):
            installer.install(source, 'deck', decky_enabled=False)
            (base / 'app/COMMIT').write_text('previous code')
            original = (home / 'Desktop/Bosun.desktop').read_text()
            mode = (home / 'Desktop/Bosun.desktop').stat().st_mode
            real = self.s.command
            def fail(*args, **kwargs):
                if args[:2] == ('systemctl', 'enable'):
                    raise BosunError('simulated enable failure')
                return real(*args, **kwargs)
            with patch.object(self.s, 'command', fail), self.assertRaises(BosunError):
                installer.install(source, 'deck', decky_enabled=False)
            self.assertEqual((base / 'app/COMMIT').read_text(), 'previous code')
            self.assertEqual((home / 'Desktop/Bosun.desktop').read_text(), original)
            self.assertEqual((home / 'Desktop/Bosun.desktop').stat().st_mode, mode)
            self.assertEqual(self.s.readonly, 'enabled')


class DesktopTests(unittest.TestCase):
    def test_real_desktop_parser_and_bootstrap_success_failure(self):
        import ctypes
        import ctypes.util
        import time
        lib = ctypes.util.find_library('gio-2.0')
        if not lib:
            self.skipTest('GIO not available; desktop-file-validate runs in CI')
        gio = ctypes.CDLL(lib)
        gio.g_desktop_app_info_new_from_filename.argtypes = [ctypes.c_char_p]
        gio.g_desktop_app_info_new_from_filename.restype = ctypes.c_void_p
        gio.g_app_info_launch.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        gio.g_app_info_launch.restype = ctypes.c_int
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            output = root / 'args.json'
            # Exercise the exact shell argument used by the distributed .desktop installer.
            import configparser
            config = configparser.ConfigParser(interpolation=None)
            config.read(Path(__file__).resolve().parents[1] / 'Bosun-installer.desktop')
            raw = config['Desktop Entry']['Exec']
            # Let GIO parse the original desktop command, but intercept bash with a Python dumper.
            # Retain the original quoted command argument unchanged.
            prefix = installer.desktop_exec(['/usr/bin/bash', '-c'])
            self.assertTrue(raw.startswith(prefix + ' '))
            shell_argument = raw[len(prefix) + 1:]
            dump_code = 'import json,sys;from pathlib import Path;Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))'
            text = installer.desktop_entry(['/usr/bin/python3', '-c', dump_code, str(output)])
            text = text.replace('Terminal=true', 'Terminal=false').rstrip() + ' ' + shell_argument + '\n'
            desktop = root / 'test.desktop'
            desktop.write_text(text)
            app = gio.g_desktop_app_info_new_from_filename(str(desktop).encode())
            self.assertTrue(app)
            self.assertTrue(gio.g_app_info_launch(app, None, None, None))
            for _ in range(200):
                if output.exists():
                    break
                time.sleep(.01)
            command = json.loads(output.read_text())[0]
            (root / 'curl').write_text('#!/bin/sh\nif [ "${FAIL_DOWNLOAD:-0}" = 1 ]; then exit 22; fi\nwhile [ "$#" -gt 0 ]; do if [ "$1" = --output ]; then shift; printf test > "$1"; break; fi; shift; done\n')
            (root / 'python3').write_text('#!/bin/sh\necho test-installer-executed\n')
            (root / 'curl').chmod(0o755)
            (root / 'python3').chmod(0o755)
            for status in [0, 1]:
                result = subprocess.run(['/usr/bin/bash', '-c', command], input='\n', text=True, capture_output=True,
                                        env={**os.environ, 'PATH': str(root) + ':' + os.environ['PATH'], 'FAIL_DOWNLOAD': str(status)})
                self.assertEqual(result.returncode, 22 if status else 0, result.stdout + result.stderr)
                self.assertIn('Press Enter to close', result.stdout)
                self.assertEqual('test-installer-executed' in result.stdout, status == 0)


class ReadonlyTests(unittest.TestCase):
    setUp = CoreTests.setUp
    tearDown = CoreTests.tearDown
    put = CoreTests.put

    def test_readonly_lease_recovers_after_killed_process(self):
        self.put(STATE + '/readonly-lease.json', json.dumps({'original': 'enabled'}))
        self.s.readonly = 'disabled'
        self.engine.recover()
        self.assertEqual(self.s.readonly, 'enabled')
        self.assertIsNone(self.s.read(STATE + '/readonly-lease.json'))

    def test_status_nonzero_disabled_is_accepted(self):
        self.s.readonly = 'disabled'
        with self.s.writable():
            self.assertEqual(self.s.readonly, 'disabled')
        status_calls = [c for c in self.s.commands if c == ['steamos-readonly', 'status']]
        self.assertTrue(status_calls)
        self.assertEqual(self.s.readonly, 'disabled')


if __name__ == '__main__':
    unittest.main()
