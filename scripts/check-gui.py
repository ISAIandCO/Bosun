#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise real Qt widgets against the isolated backend, never live sysfs/pkexec."""
import argparse
import dataclasses
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QPushButton, QToolButton
import test_bosun as fixtures
from bosun.control import Control
from bosun.core import GLOBAL_KEYS, Change, Engine
from bosun.desktop import dispatch
from bosun.gui import Window, theme


def check(screenshots):
    app = QApplication([]); theme(app)
    fixture = fixtures.CoreTests(); fixture.setUp(); fixture.cpu()
    control = Control(fixture.s)
    control.save_profile('boot-only', {'mitigations': 'off'})
    control.save_game('10', 'Тестовая игра', {'cpu_max_mhz': 2400})
    window = Window(autoload=False); window.loaded(control.state()); window.show(); app.processEvents()
    requests = []

    def synchronous(method, args=(), callback=None):
        requests.append((method, args))
        result = dispatch(control, {'method': method, 'args': list(args)})
        if callback:
            callback(result)
        window.loaded(control.state())
        return result

    def click(text):
        matches = [w for w in window.findChildren(QPushButton) if w.text() == text and w.isVisible()]
        assert len(matches) == 1, (text, len(matches))
        matches[0].click(); app.processEvents()

    def edit(key, value, game=False):
        editor = window.game_editor if game else window.system_editor
        _, checkbox, widget = editor.rows[key]
        checkbox.setChecked(True)
        widget.setValue(value)

    window.request = synchronous
    try:
        assert not GLOBAL_KEYS.intersection(window.game_editor.rows)
        assert window.game_picker.findText('boot-only') == -1
        _, enabled, epp = window.game_editor.rows['epp']
        enabled.setChecked(True); epp.setCurrentText('balance_power')
        assert window.game_editor.settings()['governor'] == 'powersave'
        window.game_editor.rows['governor'][2].setCurrentText('performance')
        assert 'epp' not in window.game_editor.settings()
        window.game_editor.load({})
        window.nav_buttons[1].click(); app.processEvents()
        edit('swappiness', 150)
        draft = window.system_editor.settings()
        window.loaded(control.state())
        assert window.system_editor.dirty and window.system_editor.settings() == draft
        click('Предпросмотр')
        assert fixture.s.read('/proc/sys/vm/swappiness') == '60'
        assert not Engine(fixture.s).file.exists()
        click('Применить профиль')
        assert fixture.s.read('/proc/sys/vm/swappiness') == '150'
        assert not window.system_editor.dirty
        with patch('bosun.gui.QInputDialog.getText', return_value=('my-profile', True)):
            click('Сохранить как…')
        assert control.profiles()['my-profile'] == {'swappiness': 150}
        window.nav_buttons[2].click(); window.load_game('10'); app.processEvents()
        edit('cpu_max_mhz', 2600, game=True)
        control.activate('10')
        window.loaded(control.state())
        click('Сохранить назначение')
        assert fixture.s.read('/sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq') == '2600000'
        assert ('activate', ['10', False]) in requests
        assert not window.game_editor.dirty
        edit('cpu_max_mhz', 2800, game=True)
        window.game_editor.search.setText('cpu_max')
        window.loaded(control.state())
        assert window.game_editor.settings()['cpu_max_mhz'] == 2800
        window.set_busy(True); assert not window.game_editor.isEnabled()
        window.set_busy(False); assert window.game_editor.isEnabled()
        window.game_editor.load(control.config()['games']['10']['settings'])
        control.activate('')
        window.nav_buttons[4].click(); app.processEvents()
        toggle = next(w for w in window.findChildren(QToolButton) if w.text() == 'Откат системных изменений')
        toggle.click(); app.processEvents(); click('Вернуть исходные системные настройки')
        assert fixture.s.read('/proc/sys/vm/swappiness') == '60'
        assert fixture.s.read('/sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq') == '3500000'
        assert not Engine(fixture.s).state['entries']
        toggle.click()

        # Desktop after a missed Steam exit: a persisted Decky record must not block
        # the user's explicit global profile, and a late exit must not undo it.
        control.automatic(True); control.activate('10')
        window.loaded(control.state()); window.nav_buttons[0].click(); app.processEvents()
        assert 'AppID 10' in window.summary.text()
        click('Применить')
        assert 'session' not in Engine(fixture.s).state
        assert fixture.s.read('/sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq') == '3500000'
        control.activate('')
        assert Engine(fixture.s).state['profile'] == 'recommended'
        control.rollback(); window.loaded(control.state())

        from bosun.sessions import session_action
        engine = Engine(fixture.s)
        session_action(engine, 'session-start', 'old-launcher', 'a' * 32, {'cpu_max_mhz': 2400})
        window.loaded(control.state()); app.processEvents()
        assert 'old-launcher' in window.summary.text()
        click('Применить')
        session_action(Engine(fixture.s), 'session-end', 'old-launcher', 'a' * 32)
        assert Engine(fixture.s).state['profile'] == 'recommended'
        assert 'session' not in Engine(fixture.s).state
        control.rollback(); window.loaded(control.state())

        # Reproduce the supplied CPU recovery error and use the actual UI action.
        engine = Engine(fixture.s)
        before = json.loads(json.dumps(engine.state))
        cpu_path = '/sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq'
        change = Change('cpu_max_mhz', cpu_path, '1500000')
        engine.state['pending'] = {'state_before': before, 'writes': [
            {'change': dataclasses.asdict(change), 'before': fixture.s.read(cpu_path)}]}
        engine.save(); fixture.put(cpu_path, '1700000')
        window.loaded(control.state()); window.nav_buttons[0].click(); app.processEvents()
        edit('cpu_max_mhz', 1500)
        assert window.cpu_recovery_button.isVisible()
        click('Разблокировать CPU, оставив текущие лимиты')
        assert not window.cpu_recovery_button.isVisible()
        assert fixture.s.read(cpu_path) == '1700000'
        assert not Engine(fixture.s).state['pending']
        assert window.system_editor.settings()['cpu_max_mhz'] == 1500 and window.system_editor.dirty
        window.system_editor.load({})
        fixture.put(cpu_path, '3500000'); window.loaded(control.state())

        # Real asynchronous request path, including cancellation. Window stays responsive;
        # denied authorization must retain the editor draft and show an error.
        window.request = Window.request.__get__(window)
        window.show_result = lambda: None
        errors = []
        window.failure = errors.append
        edit('cpu_max_mhz', 2700, game=True)
        from bosun.core import BosunError
        with patch('bosun.gui.call', side_effect=BosunError('Authorization cancelled')):
            window.request('save_game', ['10', 'Game', {'cpu_max_mhz': 2700}, True])
            timer = QTimer(); timer.setInterval(10)
            timer.timeout.connect(lambda: app.quit() if not window.workers else None)
            timer.start(); QTimer.singleShot(5000, app.quit); app.exec(); timer.stop()
        assert errors == ['Authorization cancelled'] and not window.busy
        assert window.game_editor.dirty and window.game_editor.settings()['cpu_max_mhz'] == 2700
        assert control.config()['games']['10']['settings']['cpu_max_mhz'] == 2600
        window.game_editor.load(control.config()['games']['10']['settings'])
        window.game_editor.search.clear()
        window.loaded(control.state())

        screenshots.mkdir(parents=True, exist_ok=True)
        for size in [(1280, 800), (960, 600)]:
            window.resize(*size)
            for page in range(5):
                window.nav_buttons[page].click(); app.processEvents()
                scroll = window.tabs.widget(page)
                assert scroll.widget().width() <= scroll.viewport().width(), (size, page, 'horizontal overflow')
                for editor in (window.system_editor, window.game_editor):
                    if editor.isVisible():
                        for tab in range(editor.tabs.count()):
                            editor.tabs.setCurrentIndex(tab); app.processEvents()
                            area = editor.tabs.widget(tab)
                            assert area.widget().width() <= area.viewport().width(), (size, page, tab, 'editor overflow')
                        editor.tabs.setCurrentIndex(0)
                assert window.grab().save(str(screenshots / f'desktop-{page}-{size[0]}.png'))
        print('Qt: preview/apply/readback/rollback, game activation, recipes, draft preservation, authorization cancellation, 5 pages at 1280×800 and 960×600: OK')
    finally:
        window.system_editor.dirty = window.game_editor.dirty = False
        window.close(); fixture.tearDown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', type=Path, default=ROOT / 'test-results')
    check(parser.parse_args().screenshots)
