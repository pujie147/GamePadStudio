"""macOS capabilities stay honest without input injection or permission prompts."""
import copy
import sys
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QObject, QSize, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel
from shiboken6 import delete

from gamepadstudio import (hidhide, studio, virtual_kbm_ui, macos_permissions,
                           application_profiles, application_profiles_ui, emergency_hotkey, emergency_hotkey_ui)
from gamepadstudio.i18n import get_language_preference, init_language, tr
from gamepadstudio.mapping_engine import MappingRuntime
from tests.test_battery_ui import BatteryDevice, FakeTray
from tests.test_device_scope_ui import device
from tests.test_unified_mapping import Actions
from tests.test_emergency_hotkey_ui import FakeHotkey


@pytest.fixture
def mac_workspace(tmp_path, monkeypatch, request):
    app = QApplication.instance() or QApplication([])
    previous_language = get_language_preference()
    options = getattr(request, 'param', {})
    physical = BatteryDevice()
    permission = {'supported': True, 'granted': False, 'reason': 'permission required'}
    prompts = []
    links = []
    startup_changes = []
    supported = options.get('supported', True)
    isolation = {'supported':supported,'enabled':False,'active':False,'restore_pending':False,'status':'off','reason':''}
    physical.controller_isolation_status = lambda: isolation.copy()
    def set_isolation(enabled):
        isolation.update(enabled=enabled, active=enabled, status='active' if enabled else 'off')
        return isolation.copy()
    physical.set_controller_isolation = set_isolation
    FakeHotkey.instances = []

    class Client(QObject):
        event = Signal(dict)

        def __init__(self, root, parent=None):
            super().__init__(parent)
            self.connected = True
            self.status = {'enabled': True, 'devices': [], 'input_permission': permission,
                           'controller_isolation':isolation,
                           'emergency_hotkey': {'enabled': False, 'shortcut': 'Ctrl+Alt+F10',
                                                'registered': False, 'error': ''}}
            self.state = None
            self.sent = []

        def send(self, command, **kwargs):
            self.sent.append((command, kwargs))
            return True

        def close(self):
            self.connected = False

    class Displays:
        monitors = [{'left': 0, 'top': 0, 'width': 200, 'height': 100},
                    {'left': 0, 'top': 0, 'width': 100, 'height': 100},
                    {'left': 100, 'top': 0, 'width': 100, 'height': 100}]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(studio, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(virtual_kbm_ui, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(application_profiles, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(application_profiles_ui, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(emergency_hotkey, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(emergency_hotkey_ui, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(studio, 'WINDOWS_FEATURES', False)
    monkeypatch.setattr(virtual_kbm_ui, 'WINDOWS_FEATURES', False)
    monkeypatch.setattr(studio, 'Device', lambda: physical)
    monkeypatch.setattr(studio, 'create_actions', Actions)
    monkeypatch.setattr(studio, 'MappingRuntime',
                        lambda actions, dispatch, **kwargs: MappingRuntime(actions, dispatch, start_mouse=False))
    monkeypatch.setattr(studio, 'QSystemTrayIcon', FakeTray)
    monkeypatch.setattr(studio, 'autostart_enabled', lambda root=None: False)
    monkeypatch.setattr(studio, 'autostart_supported', lambda: supported)
    monkeypatch.setattr(studio, 'emergency_hotkey_supported', lambda: supported)
    monkeypatch.setattr(studio, 'application_profiles_supported', lambda: supported)
    monkeypatch.setattr(virtual_kbm_ui, 'application_profiles_supported', lambda: supported)
    monkeypatch.setattr(application_profiles, 'foreground_application',
                        lambda: {'pid': 0, 'hwnd': 0, 'executable': ''})
    monkeypatch.setattr(studio, 'EmergencyHotkey', FakeHotkey)
    monkeypatch.setattr(studio, 'ScreenshotHotkey', FakeHotkey)
    monkeypatch.setattr(studio, 'set_autostart', lambda root, enabled: startup_changes.append((root, enabled)))
    monkeypatch.setattr(studio, 'AgentClient', Client)
    monkeypatch.setattr(studio, 'request', lambda *args, **kwargs: {'ok': True})
    monkeypatch.setattr(studio, 'spawn', lambda *args: pytest.fail('Unexpected process launch'))
    monkeypatch.setattr(studio, 'input_permission_status', lambda: permission)
    monkeypatch.setattr(studio, 'request_input_permission', lambda: prompts.append('input') or permission)
    monkeypatch.setattr(macos_permissions, 'screen_capture_permission_status', lambda: permission)
    monkeypatch.setattr(macos_permissions, 'request_screen_capture_permission',
                        lambda: prompts.append('screen') or permission)
    monkeypatch.setattr(studio.QDesktopServices, 'openUrl', lambda url: links.append(url.toString()) or True)
    monkeypatch.setattr('mss.mss', Displays)
    monkeypatch.setattr('gamepadstudio.replay_capture.create_replay_capture', Displays)
    monkeypatch.setattr('gamepadstudio.display_info.enumerate_displays', lambda: [])
    # Use the same pure key capability table on all test hosts.
    from gamepadstudio.macos_actions import MacActions
    monkeypatch.setattr('gamepadstudio.actions.supports_key', MacActions.supports_key)
    window = studio.Studio(tmp_path, standalone=not options.get('remote', False), lang=options.get('lang', 'zh'))
    window.timer.stop(); window.scan_timer.stop(); window.gallery_timer.stop()
    window.enabled = False
    notices = []
    monkeypatch.setattr(window, 'notify', notices.append)
    yield SimpleNamespace(window=window, app=app, physical=physical, permission=permission,
                          prompts=prompts, links=links, notices=notices, startup_changes=startup_changes)
    window.cleanup(); window.hide(); window.deleteLater()
    # Delete this workspace only. Flushing the whole app also processes delayed
    # deletion from unrelated tests that may still retain Qt wrapper references.
    app.sendPostedEvents(window, QEvent.DeferredDelete)
    init_language(previous_language)


@pytest.mark.parametrize('mac_workspace', [{}, {'remote': True}], indirect=True)
def test_mac_supported_capabilities_available_but_windows_controls_disabled(mac_workspace):
    w = mac_workspace.window
    state = device(801, family='ps5', touch=True)
    w.snapshot = state
    w.update_controller_ui(state)
    w.refresh_application_profile_status()
    w.refresh_emergency_hotkey_status()
    w.virtual_kbm_page.set_device_state(state)
    assert w.emergency_hotkey is None if w.remote else isinstance(w.emergency_hotkey, FakeHotkey)
    assert w.emergency_hotkey_button.isEnabled()
    assert w.gamebar_shield_box.isEnabled()
    assert not w.gamebar_shield_box.isChecked()
    assert w.autostart.isEnabled() and not w.autostart.isChecked()
    assert mac_workspace.startup_changes == []
    assert w.application_profiles_action.isEnabled()
    assert w.virtual_kbm_page.application_profiles_action.isEnabled()
    assert w.virtual_kbm_page.cloaking_action.isEnabled()
    assert '默认关闭' in w.emergency_hotkey_row.subtitle_label.text()
    assert 'F10' not in w.pause_button.toolTip()
    assert w.import_profile_action.isEnabled()
    assert w.export_profile_action.isEnabled()


@pytest.mark.parametrize('mac_workspace', [{}, {'remote': True}], indirect=True)
def test_mac_permission_checks_do_not_prompt_until_click(mac_workspace):
    env = mac_workspace
    assert env.prompts == env.links == []
    assert '辅助功能' in env.window.input_permission_row.subtitle_label.text()
    assert '屏幕录制' in env.window.screen_permission_row.subtitle_label.text()
    env.window.input_permission_button.click()
    env.window.screen_permission_button.click()
    assert env.prompts == ['input', 'screen']
    assert env.links == [studio.ACCESSIBILITY_SETTINGS_URL, studio.SCREEN_CAPTURE_SETTINGS_URL]
    env.permission['granted'] = True
    env.window.refresh_input_permission_status(force=True)
    assert '已授权' in env.window.input_permission_row.subtitle_label.text()
    assert '已授权' in env.window.screen_permission_row.subtitle_label.text()


def test_mac_capture_scopes_preserve_game_window_and_replay_modes(mac_workspace):
    w = mac_workspace.window
    capture_modes = {w.mode_combo.itemData(i) for i in range(w.mode_combo.count())}
    replay_modes = {w.replay_mode_combo.itemData(i) for i in range(w.replay_mode_combo.count())}
    assert capture_modes == {'game', 'window', 'all', 'monitor_1', 'monitor_2'}
    assert replay_modes == {'game', 'all', 'monitor_1', 'monitor_2'}
    assert w.config['capture_mode'] == w.config['replay_capture_mode'] == 'game'
    all_index = w.replay_mode_combo.findData('all')
    all_item = w.replay_mode_combo.model().item(all_index)
    assert not all_item.isEnabled()
    assert all_item.toolTip()


@pytest.mark.parametrize('mac_workspace', [{}, {'remote': True}], indirect=True)
def test_mac_screenshot_shortcut_can_be_changed_disabled_and_reports_effective_status(mac_workspace):
    from gamepadstudio.screenshot_hotkey_ui import ScreenshotHotkeyDialog
    w = mac_workspace.window
    assert w.screenshot_hotkey_button.isEnabled()
    if w.remote:
        assert w.screenshot_hotkey is None
    else:
        assert isinstance(w.screenshot_hotkey, FakeHotkey)
    dialog = ScreenshotHotkeyDialog(w)
    try:
        dialog.enabled.setChecked(True)
        dialog.shortcut.setCurrentText('Alt+Cmd+S')
        dialog.save()
        assert w.config['screenshot_hotkey'] == {'enabled': True, 'shortcut': 'Alt+Cmd+S'}
        if w.remote:
            assert '未生效' in w.screenshot_hotkey_row.subtitle_label.text()
            w.client.status['screenshot_hotkey'] = {
                'enabled': True, 'shortcut': 'Alt+Cmd+S', 'registered': True, 'error': ''}
            w.refresh_screenshot_hotkey_status()
        assert 'Alt+Cmd+S' in w.screenshot_hotkey_row.subtitle_label.text()
    finally:
        dialog.close()
        dialog.deleteLater()
    w.setting('screenshot_hotkey', {'enabled': False, 'shortcut': 'Alt+Cmd+S'})
    assert '已关闭' in w.screenshot_hotkey_row.subtitle_label.text()


@pytest.mark.parametrize('mac_workspace', [{}, {'remote': True}], indirect=True)
def test_mac_replay_button_requires_previously_running_buffer_and_av1_label_is_software(mac_workspace):
    w = mac_workspace.window
    assert tr('AV1 软件编码 (45Mbps)') == w.replay_codec_combo.itemText(w.replay_codec_combo.findData('av1'))
    assert any(tr('先启用并等待画面积累，再用手柄映射或下方按钮保存过去的片段。') == label.text()
               for label in w.settings_page.findChildren(QLabel))
    before = list(w.client.sent) if w.remote else None
    w.trigger_manual_replay()
    assert tr('请先启用回放缓存，等待画面积累后保存') in mac_workspace.notices
    if w.remote:
        assert w.client.sent == before
    w.setting('replay_buffer_enabled', True)
    w.trigger_manual_replay()
    assert tr('回放录制尚未就绪，请检查状态并等待画面积累') in mac_workspace.notices
    if w.remote:
        assert w.client.sent[-1][0] == 'reload'
        w.client.status['replay'] = {'running': True}
        w.trigger_manual_replay()
        assert w.client.sent[-1][0] == 'save_replay'


def test_mac_keyboard_names_and_unsupported_keys_preserve_stored_tokens(mac_workspace):
    page = mac_workspace.window.virtual_kbm_page
    assert page.keycaps['Win'].display_name == 'Cmd'
    assert page.keycaps['Ctrl'].display_name == 'Ctrl'
    assert page.keycaps['Alt'].display_name == 'Option'
    assert page.key_token('Win') == page.key_token('Cmd') == 'key:91'
    assert page.key_token('Ctrl') == 'key:17'
    for key in ('Insert', 'PrtScn', 'ScrLk', 'Pause', 'NumLock', 'Calc'):
        cap = page.keycaps[key]
        assert not cap.isEnabled()
        cap.update_tooltip()
        assert cap.toolTip()
    for key in ('Caps', 'Menu', 'Mute', 'Vol+', 'Vol-'):
        assert page.keycaps[key].isEnabled()
    assert page.keycaps['Space'].isEnabled()
    recording = page.keycaps['action:record_toggle']
    assert recording.isEnabled()
    assert page.keycaps['action:capture'].isEnabled()
    assert page.keycaps['action:replay_record'].isEnabled()


def test_mac_isolation_panel_applies_real_backend_result_without_installing_driver(mac_workspace):
    w = mac_workspace.window
    w.snapshot = device(801,family='ps5',touch=True)
    w.snapshot.update(buttons=[], axes=[0]*6)
    w.virtual_kbm_page.set_device_state(w.snapshot)
    page = w.virtual_kbm_page
    page.open_cloaking()
    page.cloaking_toggle.setChecked(True)
    assert w.device.controller_isolation_status()['active']
    assert '隔离' in page.cloaking_status_label.text()
    assert w.gamebar_shield_box.isChecked()
    from gamepadstudio.controller_isolation_service import isolation_requested
    assert isolation_requested(w.config,w.snapshot)
    page.cloaking_toggle.setChecked(False)
    assert not w.device.controller_isolation_status()['active']
    assert not w.gamebar_shield_box.isChecked()
    assert mac_workspace.links == []
    page.cloaking_dialog.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS recording dispatch')
def test_mac_record_toggle_uses_local_recording_instead_of_windows_hotkey(mac_workspace):
    w = mac_workspace.window
    w.dispatch({'action':'record_toggle'},False)
    assert w.manual_recording is None
    w.dispatch({'action':'record_toggle'})
    assert w.manual_recording.status()['running']
    w.dispatch({'action':'record_toggle'})
    assert not w.manual_recording.status()['running']


@pytest.mark.parametrize('mac_workspace', [{'lang': 'zh'}, {'lang': 'en'}], indirect=True)
def test_mac_permission_rows_fit_supported_workspace_widths(mac_workspace):
    w = mac_workspace.window
    w.setStyleSheet(studio.STYLE)
    w.navigate(4)
    w.show()
    for width in (960, 1440):
        w.resize(width, 900)
        QTest.qWait(20)
        mac_workspace.app.processEvents()
        for row in (w.input_permission_row, w.screen_permission_row):
            control = row.control
            assert control.isVisible() and control.isEnabled()
            assert control.x() + control.width() <= row.width()
            assert row.title_label.x() + row.title_label.width() <= control.x()
            assert row.subtitle_label.text()
        assert w.stack.currentWidget().horizontalScrollBar().maximum() == 0


def test_mac_permission_request_failure_opens_manual_settings(mac_workspace, monkeypatch):
    def failed_request():
        raise OSError('Unavailable API')
    monkeypatch.setattr(studio, 'request_input_permission', failed_request)
    mac_workspace.window.input_permission_button.click()
    assert mac_workspace.links == [studio.ACCESSIBILITY_SETTINGS_URL]
    assert any('Unavailable API' in text for text in mac_workspace.notices)


def test_mac_login_startup_writes_only_after_explicit_toggle(mac_workspace):
    env = mac_workspace
    assert env.startup_changes == []
    env.window.autostart.setChecked(True)
    env.window.autostart.setChecked(False)
    assert env.startup_changes == [(env.window.store.root, True), (env.window.store.root, False)]


@pytest.mark.parametrize('mac_workspace', [{'remote': True}], indirect=True)
def test_disabling_login_startup_waits_for_old_backend_then_preserves_pause(mac_workspace, monkeypatch):
    w = mac_workspace.window
    w.config['mapping_enabled'] = False
    launched = []
    alive = [True]
    monkeypatch.setattr(studio, 'request', lambda *args, **kwargs: None)
    monkeypatch.setattr(studio, 'read_lock_pid', lambda path: 123)
    monkeypatch.setattr(studio, 'is_process_alive', lambda pid: alive[0])
    monkeypatch.setattr(studio, 'cleanup_stale_agent', lambda root: True)
    monkeypatch.setattr(studio, 'spawn', lambda *args: launched.append(args))
    w.toggle_autostart(False)
    assert w._autostart_handoff_timer.isActive() and not launched
    w._finish_autostart_handoff()
    assert not launched
    alive[0] = False
    w._finish_autostart_handoff()
    assert launched == [(w.store.root, '--agent')]
    assert w._autostart_handoff_timer is None
    assert w.config['mapping_enabled'] is False


@pytest.mark.parametrize('mac_workspace', [{'remote': True}], indirect=True)
def test_disabling_login_startup_keeps_existing_manual_backend(mac_workspace):
    w = mac_workspace.window
    w.toggle_autostart(False)
    assert w._autostart_handoff_timer.isActive()
    w._finish_autostart_handoff()
    assert w._autostart_handoff_timer is None


def test_mac_hotkey_editor_offers_command_without_enabling_until_saved(mac_workspace):
    from PySide6.QtWidgets import QDialog
    env = mac_workspace
    w = env.window
    before = copy.deepcopy(w.config['emergency_hotkey'])
    dialog = emergency_hotkey_ui.EmergencyHotkeyDialog(w)
    try:
        values = [dialog.shortcut.itemData(i) for i in range(dialog.shortcut.count())]
        assert {'Ctrl+Alt+F10', 'Cmd+Alt+F10', 'Cmd+Shift+F10'} <= set(values)
        assert not dialog.enabled.isChecked() and not w.emergency_hotkey.registered
        dialog.enabled.setChecked(True)
        dialog.shortcut.setCurrentIndex(dialog.shortcut.findData('Cmd+Alt+F10'))
        assert w.config['emergency_hotkey'] == before
        assert not w.emergency_hotkey.registered
        dialog.save()
        assert dialog.result() == QDialog.Accepted
        assert w.config['emergency_hotkey'] == {'enabled': True, 'shortcut': 'Alt+Cmd+F10'}
        assert w.emergency_hotkey.registered
        assert 'Alt+Cmd+F10' in w.pause_button.toolTip()
    finally:
        dialog.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS application bundle picker')
def test_mac_application_picker_resolves_bundle_and_deduplicates_executable(mac_workspace, tmp_path, monkeypatch):
    import plistlib
    from PySide6.QtWidgets import QFileDialog
    env = mac_workspace
    w = env.window
    w.snapshot = device(801, family='ps5')
    w.update_controller_ui(w.snapshot)
    bundle = tmp_path / 'Actual App.app'
    binary = bundle / 'Contents' / 'MacOS' / 'DifferentBinaryName'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'fixture')
    (bundle / 'Contents' / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': binary.name}))
    selected = [str(bundle), str(binary)]
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *args: (selected.pop(0), ''))
    dialog = application_profiles_ui.ApplicationProfilesDialog(w)
    try:
        dialog.choose_application()
        assert dialog.rows[0].executable == str(binary)
        dialog.choose_application()
        assert len(dialog.rows) == 1 and '已关联' in dialog.message.text()
        dialog.enabled_box.setChecked(True)
        dialog.save()
        settings = w.store.application_settings(w.snapshot)
        assert settings['enabled']
        assert settings['rules'][0]['executable'] == str(binary)
    finally:
        dialog.close()


def test_late_workspace_callbacks_do_not_touch_devices_after_shutdown(mac_workspace, monkeypatch):
    w = mac_workspace.window
    w.cleanup()
    def closed_device_access():
        pytest.fail('A queued callback accessed the closed device')
    monkeypatch.setattr(w.device, 'read', closed_device_access)
    monkeypatch.setattr(w.device, 'scan', closed_device_access)
    w.poll()
    w.scan()
    w.refresh_gallery()
    w.reflow_workspace()


@pytest.mark.parametrize('mac_workspace', [{'remote': True}], indirect=True)
def test_remote_workspace_shows_imported_unsupported_bindings_after_agent_started(mac_workspace):
    window = mac_workspace.window
    window.client.status['enabled'] = True
    window.client.status['mapping'] = {'unsupported_bindings': [
        {'trigger': '0', 'gesture': 'short', 'value': 'PrintScreen',
         'reason': '此平台没有等效键盘输出'}]}
    window.notice_timer.stop()
    window.poll()
    assert '不支持' in window.notice.text()
    assert 'PrintScreen' in window.notice.toolTip()

    window.client.status['mapping'] = {'unsupported_bindings': []}
    window.poll()
    assert '映射运行中' in window.notice.text()
    assert window.notice.toolTip() == ''


def test_keyboard_resize_filters_tolerate_children_destroyed_before_parent(mac_workspace):
    w = mac_workspace.window
    page = w.virtual_kbm_page
    event = QResizeEvent(QSize(600, 400), QSize(640, 480))
    delete(page.area)
    assert page.eventFilter(w, event) is False
    orphan_canvas = virtual_kbm_ui.KeyboardCanvas(page)
    orphan_canvas.resizeEvent(event)
    delete(orphan_canvas)
    dialog = virtual_kbm_ui.NikkiLayoutDialog(w, page.current_scheme())
    delete(dialog.area)
    assert dialog.eventFilter(w, event) is False


@pytest.mark.parametrize('mac_workspace', [{'supported': False}], indirect=True)
def test_unsupported_ui_entry_points_cannot_mutate_platform_settings(mac_workspace):
    w = mac_workspace.window
    before = copy.deepcopy(w.config)
    w.on_toggle_gamebar_shield(True)
    w.toggle_autostart(True)
    w.open_emergency_hotkey_editor()
    w.open_application_profiles()
    w.virtual_kbm_page.open_cloaking()
    assert w.config == before
    assert len(mac_workspace.notices) == 5


def test_hidhide_mac_paths_do_not_touch_registry_or_driver(monkeypatch):
    monkeypatch.setattr(hidhide, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(hidhide, 'winreg', SimpleNamespace(
        OpenKey=lambda *args: pytest.fail('Unexpected Windows registry access')))
    # macOS 上每个入口都要在碰设备之前就返回；构造会话会被 conftest 硬闸拦下，
    # 所以这里只断言那些“提前返回”的路径确实提前返回。
    assert hidhide.self_app_paths() == []
    assert hidhide.ensure_self_access() == (True, '')
    assert hidhide.running_hidhide_clients() == []
    assert hidhide.dos_to_nt_path(r'C:\a\b.exe') == r'C:\a\b.exe'
    info = hidhide.driver_info()
    assert info.installed is False and info.loaded is False
    assert 'Windows' in info.error
    assert hidhide.install_driver().ok is False
