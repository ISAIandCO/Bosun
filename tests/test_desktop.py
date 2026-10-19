# SPDX-License-Identifier: GPL-3.0-or-later
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import test_bosun as fixtures
from bosun.control import Control
from bosun.core import APP, BOOT, GLOBAL_KEYS, BosunError, Engine
from bosun.desktop import call, dispatch


class ControlPresentationTests(unittest.TestCase):
    setUp = fixtures.CoreTests.setUp
    tearDown = fixtures.CoreTests.tearDown
    put = fixtures.CoreTests.put
    cpu = fixtures.CoreTests.cpu

    def test_game_choices_filter_every_global_key_and_boot_only_recipe(self):
        control = Control(self.s)
        control.save_profile('boot-only', {'mitigations': 'off'})
        state = control.state()
        self.assertNotIn('boot-only', state['game_profiles'])
        self.assertIn('boot-only', state['profiles'])
        self.assertIn('stock', state['game_profiles'])
        self.assertTrue(state['game_profiles']['recommended'])
        for settings in state['game_profiles'].values():
            self.assertFalse(GLOBAL_KEYS.intersection(settings))
        self.assertFalse(GLOBAL_KEYS.intersection(state['runtime_keys']))
        self.assertIn('mitigations', state['reboot_keys'])
        self.assertNotIn('home_noatime', state['reboot_keys'])
        self.assertEqual(set(state['groups']), set(state['catalog']))

    def test_game_save_and_preview_reject_global_without_mutation(self):
        control = Control(self.s)
        for settings in [{'mitigations': 'off'}, {'zram_algorithm': 'zstd'}, {'memlock_kb': 1024}, {'smt': 'off'}]:
            for method, args in [('save_game', ['10', 'Game', settings]), ('preview', [settings, True])]:
                with self.subTest(method=method, settings=settings), self.assertRaises(BosunError):
                    dispatch(control, {'method': method, 'args': args})
        self.assertFalse(control.file.exists())
        self.assertFalse(Engine(self.s).file.exists())
        self.assertIsNone(self.s.read(BOOT))

    def test_custom_recipes_shared_validated_and_do_not_apply(self):
        control = Control(self.s)
        control.save_profile('my-game', {'cpu_max_mhz': 2400})
        self.assertEqual(Control(self.s).state()['game_profiles']['my-game'], {'cpu_max_mhz': 2400})
        self.assertFalse(Engine(self.s).file.exists())
        for name in ['recommended', '../oops', '', 'a' * 65, 10]:
            for method in [control.save_profile, control.remove_profile]:
                with self.subTest(name=name, method=method), self.assertRaises(BosunError):
                    method(name, {}) if method == control.save_profile else method(name)
        with self.assertRaises(BosunError):
            control.save_profile('bad', {'cpu_max_mhz': 99999})
        control.remove_profile('my-game')
        control.remove_profile('my-game')
        self.assertNotIn('my-game', control.profiles())

    def test_removing_another_game_does_not_end_active_session(self):
        path = self.cpu()
        control = Control(self.s)
        control.save_game('10', 'Active', {'cpu_max_mhz': 2400})
        control.save_game('20', 'Other', {'cpu_max_mhz': 2800})
        control.activate('10')
        control.remove_game('20')
        self.assertEqual(Engine(self.s).state['session']['appid'], '10')
        self.assertEqual(self.s.read(path + 'scaling_max_freq'), '2400000')
        control.remove_game('10')
        self.assertNotIn('session', Engine(self.s).state)
        self.assertEqual(self.s.read(path + 'scaling_max_freq'), '3500000')

    def test_desktop_dispatch_preview_apply_repeat_and_exact_rollback(self):
        control = Control(self.s)
        def rpc(method, *args):
            return dispatch(control, {'method': method, 'args': list(args)})
        rpc('preview', {'nmi_watchdog': 0}, False)
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '1')
        self.assertFalse(Engine(self.s).file.exists())
        rpc('apply_global', 'desktop-global', {'nmi_watchdog': 0})
        before = Engine(self.s).file.read_bytes()
        rpc('apply_global', 'desktop-global', {'nmi_watchdog': 0})
        self.assertEqual(before, Engine(self.s).file.read_bytes())
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '0')
        rpc('rollback')
        self.assertEqual(self.s.read('/proc/sys/kernel/nmi_watchdog'), '1')
        self.assertFalse(Engine(self.s).state['entries'])

    def test_desktop_dispatch_rejects_arbitrary_methods_and_bad_envelopes(self):
        control = Control(self.s)
        for payload in [[], {}, {'method': 'write_config', 'args': [{}]}, {'method': '__getattribute__'},
                        {'method': 'state', 'args': {}}, {'method': 'state', 'args': [1] * 5},
                        {'method': 'state', 'path': '/etc/passwd'}]:
            with self.subTest(payload=payload), self.assertRaises(BosunError):
                dispatch(control, payload)
        self.assertFalse(control.file.exists())

    def test_cli_json_transport_operates_only_inside_isolated_root(self):
        source = Path(__file__).resolve().parents[1]
        command = [sys.executable, str(source / 'bosun.py'), '--root', str(self.root), 'desktop-rpc']
        for method, args in [('save_game', ['10', 'Game', {'cpu_max_mhz': 2400}, True]), ('state', [])]:
            result = subprocess.run(command, input=json.dumps({'method': method, 'args': args}), text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
        self.assertEqual(data['config']['games']['10']['settings'], {'cpu_max_mhz': 2400})
        self.assertIsNone(self.s.read(BOOT))


class DesktopTransportTests(unittest.TestCase):
    def test_unprivileged_call_uses_fixed_pkexec_argv_and_json_stdin(self):
        with patch('bosun.desktop.os.geteuid', return_value=1000), patch('bosun.desktop.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"notes": ["saved"]}', '')
            self.assertEqual(call('save_game', ['10', '$(touch /tmp/oops)', {}, True]), {'notes': ['saved']})
            args, kwargs = run.call_args
            self.assertEqual(args[0], ['pkexec', '--disable-internal-agent', '/usr/bin/python3', APP + '/bosun.py', 'desktop-rpc'])
            self.assertEqual(json.loads(kwargs['input'])['args'][1], '$(touch /tmp/oops)')
            self.assertNotIn('shell', kwargs)

    def test_auth_failure_is_reported_without_faking_success(self):
        with patch('bosun.desktop.subprocess.run', return_value=subprocess.CompletedProcess([], 126, '', 'Not authorized')):
            with self.assertRaisesRegex(BosunError, 'Not authorized'):
                call('state')

    def test_existing_desktop_dependencies_do_not_run_package_transaction(self):
        from bosun.dependencies import ensure_desktop
        from unittest.mock import Mock
        system = Mock(); system.command.return_value = 'ready'; system.which.return_value = '/usr/bin/pkexec'
        with patch('bosun.dependencies.install_package') as install:
            self.assertEqual(ensure_desktop(system), [])
            install.assert_not_called()

    def test_missing_desktop_dependencies_use_native_provider_installer(self):
        from bosun.dependencies import ensure_desktop
        from unittest.mock import Mock
        system = Mock(); system.command.side_effect = ['', 'ready']; system.which.return_value = None; system.fake = False
        with patch('bosun.dependencies.install_package', return_value='installed') as install:
            self.assertEqual(ensure_desktop(system), ['installed', 'installed'])
            self.assertEqual([c.args[1:] for c in install.call_args_list], [(['pyside6', 'python-pyside6'], 'PySide6.QtWidgets'), (['polkit'], 'pkexec')])
            self.assertIn('from PySide6 import QtWidgets', install.call_args_list[0].kwargs['probe'][2])


if __name__ == '__main__':
    unittest.main()
