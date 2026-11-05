"""Direction bindings must drive the virtual sticks and the emulated mouse.

These cover the runtime half of the feature: what a game actually receives. The
composition runs through the real `compose_xusb_report`; only the driver write is mocked.
"""
import pytest

from gamepadstudio.mapping_engine import (MappingRuntime, STICK_DIRECTIONS, GAMEPAD_TARGETS,
                                          mouse_axis_vector, output_tokens, stick_axis_bindings,
                                          validate_binding, validate_mappings)
from gamepadstudio.virtual_xinput import compose_xusb_report
from tests.test_unified_mapping import entry, frame


class PadActions:
    """Records the XUSB reports the runtime pushes, exactly as Studio dispatches them."""

    def __init__(self):
        self.frames = []
        self.mirror_calls = []
        self.held_gamepad_buttons = set()
        self.keys = {}
        self._last_axes = ()

    def sync_virtual_gamepad(self, buttons=None, axes=(), trigger_overrides=None):
        if axes:
            self.mirror_calls.append(tuple(axes[:4]))
            self._last_axes = tuple(axes)
        held = self.held_gamepad_buttons if buttons is None else set(buttons)
        self.frames.append(compose_xusb_report(held, self._last_axes, trigger_overrides)[1])

    def gamepad_button(self, button, down):
        if down:
            self.held_gamepad_buttons.add(str(button))
        else:
            self.held_gamepad_buttons.discard(str(button))
        self.sync_virtual_gamepad()

    def hold(self, value, down):
        self.keys[value] = max(0, self.keys.get(value, 0) + (1 if down else -1))

    def mouse_button(self, button, down):
        pass

    def release_all(self):
        self.held_gamepad_buttons.clear()


class FakeMouseThread:
    def __init__(self):
        self.stick_updates = []

    def configure(self, settings):
        pass

    def update_stick(self, x, y, is_desktop=False):
        self.stick_updates.append((round(x, 3), round(y, 3)))

    def set_click_lock(self, locked):
        pass

    def consume_output_error(self):
        return None


def runtime_for(mappings, options=None, mirror_enabled=True):
    actions = PadActions()
    runtime = MappingRuntime(actions,
                             lambda b, d: actions.gamepad_button(b['value'], d)
                             if b['action'] in ('gamepad_button', 'gamepad_axis') else None,
                             start_mouse=False)
    # Pinned so the suite never depends on ViGEmBus being installed on the test host.
    runtime.stick_mirror_enabled = mirror_enabled
    config = {'active_profile': 'test', 'profiles': {'test': mappings},
              'profile_options': {'test': options or {}}, 'long_press': .65}
    return runtime, config, actions


def tick(runtime, config, state, now):
    runtime.update(state, config, enabled=True, now=now)


def thumb(snapshot):
    return (snapshot['sThumbLX'], snapshot['sThumbLY'],
            snapshot['sThumbRX'], snapshot['sThumbRY'])


def test_direction_binding_deflects_the_target_stick_and_consumes_its_source():
    runtime, config, actions = runtime_for({'LS:up': entry('gamepad_axis', 'RS:left')})
    up = frame(axes=[0., -0.9, 0., 0., 0., 0.])
    tick(runtime, config, up, 0.)
    tick(runtime, config, up, .1)

    assert actions.frames, 'a direction mapping must emit a virtual report'
    last = actions.frames[-1]
    assert last['sThumbRX'] == -32767, 'RS:left should be fully deflected'
    assert thumb(last)[:2] == (0, 0), 'the consumed LS:up must not also reach the left stick'


def test_unbound_half_axis_still_passes_through_the_virtual_pad():
    runtime, config, actions = runtime_for({'LS:up': entry('gamepad_axis', 'RS:left')})
    diagonal = frame(axes=[0.4, -0.9, 0., 0., 0., 0.])
    tick(runtime, config, diagonal, 0.)
    tick(runtime, config, diagonal, .1)
    lx, ly = thumb(actions.frames[-1])[:2]
    assert ly == 0, 'up is consumed by the mapping'
    assert lx > 10000, 'the right half-axis was never mapped, so it still reaches the game'


def test_button_only_profiles_never_start_mirroring_sticks():
    runtime, config, actions = runtime_for({'0': entry('gamepad_button', '1')})
    for step in range(6):
        tick(runtime, config, frame(buttons=[0], axes=[0.9, -0.8, 0., 0., 0., 0.]), step * .05)
    assert not runtime.stick_mirror_live, 'no direction is bound, so the mirror stays off'
    assert actions.mirror_calls == [], 'a button-only profile must never mirror the sticks'


def test_repeated_identical_axes_emit_one_frame_and_release_returns_to_centre():
    runtime, config, actions = runtime_for({'LS:right': entry('gamepad_axis', 'LS:right')})
    pushed = frame(axes=[0.9, 0., 0., 0., 0., 0.])
    tick(runtime, config, pushed, 0.)
    tick(runtime, config, pushed, .1)
    settled = len(actions.frames)
    tick(runtime, config, pushed, .2)
    tick(runtime, config, pushed, .3)
    assert len(actions.frames) == settled, 'an unchanged report must not be re-sent every tick'

    rest = frame(axes=[0., 0., 0., 0., 0., 0.])
    tick(runtime, config, rest, .4)
    tick(runtime, config, rest, .5)
    assert thumb(actions.frames[-1]) == (0, 0, 0, 0)


def test_reset_releases_a_deflected_virtual_stick():
    runtime, config, actions = runtime_for({'LS:up': entry('gamepad_axis', 'RS:up')})
    pushed = frame(axes=[0., -0.9, 0., 0., 0., 0.])
    tick(runtime, config, pushed, 0.)
    tick(runtime, config, pushed, .1)
    assert thumb(actions.frames[-1])[2:4] != (0, 0)
    runtime.reset()
    assert not runtime.stick_mirror_live
    assert thumb(actions.frames[-1]) == (0, 0, 0, 0), 'reset must centre both sticks'


def test_mouse_move_binding_drives_the_pointer_without_the_stick_mouse_option():
    runtime, config, actions = runtime_for({'0': entry('mouse_move', 'right')})
    thread = FakeMouseThread()
    runtime.mouse_thread = thread
    for step in range(4):
        tick(runtime, config, frame(buttons=[0]), step * .05)
    assert thread.stick_updates[-1] == (1.0, 0.0), 'held direction moves the cursor right'

    tick(runtime, config, frame(buttons=[]), .5)
    tick(runtime, config, frame(buttons=[]), .6)
    assert thread.stick_updates[-1] == (0.0, 0.0)


def test_mouse_move_adds_to_the_analog_stick_and_stays_clamped():
    runtime, config, actions = runtime_for(
        {'0': entry('mouse_move', 'up')},
        options={'right_stick_mouse': True, 'mouse': {'mode': 'game', 'stick_source': 'right'}})
    thread = FakeMouseThread()
    runtime.mouse_thread = thread
    for step in range(4):
        tick(runtime, config, frame(buttons=[0], axes=[0., 0., 0.5, 0.4, 0., 0.]), step * .05)
    assert thread.stick_updates[-1] == (0.5, -0.6), 'analog plus a full up direction'

    for step in range(4):
        tick(runtime, config, frame(buttons=[0, 1], axes=[0., 0., 0.9, -0.9, 0., 0.]), step * .05)
    assert thread.stick_updates[-1] == (0.9, -1.0), 'the merged vector cannot exceed the stick'


def test_without_a_virtual_pad_backend_the_mirror_stays_shut():
    runtime, config, actions = runtime_for({'LS:up': entry('gamepad_axis', 'RS:left')},
                                           mirror_enabled=False)
    pushed = frame(axes=[0., -0.9, 0., 0., 0., 0.])
    tick(runtime, config, pushed, 0.)
    tick(runtime, config, pushed, .1)
    assert not runtime.stick_mirror_live
    assert runtime.virtual_xinput_state['stick_mirror'] is False
    assert actions.mirror_calls == [], (
        'a profile must not start mirroring sticks, and fail every tick, without a backend')


def test_direction_vectors_and_tokens_are_well_defined():
    assert mouse_axis_vector({'mouse_axis:up': 1, 'mouse_axis:left': 1}) == (-1.0, -1.0)
    assert mouse_axis_vector({'mouse_axis:up': 0}) == (0.0, 0.0)
    assert output_tokens({'action': 'mouse_move', 'value': 'down'}) == ['mouse_axis:down']
    assert output_tokens({'action': 'gamepad_axis', 'value': 'LS:right'}) == ['pad:LS:right']
    assert stick_axis_bindings({'LS:up': entry('gamepad_axis', 'RS:left'),
                                '0': entry('gamepad_button', '1')}) == {'RS:left'}


def test_direction_values_are_validated_and_stay_out_of_button_targets():
    assert validate_binding({'action': 'gamepad_axis', 'value': 'LS:up'})['value'] == 'LS:up'
    with pytest.raises(ValueError):
        validate_binding({'action': 'gamepad_axis', 'value': '1'})
    with pytest.raises(ValueError):
        validate_binding({'action': 'mouse_move', 'value': 'forward'})
    with pytest.raises(ValueError):
        validate_binding({'action': 'gamepad_axis', 'value': 'LS:up', 'mode': 'toggle'})
    assert not set(STICK_DIRECTIONS) & set(GAMEPAD_TARGETS), (
        'directions must not leak into button targets, macro steps or portable targets')
    validate_mappings({'LS:up': entry('gamepad_axis', 'RS:left')})


def test_portable_profiles_round_trip_direction_and_mouse_targets():
    from gamepadstudio.profile_transfer import export_profile, preview_profile_import
    from gamepadstudio.studio_core import profile_scope
    config = {'active_profile': 'Stick',
              'profiles': {'Stick': {'LS:up': entry('gamepad_axis', 'RS:left'),
                                     '0': entry('mouse_move', 'down')}},
              'profile_devices': {'Stick': profile_scope(None)},
              'profile_options': {'Stick': {}}}
    portable = export_profile(config, None, 'Stick')
    validated = preview_profile_import(portable, None)
    bindings = validated['profile']['mappings']
    assert bindings['LS:up']['short'] == {'action': 'gamepad_axis', 'value': 'RS:left'}
    assert bindings['0']['short'] == {'action': 'mouse_move', 'value': 'down'}

    config['profiles']['Stick']['LS:up']['short']['value'] = '9'
    with pytest.raises(ValueError):
        preview_profile_import(export_profile(config, None, 'Stick'), None)
