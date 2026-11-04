"""Emergency-pause UI contracts with fake native hotkeys and output.

Real Qt drafts and Studio/Core persistence are exercised in temporary folders.
No test registers a Windows hotkey or writes personal configuration.
"""
import copy
import os
from types import SimpleNamespace

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QScrollArea, QWidget

from gamepadstudio.i18n import get_language_preference, init_language
from gamepadstudio.mapping_engine import MappingRuntime
from gamepadstudio.studio import Studio, STYLE
from gamepadstudio.studio_core import ConfigStore
from tests.test_battery_ui import BatteryDevice, FakeTray
from tests.test_device_scope_ui import device
from tests.test_unified_mapping import Actions, entry


class FakeHotkey:
    instances = []

    def __init__(self, callback, parent=None):
        self.callback = callback
        self.configure_calls = []
        self.error_override = ''
        self.closed = False
        self.settings = {'enabled': False, 'shortcut': 'Ctrl+Alt+F10'}
        self.registered = False
        self.instances.append(self)

    def configure(self, settings):
        from gamepadstudio.emergency_hotkey import normalize_hotkey_settings
        self.settings = normalize_hotkey_settings(settings)
        self.configure_calls.append(copy.deepcopy(self.settings))
        self.registered = self.settings['enabled'] and not self.error_override
        return self.status()

    def status(self):
        return {**copy.deepcopy(self.settings), 'registered': self.registered,
                'error': self.error_override if self.settings['enabled'] else ''}

    def trigger(self):
        if self.registered:
            self.callback()

    def close(self):
        self.closed = True
        self.registered = False


@pytest.fixture
def hotkey_app():
    app = QApplication.instance() or QApplication([])
    preference = get_language_preference()
    init_language('zh')
    yield app
    init_language(preference)


@pytest.fixture
def draft_owner(tmp_path, hotkey_app, monkeypatch):
    monkeypatch.setattr('gamepadstudio.studio.WINDOWS_FEATURES', True)
    class Owner(QWidget):
        def __init__(self):
            super().__init__()
            self.store = ConfigStore(tmp_path)
            self.config = self.store.data
            self.changes = []
            self.fail = False

        def setting(self, key, value):
            self.changes.append((key, copy.deepcopy(value)))
            if self.fail:
                raise OSError('无法写入快捷键设置')
            self.store.set_setting(key, value)
            self.store.save()

    owner = Owner()
    owner.store.save()
    yield owner, hotkey_app
    owner.close()
    owner.deleteLater()
    hotkey_app.sendPostedEvents(owner, QEvent.DeferredDelete)


@pytest.fixture
def hotkey_workspace(tmp_path, monkeypatch, hotkey_app, request):
    monkeypatch.setattr('gamepadstudio.studio.WINDOWS_FEATURES', True)
    monkeypatch.setattr('gamepadstudio.virtual_kbm_ui.WINDOWS_FEATURES', True)
    fake_device = BatteryDevice()
    FakeHotkey.instances = []
    options = getattr(request, 'param', {})
    if isinstance(options, str):
        options = {'lang': options}
    remote = options.get('remote', False)

    class Client(QObject):
        event = Signal(dict)

        def __init__(self, root, parent=None):
            super().__init__(parent)
            self.connected = True
            self.status = {'enabled': True, 'devices': [], 'emergency_hotkey': {
                'enabled': False, 'shortcut': 'Ctrl+Alt+F10', 'registered': False, 'error': ''}}
            self.state = None
            self.sent = []

        def send(self, command, **kwargs):
            self.sent.append((command, copy.deepcopy(kwargs)))
            return self.connected

        def close(self):
            self.connected = False

    monkeypatch.setattr('gamepadstudio.studio.Device', lambda: fake_device)
    monkeypatch.setattr('gamepadstudio.studio.create_actions', Actions)
    monkeypatch.setattr('gamepadstudio.studio.MappingRuntime',
                        lambda actions, dispatch, **kwargs:
                        MappingRuntime(actions, dispatch, start_mouse=False))
    monkeypatch.setattr('gamepadstudio.studio.QSystemTrayIcon', FakeTray)
    monkeypatch.setattr('gamepadstudio.studio.autostart_enabled', lambda root=None: False)
    monkeypatch.setattr('gamepadstudio.studio.EmergencyHotkey', FakeHotkey)
    monkeypatch.setattr('gamepadstudio.studio.AgentClient', Client)
    monkeypatch.setattr('gamepadstudio.studio.request', lambda *args, **kwargs: {'ok': True})
    monkeypatch.setattr('gamepadstudio.studio.spawn',
                        lambda *args, **kwargs: pytest.fail('Unexpected backend process spawn'))
    monkeypatch.setattr('gamepadstudio.application_profiles.foreground_application',
                        lambda: {'hwnd': 0, 'pid': 0, 'executable': ''})
    window = Studio(tmp_path, standalone=not remote, lang=options.get('lang', 'zh'))
    window.timer.stop()
    window.scan_timer.stop()
    window.gallery_timer.stop()
    notices = []
    original_notify = window.notify

    def notify(text):
        notices.append(text)
        original_notify(text)

    monkeypatch.setattr(window, 'notify', notify)
    yield window, hotkey_app, fake_device, notices
    window.cleanup()
    window.hide()
    window.deleteLater()
    hotkey_app.sendPostedEvents(window, QEvent.DeferredDelete)


def open_draft(owner):
    from gamepadstudio.emergency_hotkey_ui import EmergencyHotkeyDialog
    return EmergencyHotkeyDialog(owner)


def close_draft(dialog, app):
    dialog.close()
    dialog.deleteLater()
    app.sendPostedEvents(dialog, QEvent.DeferredDelete)


def test_old_config_defaults_to_disabled_draft_without_writing(draft_owner):
    owner, app = draft_owner
    owner.config.pop('emergency_hotkey', None)
    owner.store.save()
    before = copy.deepcopy(owner.config)
    before_file = owner.store.path.read_bytes()
    dialog = open_draft(owner)
    try:
        assert not dialog.enabled.isChecked()
        assert dialog.shortcut.currentData() == 'Ctrl+Alt+F10'
        assert not dialog.shortcut.isEnabled()
        assert owner.config == before and owner.changes == []
        assert owner.store.path.read_bytes() == before_file
    finally:
        close_draft(dialog, app)


def test_draft_offers_three_valid_shortcuts_and_enable_switch_controls_selector(draft_owner):
    owner, app = draft_owner
    dialog = open_draft(owner)
    try:
        assert {dialog.shortcut.itemData(i) for i in range(dialog.shortcut.count())} == {
            'Ctrl+Alt+F10', 'Ctrl+Shift+F10', 'Alt+Shift+F10'}
        dialog.enabled.setChecked(True)
        assert dialog.shortcut.isEnabled()
        dialog.enabled.setChecked(False)
        assert not dialog.shortcut.isEnabled() and owner.changes == []
    finally:
        close_draft(dialog, app)


@pytest.mark.parametrize('shortcut', ['Ctrl+Alt+F10', 'Ctrl+Shift+F10', 'Alt+Shift+F10'])
def test_cancel_discards_enable_and_shortcut_edits(draft_owner, shortcut):
    owner, app = draft_owner
    before = copy.deepcopy(owner.config)
    before_file = owner.store.path.read_bytes()
    dialog = open_draft(owner)
    try:
        dialog.enabled.setChecked(True)
        dialog.shortcut.setCurrentIndex(dialog.shortcut.findData(shortcut))
        dialog.buttons.rejected.emit()
        assert dialog.result() == QDialog.Rejected
        assert owner.config == before and owner.changes == []
        assert owner.store.path.read_bytes() == before_file
    finally:
        close_draft(dialog, app)


@pytest.mark.parametrize('enabled,shortcut', [
    (True, 'Ctrl+Alt+F10'), (True, 'Ctrl+Shift+F10'), (True, 'Alt+Shift+F10'),
    (False, 'Alt+Shift+F10'),
])
def test_save_is_global_and_persists_exact_selected_shortcut(draft_owner, enabled, shortcut):
    owner, app = draft_owner
    original_devices = copy.deepcopy(owner.config['device_settings'])
    dialog = open_draft(owner)
    try:
        dialog.enabled.setChecked(enabled)
        dialog.shortcut.setCurrentIndex(dialog.shortcut.findData(shortcut))
        dialog.buttons.accepted.emit()
        assert dialog.result() == QDialog.Accepted
        expected = {'enabled': enabled, 'shortcut': shortcut}
        assert owner.changes == [('emergency_hotkey', expected)]
        assert owner.config['emergency_hotkey'] == expected
        assert ConfigStore(owner.store.root).data['emergency_hotkey'] == expected
        assert owner.config['device_settings'] == original_devices
    finally:
        close_draft(dialog, app)


def test_existing_custom_shortcut_gets_one_canonical_item_and_can_be_saved(draft_owner):
    owner, app = draft_owner
    owner.config['emergency_hotkey'] = {'enabled': True, 'shortcut': 'shift+alt+f9'}
    dialog = open_draft(owner)
    try:
        values = [dialog.shortcut.itemData(i) for i in range(dialog.shortcut.count())]
        assert values.count('Alt+Shift+F9') == 1 and len(values) == 4
        assert dialog.shortcut.currentData() == 'Alt+Shift+F9'
        dialog.save()
        assert owner.config['emergency_hotkey'] == {'enabled': True, 'shortcut': 'Alt+Shift+F9'}
    finally:
        close_draft(dialog, app)


def test_failed_save_keeps_draft_for_retry_and_error_is_visible(draft_owner):
    owner, app = draft_owner
    owner.fail = True
    before = copy.deepcopy(owner.config)
    before_file = owner.store.path.read_bytes()
    dialog = open_draft(owner)
    try:
        dialog.enabled.setChecked(True)
        dialog.shortcut.setCurrentIndex(dialog.shortcut.findData('Ctrl+Shift+F10'))
        dialog.save()
        assert dialog.result() != QDialog.Accepted
        assert dialog.enabled.isChecked() and dialog.shortcut.currentData() == 'Ctrl+Shift+F10'
        assert not dialog.error.isHidden() and '无法写入' in dialog.error.text()
        assert dialog.buttons.button(QDialogButtonBox.Save).isEnabled()
        assert owner.config == before and owner.store.path.read_bytes() == before_file
        owner.fail = False
        dialog.save()
        assert dialog.result() == QDialog.Accepted
    finally:
        close_draft(dialog, app)


def test_standalone_workspace_owns_one_disabled_native_helper(hotkey_workspace):
    window, _, _, _ = hotkey_workspace
    assert window.emergency_hotkey is FakeHotkey.instances[0]
    assert len(FakeHotkey.instances) == 1
    assert window.emergency_hotkey.status() == {
        'enabled': False, 'shortcut': 'Ctrl+Alt+F10', 'registered': False, 'error': ''}
    assert window.emergency_hotkey_button.isEnabled()
    window.refresh_emergency_hotkey_status()
    assert '默认关闭' in window.emergency_hotkey_row.subtitle_label.text()
    assert 'F10' not in window.pause_button.toolTip()


def test_saved_registered_shortcut_shows_effective_hint_and_disabling_removes_it(hotkey_workspace):
    window, _, _, _ = hotkey_workspace
    settings = {'enabled': True, 'shortcut': 'Ctrl+Shift+F10'}
    window.setting('emergency_hotkey', settings)
    assert window.emergency_hotkey.registered
    assert 'Ctrl+Shift+F10' in window.emergency_hotkey_row.subtitle_label.text()
    assert '已生效' in window.emergency_hotkey_row.subtitle_label.text()
    assert 'Ctrl+Shift+F10' in window.pause_button.toolTip()
    window.setting('emergency_hotkey', {**settings, 'enabled': False})
    assert not window.emergency_hotkey.registered
    assert '默认关闭' in window.emergency_hotkey_row.subtitle_label.text()
    assert 'F10' not in window.pause_button.toolTip()


def test_registration_conflict_keeps_preference_but_does_not_claim_active(hotkey_workspace):
    window, _, _, _ = hotkey_workspace
    window.emergency_hotkey.error_override = '快捷键已被其他软件占用'
    settings = {'enabled': True, 'shortcut': 'Alt+Shift+F10'}
    window.setting('emergency_hotkey', settings)
    assert ConfigStore(window.store.root).data['emergency_hotkey'] == settings
    assert not window.emergency_hotkey.registered
    assert '占用' in window.emergency_hotkey_row.subtitle_label.text()
    assert 'F10' not in window.pause_button.toolTip()


def test_local_setting_failure_restores_previous_config_without_reconfiguring(hotkey_workspace, monkeypatch):
    window, _, _, _ = hotkey_workspace
    previous = copy.deepcopy(window.config['emergency_hotkey'])
    before_file = window.store.path.read_bytes()
    calls = copy.deepcopy(window.emergency_hotkey.configure_calls)
    monkeypatch.setattr(window.store, 'save', lambda: (_ for _ in ()).throw(OSError('磁盘无法写入')))
    with pytest.raises(OSError, match='磁盘无法写入'):
        window.setting('emergency_hotkey', {'enabled': True, 'shortcut': 'Ctrl+Shift+F10'})
    assert window.config['emergency_hotkey'] == previous
    assert window.store.path.read_bytes() == before_file
    assert window.emergency_hotkey.configure_calls == calls


def test_real_editor_entry_saves_and_reopens_with_saved_global_value(hotkey_workspace, monkeypatch):
    from gamepadstudio.emergency_hotkey_ui import EmergencyHotkeyDialog
    window, app, _, _ = hotkey_workspace

    def save(dialog):
        dialog.enabled.setChecked(True)
        dialog.shortcut.setCurrentIndex(dialog.shortcut.findData('Alt+Shift+F10'))
        dialog.save()
        return dialog.result()

    monkeypatch.setattr(EmergencyHotkeyDialog, 'exec', save)
    window.emergency_hotkey_button.click()
    assert window.emergency_hotkey.status()['registered']
    reopened = open_draft(window)
    try:
        assert reopened.enabled.isChecked() and reopened.shortcut.currentData() == 'Alt+Shift+F10'
    finally:
        close_draft(reopened, app)


@pytest.mark.parametrize('initially_enabled', [True, False])
def test_emergency_callback_always_pauses_releases_and_never_toggles_resume(
        hotkey_workspace, initially_enabled):
    window, _, _, notices = hotkey_workspace
    window.setting('emergency_hotkey', {'enabled': True, 'shortcut': 'Ctrl+Alt+F10'})
    window.enabled = initially_enabled
    window.config['mapping_enabled'] = initially_enabled
    window.actions.hold('Ctrl+W', True)
    window.actions.mouse_button('left', True)
    window.emergency_hotkey.trigger()
    assert window.enabled is False and window.config['mapping_enabled'] is False
    assert not window.actions.keys and not window.actions.mouse
    assert window.pause_button.accessibleName() == '恢复映射'
    assert window.pause_button.symbol == 'play'
    assert ConfigStore(window.store.root).data['mapping_enabled'] is False
    assert '手动恢复' in notices[-1]
    window.emergency_hotkey.trigger()
    assert window.enabled is False
    assert window.config['mapping_enabled'] is False


@pytest.mark.parametrize('failure', ['engine', 'actions', 'save'])
def test_emergency_failure_still_pauses_and_attempts_other_release_paths(
        hotkey_workspace, monkeypatch, failure):
    window, _, _, notices = hotkey_workspace
    window.setting('emergency_hotkey', {'enabled': True, 'shortcut': 'Ctrl+Alt+F10'})
    window.enabled = True
    calls = []
    with monkeypatch.context() as patch:
        for target, method, name in ((window.engine, 'reset', 'engine'),
                                     (window.actions, 'release_all', 'actions'),
                                     (window.store, 'save', 'save')):
            def operation(kind=name):
                calls.append(kind)
                if kind == failure:
                    raise OSError(f'{kind}失败')
            patch.setattr(target, method, operation)
        window.emergency_hotkey.trigger()
        assert calls == ['engine', 'actions', 'save']
        assert window.enabled is False and window.config['mapping_enabled'] is False
        assert '失败' in notices[-1]
        assert window.pause_button.accessibleName() == '恢复映射'
        assert window.pause_button.symbol == 'play'


def test_disabled_shortcut_does_not_call_pause_and_cleanup_closes_helper(hotkey_workspace):
    window, _, _, notices = hotkey_workspace
    window.enabled = True
    window.emergency_hotkey.trigger()
    assert window.enabled is True and notices == []
    window.cleanup()
    assert window.emergency_hotkey.closed
    assert not window.emergency_hotkey.registered


def test_manual_resume_after_emergency_requires_releasing_old_controller_input(hotkey_workspace):
    window, _, fake_device, _ = hotkey_workspace
    state = device(801)
    fake_device.state = window.snapshot = state
    window.update_controller_ui(state)
    profile = window.store.profiles_for(state, 'kbm')[0]
    window.store.apply_mapping_change({'op': 'select', 'profile': profile}, state)
    window.store.apply_mapping_change({'op': 'binding', 'profile': profile,
                                      'trigger': '0', 'mapping': entry('hold', 'E')}, state)
    window.setting('emergency_hotkey', {'enabled': True, 'shortcut': 'Ctrl+Alt+F10'})
    window.enabled = True
    window.engine.update(state, window.config, enabled=True, now=0)
    state['buttons'] = [0]
    window.engine.update(state, window.config, enabled=True, now=1)
    window.engine.update(state, window.config, enabled=True, now=1.2)
    assert window.actions.keys.get(ord('E')) == 1
    window.emergency_hotkey.trigger()
    assert not window.actions.keys and window.enabled is False
    window.toggle_pause()
    assert window.enabled is True
    window.engine.update(state, window.config, enabled=True, now=2)
    assert not any(window.actions.keys.values())
    state['buttons'] = []
    window.engine.update(state, window.config, enabled=True, now=3)
    state['buttons'] = [0]
    window.engine.update(state, window.config, enabled=True, now=4)
    window.engine.update(state, window.config, enabled=True, now=4.2)
    assert window.actions.keys.get(ord('E')) == 1


@pytest.mark.parametrize('hotkey_workspace', [{'remote': True}], indirect=True)
def test_remote_workspace_does_not_register_and_reads_backend_status(hotkey_workspace):
    window, _, _, _ = hotkey_workspace
    assert window.remote and window.emergency_hotkey is None
    assert FakeHotkey.instances == []
    settings = {'enabled': True, 'shortcut': 'Ctrl+Alt+F10'}
    window.setting('emergency_hotkey', settings)
    assert ('reload', {}) in window.client.sent
    assert FakeHotkey.instances == []
    assert '未生效' in window.emergency_hotkey_row.subtitle_label.text()
    window.client.status['emergency_hotkey'] = {**settings, 'registered': True, 'error': ''}
    window.refresh_emergency_hotkey_status()
    assert '已生效' in window.emergency_hotkey_row.subtitle_label.text()
    assert 'Ctrl+Alt+F10' in window.pause_button.toolTip()
    window.client.connected = False
    window.refresh_emergency_hotkey_status()
    assert '后台离线' in window.emergency_hotkey_row.subtitle_label.text()
    assert 'F10' not in window.pause_button.toolTip()


@pytest.mark.parametrize('hotkey_workspace', [{'remote': True}], indirect=True)
@pytest.mark.parametrize('status', [
    {}, {'enabled': True, 'shortcut': 'Alt+Shift+F10', 'registered': True, 'error': ''},
    {'enabled': True, 'shortcut': 'Ctrl+Alt+F10', 'registered': False, 'error': '快捷键已被其他软件占用'},
])
def test_remote_missing_stale_or_conflicting_status_never_advertises_effective_shortcut(
        hotkey_workspace, status):
    window, _, _, _ = hotkey_workspace
    window.config['emergency_hotkey'] = {'enabled': True, 'shortcut': 'Ctrl+Alt+F10'}
    window.client.status['emergency_hotkey'] = status
    window.refresh_emergency_hotkey_status()
    assert 'F10' not in window.pause_button.toolTip()
    assert '已生效' not in window.emergency_hotkey_row.subtitle_label.text()


@pytest.mark.parametrize('hotkey_workspace', [{'remote': True}], indirect=True)
def test_remote_callback_cannot_disable_mapping_or_release_ui_output(hotkey_workspace):
    window, _, _, notices = hotkey_workspace
    before = copy.deepcopy(window.config)
    before_calls = list(window.client.sent)
    window.actions.hold('E', True)
    window.emergency_pause()
    assert window.config == before and window.client.sent == before_calls
    assert window.actions.keys and notices == []


@pytest.mark.parametrize('hotkey_workspace', ['zh', 'en'], indirect=True)
def test_global_setting_row_and_draft_fit_both_languages(hotkey_workspace):
    window, app, _, _ = hotkey_workspace
    window.setStyleSheet(STYLE)
    window.navigate(4)
    window.show()
    for width in (960, 1440):
        window.resize(width, 900)
        QTest.qWait(20)
        app.processEvents()
        row = window.emergency_hotkey_row
        position = row.control.mapTo(row, QPoint(0, 0))
        assert row.control is window.emergency_hotkey_button
        assert row.control.isVisible() and row.control.isEnabled()
        assert position.x() + row.control.width() <= row.width()
        assert row.title_label.mapTo(row, QPoint(0, 0)).x() + row.title_label.width() <= position.x()
        page = window.stack.currentWidget()
        assert isinstance(page, QScrollArea) and page.horizontalScrollBar().maximum() == 0
    dialog = open_draft(window)
    try:
        dialog.resize(350, 400)
        dialog.show()
        QTest.qWait(20)
        save = dialog.buttons.button(QDialogButtonBox.Save)
        cancel = dialog.buttons.button(QDialogButtonBox.Cancel)
        assert save.isVisible() and cancel.isVisible()
        assert dialog.rect().contains(save.mapTo(dialog, QPoint(0, 0)))
        assert dialog.rect().contains(save.mapTo(dialog, QPoint(save.width() - 1, save.height() - 1)))
        assert dialog.shortcut.count() == 3
        assert '%' not in window.emergency_hotkey_row.subtitle_label.text()
    finally:
        close_draft(dialog, app)
