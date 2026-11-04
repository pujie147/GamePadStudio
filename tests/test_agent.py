import os
import sys
import pytest
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtWidgets import QApplication
from gamepadstudio.agent import Agent


class DeviceStub:
    def __init__(self):self.state=dict(buttons=[],touch=[],led=False)
    def scan(self):pass
    def read(self):return self.state
    def close(self):pass


class ActionsStub:
    def __init__(self):
        self.held=set()
        self.shortcuts=[]
    def hold(self,value,down):
        if down:self.held.add(value)
        else:self.held.discard(value)
    def shortcut(self,value):
        self.shortcuts.append(value)
    def release_all(self):self.held.clear()


def test_pause_survives_restart_until_explicit_resume(tmp_path):
    from gamepadstudio.studio_core import ConfigStore
    app=QApplication.instance() or QApplication([])
    agent=Agent(tmp_path,DeviceStub(),ActionsStub())
    agent.handle({'command':'pause'})
    assert ConfigStore(tmp_path).data['mapping_enabled'] is False
    agent.close()
    restarted=Agent(tmp_path,DeviceStub(),ActionsStub())
    try:
        assert not restarted.enabled
        restarted.handle({'command':'resume'})
        assert restarted.enabled and ConfigStore(tmp_path).data['mapping_enabled'] is True
    finally:restarted.close()


def test_runtime_release_failure_still_attempts_native_release_and_stays_paused(tmp_path,monkeypatch):
    from gamepadstudio.studio_core import ConfigStore
    app=QApplication.instance() or QApplication([])
    output=ActionsStub();agent=Agent(tmp_path,DeviceStub(),output)
    original=agent.engine.reset
    def refused(*a,**kw):raise OSError('simulated key-up failure')
    monkeypatch.setattr(agent.engine,'reset',refused)
    output.held.add('Alt')
    try:
        with pytest.raises(OSError):agent.handle({'command':'pause'})
        assert not output.held and not agent.enabled
        assert ConfigStore(tmp_path).data['mapping_enabled'] is False
        with pytest.raises(OSError):agent.handle({'command':'resume'})
        assert not agent.enabled
    finally:
        monkeypatch.setattr(agent.engine,'reset',original);agent.close()


def test_close_releases_input_before_waiting_for_recorder(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    output=ActionsStub();agent=Agent(tmp_path,DeviceStub(),output)
    output.held.add('Alt')
    def recorder_stop():assert not output.held
    monkeypatch.setattr(agent.replay_engine,'stop',recorder_stop)
    agent.close()


def test_xbox_system_buttons_work_in_kbm_without_system_overlay(tmp_path, monkeypatch):
    from unittest.mock import Mock
    app = QApplication.instance() or QApplication([])
    device = DeviceStub()
    device.state.update(instance_id=1, family='xbox', profile_key='xb', available_buttons=list(range(16)))
    agent = Agent(tmp_path, device, ActionsStub())
    captures, launches = [], []
    agent.capture = lambda: captures.append(True)
    monkeypatch.setattr('gamepadstudio.agent.spawn', lambda *args: launches.append(args))
    try:
        agent.poll()
        agent.config['gamebar_shield_enabled'] = True
        agent.poll()  # establish the updated effective mapping while neutral
        for key in (15, 5):
            device.state['buttons'] = [key]; agent.poll()
            device.state['buttons'] = []; agent.poll()
        assert captures == [True]
        assert len(launches) == 1
        assert not agent.actions.shortcuts
        # Explicit user bindings (even 'none') take precedence over the defaults.
        agent.store.mappings['15'] = {'short':{'action':'none'}}
        device.state['buttons'] = [15]; agent.poll()
        device.state['buttons'] = []; agent.poll()
        assert captures == [True]
        agent.config['replay_buffer_enabled'] = False
        assert agent.handle({'command':'save_replay'})['status'] == ('disabled' if sys.platform == 'win32' else 'not_ready')
        agent.record_toggle()
        assert not agent.actions.shortcuts
    finally:
        agent.close()



def test_background_releases_keys_and_requires_fresh_press_after_resume(tmp_path):
    app=QApplication.instance() or QApplication([])
    device=DeviceStub(); actions=ActionsStub(); agent=Agent(tmp_path,device,actions)
    agent.store.mappings['0']={'short':{'action':'hold','value':'Enter'}}
    try:
        agent.poll();device.state['buttons']=[0];agent.poll();assert actions.held=={'Enter'}
        agent.handle({'command':'pause'});assert not actions.held
        agent.handle({'command':'resume'});agent.poll();assert not actions.held
        device.state['buttons']=[];agent.poll();device.state['buttons']=[0];agent.poll()
        assert actions.held=={'Enter'}
        device.state=None;agent.poll();assert not actions.held
    finally:agent.close()


def test_editor_lease_expires_without_delayed_capture(tmp_path):
    app=QApplication.instance() or QApplication([])
    device=DeviceStub();agent=Agent(tmp_path,device,ActionsStub());captures=[]
    agent.capture=lambda:captures.append(True)
    try:
        agent.poll();agent.handle({'command':'suspend','seconds':2})
        device.state['buttons']=[4];agent.poll()
        agent.suspended_until=0;agent.poll();device.state['buttons']=[];agent.poll()
        assert not captures
        device.state['buttons']=[4];agent.poll();device.state['buttons']=[];agent.poll()
        assert captures==[True]
    finally:agent.close()


def test_controller_switch_releases_old_keys_and_ignores_new_held_buttons(tmp_path):
    app=QApplication.instance() or QApplication([])
    device=DeviceStub();device.state.update(instance_id=1,family='dualsense',profile_key='ps')
    actions=ActionsStub();agent=Agent(tmp_path,device,actions);captures=[];agent.capture=lambda:captures.append(True)
    try:
        agent.poll();agent.store.mappings['0']={'short':{'action':'hold','value':'Enter'}}
        device.state['buttons']=[0];agent.poll();assert actions.held=={'Enter'}
        # Both old and new states are connected: identity, not bool(state), must detect the switch.
        device.state=dict(instance_id=2,family='xbox',profile_key='xb',buttons=[15],touch=[],led=False,available_buttons=list(range(16)))
        agent.poll();assert not actions.held and not captures
        device.state['buttons']=[];agent.poll();assert not captures
        device.state['buttons']=[15];agent.poll();device.state['buttons']=[];agent.poll();assert captures==[True]
    finally:agent.close()


def test_xbox_without_share_does_not_run_ps_create_or_home_actions(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([]);device=DeviceStub();captures=[];launches=[]
    device.state.update(instance_id=1,family='xbox',profile_key='xbox:one',available_buttons=list(range(15)))
    monkeypatch.setattr('gamepadstudio.agent.spawn',lambda *args:launches.append(args))
    agent=Agent(tmp_path,device,ActionsStub());agent.capture=lambda:captures.append(True)
    try:
        agent.poll()
        for key in range(15):
            device.state['buttons']=[key];agent.poll();device.state['buttons']=[];agent.poll()
        assert not captures and not launches and not agent.actions.held
        device.state={**device.state,'instance_id':2,'profile_key':'xbox:series','available_buttons':list(range(16))}
        agent.poll();device.state['buttons']=[15];agent.poll();device.state['buttons']=[];agent.poll()
        assert captures==[True] and not launches
    finally:agent.close()


def test_create_button_long_press_triggers_replay_record(tmp_path):
    app = QApplication.instance() or QApplication([])
    device = DeviceStub()
    device.state.update(instance_id=1, family='dualsense', profile_key='ps')
    actions = ActionsStub()
    agent = Agent(tmp_path, device, actions)
    captures = []
    agent.capture = lambda: captures.append(True)
    try:
        agent.poll()
        assert agent.store.mappings['4']['short']['action'] == 'capture'
        assert agent.store.mappings['4']['long']['action'] == 'replay_record'

        now = 100.0
        agent.engine.update({**device.state, 'buttons':[4]}, agent.config, now=now)
        assert not captures and not actions.shortcuts
        # Exceed threshold (0.65s) -> fires long press
        agent.engine.update({**device.state, 'buttons':[4]}, agent.config, now=now + 0.70)
        agent.executor.submit(lambda: None).result(timeout=2)
        if sys.platform == 'win32':
            assert 'Win+Alt+G' in actions.held and not actions.shortcuts
        else:
            assert not actions.held and not actions.shortcuts
        assert not captures

        # Release does not fire short
        agent.engine.update({**device.state, 'buttons':[]}, agent.config, now=now + 0.80)
        assert not captures
    finally:
        agent.close()


class ScopedDeviceStub(DeviceStub):
    def __init__(self, state=None):
        self.state = state
        self.led_calls = []
        self.rumble_calls = []

    def led(self, color):
        self.led_calls.append((self.state['device_key'], color))
        return True

    def rumble(self, strength):
        self.rumble_calls.append((self.state['device_key'], strength))
        return True


def scoped_state(key, instance=1, **capabilities):
    return dict(instance_id=instance, device_key=key, profile_key='dualsense:model',
                name='DualSense fixture',
                family='dualsense', buttons=[], touch=[], axes=[0.] * 6,
                available_buttons=list(range(21)), vendor=0x054c, product=0x0ce6,
                led=capabilities.get('led', True), rumble=capabilities.get('rumble', True),
                touchpad=capabilities.get('touchpad', True))


def test_startup_never_touches_hidhide_configuration(tmp_path, monkeypatch):
    """设备隐身改为手动：后台启动与轮询不得读写驱动配置。"""
    app = QApplication.instance() or QApplication([])
    calls = []
    monkeypatch.setattr('gamepadstudio.hidhide_control.HidHideController.dispatch',
                        lambda self, action, payload=None: calls.append((action, payload)))
    agent = Agent(tmp_path, ScopedDeviceStub(), ActionsStub())
    try:
        agent.poll()
        agent.handle({'command': 'reload'})
        agent.poll()
        assert calls == []
        assert not agent.haptic_engine.haptics_enabled
    finally:
        agent.close()


def test_hidhide_command_is_the_only_write_route(tmp_path, monkeypatch):
    """界面经 `hidhide` 命令进入控制层；控制层是唯一写驱动的地方。"""
    from types import SimpleNamespace
    app = QApplication.instance() or QApplication([])
    calls = []
    monkeypatch.setattr('gamepadstudio.hidhide_control.HidHideController.dispatch',
                        lambda self, action, payload=None: {
                            'ok': True, 'routed': action, 'payload': payload,
                            'message': '', 'occupied': False, 'holders': [], 'state': None})
    monkeypatch.setattr('gamepadstudio.hidhide_control.HidHideController.set_enabled',
                        lambda self, enabled: (calls.append(('set_enabled', enabled)) or
                                               {'ok': True, 'message': '', 'occupied': False,
                                                'holders': [], 'state': None}))
    agent = Agent(tmp_path, ScopedDeviceStub(scoped_state('pad:first')), ActionsStub())
    try:
        reply = agent.handle({'command': 'hidhide', 'action': 'set_group_hidden',
                              'payload': {'key': 'HID\\X', 'hidden': True}})
        assert reply['routed'] == 'set_group_hidden' and reply['payload']['key'] == 'HID\\X'
        legacy = agent.handle({'command': 'set_device_cloaking', 'enabled': True})
        assert legacy['applied'] is True and calls == [('set_enabled', True)]
    finally:
        agent.close()


def test_connected_device_switch_and_reload_apply_its_own_hardware_settings(tmp_path, monkeypatch):
    from gamepadstudio.studio_core import ConfigStore
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(Agent, 'apply_device_cloaking', lambda self: None)
    first, second = scoped_state('pad:first'), scoped_state('pad:second', 2)
    store = ConfigStore(tmp_path)
    store.set_setting('led', '#112233', first)
    store.set_setting('led', '#abcdef', second)
    store.set_setting('haptic_intensity', .2, first)
    store.set_setting('haptic_intensity', .8, second)
    store.set_setting('haptic_profile', 'soft', first)
    store.set_setting('capture_haptics_enabled', False, first)
    store.save()
    device = ScopedDeviceStub(first)
    agent = Agent(tmp_path, device, ActionsStub())
    try:
        agent.poll()
        assert device.led_calls[-1] == ('pad:first', '#112233')
        assert not agent.haptic_engine.haptics_enabled
        assert agent.haptic_engine.intensity == .2
        device.state = second
        agent.poll()
        assert device.led_calls[-1] == ('pad:second', '#abcdef')
        assert agent.haptic_engine.haptics_enabled and agent.haptic_engine.intensity == .8
        editor = ConfigStore(tmp_path)
        editor.set_setting('haptic_intensity', .6, second)
        editor.save()
        agent.handle({'command': 'reload'})
        assert agent.haptic_engine.intensity == .6
        device.state = first
        agent.poll()
        assert not agent.haptic_engine.haptics_enabled and agent.haptic_engine.intensity == .2
        device.state = None
        agent.poll()
        assert not agent.haptic_engine.haptics_enabled
        assert agent.config['active_profile'] in agent.store.profiles_for(None)
    finally:
        agent.close()


def test_hardware_commands_respect_reported_capabilities_and_device_scope(tmp_path, monkeypatch):
    from gamepadstudio.studio_core import profile_scope
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(Agent, 'apply_device_cloaking', lambda self: None)
    first = scoped_state('pad:first', led=False, rumble=False)
    device = ScopedDeviceStub(first)
    agent = Agent(tmp_path, device, ActionsStub())
    try:
        agent.poll()
        haptics = []
        monkeypatch.setattr(agent.haptic_engine, 'trigger_feedback', lambda pattern: haptics.append(pattern))
        assert agent.handle({'command': 'led', 'color': '#112233'}) == {'supported': False}
        assert agent.handle({'command': 'rumble', 'strength': .5}) == {'supported': False}
        assert agent.handle({'command': 'test_haptics'})['status'] == 'unsupported'
        assert not device.led_calls and not device.rumble_calls and not haptics
        device.state = scoped_state('pad:second', 2)
        agent.poll()
        before = len(device.led_calls)
        for command, values in [('led', {'color': '#112233'}), ('rumble', {'strength': .5}),
                                ('test_haptics', {}), ('mapping_change', {'change': {}}),
                                ('set_device_cloaking', {'enabled': False})]:
            with pytest.raises(ValueError, match='输入设备已变化'):
                agent.handle({'command': command, 'device_scope': profile_scope(first), **values})
        assert len(device.led_calls) == before and not device.rumble_calls and not haptics
    finally:
        agent.close()


def test_exit_reconciles_only_the_paths_this_program_hidden(tmp_path, monkeypatch):
    """退出时只解除本程序登记的隐藏，其他工具写进 HidHide 的条目必须原样保留。"""
    from types import SimpleNamespace
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr('gamepadstudio.agent.sys', SimpleNamespace(platform='win32'))
    calls = []
    closed = []

    class Device:
        def close(self):
            closed.append(True)

    agent = Agent(tmp_path, Device(), ActionsStub())
    agent.hidhide_controller = lambda: SimpleNamespace(
        reconcile_on_exit=lambda: calls.append('reconcile') or {'ok': True, 'message': ''})
    # close() 内部会走 _restore_device_access，这里只调用一次 close。
    agent.close()
    assert calls == ['reconcile'] and closed == [True]


def test_hide_current_never_guesses_between_two_identical_pads(tmp_path, monkeypatch):
    """歧义设备必须拒绝写入，而不是在两台同型号手柄里猜一台。"""
    from types import SimpleNamespace
    from gamepadstudio import agent as agent_module
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(agent_module, 'sys', SimpleNamespace(platform='win32'))
    writes = []
    monkeypatch.setattr('gamepadstudio.hidhide_control.HidHideController.set_paths_hidden',
                        lambda self, paths, hidden: writes.append((tuple(paths), hidden)))
    monkeypatch.setattr('gamepadstudio.hidhide_devices.group_for_device',
                        lambda vendor, product, device_path='': None)
    state = {**scoped_state('pad:first'), 'device_path': r'\\?\HID#VID_054C&PID_0CE6#FIRST'}
    agent = Agent(tmp_path, ScopedDeviceStub(state), ActionsStub())
    try:
        reply = agent.handle({'command': 'hidhide', 'action': 'hide_current',
                              'payload': {'vendor': state['vendor'], 'product': state['product'],
                                          'device_path': state['device_path']}})
        assert reply['ok'] is False and '唯一匹配' in reply['message']
        assert writes == []
    finally:
        agent.close()


def test_selected_device_preference_uses_physical_key_before_model_key(tmp_path, monkeypatch):
    from gamepadstudio.studio_core import ConfigStore
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(Agent, 'apply_device_cloaking', lambda self: None)
    first, second = scoped_state('pad:first'), scoped_state('pad:second', 2)
    device = ScopedDeviceStub(first)

    def select(instance):
        assert instance == 2
        device.state = second

    device.select = select
    agent = Agent(tmp_path, device, ActionsStub())
    try:
        agent.poll()
        agent.handle({'command': 'select_device', 'instance_id': 2})
        assert agent.config['preferred_controller'] == 'pad:second'
        assert device.preferred_key == 'pad:second'
        assert ConfigStore(tmp_path).data['preferred_controller'] == 'pad:second'
    finally:
        agent.close()


@pytest.mark.parametrize('kind', ['capture', 'replay'])
@pytest.mark.parametrize('switch_device', [False, True])
def test_async_capture_feedback_stays_with_starting_device_and_keeps_global_sound(
        tmp_path, monkeypatch, kind, switch_device):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(Agent, 'apply_device_cloaking', lambda self: None)
    first, second = scoped_state('pad:first'), scoped_state('pad:second', 2)
    device = ScopedDeviceStub(first)
    agent = Agent(tmp_path, device, ActionsStub())
    patterns, sounds = [], []
    try:
        agent.poll()
        monkeypatch.setattr(agent.executor, 'submit', lambda worker: None)
        monkeypatch.setattr(agent.haptic_engine, 'trigger_feedback', patterns.append)
        monkeypatch.setattr(agent.haptic_engine, 'play_shutter_sound', lambda: sounds.append(True))
        if kind == 'capture':
            agent.capture()
            assert agent.busy
        else:
            agent.config['replay_buffer_enabled'] = True
            monkeypatch.setattr(agent.replay_engine, 'is_running', lambda: True)
            assert agent.replay_record()['status'] == 'saving'
        if switch_device:
            device.state = second
            agent.poll()
        if kind == 'capture':
            agent.on_captured('finished.png', '')
            assert not agent.busy
        else:
            agent.on_replay_finished('finished.mp4', '')
            assert not agent.replay_busy
        if switch_device:
            assert patterns == [] and sounds == [True]
        else:
            assert patterns == ['capture' if kind == 'capture' else 'replay_saved'] and sounds == []
    finally:
        agent.close()
