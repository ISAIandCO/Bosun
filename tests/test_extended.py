# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_bosun as fixtures
from bosun.core import BOOT, STATE, BosunError, Change, Engine, plan, validate
from bosun.control import Control
from bosun.launch import compose
from bosun.tweaks import SCALARS, STORAGE, ZRAM_QUEUE


class ExtendedTests(unittest.TestCase):
    setUp = fixtures.CoreTests.setUp
    tearDown = fixtures.CoreTests.tearDown
    put = fixtures.CoreTests.put
    apply = fixtures.CoreTests.apply
    cpu = fixtures.CoreTests.cpu

    def test_recommended_mglru_enables_documented_bits_without_losing_other_bits(self):
        self.put('/sys/kernel/mm/lru_gen/enabled', '0x8')
        self.apply({'mglru': 'full'})
        self.assertEqual(self.s.read('/sys/kernel/mm/lru_gen/enabled'), '0xf')
        self.engine.apply('stock', {})
        self.assertEqual(self.s.read('/sys/kernel/mm/lru_gen/enabled'), '0x8')

    def test_measurement_tools_reuses_installed_tool_or_installs_native_provider(self):
        c = Control(self.s)
        self.put('/usr/bin/mangohud', '')
        with patch('bosun.dependencies.install_package') as install:
            self.assertIn('already installed', c.measurement_tools()['notes'][0])
            install.assert_not_called()
            self.s.path('/usr/bin/mangohud').unlink()
            install.return_value = 'Installed mangohud'
            self.assertEqual(c.measurement_tools()['notes'][0], 'Installed mangohud')
            install.assert_called_once_with(self.s, ['mangohud'], 'mangohud')

    def test_ignored_driver_write_is_not_reported_as_applied(self):
        with patch.object(Change, 'put'), self.assertRaisesRegex(BosunError, 'requested value'):
            self.apply({'nmi_watchdog': 0})
        self.assertFalse(self.engine.state['entries'])
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '1')

    def test_boot_modes_are_gated_and_restore_on_both_models(self):
        self.cpu()
        self.put('/sys/module/gpu_sched/parameters/sched_policy', '1')
        self.put('/sys/module/amd_pstate/parameters/epp_boost', 'N')
        self.put('/sys/module/usbhid/parameters/mousepoll', '0')
        for model in ['Jupiter', 'Galileo']:
            self.put('/sys/class/dmi/id/board_name', model)
            notes, conflict = self.apply({'amd_pstate': 'active', 'gpu_sched_policy': 0, 'epp_boost': 1, 'usb_poll_ms': 1})
            self.assertFalse(conflict)
            for text in ['amd_pstate=active', 'gpu_sched.sched_policy=0', 'amd_pstate.epp_boost=1', 'usbhid.jspoll=1']:
                self.assertIn(text, self.s.read(BOOT))
            self.assertEqual(self.s.read('/sys/module/gpu_sched/parameters/sched_policy'), '1')
            self.engine.apply('stock', {})
            self.assertIsNone(self.s.read(BOOT))
        self.s.path('/sys/module/amd_pstate/parameters/epp_boost').unlink()
        notes, _ = self.apply({'epp_boost': 1, 'nmi_watchdog': 0})
        self.assertIsNone(self.s.read(BOOT))
        self.assertTrue(any('epp_boost' in n for n in notes))
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')

    def test_pstate_does_not_assume_bios_cppc_support(self):
        self.assertFalse(plan(self.s, {'amd_pstate': 'active'})[0])
        self.put('/sys/devices/system/cpu/cpu0/acpi_cppc/highest_perf', '166')
        self.assertTrue(plan(self.s, {'amd_pstate': 'active'})[0])

    def dirty(self):
        for stem, ratio in [('dirty_background', 10), ('dirty', 20)]:
            self.put('/proc/sys/vm/' + stem + '_bytes', '0')
            self.put('/proc/sys/vm/' + stem + '_ratio', str(ratio))

    def test_writeback_saves_ratios_and_restores_them_after_byte_mode(self):
        self.dirty()
        self.apply({'dirty_background_mib': 64, 'dirty_mib': 256})
        self.assertEqual(self.s.read('/proc/sys/vm/dirty_ratio'), '0')
        state = json.dumps(self.engine.state, sort_keys=True)
        self.apply({'dirty_background_mib': 64, 'dirty_mib': 256})
        self.assertEqual(json.dumps(self.engine.state, sort_keys=True), state)
        self.engine.apply('stock', {})
        self.assertEqual(self.s.read('/proc/sys/vm/dirty_background_bytes'), '0')
        self.assertEqual(self.s.read('/proc/sys/vm/dirty_background_ratio'), '10')
        self.assertEqual(self.s.read('/proc/sys/vm/dirty_ratio'), '20')

    def test_writeback_partial_failure_recovers_related_ratio(self):
        self.dirty()
        real = Change.put
        def fail(change, system, value):
            if change.key == 'dirty_mib':
                raise BosunError('simulated second write failure')
            real(change, system, value)
        with patch.object(Change, 'put', fail), self.assertRaises(BosunError):
            self.apply({'dirty_background_mib': 64, 'dirty_mib': 256})
        self.assertEqual(self.s.read('/proc/sys/vm/dirty_background_ratio'), '10')
        self.assertEqual(self.s.read('/proc/sys/vm/dirty_background_bytes'), '0')
        self.assertFalse(self.engine.state['entries'])

    def test_external_writeback_ratio_is_detected_and_pair_can_be_released(self):
        self.dirty()
        c = Control(self.s)
        c.apply_global('writeback', {'dirty_background_mib': 64, 'dirty_mib': 256})
        self.put('/proc/sys/vm/dirty_bytes', '0')
        self.put('/proc/sys/vm/dirty_ratio', '15')
        self.assertTrue(c.rollback()['conflict'])
        c.release('dirty_mib')
        self.assertFalse(Engine(self.s).state['desired'])
        self.assertEqual(self.s.read('/proc/sys/vm/dirty_ratio'), '15')

    def test_rejects_invalid_writeback_and_scheduler_combinations(self):
        for settings in [{'dirty_mib': 400}, {'dirty_background_mib': 256, 'dirty_mib': 64},
                         {'nvme_scheduler': 'none', 'nvme_kyber_read_us': 1000},
                         {'sd_scheduler': 'mq-deadline', 'sd_bfq_slice_idle_us': 0}]:
            with self.assertRaises(BosunError):
                validate(settings)

    def mount_runner(self, info):
        self.put('/usr/bin/findmnt', 'fixture')
        self.put('/usr/bin/mount', 'fixture')
        real = self.s.command
        def run(*args, **kwargs):
            if args[0] == 'findmnt':
                return json.dumps({'filesystems': [info]})
            if args[0] == 'mount':
                self.s.commands.append(list(args))
                tokens = args[2].split(',')
                info['vfs-options'] = ','.join(t for t in tokens if t != 'remount')
                return ''
            return real(*args, **kwargs)
        return run

    def test_noatime_changes_only_managed_flags_and_restores_exact_policy(self):
        info = {'target': '/home', 'source': '/dev/nvme0n1p8', 'fstype': 'ext4', 'vfs-options': 'rw,nosuid,nodev,relatime,lazytime'}
        with patch.object(self.s, 'command', self.mount_runner(info)):
            self.apply({'home_noatime': 1})
            self.assertIn('noatime', info['vfs-options'])
            self.assertIn('lazytime', info['vfs-options'])
            before = len([c for c in self.s.commands if c[0] == 'mount'])
            self.apply({'home_noatime': 1})
            self.assertEqual(before, len([c for c in self.s.commands if c[0] == 'mount']))
            self.engine.apply('stock', {})
            self.assertIn('relatime', info['vfs-options'])
            self.assertNotIn('noatime', info['vfs-options'])
            self.assertIn('lazytime', info['vfs-options'])
            self.assertEqual(self.s.commands[-1][-2:], ['/dev/nvme0n1p8', '/home'])

    def test_noatime_does_not_touch_replaced_mount(self):
        info = {'target': '/home', 'source': '/dev/nvme0n1p8', 'fstype': 'btrfs', 'vfs-options': 'rw,strictatime'}
        with patch.object(self.s, 'command', self.mount_runner(info)):
            self.apply({'home_noatime': 1})
            info['source'] = '/dev/other'
            self.assertTrue(self.engine.apply('stock', {})[1])
            self.assertIn('noatime', info['vfs-options'])

    def test_zswap_disable_is_conditional_and_never_flushes_swap(self):
        self.put('/sys/module/zswap/parameters/enabled', 'Y')
        self.apply({'zswap': 'off'})
        self.assertEqual(self.s.read('/sys/module/zswap/parameters/enabled'), 'N')
        self.engine.apply('stock', {})
        self.assertEqual(self.s.read('/sys/module/zswap/parameters/enabled'), 'Y')
        self.put('/proc/swaps', 'Filename Type Size Used Priority\n/dev/zram0 partition 8000 0 100\n/swapfile file 8000 0 -2')
        self.assertFalse(plan(self.s, {'zswap': 'off'})[0])
        self.assertFalse(any('swapoff' in c for c in self.s.commands))

    def test_new_memory_controls_restore_values_and_enforce_ram_reserve_bound(self):
        self.put('/proc/meminfo', 'MemTotal: 16777216 kB')
        settings = {'page_lock_unfairness': 8, 'watermark_scale_factor': 125, 'min_free_kbytes': 131072, 'khugepaged_scan_sleep_ms': 8000}
        originals = {'page_lock_unfairness': '5', 'watermark_scale_factor': '10', 'min_free_kbytes': '65536', 'khugepaged_scan_sleep_ms': '10000'}
        for key, value in originals.items():
            self.put(SCALARS[key][0], value)
        self.apply(settings)
        self.engine.apply('stock', {})
        for key, value in originals.items():
            self.assertEqual(self.s.read(SCALARS[key][0]), value)
        self.put('/proc/meminfo', 'MemTotal: 4194304 kB')
        self.assertFalse(plan(self.s, {'min_free_kbytes': 131072})[0])

    def test_storage_tunables_exclude_emmc_and_wait_for_matching_scheduler(self):
        self.put('/sys/block/mmcblk0/device/type', 'SD')
        self.put('/sys/block/mmcblk1/device/type', 'MMC')
        for block in ['mmcblk0', 'mmcblk1']:
            self.put('/sys/block/' + block + '/queue/scheduler', 'none [bfq]')
            self.put('/sys/block/' + block + '/queue/iosched/slice_idle_us', '8000')
        self.apply({'sd_bfq_slice_idle_us': 0})
        self.assertEqual(self.s.read('/sys/block/mmcblk0/queue/iosched/slice_idle_us'), '0')
        self.assertEqual(self.s.read('/sys/block/mmcblk1/queue/iosched/slice_idle_us'), '8000')
        self.engine.apply('stock', {})
        self.assertEqual(self.s.read('/sys/block/mmcblk0/queue/iosched/slice_idle_us'), '8000')
        self.put('/sys/block/nvme0n1/queue/scheduler', '[none] kyber')
        self.put('/sys/block/nvme0n1/queue/iosched/read_lat_nsec', '2000000')
        self.assertFalse(plan(self.s, {'nvme_kyber_read_us': 1000})[0])

    def test_memlock_configuration_has_reboot_notice_and_owns_only_its_dropins(self):
        self.put('/home/.bosun/install.json', '{"user":"deck"}')
        user = SimpleNamespace(pw_uid=1000)
        with patch('pwd.getpwnam', return_value=user):
            notes, _ = self.apply({'memlock_kb': 262144})
            self.assertIn('deck hard memlock 262144', self.s.read('/etc/security/limits.d/90-bosun.conf'))
            self.assertIn('268435456', self.s.read('/etc/systemd/system/user@1000.service.d/90-bosun.conf'))
            self.assertTrue(any('REBOOT' in n for n in notes))
            self.engine.apply('stock', {})
        self.assertIsNone(self.s.read('/etc/security/limits.d/90-bosun.conf'))

    def test_lavd_mode_change_restarts_owned_service(self):
        self.put('/usr/bin/scx_lavd', 'fixture')
        self.put('/sys/kernel/sched_ext/state', 'disabled')
        self.put('/sys/kernel/btf/vmlinux', 'fixture')
        self.apply({'lavd': 'autopilot'})
        self.put('/sys/kernel/sched_ext/state', 'enabled')
        self.put('/sys/kernel/sched_ext/root/ops', 'lavd')
        self.apply({'lavd': 'balanced'})
        self.assertIn('--balanced', self.s.read('/etc/systemd/system/bosun-lavd.service'))
        self.assertIn(['systemctl', 'restart', 'bosun-lavd.service'], self.s.commands)
        self.engine.apply('stock', {})
        self.assertEqual(self.s.services['bosun-lavd.service'], 'inactive')

    def test_lavd_failed_mode_transaction_restores_unit_before_restarting_old_mode(self):
        self.put('/usr/bin/scx_lavd', 'fixture')
        self.put('/sys/kernel/sched_ext/state', 'disabled')
        self.put('/sys/kernel/btf/vmlinux', 'fixture')
        self.apply({'lavd': 'autopilot'})
        self.put('/sys/kernel/sched_ext/state', 'enabled')
        self.put('/sys/kernel/sched_ext/root/ops', 'lavd')
        real = Change.put
        recorded, failed = [], []
        def simulate(change, system, value):
            real(change, system, value)
            if change.kind == 'service' and value == 'active':
                recorded.append(system.read('/etc/systemd/system/bosun-lavd.service'))
                if not failed:
                    failed.append(True)
                    system.services['bosun-lavd.service'] = 'failed'
                    raise BosunError('failure after scheduler restart')
        with patch.object(Change, 'put', simulate), self.assertRaises(BosunError):
            self.apply({'lavd': 'balanced'})
        self.assertIn('--balanced', recorded[0])
        self.assertIn('--autopilot', recorded[-1])
        self.assertEqual(self.s.services['bosun-lavd.service'], 'active')
        self.assertEqual(self.engine.state['desired'], {'lavd': 'autopilot'})


class LaunchTests(unittest.TestCase):
    setUp = fixtures.CoreTests.setUp
    tearDown = fixtures.CoreTests.tearDown
    put = fixtures.CoreTests.put

    account = '76561198000000001'

    def test_preview_does_not_install_or_write_and_preserves_game_arguments(self):
        result = compose(self.s, '--language ru', {'cache_gb': 4, 'io_priority': 0})
        self.assertIn('MESA_SHADER_CACHE_MAX_SIZE=4G', result['target'])
        self.assertIn('/usr/bin/ionice -c 2 -n 0 /usr/bin/env %command% --language ru', result['target'])
        self.assertFalse(any(c[0] == 'pacman' for c in self.s.commands))
        self.assertFalse(self.s.path(STATE + '/games.json').exists())

    @unittest.skipUnless(shutil.which('ionice'), 'native util-linux ionice unavailable')
    def test_generated_prefix_executes_with_existing_environment_assignments(self):
        original = 'MANGOHUD=1 %command% --language "ru RU"'
        target = compose(self.s, original, {'cache_gb': 4, 'io_priority': 0})['target']
        code = 'import json,os,sys; print(json.dumps([os.environ.get("MANGOHUD"),os.environ.get("MESA_SHADER_CACHE_MAX_SIZE"),sys.argv[1:]]))'
        command = target.replace('%command%', shlex.join([sys.executable, '-c', code]))
        result = subprocess.run(['/bin/sh', '-c', command], text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), ['1', '4G', ['--language', 'ru RU']])

    def test_compound_shell_is_retained_for_manual_editor_but_not_wrapped_as_a_preset(self):
        text = 'cd /tmp && %command%'
        self.assertEqual(compose(self.s, text, {})['target'], text)
        with self.assertRaises(BosunError):
            compose(self.s, text, {'cache_gb': 4})

    def test_multilib_radv_uses_only_flags_supported_by_both_installed_libraries(self):
        self.put('/usr/lib/libvulkan_radeon.so', '\x00nircache\x00cswave32\x00')
        self.put('/usr/lib32/libvulkan_radeon.so', '\x00cswave32\x00')
        result = compose(self.s, '%command%', {'radv_wave32': 'compute', 'radv_nir_cache': True})
        self.assertIn('RADV_PERFTEST=cswave32', result['target'])
        self.assertNotIn('nircache', result['target'])

    def test_radv_flags_must_be_advertised_by_installed_driver(self):
        self.put('/usr/lib/libvulkan_radeon.so', '\x00cswave32\x00nircache\x00')
        result = compose(self.s, 'gamemoderun %command% --arg', {'radv_wave32': 'all', 'radv_nir_cache': True})
        self.assertIn('RADV_PERFTEST=cswave32,nircache', result['target'])
        self.assertTrue(any('gewave32' in n and 'SKIP' in n for n in result['notes']))
        self.assertIn('gamemoderun %command% --arg', result['target'])

    def test_detects_conflicting_environment_and_rejects_unbounded_options(self):
        for text, options in [('RADV_PERFTEST=sam %command%', {'radv_wave32': 'compute'}),
                              ('%command%', {'cache_gb': 999}), ('%command%', {'io_priority': -1}),
                              ('%command%', {'sync': 'esync'}), ('%command%', {'arbitrary': 'value'})]:
            if options.get('radv_wave32'):
                self.put('/usr/lib/libvulkan_radeon.so', '\x00cswave32\x00')
            with self.assertRaises(BosunError):
                compose(self.s, text, options)

    def test_topology_selects_one_online_thread_per_core(self):
        self.put('/usr/bin/taskset', 'fixture')
        for cpu in range(8):
            self.put(f'/sys/devices/system/cpu/cpu{cpu}/topology/core_id', str(cpu % 4))
            self.put(f'/sys/devices/system/cpu/cpu{cpu}/topology/physical_package_id', '0')
        result = compose(self.s, '%command%', {'affinity': 'physical'})
        self.assertIn('--cpu-list 0,1,2,3 /usr/bin/env %command%', result['target'])

    def test_launch_backup_write_confirm_conflict_and_exact_restore(self):
        c = Control(self.s)
        before = 'MANGOHUD=1 %command% --language "ru RU"'
        new = 'MESA_SHADER_CACHE_MAX_SIZE=4G ' + before
        c.prepare_launch(self.account, '123', before, new)
        self.assertTrue(c.config()['launch_options'][self.account + ':123']['pending'])
        with self.assertRaises(BosunError):
            c.finish_launch(self.account, '123', before)
        c.finish_launch(self.account, '123', new)
        with self.assertRaises(BosunError):
            c.prepare_launch(self.account, '123', '--external', '', 'restore')
        self.assertEqual(c.prepare_launch(self.account, '123', new, '', 'restore')['target'], before)
        c.finish_launch(self.account, '123', before)
        self.assertFalse(c.config()['launch_options'])

    def test_interrupted_launch_write_can_be_restored_even_if_not_applied(self):
        c = Control(self.s)
        c.prepare_launch(self.account, '123', '', 'new')
        self.assertEqual(c.prepare_launch(self.account, '123', '', '', 'restore')['target'], '')
        c.finish_launch(self.account, '123', '')
        self.assertFalse(c.config()['launch_options'])

    def test_external_launch_options_can_be_kept_without_erasing_other_accounts(self):
        c = Control(self.s)
        other = '76561198000000002'
        c.prepare_launch(self.account, '123', '', 'A')
        c.prepare_launch(other, '123', 'other', 'B')
        c.prepare_launch(self.account, '123', 'external', '', 'keep')
        self.assertNotIn(self.account + ':123', c.config()['launch_options'])
        self.assertEqual(c.config()['launch_options'][other + ':123']['original'], 'other')
        with self.assertRaises(BosunError):
            c.rollback()

    def test_preset_reapplication_uses_original_without_prefix_stacking(self):
        c = Control(self.s)
        first = c.prepare_launch(self.account, '123', '--arg', '', options={'cache_gb': 2})['target']
        c.finish_launch(self.account, '123', first)
        second = c.prepare_launch(self.account, '123', first, '', options={'cache_gb': 4})['target']
        self.assertEqual(second.count('MESA_SHADER_CACHE_MAX_SIZE'), 1)
        self.assertIn('=4G', second)
