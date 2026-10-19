# SPDX-License-Identifier: GPL-3.0-or-later
import errno
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_bosun as fixtures
installer = fixtures.installer
from bosun.control import Control, REBOOT_KEYS
from bosun.core import BOOT, ZRAM, STATE, BosunError, Change, Engine, plan
from bosun.dependencies import ensure_decky, install_package
from bosun.sessions import session_action


class ControlTests(unittest.TestCase):
    setUp = fixtures.CoreTests.setUp
    tearDown = fixtures.CoreTests.tearDown
    put = fixtures.CoreTests.put
    cpu = fixtures.CoreTests.cpu
    apply = fixtures.CoreTests.apply

    def control(self):
        return Control(self.s)

    def test_automatic_game_lifecycle(self):
        base = self.cpu()
        self.apply({'nmi_watchdog': 0})
        c = self.control()
        c.save_game('123', 'Game', {'cpu_max_mhz': 2800})
        c.activate('123')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2800000')
        first = Engine(self.s).state['session']['token']
        c.activate('123')
        self.assertEqual(Engine(self.s).state['session']['token'], first)
        c.activate('')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')
        self.assertNotIn('session', Engine(self.s).state)

    def test_editing_the_active_game_profile_applies_new_values_and_preserves_rollback(self):
        base = self.cpu()
        c = self.control()
        c.apply_global('global', {'nmi_watchdog': 0})
        c.save_game('123', 'Game', {'cpu_max_mhz': 2800})
        c.activate('123')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2800000')
        c.save_game('123', 'Game', {'cpu_max_mhz': 2400})
        c.activate('123')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2400000')
        c.activate('')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')

    def test_switch_game_preserves_global_baseline(self):
        base = self.cpu()
        self.apply({'cpu_max_mhz': 3000})
        c = self.control()
        c.save_game('123', 'One', {'cpu_max_mhz': 2800})
        c.save_game('456', 'Two', {'cpu_max_mhz': 2600})
        c.activate('123')
        c.activate('456')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2600000')
        c.activate('')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3000000')

    def test_known_resume_reset_and_unknown_external_change(self):
        base = self.cpu()
        c = self.control()
        c.save_game('123', 'Game', {'cpu_max_mhz': 2800})
        c.activate('123')
        self.put(base + 'scaling_max_freq', '3500000')
        c.activate('123', resume=True)
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2800000')
        self.put(base + 'scaling_max_freq', '3100000')
        with self.assertRaises(BosunError):
            c.activate('123', resume=True)
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3100000')

    def test_resume_replay_can_be_disabled(self):
        base = self.cpu()
        c = self.control()
        c.save_game('123', 'Game', {'cpu_max_mhz': 2800}, resume=False)
        c.activate('123')
        self.put(base + 'scaling_max_freq', '3500000')
        c.activate('123', resume=True)
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')

    def test_unmapped_game_restores_previous(self):
        base = self.cpu()
        c = self.control()
        c.save_game('123', 'Game', {'cpu_max_mhz': 2800})
        c.activate('123')
        c.activate('999')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')

    def test_does_not_hijack_cli_session(self):
        self.cpu()
        c = self.control()
        c.save_game('123', 'Game', {'cpu_max_mhz': 2800})
        session_action(self.engine, 'session-start', 'cli', 'a' * 32, {})
        with self.assertRaises(BosunError):
            c.activate('123')
        self.assertEqual(Engine(self.s).state['session']['token'], 'a' * 32)

    def test_rejects_boot_topology_and_malformed_game_profiles(self):
        c = self.control()
        for key in REBOOT_KEYS:
            with self.assertRaises(BosunError):
                c.save_game('123', 'Game', {key: 'invalid'})
        for appid in ['0', '../file', 'abc', '1; reboot']:
            with self.assertRaises(BosunError):
                c.save_game(appid, 'Game', {})
        with self.assertRaises(BosunError):
            c.save_game('123', 'Game', {'tdp_w': 40})

    def test_game_preview_preserves_global_settings(self):
        self.cpu()
        self.apply({'nmi_watchdog': 0})
        before = self.engine.file.read_text()
        notes = self.control().preview({'cpu_max_mhz': 2800}, game=True)['notes']
        self.assertFalse(any('nmi_watchdog' in n and '(remove)' in n for n in notes))
        self.assertEqual(self.engine.file.read_text(), before)

    def test_full_rollback_disables_auto_profiles(self):
        self.cpu()
        c = self.control()
        c.save_game('123', 'Game', {'cpu_max_mhz': 2800})
        c.activate('123')
        c.rollback()
        self.assertFalse(c.config()['automatic'])
        self.assertNotIn('session', Engine(self.s).state)
        c.activate('123')
        self.assertFalse(Engine(self.s).state['entries'])

    def test_os_update_pauses_automatic_game_profile_until_explicit_apply(self):
        base = self.cpu()
        self.apply({'nmi_watchdog': 0})
        c = self.control()
        c.save_game('123', 'Game', {'cpu_max_mhz': 2800})
        self.put('/proc/sys/kernel/osrelease', 'new-kernel')
        self.assertTrue(c.activate('123')['conflict'])
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3500000')
        c.apply_global('accepted-update', {'nmi_watchdog': 0})
        c.activate('123')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '2800000')

    def test_stale_session_after_reboot_does_not_block_global_apply(self):
        self.cpu()
        c = self.control()
        c.save_game('123', 'Game', {'cpu_max_mhz': 2800})
        c.activate('123')
        self.put('/proc/sys/kernel/random/boot_id', 'new-boot')
        c.apply_global('recommended', {'nmi_watchdog': 0})
        self.assertNotIn('session', Engine(self.s).state)

    def test_resolve_external_value_from_decky_keeps_it_after_game_exit(self):
        base = self.cpu()
        c = self.control()
        c.save_game('123', 'Game', {'cpu_max_mhz': 2800})
        c.activate('123')
        self.put(base + 'scaling_max_freq', '3100000')
        c.release('cpu_max_mhz')
        self.assertNotIn('cpu_max_mhz', c.config()['games']['123']['settings'])
        c.activate('')
        self.assertEqual(self.s.read(base + 'scaling_max_freq'), '3100000')



class NewTweakTests(unittest.TestCase):
    setUp = fixtures.CoreTests.setUp
    tearDown = fixtures.CoreTests.tearDown
    put = fixtures.CoreTests.put
    cpu = fixtures.CoreTests.cpu
    apply = fixtures.CoreTests.apply

    def test_compaction_and_storage_readahead_exact_restore(self):
        self.put('/proc/sys/vm/compaction_proactiveness', '20')
        self.put('/sys/block/mmcblk0/device/type', 'SD')
        self.put('/sys/block/mmcblk0/queue/read_ahead_kb', '128')
        self.put('/sys/block/mmcblk1/device/type', 'MMC')
        self.put('/sys/block/mmcblk1/queue/read_ahead_kb', '64')
        self.apply({'compaction_proactiveness': 0, 'sd_readahead_kb': 256})
        self.assertEqual(self.s.read('/proc/sys/vm/compaction_proactiveness'), '0')
        self.assertEqual(self.s.read('/sys/block/mmcblk0/queue/read_ahead_kb'), '256')
        self.assertEqual(self.s.read('/sys/block/mmcblk1/queue/read_ahead_kb'), '64')
        self.engine.apply('stock', {})
        self.assertEqual(self.s.read('/sys/block/mmcblk0/queue/read_ahead_kb'), '128')
        self.assertEqual(self.s.read('/proc/sys/vm/compaction_proactiveness'), '20')

    def test_zram_is_reboot_only_and_owned_override(self):
        self.put('/usr/lib/systemd/system-generators/zram-generator', 'generator fixture')
        self.put('/sys/block/zram0/comp_algorithm', 'lzo-rle [zstd] lz4')
        self.s.services['systemd-zram-setup@zram0.service'] = 'active'
        notes, _ = self.apply({'zram_algorithm': 'lz4', 'zram_size_percent': 100})
        self.assertIn('compression-algorithm = lz4', self.s.read(ZRAM))
        self.assertIn('ram * 100 / 100', self.s.read(ZRAM))
        self.assertEqual(self.s.read('/sys/block/zram0/comp_algorithm'), 'lzo-rle [zstd] lz4')
        self.assertTrue(any('REBOOT' in n for n in notes))
        self.assertFalse(any('swapoff' in cmd or 'restart' in cmd for cmd in self.s.commands))
        self.engine.apply('stock', {})
        self.assertIsNone(self.s.read(ZRAM))

    def test_foreign_zram_backend_skips_override_and_keeps_other_tweaks(self):
        self.apply({'zram_algorithm': 'lz4', 'nmi_watchdog': 0})
        self.assertIsNone(self.s.read(ZRAM))
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')

    def test_workqueue_is_boot_parameter(self):
        self.put('/sys/module/workqueue/parameters/power_efficient', 'N')
        self.apply({'workqueue_power_efficient': 1})
        self.assertIn('workqueue.power_efficient=1', self.s.read(BOOT))
        self.assertEqual(self.s.read('/sys/module/workqueue/parameters/power_efficient'), 'N')
        self.engine.apply('stock', {})
        self.assertIsNone(self.s.read(BOOT))

    def test_old_driver_never_gets_legacy_powersave_from_epp_preset(self):
        base = self.cpu()
        self.put(base + 'scaling_driver', 'acpi-cpufreq')
        self.apply({'governor': 'powersave', 'epp': 'balance_power'})
        self.assertEqual(self.s.read(base + 'scaling_governor'), 'performance')

    def test_readonly_runtime_write_retries_and_restores_mode(self):
        real = Change.put
        failed = []
        def simulate(c, system, value):
            if not failed:
                failed.append(True)
                raise OSError(errno.EROFS, 'read-only fixture')
            return real(c, system, value)
        with patch.object(Change, 'put', simulate):
            self.apply({'nmi_watchdog': 0})
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')
        self.assertEqual(self.s.readonly, 'enabled')

    def test_model_specs_and_manufacturer_tdp_limit(self):
        lcd = self.s.info()['model_specs']
        self.put('/sys/class/dmi/id/board_name', 'Galileo')
        oled = self.s.info()['model_specs']
        self.assertEqual((lcd['ram_mt_s'], oled['ram_mt_s']), (5500, 6400))
        self.assertEqual((lcd['battery_design_wh'], oled['battery_design_wh']), (40, 50))
        with self.assertRaises(BosunError):
            self.apply({'tdp_w': 3})

    def test_lbpw_is_not_applied_to_deck_rdna2(self):
        self.put('/sys/module/amdgpu/parameters/lbpw', '-1')
        notes, _ = self.apply({'lbpw': 0, 'nmi_watchdog': 0})
        self.assertIsNone(self.s.read(BOOT))
        self.assertEqual(self.s.read('/sys/module/amdgpu/parameters/lbpw'), '-1')
        self.assertTrue(any('GFX9' in n for n in notes))
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')


class DependencyTests(unittest.TestCase):
    setUp = fixtures.CoreTests.setUp
    tearDown = fixtures.CoreTests.tearDown
    put = fixtures.CoreTests.put

    def package_runner(self, installed=''):
        self.put('/usr/bin/pacman', 'fixture')
        real = self.s.command
        def runner(*args, **kwargs):
            if args[:2] == ('pacman', '-Si'):
                return 'Name : scx-scheds'
            if args[:2] == ('pacman', '-Sp'):
                return 'scx-scheds 1.1.4\n' + ('glibc 2.50' if 'glibc' in installed else '')
            if args[:2] == ('pacman', '-Q'):
                return installed if args[2] in installed else ''
            return real(*args, **kwargs)
        return runner

    def test_install_missing_software_from_current_repo_and_restore_ro(self):
        with patch.object(self.s, 'command', self.package_runner()):
            install_package(self.s, ['scx-scheds'], 'scx_lavd')
        self.assertIn(['pacman', '-S', '--noconfirm', 'scx-scheds'], self.s.commands)
        self.assertEqual(self.s.readonly, 'enabled')

    def test_refuse_dependency_upgrade_or_downgrade(self):
        with patch.object(self.s, 'command', self.package_runner('glibc 2.40')), self.assertRaises(BosunError):
            install_package(self.s, ['scx-scheds'], 'scx_lavd')
        self.assertFalse(any(cmd[:2] == ['pacman', '-S'] for cmd in self.s.commands))
        self.assertEqual(self.s.readonly, 'enabled')

    def test_same_version_reinstall_repairs_missing_binary(self):
        with patch.object(self.s, 'command', self.package_runner('scx-scheds 1.1.4')):
            install_package(self.s, ['scx-scheds'], 'scx_lavd')
        self.assertIn(['pacman', '-S', '--noconfirm', 'scx-scheds'], self.s.commands)

    def test_decky_release_download_digest_unit_and_existing_plugins(self):
        self.s.fake = False  # allow mocked HTTPS transport, paths still point into fixture root
        home = self.root / 'home/deck'
        existing = home / 'homebrew/plugins/OtherPlugin/plugin.json'
        existing.parent.mkdir(parents=True)
        existing.write_text('{"name":"OtherPlugin"}')
        blob = bytearray(64)
        blob[:6] = b'\x7fELF\x02\x01'
        blob[18:20] = b'\x3e\x00'
        blob = bytes(blob)
        release = {'tag_name': 'v3.3.0', 'assets': [{'name': 'PluginLoader', 'browser_download_url': 'https://github.com/release/PluginLoader', 'digest': 'sha256:' + hashlib.sha256(blob).hexdigest()}]}
        def transport(url, limit=0):
            return json.dumps(release).encode() if url.endswith('/latest') else blob
        user = SimpleNamespace(pw_dir=str(home), pw_uid=1000, pw_gid=1000)
        with patch('bosun.dependencies.fetch', transport), patch('bosun.dependencies.os.chown'):
            result, _ = ensure_decky(self.s, user, start=True)
        self.assertEqual((result / 'services/PluginLoader').read_bytes(), blob)
        self.assertTrue(existing.exists())
        self.assertIn('UNPRIVILEGED_PATH=', self.s.read('/etc/systemd/system/plugin_loader.service'))
        self.assertEqual(self.s.readonly, 'enabled')

    def test_decky_wrong_binary_or_digest_is_rejected(self):
        self.s.fake = False
        home = self.root / 'home/deck'
        user = SimpleNamespace(pw_dir=str(home), pw_uid=1000, pw_gid=1000)
        release = {'assets': [{'name': 'PluginLoader', 'browser_download_url': 'https://github.com/release/PluginLoader'}]}
        with patch('bosun.dependencies.fetch', side_effect=[json.dumps(release).encode(), b'not an ELF']), self.assertRaises(BosunError):
            ensure_decky(self.s, user)
        self.assertFalse((home / 'homebrew/services/PluginLoader').exists())


@unittest.skipUnless(__import__('os').geteuid() == 0, 'Installer fixture requires root ownership')
class DeckyInstallerTests(unittest.TestCase):
    setUp = fixtures.CoreTests.setUp
    tearDown = fixtures.CoreTests.tearDown
    put = fixtures.CoreTests.put
    cpu = fixtures.CoreTests.cpu
    environment = fixtures.InstallerTests.environment

    def test_installed_plugin_privileges_match_decky_and_can_manage_protected_state(self):
        # Decky sandboxed_plugin.py chooses EFFECTIVE_USER only for the literal "root" flag.
        # Exercise the installed backend in a real child process, not with mocked filesystem permissions.
        probe = r'''
import asyncio, importlib.util, json, os, pathlib, sys
plugin_dir, root, app = map(pathlib.Path, sys.argv[1:])
flags = json.loads((plugin_dir / 'plugin.json').read_text())['flags']
if 'root' not in flags:
    # Some local containers map only UID/GID 0. Ubuntu CI can exercise both identities.
    maps = [pathlib.Path('/proc/self/' + name).read_text().splitlines() for name in ['uid_map', 'gid_map']]
    if not all(any(int(row.split()[0]) <= 1000 < int(row.split()[0]) + int(row.split()[2]) for row in rows) for rows in maps):
        print(json.dumps({'unmapped_uid': True}))
        sys.exit(0)
    os.setgid(1000)
    os.setuid(1000)
sys.path.insert(0, str(app))
spec = importlib.util.spec_from_file_location('bosun_decky_probe', plugin_dir / 'main.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from bosun.core import System, STATE
from bosun.control import Control
async def main():
    plugin = module.Plugin()
    await plugin._main()
    plugin.control = Control(System(str(root)))
    saved = await plugin.save_game('123', 'Permission regression', {'nmi_watchdog': 0})
    if os.geteuid() != 0:
        denied = False
        try:
            (root / STATE.lstrip('/') / 'lock').open('a').close()
        except PermissionError:
            denied = True
        updated = await plugin.update()
        print(json.dumps({'uid': os.geteuid(), 'saved': saved, 'updated': updated, 'lock_denied': denied}))
        return
    assert saved['ok'], saved
    result = await plugin.apply_global('permission-probe', {'nmi_watchdog': 0})
    assert result['ok'], result
    assert (root / 'proc/sys/kernel/nmi_watchdog').read_text().strip() == '0'
    result = await plugin.rollback()
    assert result['ok'], result
    assert (root / 'proc/sys/kernel/nmi_watchdog').read_text().strip() == '1'
    lock = root / STATE.lstrip('/') / 'lock'
    print(json.dumps({'uid': os.geteuid(), 'lock_uid': lock.stat().st_uid, 'saved': saved}))
asyncio.run(main())
'''
        with self.environment() as (source, home, base, units):
            self.root.chmod(0o755)
            with patch('bosun.dependencies.ensure_decky', return_value=(home / 'homebrew', [])):
                installer.install(source, 'deck')
            plugin = home / 'homebrew/plugins/Bosun'
            args = [sys.executable, '-c', probe, str(plugin), str(self.root), str(base / 'app')]
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            good = json.loads(result.stdout)
            self.assertEqual((good['uid'], good['lock_uid']), (0, 0))
            self.assertTrue(good['saved']['ok'])
            self.assertEqual((base / 'state').stat().st_mode & 0o777, 0o700)
            manifest = json.loads((plugin / 'plugin.json').read_text())
            manifest['flags'] = ['_root']
            (plugin / 'plugin.json').write_text(json.dumps(manifest))
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            bad = json.loads(result.stdout)
            if bad.get('unmapped_uid'):
                return
            self.assertEqual(bad['uid'], 1000)
            self.assertTrue(bad['lock_denied'])
            for reply in [bad['saved'], bad['updated']]:
                self.assertFalse(reply['ok'])
                self.assertIn('не предоставил права root', reply['error'])

    def test_old_installer_stage_fetches_new_file_list_from_same_commit(self):
        stage = self.root / 'old-download'
        stage.mkdir()
        (stage / 'install.py').write_text('old file list fixture')
        (stage / 'COMMIT').write_text('a' * 40 + '\n')
        source = Path(__file__).resolve().parents[1]
        temp = SimpleNamespace(cleanup=lambda: None)
        with patch('sys.argv', ['install.py', '--source', str(stage), '--user', 'deck']), \
             patch.object(installer, 'download', return_value=(temp, source)) as download, \
             patch.object(installer, 'install') as install:
            self.assertEqual(installer.main(), 0)
            download.assert_called_once_with(revision='a' * 40)
            install.assert_called_once_with(source, 'deck', decky_enabled=True)

    def test_plugin_install_update_uninstall_and_profile_retention(self):
        with self.environment() as (source, home, base, units):
            with patch('bosun.dependencies.ensure_decky', return_value=(home / 'homebrew', [])) as ensure:
                installer.install(source, 'deck')
                self.assertTrue((home / 'homebrew/plugins/Bosun/dist/index.js').exists())
                self.assertTrue(ensure.call_args.kwargs.get('start'))
                (base / 'state/games.json').write_text('{"schema":1,"automatic":true,"games":{}}')
                installer.install(source, 'deck')
                self.assertTrue((base / 'state/games.json').exists())
                self.cpu()
                c = Control(self.s)
                c.apply_global('global', {'nmi_watchdog': 0})
                c.save_game('123', 'Game', {'cpu_max_mhz': 2800})
                c.activate('123')
                installer.uninstall()
                self.assertFalse((home / 'homebrew/plugins/Bosun/plugin.json').exists())
                self.assertIn(['systemctl', 'restart', 'plugin_loader.service'], self.s.commands)
                c.activate('')  # Decky unload after app removal must not reapply global tuning.
                self.assertFalse(Engine(self.s).state['entries'])
                self.assertFalse(c.config()['automatic'])
                self.assertNotIn('session', Engine(self.s).state)

    def test_foreign_plugin_file_is_not_overwritten(self):
        with self.environment() as (source, home, base, units):
            file = home / 'homebrew/plugins/Bosun/main.py'
            file.parent.mkdir(parents=True)
            file.write_text('other owner')
            with patch('bosun.dependencies.ensure_decky', return_value=(home / 'homebrew', [])), self.assertRaises(RuntimeError):
                installer.install(source, 'deck')
            self.assertEqual(file.read_text(), 'other owner')
