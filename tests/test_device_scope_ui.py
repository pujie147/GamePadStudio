"""Device changes restore their own workspace without phantom controls."""
import copy
import os

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from gamepadstudio.i18n import get_language_preference, init_language
from gamepadstudio.studio import Studio
from gamepadstudio.studio_core import ConfigStore, is_nikki_profile, profile_scope
from gamepadstudio.virtual_kbm_ui import KbmFeelDialog


def device(identity, family='xbox', *, rumble=True, led=False, touch=False,
           buttons=None, axes=None):
    return dict(device_key=f'fixture:{identity}', profile_key=f'{family}:model',
                instance_id=identity, family=family, name=f'Controller {identity}',
                controller_type=0, available_buttons=list(range(15)) if buttons is None else buttons,
                available_axes=list(range(6)) if axes is None else axes,
                buttons=[], axes=[0.] * 6, power=-1, touch=[],
                rumble=rumble, led=led, touchpad=touch, vendor=0x1234, product=0x5678)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    # This fixture exercises the existing Windows workspace contract using
    # fake output; native Mac capability coverage lives in test_platform_ui.
    from tests.test_unified_mapping import Actions
    monkeypatch.setattr('gamepadstudio.studio.WINDOWS_FEATURES', True)
    monkeypatch.setattr('gamepadstudio.virtual_kbm_ui.WINDOWS_FEATURES', True)
    monkeypatch.setattr('gamepadstudio.studio.create_actions', Actions)
    class Disconnected:
        available = []
        def scan(self): pass
        def read(self): return None
        def close(self): pass
        def led(self, color): return True
    monkeypatch.setattr('gamepadstudio.studio.Device', Disconnected)
    app = QApplication.instance() or QApplication([])
    previous = get_language_preference()
    window = Studio(tmp_path, standalone=True, lang='zh')
    window.timer.stop(); window.scan_timer.stop(); window.gallery_timer.stop()
    window.enabled = False
    yield window, app
    window.cleanup(); window.hide()
    init_language(previous)


def select(window, app, state):
    window.snapshot = state
    window.update_controller_ui(state)
    window.tester.update_state(state)
    window.virtual_kbm_page.set_device_state(state)
    app.processEvents()


def items(combo):
    return {combo.itemText(index) for index in range(combo.count())}


def test_disconnect_clears_playstation_controls_and_keeps_its_saved_data(workspace):
    window, app = workspace
    ps = device(1, 'dualsense', led=True, touch=True, buttons=list(range(21)))
    select(window, app, ps)
    ps_profiles = items(window.profile_combo)
    assert window.art.family == 'dualsense'
    assert not window.mapping_boxes[20][0].isHidden()
    assert not window.touch_settings_row.isHidden()

    select(window, app, None)
    assert window.controller_heading.text() == '通用 XInput'
    assert window.art.family == window.mapping_art.family == window.tester.family == 'generic'
    assert window.tester.btn_tiles[0].text() == 'A'
    assert window.tester.trigger_names[0].text() == 'LT'
    assert window.mapping_boxes[15][0].isHidden() and window.mapping_boxes[20][0].isHidden()
    assert not ps_profiles.intersection(items(window.profile_combo))
    assert ps_profiles <= set(window.config['profiles'])
    assert window.config['active_profile'] in window.store.profiles_for(None)
    assert window.touch_settings_row.isHidden() and window.led_settings_row.isHidden()
    assert not window.virtual_kbm_page.cloaking_action.isEnabled()
    space = window.virtual_kbm_page.keycaps['Space']
    assert space.binding_sources
    assert all(binding['family'] == 'generic' for binding in space.binding_sources.values())


def test_same_model_controllers_restore_separate_ui_values_and_selection(workspace):
    window, app = workspace
    first, second = device(1), device(2)
    select(window, app, first)
    first_profiles = items(window.profile_combo)
    first_keyboard = next(name for name in first_profiles if is_nikki_profile(window.config, name))
    window.change_profile(first_keyboard)
    window.rumble_slider.setValue(81)
    window.long_press_slider.setValue(92)
    window.store.apply_mapping_change({'op': 'options', 'profile': first_keyboard,
                                      'options': {'mouse': {'sensitivity': 41}}}, first)
    first_settings = copy.deepcopy(window.store.settings_for(first))

    select(window, app, second)
    assert not first_profiles.intersection(items(window.profile_combo))
    assert window.rumble_slider.value() == 35 and window.long_press_slider.value() == 65
    assert window.store.settings_for(first) == first_settings
    assert window.config['active_profile'] in window.store.profiles_for(second)
    second_keyboard = window.virtual_kbm_page.current_scheme()
    assert window.config['profile_options'][second_keyboard]['mouse']['sensitivity'] == 24
    window.rumble_slider.setValue(22)

    select(window, app, first)
    assert window.config['active_profile'] == first_keyboard
    assert window.rumble_slider.value() == 81 and window.long_press_slider.value() == 92
    assert window.store.settings_for(second)['rumble'] == .22
    assert window.config['profile_options'][first_keyboard]['mouse']['sensitivity'] == 41
    persisted = ConfigStore(window.store.root)
    assert persisted.settings_for(first)['rumble'] == .81
    assert persisted.data['controller_profiles'][profile_scope(first)] == first_keyboard


def test_button_only_device_hides_unsupported_hardware_and_analog_options(workspace):
    window, app = workspace
    state = device(3, 'generic', rumble=False, buttons=[0, 1, 4], axes=[])
    select(window, app, state)
    assert window.mapping_available == {0, 1, 4}
    assert all(box.isHidden() == (key not in {0, 1, 4}) for key, (box, _) in window.mapping_boxes.items())
    assert window.led_settings_row.isHidden() and window.rumble_settings_row.isHidden()
    assert window.touch_settings_row.isHidden() and window.haptic_settings_row.isHidden()
    assert not window.hardware_empty.isHidden()
    assert window.virtual_kbm_page.mouse_toggle.isHidden()
    profile = window.virtual_kbm_page.current_scheme()
    original = copy.deepcopy(window.config['profile_options'][profile])
    dialog = KbmFeelDialog(window, profile)
    try:
        assert not dialog.has_pointer and not dialog.has_stick and not dialog.has_trigger
        assert 'stick_press' not in dialog.input_fields and 'trigger_press' not in dialog.input_fields
        result = dialog.options()
        assert result['mouse'] == original['mouse']
        assert result['input']['stick_press'] == original['input']['stick_press']
        assert result['input']['trigger_press'] == original['input']['trigger_press']
        dialog.validate()
        assert dialog.result() == QDialog.Accepted
    finally:
        dialog.close()


def test_feel_editor_cannot_save_after_switching_physical_device(workspace):
    window, app = workspace
    first, second = device(1), device(2)
    select(window, app, first)
    profile = window.virtual_kbm_page.current_scheme()
    original = copy.deepcopy(window.config['profile_options'][profile])
    dialog = KbmFeelDialog(window, profile)
    try:
        dialog.mouse_fields['sensitivity'].setValue(99)
        select(window, app, second)
        dialog.validate()
        assert dialog.result() != QDialog.Accepted
        assert not dialog.controls.button(QDialogButtonBox.Save).isEnabled()
        assert not dialog.error.isHidden()
        assert window.config['profile_options'][profile] == original
    finally:
        dialog.close()


def test_telemetry_delayed_rumble_never_reaches_next_device(workspace, monkeypatch):
    window, app = workspace
    callbacks, pulses = [], []
    monkeypatch.setattr('gamepadstudio.input_tester.QTimer.singleShot', lambda delay, callback: callbacks.append(callback))
    window.tester.send_rumble = lambda strength: pulses.append((profile_scope(window.tester.state), strength))
    first, second = device(1), device(2)
    window.tester.update_state(first)
    window.tester.rumble_pattern(.7, 3)
    window.tester.update_state(second)
    for callback in callbacks:
        callback()
    assert pulses == [(profile_scope(first), .7)]
