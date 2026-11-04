"""Battery notices stay with one real device session and never steal focus.

The workspace is real Qt/Core code; SDL, keyboard output and the tray are fakes.
No test sends a native notification or changes the personal configuration.
"""
import copy
import json
import os
from types import SimpleNamespace

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea, QSystemTrayIcon

from gamepadstudio.i18n import get_language_preference, init_language
from gamepadstudio.mapping_engine import MappingRuntime
from gamepadstudio.studio import Studio, STYLE
from gamepadstudio.studio_core import ConfigStore, profile_scope
from tests.test_device_scope_ui import device
from tests.test_unified_mapping import Actions


class FakeTray(QObject):
    activated = Signal(object)
    messageClicked = Signal()
    Trigger = QSystemTrayIcon.Trigger
    DoubleClick = QSystemTrayIcon.DoubleClick
    Information = QSystemTrayIcon.Information
    Warning = QSystemTrayIcon.Warning
    Critical = QSystemTrayIcon.Critical
    Context = QSystemTrayIcon.Context
    MiddleClick = QSystemTrayIcon.MiddleClick

    def __init__(self, icon, parent=None):
        super().__init__(parent)
        self.visible = False
        self.messages = []
        self.menu = None
        self.tooltip = ''
        self.show_count = 0

    def setToolTip(self, value):
        self.tooltip = value

    def toolTip(self):
        return self.tooltip

    def setContextMenu(self, menu):
        self.menu = menu

    def contextMenu(self):
        return self.menu

    def show(self):
        self.visible = True
        self.show_count += 1

    def hide(self):
        self.visible = False

    def isVisible(self):
        return self.visible

    def showMessage(self, *args):
        self.messages.append(args)

    @staticmethod
    def isSystemTrayAvailable():
        return True

    @staticmethod
    def supportsMessages():
        return True


class BatteryDevice:
    def __init__(self):
        self.state = None
        self.available = []
        self.closed = False

    def scan(self):
        pass

    def read(self):
        return self.state

    def close(self):
        self.closed = True

    def led(self, color):
        return False

    def set_response_curves(self, settings):
        pass


@pytest.fixture
def battery_workspace(tmp_path, monkeypatch, request):
    app = QApplication.instance() or QApplication([])
    preference = get_language_preference()
    fake_device = BatteryDevice()
    monkeypatch.setattr('gamepadstudio.studio.Device', lambda: fake_device)
    monkeypatch.setattr('gamepadstudio.studio.create_actions', Actions)
    monkeypatch.setattr('gamepadstudio.studio.MappingRuntime',
                        lambda actions, dispatch, **kwargs:
                        MappingRuntime(actions, dispatch, start_mouse=False))
    monkeypatch.setattr('gamepadstudio.studio.QSystemTrayIcon', FakeTray)
    monkeypatch.setattr('gamepadstudio.studio.autostart_enabled', lambda root=None: False)
    monkeypatch.setattr('gamepadstudio.application_profiles.foreground_application',
                        lambda: {'hwnd': 0, 'pid': 0, 'executable': ''})
    window = Studio(tmp_path, standalone=True, lang=getattr(request, 'param', 'zh'))
    window.timer.stop()
    window.scan_timer.stop()
    window.gallery_timer.stop()
    window.enabled = False
    notices = []
    original_notify = window.notify

    def notify(text):
        notices.append(text)
        original_notify(text)

    monkeypatch.setattr(window, 'notify', notify)
    yield window, app, fake_device, notices
    window.cleanup()
    window.hide()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    init_language(preference)


def select_battery_device(workspace, state):
    window, app, fake_device, notices = workspace
    fake_device.state = state
    window.snapshot = state
    window.update_controller_ui(state)
    app.processEvents()
    notices.clear()
    window.tray.messages.clear()


def event_for(state, level=None, identifier='ui-session:1'):
    return {'type': 'battery', 'id': identifier,
            'device_scope': profile_scope(state), 'instance_id': state['instance_id'],
            'level': state['power'] if level is None else level, 'name': state['name']}


@pytest.mark.parametrize('power', [0, 1, 2, 3])
def test_reported_battery_levels_show_current_device_setting(battery_workspace, power):
    window, _, _, _ = battery_workspace
    state = device(801)
    state['power'] = power
    select_battery_device(battery_workspace, state)
    assert not window.battery_settings_row.isHidden()
    assert window.battery_notifications_box.isEnabled()
    assert window.battery_notifications_box.isChecked()


@pytest.mark.parametrize('power', [-1, 4, 5, None, '1', True, False, 1.0, 0.0])
def test_unreported_or_wired_power_hides_setting_without_changing_saved_value(
        battery_workspace, power):
    window, _, _, _ = battery_workspace
    state = device(801)
    state['power'] = 2
    select_battery_device(battery_workspace, state)
    window.battery_notifications_box.setChecked(False)
    before = copy.deepcopy(window.config)
    before_file = window.store.path.read_bytes()
    state['power'] = power
    window.snapshot = state
    window.refresh_battery_status()
    assert window.battery_settings_row.isHidden()
    assert not window.battery_notifications_box.isEnabled()
    assert window.config == before
    assert window.store.path.read_bytes() == before_file
    assert window.store.settings_for(state)['battery_notifications_enabled'] is False


def test_disconnect_hides_setting_and_device_values_restore_independently(battery_workspace):
    window, _, _, _ = battery_workspace
    first, second = device(801), device(802)
    first['power'], second['power'] = 2, 3
    select_battery_device(battery_workspace, first)
    window.battery_notifications_box.setChecked(False)
    select_battery_device(battery_workspace, second)
    assert window.battery_notifications_box.isChecked()
    select_battery_device(battery_workspace, None)
    assert window.battery_settings_row.isHidden()
    assert not window.battery_notifications_box.isEnabled()
    select_battery_device(battery_workspace, first)
    assert not window.battery_notifications_box.isChecked()
    restored = ConfigStore(window.store.root)
    assert restored.settings_for(first)['battery_notifications_enabled'] is False
    assert restored.settings_for(second)['battery_notifications_enabled'] is True


@pytest.mark.parametrize('level,title', [(1, '手柄电量低'), (0, '手柄电量极低')])
def test_notice_routes_to_local_log_and_tray_once_without_taking_focus(
        battery_workspace, monkeypatch, level, title):
    window, _, _, notices = battery_workspace
    state = device(801)
    state.update(power=level, name='当前蓝牙手柄')
    select_battery_device(battery_workspace, state)
    focus_calls = []
    for method in ('show_home', 'activateWindow', 'raise_', 'show'):
        monkeypatch.setattr(window, method, lambda m=method: focus_calls.append(m))
    event = event_for(state)
    event['name'] = '过期或伪造的设备名称'
    assert window.handle_battery_event(event)
    assert window.handle_battery_event(event) is False
    assert len(notices) == len(window.tray.messages) == 1
    assert title in notices[0] and state['name'] in notices[0]
    assert event['name'] not in notices[0]
    toast = window.tray.messages[0]
    assert toast[0] == title and state['name'] in toast[1]
    assert '%' not in notices[0]
    assert focus_calls == []
    rows = [json.loads(row) for row in (window.store.root / 'events.jsonl').read_text(
        encoding='utf-8').splitlines()]
    assert rows[-1]['message'] == notices[0]
    assert window.enabled is False
    assert window.actions.calls == []


@pytest.mark.parametrize('mutation', [
    'other_scope', 'old_instance', 'other_level', 'disabled', 'disconnect',
    'wired', 'unknown', 'bool_level', 'float_level', 'string_level',
    'empty_id', 'long_id', 'missing_id', 'nonstring_id',
])
def test_stale_or_invalid_notice_is_rejected_without_logging_or_toast(
        battery_workspace, mutation):
    window, _, _, notices = battery_workspace
    state = device(801)
    state['power'] = 1
    select_battery_device(battery_workspace, state)
    event = event_for(state)
    if mutation == 'other_scope':
        event['device_scope'] = profile_scope(device(802))
    elif mutation == 'old_instance':
        event['instance_id'] -= 1
    elif mutation == 'other_level':
        event['level'] = 0
    elif mutation == 'disabled':
        window.battery_notifications_box.setChecked(False)
    elif mutation == 'disconnect':
        window.snapshot = None
    elif mutation in ('wired', 'unknown'):
        state['power'] = 4 if mutation == 'wired' else -1
    elif mutation == 'bool_level':
        event['level'] = True
    elif mutation == 'float_level':
        event['level'] = 1.0
    elif mutation == 'string_level':
        event['level'] = '1'
    elif mutation == 'empty_id':
        event['id'] = ''
    elif mutation == 'long_id':
        event['id'] = 'x' * 101
    elif mutation == 'missing_id':
        event.pop('id')
    else:
        event['id'] = 1
    before_warning = copy.deepcopy(window.battery_warning)
    assert window.handle_battery_event(event) is False
    assert notices == [] and window.tray.messages == []
    assert window.battery_warning == before_warning


def test_old_instance_notice_stays_invalid_after_same_device_reconnect(battery_workspace):
    window, _, _, notices = battery_workspace
    state = device(801)
    state['power'] = 1
    select_battery_device(battery_workspace, state)
    old_event = event_for(state)
    select_battery_device(battery_workspace, None)
    reconnected = copy.deepcopy(state)
    reconnected['instance_id'] = 999
    select_battery_device(battery_workspace, reconnected)
    assert profile_scope(state) == profile_scope(reconnected)
    assert not window.handle_battery_event(old_event)
    assert window.handle_battery_event(event_for(reconnected, identifier='ui-session:2'))
    assert len(notices) == len(window.tray.messages) == 1


def test_low_to_critical_notice_and_bounded_duplicate_memory(battery_workspace):
    window, _, _, notices = battery_workspace
    state = device(801)
    state['power'] = 1
    select_battery_device(battery_workspace, state)
    assert window.handle_battery_event(event_for(state))
    state['power'] = 0
    assert window.handle_battery_event(event_for(state, identifier='ui-session:2'))
    assert len(window.tray.messages) == 2
    assert '极低' in notices[-1]
    for index in range(3, 132):
        assert window.handle_battery_event(event_for(state, identifier=f'ui-session:{index}'))
    assert len(window._battery_alert_ids) == len(window._battery_alert_order) == 128
    assert 'ui-session:1' not in window._battery_alert_ids
    assert not window.handle_battery_event(event_for(state, identifier='ui-session:131'))


@pytest.mark.parametrize('tray_mode', ['hidden', 'unsupported'])
def test_local_warning_remains_useful_when_tray_cannot_show_messages(
        battery_workspace, monkeypatch, tray_mode):
    window, _, _, notices = battery_workspace
    state = device(801)
    state['power'] = 0
    select_battery_device(battery_workspace, state)
    if tray_mode == 'hidden':
        window.tray.hide()
    else:
        monkeypatch.setattr(FakeTray, 'supportsMessages', staticmethod(lambda: False))
    assert window.handle_battery_event(event_for(state))
    assert len(notices) == 1 and window.tray.messages == []
    assert window.battery_warning['level'] == 0


def test_status_warning_only_updates_tooltip_and_never_replays_notification(battery_workspace):
    window, _, _, notices = battery_workspace
    state = device(801)
    state['power'] = 1
    select_battery_device(battery_workspace, state)
    warning = event_for(state)
    window.remote = True
    window.client = SimpleNamespace(connected=True, status={'battery_warning': warning})
    try:
        for _ in range(3):
            window.refresh_battery_status()
        assert window.battery_warning == warning
        assert '充电' in window.power_label.toolTip()
        assert notices == [] and window.tray.messages == []
        assert warning['id'] not in window._battery_alert_ids
        window.snapshot['power'] = 2
        window.refresh_battery_status()
        assert window.battery_warning is None and not window.power_label.toolTip()
        window.snapshot['power'] = 1
        window.client.connected = False
        window.refresh_battery_status()
        assert window.battery_warning is None and not window.power_label.toolTip()
    finally:
        window.remote = False
        window.client = None


def test_no_notify_route_does_not_consume_later_live_notice(battery_workspace):
    window, _, _, notices = battery_workspace
    state = device(801)
    state['power'] = 1
    select_battery_device(battery_workspace, state)
    event = event_for(state)
    assert window.handle_battery_event(event, notify=False)
    assert window.battery_warning == event
    assert notices == [] and not window.tray.messages
    assert window.handle_battery_event(event)
    assert len(notices) == len(window.tray.messages) == 1


def test_standalone_monitor_continues_while_mapping_is_paused(battery_workspace, monkeypatch):
    window, _, fake_device, notices = battery_workspace
    state = device(801)
    state['power'] = 1
    select_battery_device(battery_workspace, state)
    now = [0.]
    monkeypatch.setattr('gamepadstudio.studio.time.monotonic', lambda: now[0])
    window.config['mapping_enabled'] = False
    window.enabled = False
    for elapsed in (0., 2.99):
        now[0] = elapsed
        window.poll()
    assert not window.tray.messages
    now[0] = 3.
    window.poll()
    assert len(window.tray.messages) == 1
    assert window.battery_warning['level'] == 1
    assert '充电' in window.power_label.toolTip()
    now[0] = 4.
    window.poll()
    assert len(window.tray.messages) == 1
    assert window.enabled is False
    assert not window.actions.calls


def test_power_reporting_changes_do_not_reset_mapping_or_change_input_signature(
        battery_workspace, monkeypatch):
    window, _, _, _ = battery_workspace
    state = device(801)
    state['power'] = 2
    select_battery_device(battery_workspace, state)
    window.enabled = True
    window.poll()
    signature = window._device_ui_signature(state)
    resets = []
    monkeypatch.setattr(window.engine, 'reset', lambda *args, **kwargs: resets.append(True))
    for power in (-1, 2, 4, 0, 3, True):
        state['power'] = power
        window.poll()
        assert window._device_ui_signature(state) == signature
        assert window.battery_settings_row.isHidden() == (type(power) is not int or power not in (0, 1, 2, 3))
    assert resets == []


def test_tray_is_visible_and_retains_explicit_open_and_exit_controls(battery_workspace, monkeypatch):
    window, app, _, _ = battery_workspace
    assert window.tray.isVisible() and window.tray.show_count == 1
    assert window.tray.contextMenu() is window.tray_menu
    assert window.tray_open_action.text() == '打开工作台'
    assert window.tray_exit_action.text() == '退出'
    assert window.config['close_to_tray'] is False
    window.hide()
    window.tray.activated.emit(FakeTray.Trigger)
    app.processEvents()
    assert window.isVisible()
    window.hide()
    window.tray.activated.emit(FakeTray.Context)
    assert not window.isVisible()
    window.tray_open_action.trigger()
    assert window.isVisible()
    window.hide()
    window.tray.messageClicked.emit()
    assert window.isVisible()
    quits = []
    monkeypatch.setattr(QApplication, 'quit', staticmethod(lambda: quits.append(True)))
    window.tray_exit_action.trigger()
    assert window.quitting and window.closed and quits == [True]
    assert not window.tray.isVisible()


def test_default_window_close_still_exits_instead_of_hiding_in_tray(battery_workspace):
    window, _, _, _ = battery_workspace
    assert window.tray.isVisible() and window.config['close_to_tray'] is False
    window.show()
    window.close()
    assert window.closed and not window.tray.isVisible()


def test_live_ipc_battery_event_uses_notice_handler_and_duplicate_guard(battery_workspace):
    window, _, _, notices = battery_workspace
    state = device(801)
    state['power'] = 1
    select_battery_device(battery_workspace, state)
    event = event_for(state)
    window.agent_event(event)
    window.agent_event(event)
    assert len(notices) == len(window.tray.messages) == 1
    foreign = event_for(device(802), level=1, identifier='ui-session:2')
    window.agent_event(foreign)
    assert len(notices) == len(window.tray.messages) == 1


@pytest.mark.parametrize('battery_workspace', ['zh', 'en'], indirect=True)
def test_setting_fits_both_languages_and_restored_window_sizes(battery_workspace):
    window, app, _, _ = battery_workspace
    state = device(801)
    state['power'] = 2
    select_battery_device(battery_workspace, state)
    window.setStyleSheet(STYLE)
    window.navigate(4)
    window.show()
    row = window.battery_settings_row
    for width in (960, 1440):
        window.resize(width, 900)
        QTest.qWait(20)
        app.processEvents()
        assert row.title_label.text() in ('低电量提醒', 'Low battery alerts')
        assert row.title_label.text() and row.subtitle_label.text()
        assert row.control is window.battery_notifications_box
        assert row.control.isVisible() and row.control.isEnabled()
        control_position = row.control.mapTo(row, QPoint(0, 0))
        title_position = row.title_label.mapTo(row, QPoint(0, 0))
        subtitle_position = row.subtitle_label.mapTo(row, QPoint(0, 0))
        assert control_position.x() + row.control.width() <= row.width()
        assert title_position.x() + row.title_label.width() <= control_position.x()
        assert subtitle_position.x() + row.subtitle_label.width() <= control_position.x()
        assert subtitle_position.y() + row.subtitle_label.height() <= row.height()
        page = window.stack.currentWidget()
        assert isinstance(page, QScrollArea)
        assert page.horizontalScrollBar().maximum() == 0
