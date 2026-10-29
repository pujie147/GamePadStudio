"""Swaps preserve complete effective actions and reject stale device edits."""
import copy
import json
import math

import pytest

from gamepadstudio.mapping_engine import MappingRuntime, effective_mappings
from gamepadstudio.studio_core import (
    ConfigStore, device_config, mapping_input_signature, profile_scope, swap_binding_entry,
)
from tests.test_application_profile_backend import GAME_A, controller, foreground, settings
from tests.test_battery_backend import battery_backend
from tests.test_profile_transfer_ipc import app, request_over_socket
from tests.test_unified_mapping import Actions, entry


@pytest.fixture
def swap_store(tmp_path):
    state, store = controller(), ConfigStore(tmp_path)
    store.activate_controller(state)
    profile = store.profiles_for(state, 'kbm')[0]
    store.data['profiles'][profile]['0'] = {
        'short': {'action': 'launch', 'executable': r'C:\Games\Nikki\InfinityNikki.exe',
                  'arguments': '--example'},
        'long': {'action': 'gamepad_macro',
                 'sequence': [{'buttons': ['1'], 'hold_ms': 100, 'gap_ms': 0}]},
        'long_press': .93,
    }
    store.data['profiles'][profile]['1'] = {
        'short': {'action': 'hold', 'value': 'Q'},
        'long': {'action': 'gamepad_turbo', 'value': '0', 'rate_hz': 17},
        'long_press': .41,
    }
    store.save()
    return store, state, profile


def swap_change(store, state, profile, first='0', second='1'):
    return {'op': 'swap_bindings', 'profile': profile, 'first': first, 'second': second,
            'expected_inputs': mapping_input_signature(state),
            'expected_first': swap_binding_entry(store.data, state, profile, first),
            'expected_second': swap_binding_entry(store.data, state, profile, second)}


def remote_change(context, profile, first='0', second='1'):
    state = context.device.state
    return {'command': 'mapping_change', 'device_scope': profile_scope(state),
            'instance_id': state['instance_id'],
            'change': swap_change(context.agent.store, state, profile, first, second)}


def test_swap_moves_full_short_long_threshold_and_metadata_only(swap_store):
    store, state, profile = swap_store
    before = copy.deepcopy(store.data)
    payload = swap_change(store, state, profile)
    assert before['active_profile'] != profile
    result = store.apply_mapping_change(payload, state)
    expected = copy.deepcopy(before)
    expected['profiles'][profile]['0'] = payload['expected_second']
    expected['profiles'][profile]['1'] = payload['expected_first']
    expected['mapping_revision'] += 1
    assert store.data == result == expected
    assert json.loads(store.path.read_text(encoding='utf-8')) == expected


def test_canonical_combination_swaps_without_creating_duplicate_alias_keys(swap_store):
    store, state, profile = swap_store
    store.data['profiles'][profile]['0+9'] = entry('shortcut', 'Ctrl+E')
    store.save()
    payload = swap_change(store, state, profile, first='9+0')
    store.apply_mapping_change(payload, state)
    assert store.data['profiles'][profile]['0+9'] == payload['expected_second']
    assert store.data['profiles'][profile]['1'] == payload['expected_first']
    assert '9+0' not in store.data['profiles'][profile]


def test_missing_source_gets_explicit_empty_binding_after_move(swap_store):
    store, state, profile = swap_store
    store.data['profiles'][profile].pop('14', None)
    store.save()
    payload = swap_change(store, state, profile, second='14')
    store.apply_mapping_change(payload, state)
    assert store.data['profiles'][profile]['0'] == {
        'short': {'action': 'none'}, 'long': {'action': 'none'}, 'long_press': .65}
    assert store.data['profiles'][profile]['14'] == payload['expected_first']


def test_implicit_gamebar_default_stays_suppressed_at_old_source_after_swap(swap_store):
    store, state, profile = swap_store
    state['available_buttons'].extend([15, 16])
    store.data['gamebar_shield_enabled'] = True
    store.data['profiles'][profile].pop('5', None)
    store.data['profiles'][profile].pop('16', None)
    store.save()
    payload = swap_change(store, state, profile, first='5', second='16')
    assert payload['expected_first']['short']['action'] == 'home'
    store.apply_mapping_change(payload, state)
    effective = effective_mappings(device_config(store.data, state), state, profile)
    assert effective['5']['short']['action'] == 'none'
    assert effective['16']['short']['action'] == 'home'
    reopened = ConfigStore(store.root)
    assert swap_binding_entry(reopened.data, state, profile, '5')['short']['action'] == 'none'


def test_buttons_only_device_can_swap_without_rs_or_axes(swap_store):
    store, state, profile = swap_store
    state.update(available_axes=[], axes=[])
    payload = swap_change(store, state, profile)
    assert not any(key.startswith('RS:') for key in payload['expected_inputs']['inputs'])
    store.apply_mapping_change(payload, state)
    assert store.data['profiles'][profile]['0'] == payload['expected_second']


def test_stale_store_preserves_latest_unrelated_bindings_hardware_and_application_rules(swap_store):
    stale, state, profile = swap_store
    payload = swap_change(stale, state, profile)
    editor = ConfigStore(stale.root)
    editor.data['profiles'][profile]['2'] = entry('hold', 'T')
    editor.data['profile_options'][profile]['mouse']['sensitivity'] = 52.
    editor.set_setting('battery_notifications_enabled', False, state)
    editor.set_setting('long_press', .88, controller('pad:other', 2))
    editor.apply_mapping_change({'op': 'application_profiles', 'settings': settings(profile)}, state)
    before = copy.deepcopy(editor.data)
    stale.apply_mapping_change(payload, state)
    expected = copy.deepcopy(before)
    expected['profiles'][profile]['0'] = payload['expected_second']
    expected['profiles'][profile]['1'] = payload['expected_first']
    expected['mapping_revision'] += 1
    assert stale.data == expected


@pytest.mark.parametrize('edited', ['short', 'long', 'long_press', 'owner', 'deleted'])
def test_latest_file_changes_to_target_or_ownership_reject_old_preview(swap_store, edited):
    stale, state, profile = swap_store
    payload = swap_change(stale, state, profile)
    editor = ConfigStore(stale.root)
    if edited == 'short':
        editor.data['profiles'][profile]['0']['short']['arguments'] = '--changed'
    elif edited == 'long':
        editor.data['profiles'][profile]['1']['long']['rate_hz'] = 22
    elif edited == 'long_press':
        editor.data['profiles'][profile]['0']['long_press'] = 1.
    elif edited == 'owner':
        editor.data['profile_devices'][profile] = 'pad:other'
    else:
        editor.data['profiles'].pop(profile)
    editor.save()
    before, before_file = copy.deepcopy(stale.data), stale.path.read_bytes()
    with pytest.raises(ValueError):
        stale.apply_mapping_change(payload, state)
    assert stale.data == before and stale.path.read_bytes() == before_file


def test_change_to_inherited_device_long_press_rejects_old_preview(swap_store):
    stale, state, profile = swap_store
    stale.data['profiles'][profile]['0'].pop('long_press')
    stale.save()
    payload = swap_change(stale, state, profile)
    editor = ConfigStore(stale.root)
    editor.set_setting('long_press', .99, state)
    editor.save()
    before = stale.path.read_bytes()
    with pytest.raises(ValueError, match='长按设置已变化'):
        stale.apply_mapping_change(payload, state)
    assert stale.path.read_bytes() == before


def test_explicit_thresholds_are_unaffected_by_changed_device_default(swap_store):
    stale, state, profile = swap_store
    payload = swap_change(stale, state, profile)
    editor = ConfigStore(stale.root)
    editor.set_setting('long_press', .99, state)
    editor.save()
    stale.apply_mapping_change(payload, state)
    assert stale.settings_for(state)['long_press'] == .99
    assert stale.data['profiles'][profile]['0']['long_press'] == .41
    assert stale.data['profiles'][profile]['1']['long_press'] == .93


@pytest.mark.parametrize('field', ['expected_inputs', 'expected_first', 'expected_second'])
def test_missing_or_bad_guard_is_rejected_without_mutation(swap_store, field):
    store, state, profile = swap_store
    payload = swap_change(store, state, profile)
    payload.pop(field)
    before, before_file = copy.deepcopy(store.data), store.path.read_bytes()
    with pytest.raises(ValueError):
        store.apply_mapping_change(payload, state)
    assert store.data == before and store.path.read_bytes() == before_file


@pytest.mark.parametrize('source', ['0', '30', 'TP:tap', '0+0', '0+1+2+3+4'])
def test_duplicate_unavailable_and_invalid_sources_reject(swap_store, source):
    store, state, profile = swap_store
    payload = swap_change(store, state, profile)
    payload['second'] = source
    before = copy.deepcopy(store.data)
    with pytest.raises(ValueError):
        store.apply_mapping_change(payload, state)
    assert store.data == before


@pytest.mark.parametrize('capability', ['family', 'buttons', 'axes', 'raw_model'])
def test_changed_input_signature_rejects_even_if_selected_sources_still_exist(swap_store, capability):
    store, state, profile = swap_store
    payload = swap_change(store, state, profile)
    if capability == 'family':
        state['family'] = 'dualsense'
    elif capability == 'buttons':
        state['available_buttons'].append(16)
    elif capability == 'axes':
        state['available_axes'] = [0, 1]
    else:
        state.update(is_gamecontroller=False, model_key='fixture-model')
    before = copy.deepcopy(store.data)
    with pytest.raises(ValueError, match='能力已变化'):
        store.apply_mapping_change(payload, state)
    assert store.data == before


@pytest.mark.parametrize('bad_state', [None, {}, {'instance_id': True}, {'instance_id': -1},
                                     {'instance_id': 2, 'connected': False}])
def test_generic_signature_requires_actual_connected_device(bad_state):
    with pytest.raises(ValueError, match='先连接'):
        mapping_input_signature(bad_state)


@pytest.mark.parametrize('threshold', [None, True, '0.6', math.nan, math.inf, 0., 3.01])
def test_invalid_inherited_threshold_cannot_be_swallowed_by_snapshot(swap_store, threshold):
    store, state, profile = swap_store
    store.data['profiles'][profile]['0'].pop('long_press')
    store.set_setting('long_press', threshold, state)
    with pytest.raises(ValueError, match='长按时长'):
        swap_binding_entry(store.data, state, profile, '0')


def test_unknown_entry_metadata_is_rejected_instead_of_silently_removed(swap_store):
    store, state, profile = swap_store
    store.data['profiles'][profile]['0']['custom_future_field'] = {'important': True}
    with pytest.raises(ValueError, match='不支持的内容'):
        swap_binding_entry(store.data, state, profile, '0')
    assert store.data['profiles'][profile]['0']['custom_future_field'] == {'important': True}


def test_two_touch_gestures_swap_full_valid_actions_without_dropping_metadata(swap_store):
    store, state, profile = swap_store
    state.update(family='dualsense', touchpad=True, touchpad_fingers=2)
    store.data['profiles'][profile]['TP:tap'] = entry('launch', '', 'suppress')
    store.data['profiles'][profile]['TP:tap']['short']['executable'] = r'C:\Games\Game.exe'
    store.data['profiles'][profile]['TP:tap']['long_press'] = .22
    store.data['profiles'][profile]['TP:swipe_up'] = entry('shortcut', 'Ctrl+R')
    store.save()
    payload = swap_change(store, state, profile, first='TP:tap', second='TP:swipe_up')
    store.apply_mapping_change(payload, state)
    assert store.data['profiles'][profile]['TP:tap'] == payload['expected_second']
    assert store.data['profiles'][profile]['TP:swipe_up'] == payload['expected_first']


def test_touch_with_long_action_is_rejected_without_truncation(swap_store):
    store, state, profile = swap_store
    state.update(family='dualsense', touchpad=True)
    store.data['profiles'][profile]['TP:tap'] = entry('capture', '', 'hold', 'E')
    with pytest.raises(ValueError, match='一次触发'):
        swap_binding_entry(store.data, state, profile, 'TP:tap')
    assert store.data['profiles'][profile]['TP:tap']['long']['value'] == 'E'


@pytest.mark.parametrize('direction,steps', [('up', 1), ('down', -1)])
def test_scroll_default_moves_to_another_gesture_and_old_source_stays_suppressed(swap_store, monkeypatch, direction, steps):
    store, state, profile = swap_store
    state.update(family='dualsense', touchpad=True, touchpad_fingers=2)
    source = 'TP:scroll_' + direction
    store.data['profiles'][profile].pop(source, None)
    store.data['profiles'][profile].pop('TP:tap', None)
    store.set_setting('touch_scroll', True, state)
    store.set_setting('touch_gestures_enabled', True, state)
    store.save()
    payload = swap_change(store, state, profile, first=source, second='TP:tap')
    assert payload['expected_first']['short'] == {'action': 'wheel', 'value': direction}
    store.apply_mapping_change(payload, state)
    assert store.data['profiles'][profile][source]['short']['action'] == 'suppress'
    assert store.data['profiles'][profile]['TP:tap']['short'] == payload['expected_first']['short']
    config = copy.deepcopy(store.data)
    config['active_profile'] = profile
    actions = Actions()
    runtime = MappingRuntime(actions, lambda *args: None, start_mouse=False)
    monkeypatch.setattr(runtime.touch_recognizer, 'update', lambda *args: {
        'contacts': 0, 'mode': 'scroll', 'events': [], 'scroll_steps': steps,
        'pointer_delta': (0., 0.)})
    try:
        runtime.update(state, config, enabled=True, now=1.)
        assert not actions.calls
        runtime.engine.pulse('TP:tap', config['profiles'][profile]['TP:tap']['short'], 2.)
        assert actions.calls == [('wheel', steps)]
    finally:
        runtime.close()


@pytest.mark.parametrize('direction', ['up', 'down'])
def test_direct_scroll_without_gesture_binding_refuses_swap_and_keeps_settings(swap_store, direction):
    store, state, profile = swap_store
    state.update(family='dualsense', touchpad=True, touchpad_fingers=2)
    source = 'TP:scroll_' + direction
    store.set_setting('touch_scroll', True, state)
    store.set_setting('touch_gestures_enabled', False, state)
    store.data['profiles'][profile].pop(source, None)
    store.data['profiles'][profile]['TP:tap'] = entry('shortcut', 'E')
    store.save()
    payload = swap_change(store, state, profile, first=source, second='TP:tap')
    before, before_file = copy.deepcopy(store.data), store.path.read_bytes()
    with pytest.raises(ValueError, match='开启手势绑定'):
        store.apply_mapping_change(payload, state)
    assert store.data == before and store.path.read_bytes() == before_file
    assert store.settings_for(state)['touch_gestures_enabled'] is False
    assert store.settings_for(state)['touch_scroll'] is True


@pytest.mark.parametrize('order', ['touch_first', 'button_first'])
def test_touch_and_continuous_source_never_cross_even_for_single_shortcut(swap_store, order):
    store, state, profile = swap_store
    state.update(family='dualsense', touchpad=True)
    store.data['profiles'][profile]['TP:tap'] = entry('shortcut', 'E')
    store.save()
    first, second = ('TP:tap', '0') if order == 'touch_first' else ('0', 'TP:tap')
    payload = swap_change(store, state, profile, first, second)
    before = copy.deepcopy(store.data)
    with pytest.raises(ValueError, match='触摸手势只能'):
        store.apply_mapping_change(payload, state)
    assert store.data == before


def test_atomic_replace_failure_rolls_back_in_memory_file_and_baseline(swap_store, monkeypatch):
    store, state, profile = swap_store
    payload = swap_change(store, state, profile)
    before, baseline, before_file = copy.deepcopy(store.data), copy.deepcopy(store._baseline), store.path.read_bytes()
    original = __import__('os').replace
    def fail_replace(source, target):
        if target == store.path:
            raise OSError('Configuration replacement is unavailable')
        return original(source, target)
    monkeypatch.setattr('gamepadstudio.studio_core.os.replace', fail_replace)
    with pytest.raises(OSError):
        store.apply_mapping_change(payload, state)
    assert store.data == before and store._baseline == baseline
    assert store.path.read_bytes() == before_file


@pytest.mark.parametrize('guard', ['missing_scope', 'wrong_scope', 'missing_instance', 'wrong_instance', 'boolean_instance'])
def test_agent_requires_scope_and_strict_instance(battery_backend, guard):
    context = battery_backend
    profile = context.agent.store.profiles_for(context.device.state, 'kbm')[0]
    message = remote_change(context, profile)
    if guard == 'missing_scope':
        message.pop('device_scope')
    elif guard == 'wrong_scope':
        message['device_scope'] = 'pad:other'
    elif guard == 'missing_instance':
        message.pop('instance_id')
    elif guard == 'wrong_instance':
        message['instance_id'] += 1
    else:
        message['instance_id'] = True
    before, before_file = copy.deepcopy(context.agent.config), context.agent.store.path.read_bytes()
    with pytest.raises(ValueError):
        context.agent.handle(message)
    assert context.agent.config == before and context.agent.store.path.read_bytes() == before_file


def test_real_socket_swap_preserves_automatic_active_fallback_and_pause(app, battery_backend, monkeypatch):
    context = battery_backend
    agent, state = context.agent, context.device.state
    automatic = agent.store.profiles_for(state, 'kbm')[0]
    target = 'Swap alternate'
    agent.store.apply_mapping_change({'op': 'create', 'profile': target,
                                     'source': automatic, 'mode': 'kbm'}, state)
    agent.store.apply_mapping_change({'op': 'application_profiles', 'settings': settings(automatic)}, state)
    monkeypatch.setattr('gamepadstudio.agent.foreground_application',
                        lambda: foreground(GAME_A, pid=912345, hwnd=45678))
    assert agent.update_application_profile(force=True)
    assert agent.config['active_profile'] == automatic != target
    assert agent.status()['application_profile']['automatic']
    agent.set_enabled(False)
    before, automatic_status = copy.deepcopy(agent.config), copy.deepcopy(agent.application_profile)
    payload = swap_change(agent.store, state, target)
    result = request_over_socket(app, agent.root, 'mapping_change',
                                 device_scope=profile_scope(state), instance_id=state['instance_id'], change=payload)
    assert result['ok'] is True
    expected = copy.deepcopy(before)
    expected['profiles'][target]['0'] = payload['expected_second']
    expected['profiles'][target]['1'] = payload['expected_first']
    expected['mapping_revision'] += 1
    assert result['config'] == agent.config == expected
    assert json.loads(agent.store.path.read_text(encoding='utf-8')) == expected
    assert agent.application_profile == automatic_status
    assert not agent.enabled and not agent.config['mapping_enabled']
    assert not context.actions.calls


@pytest.mark.parametrize('stale_reason', ['same_scope_new_instance', 'binding_changed', 'default_threshold_changed'])
def test_real_socket_refuses_stale_swap_without_losing_newer_file(app, battery_backend, stale_reason):
    context = battery_backend
    agent, state = context.agent, context.device.state
    profile = agent.store.profiles_for(state, 'kbm')[0]
    if stale_reason == 'default_threshold_changed':
        agent.config['profiles'][profile]['0'].pop('long_press', None)
        agent.store.save()
    measured = copy.deepcopy(state)
    payload = swap_change(agent.store, state, profile)
    if stale_reason == 'same_scope_new_instance':
        context.device.state = agent.state = copy.deepcopy(state)
        context.device.state['instance_id'] += 1
    else:
        editor = ConfigStore(agent.root)
        if stale_reason == 'binding_changed':
            editor.data['profiles'][profile]['0']['short'] = {'action': 'hold', 'value': 'P'}
        else:
            editor.set_setting('long_press', .96, state)
        editor.save()
    before, before_file = copy.deepcopy(agent.config), agent.store.path.read_bytes()
    result = request_over_socket(app, agent.root, 'mapping_change',
                                 device_scope=profile_scope(measured), instance_id=measured['instance_id'], change=payload)
    assert result['ok'] is False
    assert agent.config == before and agent.store.path.read_bytes() == before_file
    assert not context.actions.calls


def test_swapping_live_mapping_releases_old_key_and_requires_fresh_press(battery_backend):
    context = battery_backend
    agent, state = context.agent, context.device.state
    active = agent.config['active_profile']
    agent.config['profiles'][active]['0'] = entry('hold', 'E')
    agent.config['profiles'][active]['1'] = entry('hold', 'Q')
    agent.store.save()
    state['buttons'] = [0]
    agent.engine.update(state, agent.config, enabled=True, now=1.)
    agent.engine.update(state, agent.config, enabled=True, now=1.2)
    assert context.actions.keys[ord('E')] == 1
    agent.handle(remote_change(context, active))
    assert not any(context.actions.keys.values())
    agent.engine.update(state, agent.config, enabled=True, now=2.)
    assert not any(context.actions.keys.values())
    state['buttons'] = []
    agent.engine.update(state, agent.config, enabled=True, now=2.2)
    state['buttons'] = [0]
    agent.engine.update(state, agent.config, enabled=True, now=3.)
    agent.engine.update(state, agent.config, enabled=True, now=3.2)
    assert context.actions.keys[ord('Q')] == 1
    assert context.actions.keys.get(ord('E'), 0) == 0
