"""Mac isolation/record controls use fake HID, fake recorders and fake IPC only."""
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from gamepadstudio import agent as agent_module, studio, virtual_kbm_ui
from gamepadstudio.agent import Agent
from gamepadstudio.controller_isolation_service import (
    controller_neutral, isolation_requested)
from gamepadstudio.mapping_engine import MappingRuntime
from gamepadstudio.studio_core import ConfigStore, device_config, profile_scope
from tests.test_macos_controller_isolation import fake_device


def state_for(instance=7, key='fixture:first'):
    return dict(instance_id=instance, device_key=key, profile_key='fixture:model',
                family='dualsense', name='Fake DualSense', buttons=[], axes=[0.] * 6,
                available_buttons=list(range(21)), vendor=0x054c, product=0x0ce6,
                touch=[], touchpad=False, led=False, rumble=False, power=4)


class FakeActions:
    def __init__(self, order):
        self.order = order
        self.held = set()
        self.shortcuts = []

    def hold(self, value, down):
        self.held.add(value) if down else self.held.discard(value)

    def release_all(self):
        self.order.append('release')
        self.held.clear()

    def shortcut(self, value):
        self.shortcuts.append(value)


class FakeSignal:
    def __init__(self):
        self.emissions, self.callbacks = [], []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        self.emissions.append(args)
        for callback in self.callbacks:
            callback(*args)


class FakeServer:
    def __init__(self, root, handler):
        self.messages, self.closed = [], False

    def broadcast(self, value):
        self.messages.append(value)

    def close(self):
        self.closed = True


class FakeHotkey:
    def __init__(self, *args):
        self.closed = False

    def configure(self, settings):
        pass

    def status(self):
        return {'enabled': False, 'registered': False, 'error': ''}

    def close(self):
        self.closed = True


class FakeReplay:
    def __init__(self, **kwargs):
        self.running = False
        self.stops = 0

    def start(self):
        self.running = True

    def stop(self):
        self.running = False
        self.stops += 1

    def get_status(self):
        return {'running': self.running}


class FakeHaptics:
    def __init__(self, *args, **kwargs):
        self.closed = False

    def set_config(self, **kwargs):
        pass

    def close(self):
        self.closed = True


class FakeRecording:
    instances = []
    start_ok = True

    def __init__(self, save_dir, **options):
        self.options = options
        self.save_dir = Path(save_dir)
        self.phase = 'idle'
        self.requests = self.finalizations = 0
        self.stop_check = None
        self.instances.append(self)

    def status(self):
        return {'running': self.phase in ('starting', 'recording', 'stopping'),
                'phase': self.phase, 'last_error': '' if self.start_ok else 'simulated capture refused'}

    def start(self):
        self.phase = 'recording' if self.start_ok else 'failed'
        return self.start_ok

    def request_stop(self):
        self.requests += 1
        self.phase = 'stopping'
        return True

    def stop(self):
        if self.stop_check:
            self.stop_check()
        self.finalizations += 1
        self.phase = 'idle'


class FakeControl:
    def __init__(self):
        self.checked = False
        self.enabled = True
        self.text = self.tooltip = ''

    def blockSignals(self, value):
        pass

    def setChecked(self, value):
        self.checked = value

    def setEnabled(self, value):
        self.enabled = value

    def setText(self, value):
        self.text = value

    def setToolTip(self, value):
        self.tooltip = value

    def set_symbol(self, value):
        self.symbol = value


@pytest.fixture
def mac_environment(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    for module in (agent_module, studio, virtual_kbm_ui):
        monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr('gamepadstudio.device.sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(studio, 'WINDOWS_FEATURES', False)
    monkeypatch.setattr(virtual_kbm_ui, 'WINDOWS_FEATURES', False)
    monkeypatch.setattr(agent_module, 'LocalServer', FakeServer)
    monkeypatch.setattr(agent_module, 'EmergencyHotkey', FakeHotkey)
    monkeypatch.setattr(agent_module, 'ScreenshotHotkey', FakeHotkey)
    monkeypatch.setattr(agent_module, 'ReplayBufferEngine', FakeReplay)
    monkeypatch.setattr(agent_module, 'HapticEngine', FakeHaptics)
    monkeypatch.setattr(agent_module, 'MappingRuntime',
                        lambda actions, dispatch: MappingRuntime(actions, dispatch, start_mouse=False))
    monkeypatch.setattr(agent_module, 'application_profiles_supported', lambda: False)
    monkeypatch.setattr(agent_module, 'emergency_hotkey_supported', lambda: True)
    monkeypatch.setattr(agent_module, 'input_permission_status', lambda: {'supported': True, 'granted': False})
    monkeypatch.setattr(agent_module, 'create_actions', lambda: pytest.fail('Unexpected native output'))
    monkeypatch.setattr('gamepadstudio.hidhide.HidHideSession',
                        lambda *a, **k: pytest.fail('Unexpected Windows driver call'))
    FakeRecording.instances = []
    monkeypatch.setattr(FakeRecording, 'start_ok', True)
    monkeypatch.setattr(agent_module, 'ManualRecording', FakeRecording)
    monkeypatch.setattr(studio, 'ManualRecording', FakeRecording)
    actors = []

    def make_agent(configure=None):
        root = tmp_path / str(len(actors))
        root.mkdir()
        physical = fake_device()
        physical._test_state = state_for()
        physical.read = lambda: physical._test_state
        physical.scan = lambda: None
        physical.available = [physical._test_state]
        order = []
        output = FakeActions(order)
        store = ConfigStore(root)
        if configure:
            configure(store, physical._test_state)
        store.save()
        actor = Agent(root, physical, output)
        for timer in (actor.timer, actor.scan_timer, actor.broadcast_timer):
            timer.stop()
        actors.append(actor)
        actor.poll()
        return SimpleNamespace(actor=actor, physical=physical, output=output, order=order, root=root)

    yield SimpleNamespace(make_agent=make_agent, app=app, root=tmp_path)
    for actor in actors:
        actor.close()
        actor.deleteLater()
        app.sendPostedEvents(actor, QEvent.DeferredDelete)


def owner_for(root, *, physical=None, remote=False):
    store = ConfigStore(root)
    store.save()
    order = []
    actions = FakeActions(order)
    owner = SimpleNamespace(store=store, config=store.data, snapshot=state_for(),
        device=physical or fake_device(), remote=remote, notices=[], manual_recording=None,
        actions=actions, recording_finished=FakeSignal(), order=order)
    owner.engine = SimpleNamespace(reset=lambda: order.append('engine-reset'),
                                   close=lambda: order.append('engine-close'))
    owner.notify = owner.notices.append
    owner.gamebar_shield_box = FakeControl()
    owner.refresh_controller_isolation = lambda: studio.Studio.refresh_controller_isolation(owner)
    owner.set_controller_isolation = lambda enabled: studio.Studio.set_controller_isolation(owner, enabled)
    owner.client = SimpleNamespace(connected=True, sent=[],
        send=lambda command, **kwargs: owner.client.sent.append((command, kwargs)))
    owner.closed = False
    owner._cancel_autostart_handoff = lambda: None
    for name in ('timer', 'scan_timer', 'gallery_timer', 'notice_timer'):
        setattr(owner, name, SimpleNamespace(stop=lambda: None))
    owner.events = SimpleNamespace(close=lambda: None)
    owner.emergency_hotkey = None
    owner.screenshot_hotkey = None
    owner.tray = SimpleNamespace(hide=lambda: order.append('tray-hide'))
    owner.worker = None
    return owner


def enable_setting(store, state):
    store.set_setting('device_cloaking_enabled', True, state)
    store.set_setting('mac_controller_isolation_requested', True, state)


def prepare_studio_poll(owner):
    """Run actual Studio polling with controls that never create native windows."""
    owner.device._test_state = owner.snapshot
    owner.device.read = lambda: owner.device._test_state
    owner.device.available = [owner.snapshot]
    owner.device_sample = FakeSignal()
    owner.device_identity = None
    owner.device_capabilities_signature = None
    owner._device_ui_signature = lambda state: 'fixture' if state else None
    def update_controller_ui(state):
        owner.device_capabilities_signature = owner._device_ui_signature(state)
    owner.update_controller_ui = update_controller_ui
    owner.update_application_profile = lambda: None
    owner.refresh_battery_status = lambda: None
    owner.refresh_emergency_hotkey_status = lambda: None
    owner.refresh_screenshot_hotkey_status = lambda: None
    owner.refresh_input_permission_status = lambda: None
    owner.refresh_application_profile_status = lambda: None
    owner.controllers = SimpleNamespace(set_devices=lambda *args: None)
    owner.previous_connected = True
    owner.enabled = owner.learn = False
    owner.last_buttons = set()
    owner.button_names = {}
    owner.last_touch = None
    owner.stack = SimpleNamespace(currentIndex=lambda: 0)
    owner.tester = SimpleNamespace(update_state=lambda *args, **kwargs: None)
    owner.mapping_art = SimpleNamespace(update_state=lambda state: None)
    owner.virtual_kbm_page = SimpleNamespace(
        is_capturing=False, set_device_state=lambda state: None,
        update_feedback=lambda *args: None)
    owner.mapping_deck = SimpleNamespace(feedback=lambda value: None)
    owner.notice_timer.isActive = lambda: False
    owner.engine.feedback = lambda: {}
    owner.engine.update = lambda *args, **kwargs: None
    owner.testing_protected = owner.preview_requested = lambda: False
    for name in ('home_connection_hint', 'status_badge', 'side_status', 'rumble_button',
                 'notice', 'mapping_feedback', 'home_input_feedback', 'device_details',
                 'power_label', 'pause_button'):
        setattr(owner, name, FakeControl())


def test_mac_windows_default_cloaking_flag_does_not_implicitly_seize(mac_environment):
    env = mac_environment.make_agent()
    assert device_config(env.actor.config, env.actor.state)['device_cloaking_enabled']
    assert not isolation_requested(env.actor.config, env.actor.state)
    assert env.physical.native.calls == []
    for _ in range(3):
        env.actor.poll()
    assert env.physical.native.calls == []


def test_agent_explicit_success_persists_selected_device_and_releases_output_first(mac_environment):
    env = mac_environment.make_agent()
    env.output.held.add('Alt')
    original = env.physical.native.open
    def checked_open(reference, options):
        assert not env.output.held
        return original(reference, options)
    env.physical.native.open = checked_open
    result = env.actor.handle({'command': 'set_device_cloaking', 'enabled': True,
                               'device_scope': profile_scope(env.actor.state)})
    assert result['applied'] and result['isolation']['active']
    saved = ConfigStore(env.root).data
    assert isolation_requested(saved, env.actor.state)
    assert not isolation_requested(saved, state_for(8, 'fixture:other'))
    assert not saved['gamebar_shield_enabled']
    result = env.actor.handle({'command': 'set_device_cloaking', 'enabled': False,
                               'device_scope': profile_scope(env.actor.state)})
    assert result['applied'] and not result['enabled']
    assert not isolation_requested(ConfigStore(env.root).data, env.actor.state)


def test_agent_cross_device_request_is_refused_before_release_or_native_write(mac_environment):
    env = mac_environment.make_agent()
    env.order.clear()
    original = copy.deepcopy(env.actor.config)
    with pytest.raises(ValueError, match='设备已变化'):
        env.actor.handle({'command': 'set_device_cloaking', 'enabled': True, 'device_scope': 'fixture:other'})
    assert env.order == [] and env.physical.native.calls == []
    assert env.actor.config == original


@pytest.mark.parametrize('value', ['false', 0, 1, None])
def test_agent_invalid_isolation_boolean_is_rejected_without_native_write(mac_environment, value):
    env = mac_environment.make_agent()
    with pytest.raises(ValueError, match='开启或关闭'):
        env.actor.handle({'command': 'set_device_cloaking', 'enabled': value})
    assert env.physical.native.calls == []


def test_native_refusal_restores_shared_and_does_not_save_requested(mac_environment, monkeypatch):
    env = mac_environment.make_agent()
    original = copy.deepcopy(env.actor.config)
    saved = []
    monkeypatch.setattr(env.actor.store, 'save', lambda: saved.append(True))
    env.physical.native.open_results.extend([0xE00002C5, 0])
    result = env.actor.handle({'command': 'set_device_cloaking', 'enabled': True})
    assert not result['applied'] and not result['isolation']['restore_pending']
    assert env.actor.config == original and saved == []
    assert env.physical.native.calls[-3:] == [('close', 0x100000), ('open', 0x100000, 0), ('release', 0x100000)]


def test_restoration_pending_remains_checked_and_disable_failure_does_not_save(mac_environment, monkeypatch):
    owner = owner_for(mac_environment.root / 'studio')
    assert owner.set_controller_isolation(True)['applied']
    original = copy.deepcopy(owner.config)
    saved = []
    monkeypatch.setattr(owner.store, 'save', lambda: saved.append(True))
    owner.device.native.close_results.append(0xE00002BC)
    result = owner.set_controller_isolation(False)
    owner.refresh_controller_isolation()
    assert not result['applied'] and result['isolation']['restore_pending']
    assert owner.gamebar_shield_box.checked and owner.gamebar_shield_box.enabled
    assert owner.config == original and not saved
    assert owner.set_controller_isolation(False)['applied']


@pytest.mark.parametrize('previous_active', [False, True])
def test_save_failure_rolls_native_mode_and_config_back(mac_environment, monkeypatch, previous_active):
    owner = owner_for(mac_environment.root / 'save-failure')
    if previous_active:
        assert owner.set_controller_isolation(True)['applied']
    original = copy.deepcopy(owner.config)
    disk = copy.deepcopy(ConfigStore(owner.store.root).data)
    def failed_save():
        raise OSError('simulated disk full')
    monkeypatch.setattr(owner.store, 'save', failed_save)
    result = owner.set_controller_isolation(not previous_active)
    assert not result['applied'] and 'disk full' in result['message']
    assert owner.device.controller_isolation_status()['active'] is previous_active
    assert owner.config == original and ConfigStore(owner.store.root).data == disk
    owner.device.close()


def test_studio_isolation_reset_failure_releases_other_held_output_without_native_change(mac_environment):
    owner = owner_for(mac_environment.root / 'reset-failure')
    original = copy.deepcopy(owner.config)
    owner.actions.held.add('Ctrl')
    def failed_reset():
        raise OSError('simulated mapping reset failed')
    owner.engine.reset = failed_reset
    result = owner.set_controller_isolation(True)
    assert not result['applied'] and 'reset failed' in result['message']
    assert not owner.actions.held
    assert owner.device.native.calls == []
    assert owner.config == original
    owner.device.close()


@pytest.mark.parametrize('held', ['button', 'stick', 'trigger'])
def test_saved_requested_isolation_waits_for_neutral_and_applies_once(mac_environment, held):
    def configure(store, state):
        enable_setting(store, state)
        if held == 'button':
            state['buttons'] = [5]
        else:
            state['axes'][0 if held == 'stick' else 4] = .8
    env = mac_environment.make_agent(configure)
    assert env.actor._isolation_apply_pending and env.physical.native.calls == []
    env.actor.poll()
    assert env.physical.native.calls == []
    env.physical._test_state['buttons'] = []
    env.physical._test_state['axes'] = [0.] * 6
    env.actor.poll()
    assert env.physical.controller_isolation_status()['active']
    assert not env.actor._isolation_apply_pending
    env.actor.poll()
    assert env.physical.native.calls.count(('open', 0x100000, 1)) == 1


@pytest.mark.parametrize('held', ['button', 'stick', 'trigger'])
def test_studio_saved_isolation_waits_for_neutral_before_native_open(mac_environment, held):
    owner = owner_for(mac_environment.root / 'studio-restore')
    enable_setting(owner.store, owner.snapshot)
    owner.store.save()
    if held == 'button':
        owner.snapshot['buttons'] = [5]
    else:
        owner.snapshot['axes'][0 if held == 'stick' else 4] = .8
    prepare_studio_poll(owner)
    studio.Studio.poll(owner)
    assert owner._isolation_apply_pending
    assert owner.device.native.calls == []
    studio.Studio.poll(owner)
    assert owner.device.native.calls == []
    owner.snapshot['buttons'] = []
    owner.snapshot['axes'] = [0.] * 6
    studio.Studio.poll(owner)
    assert not owner._isolation_apply_pending
    assert owner.device.controller_isolation_status()['active']
    studio.Studio.poll(owner)
    assert owner.device.native.calls.count(('open', 0x100000, 1)) == 1
    assert not any('映射已暂停：' in notice for notice in owner.notices)
    owner.device.close()


def test_switch_to_another_controller_does_not_inherit_previous_mac_request(mac_environment):
    env = mac_environment.make_agent(enable_setting)
    assert env.physical.controller_isolation_status()['active']
    env.physical.close_handle()
    env.physical.handle, env.physical.instance_id = 12, 8
    env.physical.lib.handles[12] = 8
    env.physical._test_state = state_for(8, 'fixture:second')
    env.actor.poll()
    assert not env.physical.controller_isolation_status()['active']
    assert ('open', 0x200000, 1) not in env.physical.native.calls
    assert not isolation_requested(env.actor.config, env.actor.state)


@pytest.mark.parametrize('state', [None, state_for(), {**state_for(), 'buttons': [5]},
    {**state_for(), 'axes': [.8, 0, 0, 0, 0, 0]}, {**state_for(), 'axes': [0, 0, 0, 0, .8, 0]},
    {**state_for(), 'axes': [float('nan'), 0, 0, 0, 0, 0]},
    {**state_for(), 'axes': [0, 0, 0, 0, float('nan'), 0]}])
def test_neutral_guard_is_conservative_about_active_or_invalid_input(state):
    assert controller_neutral(state) is (state is not None and state == state_for())


def test_virtual_kbm_control_and_main_panel_follow_backend_result(mac_environment):
    owner = owner_for(mac_environment.root / 'keyboard')
    page = SimpleNamespace(owner=owner, cloaking_toggle=FakeControl(), cloaking_status_label=FakeControl())
    page.refresh_cloaking_status = lambda: virtual_kbm_ui.VirtualKbmPage.refresh_cloaking_status(page)
    virtual_kbm_ui.VirtualKbmPage.toggle_cloaking(page, True)
    assert page.cloaking_toggle.checked and owner.gamebar_shield_box.checked
    assert isolation_requested(owner.config, owner.snapshot)
    owner.device.native.close_results.append(0xE00002BC)
    virtual_kbm_ui.VirtualKbmPage.toggle_cloaking(page, False)
    assert page.cloaking_toggle.checked and owner.gamebar_shield_box.checked
    assert '失败' in page.cloaking_status_label.text
    virtual_kbm_ui.VirtualKbmPage.toggle_cloaking(page, False)
    assert not page.cloaking_toggle.checked and not owner.gamebar_shield_box.checked
    assert not isolation_requested(owner.config, owner.snapshot)
    owner.device.close()


def test_studio_remote_isolation_passes_scope_and_waits_for_confirmed_persistence(mac_environment, monkeypatch):
    owner = owner_for(mac_environment.root / 'remote', remote=True)
    calls = []
    def request(root, command, **kwargs):
        calls.append((command, kwargs))
        store = ConfigStore(root)
        enable_setting(store, owner.snapshot)
        store.save()
        return {'ok': True, 'applied': True, 'enabled': True, 'message': 'confirmed'}
    monkeypatch.setattr(studio, 'request', request)
    assert owner.set_controller_isolation(True)['applied']
    assert calls == [('set_device_cloaking', {'enabled': True, 'device_scope': profile_scope(owner.snapshot), 'timeout': 4000})]
    assert isolation_requested(owner.config, owner.snapshot)
    assert owner.device.native.calls == []


@pytest.mark.parametrize('reply', [None, {'ok': False, 'error': 'wrong device'}, {'ok': True}])
def test_studio_remote_unconfirmed_change_does_not_save_or_touch_local_backend(mac_environment, monkeypatch, reply):
    owner = owner_for(mac_environment.root / 'remote-failure', remote=True)
    original = copy.deepcopy(owner.config)
    monkeypatch.setattr(studio, 'request', lambda *args, **kwargs: reply)
    assert not owner.set_controller_isolation(True)['applied']
    assert owner.config == original and owner.device.native.calls == []


def test_agent_mac_record_toggle_starts_once_and_waits_for_finalization(mac_environment):
    env = mac_environment.make_agent()
    env.actor.config.update(replay_capture_mode='monitor_2', replay_codec='h264', replay_fps=45,
                            replay_bitrate_mbps=60, gamebar_shield_enabled=True)
    env.actor.dispatch({'action': 'record_toggle'}, False)
    assert FakeRecording.instances == []
    env.actor.dispatch({'action': 'record_toggle'}, True)
    recorder = FakeRecording.instances[0]
    assert recorder.options['capture_mode'] == 'monitor_2'
    assert (recorder.options['codec'], recorder.options['fps'], recorder.options['bitrate_mbps']) == ('h264', 45, 60)
    env.actor.dispatch({'action': 'record_toggle'}, True)
    env.actor.record_toggle()
    assert len(FakeRecording.instances) == 1 and recorder.phase == 'stopping'
    assert recorder.finalizations == 0 and not env.output.shortcuts
    env.output.held.add('Ctrl')
    recorder.stop_check = lambda: pytest.fail('Output still held at finalization') if env.output.held else None
    env.actor.close()
    assert recorder.finalizations == 1 and recorder.phase == 'idle'
    assert env.physical.handle is None


def test_studio_local_record_controls_use_current_capture_settings_and_full_stop_on_cleanup(mac_environment):
    owner = owner_for(mac_environment.root / 'local-record')
    owner.config.update(replay_capture_mode='all', replay_codec='hevc', replay_fps=60)
    studio.Studio.record_toggle(owner)
    recorder = FakeRecording.instances[0]
    assert recorder.options['capture_mode'] == 'all' and recorder.options['fps'] == 60
    studio.Studio.record_toggle(owner)
    assert recorder.requests == 1 and recorder.phase == 'stopping'
    owner.actions.held.add('Alt')
    recorder.stop_check = lambda: pytest.fail('Output still held at finalization') if owner.actions.held else None
    studio.Studio.cleanup(owner)
    assert recorder.finalizations == 1 and recorder.phase == 'idle'
    assert not owner.actions.shortcuts and owner.device.handle is None


def test_studio_remote_recording_only_sends_backend_command(mac_environment):
    owner = owner_for(mac_environment.root / 'remote-record', remote=True)
    studio.Studio.record_toggle(owner)
    assert owner.client.sent == [('record_toggle', {})]
    assert FakeRecording.instances == []


@pytest.mark.parametrize('failure_at', ['input-release', 'recorder-stop'])
def test_studio_cleanup_always_finalizes_recorder_and_restores_isolation_despite_teardown_failure(mac_environment, failure_at):
    owner = owner_for(mac_environment.root / 'cleanup-failure')
    assert owner.set_controller_isolation(True)['applied']
    studio.Studio.record_toggle(owner)
    recorder = FakeRecording.instances[0]
    owner.actions.held.add('Ctrl')
    original_reset, original_stop = owner.engine.reset, recorder.stop
    def failed_release():
        raise OSError('simulated key-up failure')
    def failed_stop():
        recorder.finalizations += 1
        raise OSError('simulated recorder failure')
    if failure_at == 'input-release':
        owner.engine.reset = failed_release
    else:
        recorder.stop = failed_stop
    try:
        result = studio.Studio.cleanup(owner)
    except OSError:
        pass  # A reported error must never skip independent resource cleanup.
    else:
        assert result is False and not owner.closed
    assert recorder.finalizations == 1
    assert not owner.actions.held
    assert ('open', 0x100000, 0) in owner.device.native.calls
    assert owner.device.handle is None
    assert owner.cleanup_errors and 'tray-hide' not in owner.order
    owner.engine.reset, recorder.stop = original_reset, original_stop
    assert studio.Studio.cleanup(owner) is True
    assert owner.closed and 'tray-hide' in owner.order


def test_studio_cleanup_success_is_idempotent(mac_environment):
    owner = owner_for(mac_environment.root / 'cleanup-success')
    assert owner.set_controller_isolation(True)['applied']
    studio.Studio.record_toggle(owner)
    recorder = FakeRecording.instances[0]
    assert studio.Studio.cleanup(owner) is True
    native_calls = list(owner.device.native.calls)
    assert studio.Studio.cleanup(owner) is True
    assert recorder.finalizations == 1
    assert owner.device.native.calls == native_calls
    assert owner.closed


def test_agent_cleanup_persists_recorder_timeout_and_releases_device(mac_environment):
    env = mac_environment.make_agent(enable_setting)
    env.actor.record_toggle()
    recorder = FakeRecording.instances[0]
    def failed_stop():
        recorder.finalizations += 1
        raise TimeoutError('录像结束超时，无法确认文件完整性')
    recorder.stop = failed_stop
    env.actor.close()
    assert recorder.finalizations == 1 and env.physical.handle is None
    assert not env.physical.controller_isolation_status()['active']
    assert env.actor.server.closed
    assert env.actor.cleanup_errors == ['结束录像失败：录像结束超时，无法确认文件完整性']
    events = (env.root / 'agent-events.jsonl').read_text(encoding='utf-8').splitlines()
    assert any('结束录像失败：录像结束超时' in row for row in events)


@pytest.mark.parametrize('failure_at', ['input-release', 'recorder-stop'])
def test_agent_cleanup_still_finalizes_when_error_logging_fails(mac_environment, failure_at):
    env = mac_environment.make_agent(enable_setting)
    env.actor.record_toggle()
    recorder = FakeRecording.instances[0]
    env.output.held.add('Ctrl')
    def failed_reset():
        raise OSError('simulated reset failure')
    def failed_stop():
        recorder.finalizations += 1
        raise OSError('simulated recorder failure')
    def failed_log(message):
        raise OSError('simulated disk full while reporting cleanup')
    if failure_at == 'input-release':
        env.actor.engine.reset = failed_reset
    else:
        recorder.stop = failed_stop
    env.actor.log = failed_log
    env.actor.close()
    assert not env.output.held
    assert recorder.finalizations == 1
    assert env.physical.handle is None
    assert ('open', 0x100000, 0) in env.physical.native.calls
    assert env.actor.server.closed


def test_start_recording_failure_is_reported_without_windows_shortcut(mac_environment, monkeypatch):
    owner = owner_for(mac_environment.root / 'failed-record')
    monkeypatch.setattr(FakeRecording, 'start_ok', False)
    studio.Studio.record_toggle(owner)
    assert 'simulated capture refused' in owner.notices
    assert not owner.actions.shortcuts


@pytest.mark.parametrize('command', ['stop', 'exit', 'quit'])
def test_agent_stop_commands_reach_full_recording_finalization_through_app_quit(mac_environment, monkeypatch, command):
    env = mac_environment.make_agent()
    env.actor.record_toggle()
    recorder = FakeRecording.instances[0]
    loop = SimpleNamespace(aboutToQuit=FakeSignal())
    loop.quit = lambda: loop.aboutToQuit.emit()
    loop.exec = lambda: (env.actor.handle({'command': command}), 0)[1]
    monkeypatch.setattr(agent_module, 'QCoreApplication', SimpleNamespace(instance=lambda: loop, quit=loop.quit))
    monkeypatch.setattr(agent_module, 'QTimer', SimpleNamespace(singleShot=lambda delay, callback: callback()))
    monkeypatch.setattr(agent_module, 'request', lambda *args, **kwargs: None)
    monkeypatch.setattr(agent_module, 'QLockFile', lambda path: SimpleNamespace(
        setStaleLockTime=lambda value: None, tryLock=lambda timeout: True, unlock=lambda: None))
    monkeypatch.setattr(agent_module, '_install_shutdown_signals', lambda app: {})
    monkeypatch.setattr(agent_module, 'Agent', lambda root: env.actor)
    monkeypatch.setattr('gamepadstudio.actions.attach_to_default_desktop', lambda: True)
    assert agent_module.run(env.root) == 0
    assert recorder.finalizations == 1 and not recorder.status()['running']
    assert env.actor.closed and env.physical.handle is None and env.actor.server.closed


def test_windows_agent_cloaking_is_manual_only_and_mac_opt_in_is_preserved(mac_environment, monkeypatch):
    """Windows：设备隐身改成手动，后台不得再自己读写驱动；macOS 的隔离开关不受影响。"""
    env = mac_environment.make_agent()
    monkeypatch.setattr(agent_module, 'sys', SimpleNamespace(platform='win32'))
    writes = []
    monkeypatch.setattr('gamepadstudio.hidhide_control.HidHideController.set_paths_hidden',
                        lambda self, paths, hidden: writes.append((tuple(paths), hidden)))
    monkeypatch.setattr('gamepadstudio.hidhide.HidHideSession',
                        lambda *a, **k: pytest.fail('Windows agent must not open the driver on its own'))
    env.actor.state['device_path'] = 'fake-windows-device-path'
    env.actor.apply_device_cloaking()
    env.actor.store.set_setting('device_cloaking_enabled', False, env.actor.state)
    env.actor.apply_device_cloaking()
    assert writes == []
    assert env.physical.native.calls == []
    assert not isolation_requested(env.actor.config, env.actor.state)
