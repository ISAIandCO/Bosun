# SPDX-License-Identifier: GPL-3.0-or-later
"""Qt Desktop window. Uses the same Control operations and journal as Decky."""
import json
import re
import sys

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog,
                               QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
                               QStackedWidget, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from .core import VERSION
from .desktop import call
from .presentation import GROUPS, LABELS


def label(text, role='body'):
    widget = QLabel(text)
    widget.setObjectName(role)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.PlainText)
    return widget


def card(layout, title):
    frame = QFrame(); frame.setObjectName('card')
    body = QVBoxLayout(frame); body.setContentsMargins(20, 18, 20, 18); body.setSpacing(12)
    body.addWidget(label(title, 'cardTitle'))
    layout.addWidget(frame)
    return body


def button(layout, text, callback, primary=False):
    widget = QPushButton(text)
    widget.setMinimumHeight(40)
    widget.setAccessibleName(text)
    if primary:
        widget.setProperty('primary', True)
    widget.clicked.connect(callback)
    layout.addWidget(widget)
    return widget


def disclosure(layout, title):
    toggle = QToolButton(); toggle.setText(title); toggle.setCheckable(True)
    toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon); toggle.setArrowType(Qt.RightArrow)
    toggle.setMinimumHeight(40); toggle.setAccessibleName(title)
    layout.addWidget(toggle)
    content = QWidget(); content.setObjectName('transparent')
    body = QVBoxLayout(content); body.setContentsMargins(0, 8, 0, 8)
    content.setVisible(False); layout.addWidget(content)
    toggle.toggled.connect(lambda opened: (content.setVisible(opened), toggle.setArrowType(Qt.DownArrow if opened else Qt.RightArrow)))
    return body


class Request(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, method, args):
        super().__init__()
        self.method, self.args = method, args

    def run(self):
        try:
            self.done.emit(call(self.method, self.args))
        except Exception as exc:
            self.failed.emit(str(exc))


class ProfileEditor(QWidget):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.rows = {}
        self.loading = False
        self.dirty = False
        body = QVBoxLayout(self); body.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit(); self.search.setPlaceholderText('Найти параметр…'); self.search.setAccessibleName('Поиск настройки')
        self.search.textChanged.connect(self.filter_rows); body.addWidget(self.search)
        self.tabs = QTabWidget(); body.addWidget(self.tabs)
        self.setMinimumHeight(280)
        self.setMaximumHeight(440)

    def configure(self, state, game=False):
        if self.rows:
            return
        allowed = set(state['runtime_keys']) if game else set(state['catalog'])
        for group_name in GROUPS:
            keys = [k for k in state['catalog'] if k in allowed and k != 'lbpw' and state['groups'][k] == group_name]
            if not keys:
                continue
            content = QWidget(); layout = QVBoxLayout(content); layout.setContentsMargins(10, 10, 10, 10); layout.setSpacing(10)
            scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); scroll.setWidget(content)
            self.tabs.addTab(scroll, group_name)
            for key in keys:
                row = QFrame(); row.setObjectName('setting')
                stack = QVBoxLayout(row); stack.setContentsMargins(12, 10, 12, 10); stack.setSpacing(6)
                caption = LABELS.get(key, key)
                if key in state['reboot_keys']:
                    caption += ' · после перезагрузки'
                enabled = QCheckBox(caption); enabled.setAccessibleName(caption)
                enabled.setToolTip(state['catalog'][key][1]); stack.addWidget(enabled)
                choices = state['catalog'][key][0]
                bounds = re.fullmatch(r'(\d+)\.\.(\d+)', choices)
                if bounds:
                    widget = QSpinBox(); widget.setRange(int(bounds[1]), int(bounds[2]))
                    widget.setValue({'cpu_max_mhz': 2800, 'tdp_w': 10}.get(key, int(bounds[1])))
                    widget.valueChanged.connect(self.edited)
                else:
                    widget = QComboBox(); widget.setEditable(True)
                    if '|' in choices:
                        values = choices.split('|')
                    elif key == 'governor':
                        values = sorted(set(' '.join(p.get('scaling_available_governors') or '' for p in state['report']['system']['cpu'].values()).split())) or ['powersave', 'performance', 'schedutil']
                    elif key == 'epp':
                        values = ['performance', 'balance_performance', 'balance_power', 'power']
                    else:
                        values = []
                    widget.addItems(values); widget.currentTextChanged.connect(self.edited)
                widget.setAccessibleName(caption + ': значение'); widget.setEnabled(False)
                enabled.toggled.connect(widget.setEnabled); enabled.toggled.connect(self.edited)
                stack.addWidget(widget); layout.addWidget(row)
                self.rows[key] = (row, enabled, widget)
            layout.addStretch()

    def edited(self, *_):
        if not self.loading:
            # Active amd-pstate accepts EPP only with powersave; keep the pair coherent
            # just as the Decky editor does, rather than presenting a recipe that skips EPP.
            if 'epp' in self.rows and 'governor' in self.rows:
                _, epp_on, epp = self.rows['epp']
                _, gov_on, gov = self.rows['governor']
                self.loading = True
                if self.sender() in (epp_on, epp) and epp_on.isChecked() and epp.currentText():
                    gov_on.setChecked(True); gov.setCurrentText('powersave')
                elif self.sender() in (gov_on, gov) and (not gov_on.isChecked() or gov.currentText() != 'powersave'):
                    epp_on.setChecked(False)
                self.loading = False
            self.dirty = True
            self.changed.emit()

    def filter_rows(self, text):
        text = text.casefold()
        for key, (row, enabled, _) in self.rows.items():
            row.setVisible(text in key.casefold() or text in enabled.text().casefold())
        if text:
            matching = [i for i in range(self.tabs.count()) if any(not row.isHidden() and self.tabs.widget(i).widget().isAncestorOf(row) for row, _, _ in self.rows.values())]
            if matching and self.tabs.currentIndex() not in matching:
                self.tabs.setCurrentIndex(matching[0])

    def load(self, settings):
        self.loading = True
        for key, (_, enabled, widget) in self.rows.items():
            enabled.setChecked(key in settings)
            if key in settings:
                if isinstance(widget, QSpinBox):
                    widget.setValue(int(settings[key]))
                else:
                    widget.setCurrentText(str(settings[key]))
        self.loading = False
        self.dirty = False
        self.changed.emit()

    def settings(self):
        result = {}
        for key, (_, enabled, widget) in self.rows.items():
            if enabled.isChecked():
                value = widget.value() if isinstance(widget, QSpinBox) else widget.currentText().strip()
                result[key] = int(value) if isinstance(value, str) and re.fullmatch(r'-?\d+', value) else value
        return result


class Window(QMainWindow):
    def __init__(self, autoload=True):
        super().__init__()
        self.setWindowTitle('Bosun'); self.setMinimumSize(960, 600)
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1180, available.width() - 24), min(720, available.height() - 48))
        self.state = None; self.busy = False; self.workers = set(); self.game_id = ''; self.last_message = ''
        shell = QWidget(); shell.setObjectName('shell')
        root = QHBoxLayout(shell); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        sidebar = QWidget(); sidebar.setObjectName('sidebar'); sidebar.setFixedWidth(186)
        side = QVBoxLayout(sidebar); side.setContentsMargins(16, 24, 16, 20); side.setSpacing(8)
        side.addWidget(label('⚓ Bosun', 'brand')); side.addSpacing(20)
        self.nav_buttons = []; self.nav_group = QButtonGroup(self)
        self.tabs = QStackedWidget(); self.page_title = label('', 'pageTitle')
        self.descriptions = []; self.page_caption = label('', 'caption')
        for index, (name, caption) in enumerate([
                ('Обзор', 'Настройка производительности на ватт для Steam Deck.'),
                ('Система', 'Общий профиль устройства, включая параметры после перезагрузки.'),
                ('Игры', 'Индивидуальные назначения: только параметры, применяемые на лету.'),
                ('Диагностика', 'Прочитанные значения, расхождения и короткая проверка CPU.'),
                ('Обслуживание', 'Обновление, инструменты измерения и возврат исходных настроек.')]):
            nav = QPushButton(name); nav.setObjectName('nav'); nav.setCheckable(True); nav.setMinimumHeight(46)
            nav.setShortcut('Alt+' + str(index + 1)); nav.setAccessibleName(name)
            nav.clicked.connect(lambda _=False, i=index: self.tabs.setCurrentIndex(i))
            self.nav_group.addButton(nav); self.nav_buttons.append(nav); side.addWidget(nav)
            page = QWidget(); body = QVBoxLayout(page); body.setContentsMargins(0, 0, 8, 8); body.setSpacing(16)
            scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); scroll.setWidget(page)
            self.tabs.addWidget(scroll); self.descriptions.append((name, caption))
        side.addStretch(); side.addWidget(label('STEAM DECK LCD · OLED', 'caption')); side.addWidget(label(VERSION, 'caption'))
        root.addWidget(sidebar)
        workspace = QVBoxLayout(); workspace.setContentsMargins(26, 22, 24, 12); workspace.setSpacing(8)
        workspace.addWidget(self.page_title); workspace.addWidget(self.page_caption); workspace.addWidget(self.tabs, 1)
        self.notice = label('Выберите действие. Права администратора запрашиваются отдельным системным окном.', 'caption')
        self.notice.setMaximumHeight(76)
        footer = QHBoxLayout(); footer.addWidget(self.notice, 1); button(footer, 'Результат', self.show_result)
        workspace.addLayout(footer); root.addLayout(workspace, 1); self.setCentralWidget(shell)
        self.tabs.currentChanged.connect(self.page_changed)
        self.build_home(); self.build_system(); self.build_games(); self.build_diagnostics(); self.build_service()
        self.page_changed(0)
        if autoload:
            QTimer.singleShot(0, self.refresh)

    def body(self, index):
        return self.tabs.widget(index).widget().layout()

    def page_changed(self, index):
        self.nav_buttons[index].setChecked(True)
        title, caption = self.descriptions[index]
        self.page_title.setText(title); self.page_caption.setText(caption)

    def build_home(self):
        body = self.body(0)
        box = card(body, 'Ваш Steam Deck')
        self.summary = label('Загрузка состояния…'); box.addWidget(self.summary)
        button(box, 'Обновить состояние', self.refresh)
        box = card(body, 'Рекомендуемый профиль')
        box.addWidget(label('Системные настройки памяти, фоновой работы и программных CPU mitigations. '
                            'Только доступные параметры; исходные значения сохраняются для отката.'))
        actions = QHBoxLayout(); box.addLayout(actions)
        button(actions, 'Предпросмотр', lambda: self.recommended(True))
        button(actions, 'Применить', lambda: self.recommended(False), True)
        box.addWidget(label('Часть настроек требует перезагрузки. Измеренный прирост FPS или автономности не заявляется.', 'caption'))
        box = card(body, 'Профили игр')
        box.addWidget(label('Для разных игр задайте CPU/TDP, память и I/O. В Gaming Mode назначения переключаются по событиям Steam.'))
        button(box, 'Настроить назначения', lambda: self.tabs.setCurrentIndex(2))
        body.addStretch()

    def recipe_controls(self, layout, editor, game=False):
        row = QHBoxLayout(); picker = QComboBox(); picker.setAccessibleName('Основа игрового профиля' if game else 'Основа системного профиля')
        row.addWidget(picker, 1); layout.addLayout(row)
        def load():
            if self.state:
                source = self.state['game_profiles' if game else 'profiles']
                editor.load(source.get(picker.currentText(), {}))
                self.notice.setText('Основа загружена в черновик. Изменения ещё не применены.')
        button(row, 'Загрузить основу', load)
        button(row, 'Сохранить как…', lambda: self.save_recipe(editor))
        return picker

    def build_system(self):
        body = self.body(1)
        box = card(body, 'Общий профиль')
        self.system_editor = ProfileEditor(); self.system_picker = self.recipe_controls(box, self.system_editor)
        actions = QHBoxLayout(); box.addLayout(actions)
        button(actions, 'Предпросмотр', lambda: self.apply_system(True))
        button(actions, 'Применить профиль', lambda: self.apply_system(False), True)
        button(actions, 'Отменить черновик', lambda: self.system_editor.load(self.state['report']['desired']) if self.state else None)
        box.addWidget(self.system_editor)
        extra = disclosure(body, 'Импорт, экспорт и удаление своего профиля')
        button(extra, 'Импортировать JSON в черновик', self.import_recipe)
        button(extra, 'Экспортировать черновик', self.export_recipe)
        button(extra, 'Удалить выбранный свой профиль', lambda: self.request('remove_profile', [self.system_picker.currentText()]))
        body.addStretch()

    def build_games(self):
        body = self.body(2); box = card(body, 'Назначение игре')
        self.saved_games = QComboBox(); self.saved_games.setAccessibleName('Сохранённые игры')
        self.saved_games.activated.connect(lambda _: self.load_game(str(self.saved_games.currentData() or '')))
        box.addWidget(self.saved_games)
        row = QHBoxLayout(); self.appid = QLineEdit(); self.appid.setPlaceholderText('Steam AppID'); self.appid.setAccessibleName('Steam AppID')
        self.game_name = QLineEdit(); self.game_name.setPlaceholderText('Название игры'); self.game_name.setAccessibleName('Название игры')
        self.appid.editingFinished.connect(lambda: self.load_game(self.appid.text().strip()) if self.appid.text().strip() != self.game_id else None)
        row.addWidget(self.appid); row.addWidget(self.game_name, 1); box.addLayout(row)
        self.resume = QCheckBox('Один раз восстановить известные сбросы после сна'); self.resume.setChecked(True); box.addWidget(self.resume)
        self.automatic = QCheckBox('Автоматические профили в Gaming Mode'); self.automatic.clicked.connect(lambda on: self.request('automatic', [on])); box.addWidget(self.automatic)
        self.game_editor = ProfileEditor(); self.game_picker = self.recipe_controls(box, self.game_editor, True)
        self.game_name.textEdited.connect(self.game_editor.edited); self.resume.toggled.connect(self.game_editor.edited)
        actions = QHBoxLayout(); box.addLayout(actions)
        button(actions, 'Предпросмотр', lambda: self.request('preview', [self.game_editor.settings(), True]))
        button(actions, 'Сохранить назначение', self.save_game, True)
        button(actions, 'Удалить назначение', lambda: self.request('remove_game', [self.appid.text().strip()]))
        box.addWidget(self.game_editor)
        box.addWidget(label('Назначение используется при следующем запуске через Decky. Если профиль этой игры уже активен, сохранение обновляет его сразу. '
                            'Параметры запуска Steam редактируются отдельно в Decky и действуют после перезапуска игры.', 'caption'))
        body.addStretch()

    def build_diagnostics(self):
        body = self.body(3); box = card(body, 'Прочитанные значения устройства')
        actions = QHBoxLayout(); box.addLayout(actions)
        button(actions, 'Обновить', self.refresh)
        button(actions, 'Проверить CPU (2 с)', lambda: self.request('cpu_check', callback=lambda r: self.cpu_result.setPlainText('\n'.join(r['notes']))))
        self.actual = QPlainTextEdit(); self.actual.setReadOnly(True); self.actual.setMinimumHeight(210); self.actual.setAccessibleName('Фактическое состояние')
        box.addWidget(self.actual)
        self.cpu_result = QPlainTextEdit(); self.cpu_result.setReadOnly(True); self.cpu_result.setMinimumHeight(150); self.cpu_result.setAccessibleName('Результат проверки CPU'); box.addWidget(self.cpu_result)
        box.addWidget(label('Чтение CPUFreq подтверждает значение policy. Аппаратную частоту и результат в игре проверяйте отдельно.', 'caption'))
        body.addStretch()

    def build_service(self):
        body = self.body(4); box = card(body, 'Обслуживание')
        button(box, 'Обновить Bosun и плагин', lambda: self.request('update'))
        button(box, 'Подготовить MangoHud для сравнения до/после', lambda: self.request('measurement_tools'))
        extra = disclosure(body, 'Откат системных изменений')
        extra.addWidget(label('Возвращает сохранённые значения этой системы. После загрузочных изменений нужна перезагрузка. '
                              'Если Bosun менял параметры запуска Steam, сначала восстановите их в Decky.'))
        button(extra, 'Вернуть исходные системные настройки', lambda: self.request('rollback'))
        log = disclosure(body, 'Последний результат операции')
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMinimumHeight(180); log.addWidget(self.log)
        body.addStretch()

    def recommended(self, dry):
        if self.state:
            self.request('preview' if dry else 'apply_global',
                         [self.state['profiles']['recommended'], False] if dry else ['recommended', self.state['profiles']['recommended']])

    def apply_system(self, dry):
        self.request('preview' if dry else 'apply_global',
                     [self.system_editor.settings(), False] if dry else ['desktop-global', self.system_editor.settings()],
                     callback=(None if dry else lambda r: self.committed(self.system_editor, r)))

    def committed(self, editor, result):
        if not result.get('conflict'):
            editor.dirty = False

    def load_game(self, appid):
        if not self.state:
            return
        if self.game_editor.dirty and QMessageBox.question(self, 'Есть черновик', 'Перейти к другой игре и закрыть несохранённый черновик?') != QMessageBox.Yes:
            index = self.saved_games.findData(self.game_id)
            if index >= 0:
                self.saved_games.setCurrentIndex(index)
            self.appid.setText(self.game_id)
            return
        saved = self.state['config']['games'].get(appid, {})
        self.game_id = appid; self.appid.setText(appid); self.game_name.setText(saved.get('name', '')); self.resume.setChecked(saved.get('resume', True))
        index = self.saved_games.findData(appid)
        self.saved_games.setCurrentIndex(max(0, index))
        self.game_editor.load(saved.get('settings', {}))

    def save_game(self):
        appid = self.appid.text().strip()
        args = [appid, self.game_name.text(), self.game_editor.settings(), self.resume.isChecked()]
        def saved(result):
            self.committed(self.game_editor, result)
            if self.state and (self.state['report'].get('session') or {}).get('appid') == appid:
                self.request('activate', [appid, False])
        self.request('save_game', args, saved)

    def save_recipe(self, editor):
        name, ok = QInputDialog.getText(self, 'Сохранить профиль', 'Имя: латинские буквы, цифры, _ или -')
        if ok and name:
            self.request('save_profile', [name, editor.settings()])

    def import_recipe(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Импорт профиля', '', 'JSON (*.json)')
        if path:
            try:
                with open(path) as stream:
                    data = json.load(stream)
                from .core import validate
                self.system_editor.load(validate(data['settings']))
                self.notice.setText('Импортировано в черновик; для применения нажмите кнопку профиля.')
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.failure(str(exc))

    def export_recipe(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Экспорт профиля', 'bosun-profile.json', 'JSON (*.json)')
        if path:
            try:
                with open(path, 'w') as stream:
                    json.dump({'schema': 1, 'settings': self.system_editor.settings()}, stream, indent=2, ensure_ascii=False)
            except OSError as exc:
                self.failure(str(exc))

    def request(self, method, args=(), callback=None):
        if self.busy:
            self.notice.setText('Дождитесь завершения текущей операции.')
            return
        self.set_busy(True); self.notice.setText('Выполняется… При необходимости подтвердите действие в системном окне.')
        worker = Request(method, args); self.workers.add(worker)
        def success(result):
            self.set_busy(False)
            if method == 'state':
                self.loaded(result)
            else:
                notes = '\n'.join(result.get('notes', [])) or 'Готово'
                self.last_message = notes; self.log.setPlainText(notes); self.notice.setText(notes[:600])
                if method == 'preview':
                    self.show_result()
                if callback:
                    callback(result)
                if not self.busy and method != 'update':
                    self.refresh()
        def failed(message):
            self.set_busy(False); self.failure(message)
        worker.done.connect(success); worker.failed.connect(failed)
        worker.finished.connect(lambda: (self.workers.discard(worker), worker.deleteLater()))
        worker.start()

    def set_busy(self, busy):
        self.busy = busy
        for widget in (self.system_editor, self.game_editor, self.appid, self.game_name, self.saved_games,
                       self.system_picker, self.game_picker, self.resume, self.automatic):
            widget.setEnabled(not busy)
        for widget in self.centralWidget().findChildren(QPushButton):
            if widget not in self.nav_buttons:
                widget.setEnabled(not busy)

    def show_result(self):
        dialog = QDialog(self); dialog.setWindowTitle('Результат операции'); dialog.resize(720, 480)
        body = QVBoxLayout(dialog); text = QPlainTextEdit(self.log.toPlainText() or 'Операции ещё не выполнялись.'); text.setReadOnly(True); body.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Close); buttons.rejected.connect(dialog.reject); body.addWidget(buttons)
        dialog.exec()

    def failure(self, message):
        self.notice.setText(message[:600]); self.log.setPlainText(message)
        QMessageBox.warning(self, 'Bosun', message)

    def refresh(self):
        self.request('state')

    def loaded(self, state):
        first = self.state is None
        self.state = state
        system = state['report']['system']; report = state['report']
        warnings = []
        if report['update_pending']:
            warnings.append('SteamOS/ядро обновились: проверьте и явно примените глобальный профиль.')
        if state['power_conflicts']:
            warnings.append('Другой менеджер питания: ' + ', '.join(state['power_conflicts']))
        self.summary.setText(f"Steam Deck {system['model']} · SteamOS {system['os'].get('VERSION_ID', '?')}\n"
                             f"Профиль: {report['profile']} · управляемых параметров: {len(report['managed'])}\n" + '\n'.join(warnings))
        session = report.get('session')
        if session:
            identity = 'AppID ' + session['appid'] if session.get('appid') else report['profile']
            self.summary.setText(self.summary.text() + f"\nСохранённый игровой профиль: {identity}. "
                                 'Применение глобального профиля заменит это назначение.')
        for editor, game in [(self.system_editor, False), (self.game_editor, True)]:
            editor.configure(state, game)
        if first or not self.system_editor.dirty:
            self.system_editor.load(report['desired'])
        if first:
            self.game_editor.load({})
        self.automatic.setChecked(state['config']['automatic'])
        for picker, profiles in [(self.system_picker, state['profiles']), (self.game_picker, state['game_profiles'])]:
            selected = picker.currentText(); picker.clear(); picker.addItems(profiles)
            if selected in profiles:
                picker.setCurrentText(selected)
        selected = self.saved_games.currentData(); self.saved_games.clear(); self.saved_games.addItem('Указать другую игру по AppID', '')
        for appid, saved in state['config']['games'].items():
            self.saved_games.addItem(f"{saved['name']} ({appid})", appid)
        index = self.saved_games.findData(selected)
        if index >= 0:
            self.saved_games.setCurrentIndex(index)
        lines = [f"{system['model']} / {system['kernel']} / RO {system['readonly']}"]
        for path, cpu in system['cpu'].items():
            lines.append(f"{path.split('/')[-1]}: {cpu.get('scaling_driver')} / {cpu.get('scaling_governor')} / min {cpu.get('scaling_min_freq')} / max {cpu.get('scaling_max_freq')} kHz")
        for item in report['managed']:
            lines.append(f"{item['change']['key']}: ожидалось {str(item['last'])[:120]}; прочитано {str(item['actual'])[:120]} {'✓' if item['matches'] else '— РАСХОЖДЕНИЕ'}")
        self.actual.setPlainText('\n'.join(lines))
        self.notice.setText(self.last_message[:600] or 'Состояние загружено. Изменения применяются только после нажатия соответствующей кнопки.')

    def closeEvent(self, event):
        if self.workers:
            self.notice.setText('Дождитесь завершения системной операции перед закрытием окна.'); event.ignore()
        elif (self.system_editor.dirty or self.game_editor.dirty) and QMessageBox.question(self, 'Есть черновик', 'Закрыть окно без сохранения черновика?') != QMessageBox.Yes:
            event.ignore()
        else:
            event.accept()


def theme(app):
    # Same dark surfaces, lavender accent and sidebar approach as Helmsman.
    app.setStyle('Fusion')
    app.setFont(QFont('Noto Sans', 10))
    palette = QPalette()
    for role, color in [(QPalette.Window, '#211d29'), (QPalette.WindowText, '#f0edf8'), (QPalette.Base, '#211d29'),
                        (QPalette.Text, '#f0edf8'), (QPalette.Button, '#38303f'), (QPalette.ButtonText, '#f0edf8'),
                        (QPalette.Highlight, '#675287'), (QPalette.HighlightedText, '#ffffff')]:
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setStyleSheet('''
QWidget { color: #f0edf8; font-family: "Noto Sans", sans-serif; font-size: 13px; }
QWidget#shell, QScrollArea, QTabWidget::pane { background: #17151e; border: none; }
QDialog, QMessageBox { background: #211d29; }
QWidget#sidebar { background: #211d2c; border-right: 1px solid #393244; }
QScrollArea > QWidget > QWidget { background: #17151e; }
QFrame#card { background: #25212e; border: 1px solid #3b3348; border-radius: 18px; }
QFrame#setting { background: #2b2635; border: 1px solid #40364d; border-radius: 12px; }
QLabel, QCheckBox, QWidget#transparent { background: transparent; }
QLabel#brand { font-size: 26px; font-weight: 700; color: #cdbdff; }
QLabel#pageTitle { font-size: 27px; font-weight: 650; }
QLabel#cardTitle { font-size: 18px; font-weight: 600; }
QLabel#caption { color: #b5adc3; font-size: 12px; }
QPushButton, QToolButton { background: #38303f; border: 1px solid #51445e; border-radius: 12px; padding: 9px 13px; }
QPushButton:hover, QToolButton:hover { background: #493a57; }
QPushButton:focus, QToolButton:focus, QLineEdit:focus, QComboBox:focus { border: 2px solid #cdbdff; }
QPushButton[primary="true"] { background: #cdbdff; color: #281d40; font-weight: 600; }
QPushButton#nav { text-align: left; border: none; background: transparent; padding-left: 16px; }
QPushButton#nav:checked { background: #473858; color: #eaddff; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit { background: #211d29; border: 1px solid #60506e; border-radius: 9px; padding: 8px; selection-background-color: #675287; }
QComboBox QAbstractItemView { background: #2c2438; selection-background-color: #55406d; }
QCheckBox { min-height: 28px; }
QCheckBox::indicator { width: 21px; height: 21px; }
QCheckBox::indicator:unchecked { background: #211d29; border: 1px solid #897799; border-radius: 5px; }
QCheckBox::indicator:checked { background: #cdbdff; border: 1px solid #cdbdff; border-radius: 5px; }
QWidget:disabled { color: #aaa0b7; }
QTabBar::tab { background: #282231; padding: 11px 12px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #eaddff; border-bottom: 2px solid #cdbdff; }
QScrollBar:vertical { width: 10px; background: #211b2b; }
QScrollBar::handle:vertical { background: #655473; border-radius: 5px; min-height: 24px; }
QToolTip { background: #40324e; color: #f0edf8; padding: 8px; }
''')


def run():
    app = QApplication(sys.argv[:1]); theme(app)
    window = Window(); window.show()
    return app.exec()
