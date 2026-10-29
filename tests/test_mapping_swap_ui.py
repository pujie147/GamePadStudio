"""Atomic mapping rearrangement through real Qt and isolated Core/Studio state.

All physical devices, native output and trays are fakes. No personal files change.
"""
import copy
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, Qt, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QScrollArea, QWidget

from gamepadstudio.i18n import get_language, init_language
from gamepadstudio.mapping_engine import effective_mappings
from gamepadstudio.studio import STYLE
from gamepadstudio.studio_core import ConfigStore, profile_scope
from tests.test_device_scope_ui import device
from tests.test_emergency_hotkey_ui import hotkey_app, hotkey_workspace
from tests.test_stick_calibration_ui import readable_offscreen_fonts
from tests.test_unified_mapping import entry


@pytest.fixture
def swap_owner(tmp_path, hotkey_app, request):
    options = getattr(request, 'param', {})

    class Socket(QObject):
        disconnected = Signal()

    class Client(QObject):
        event = Signal(dict)

        def __init__(self, parent):
            super().__init__(parent)
            self.connected = True
            self.socket = Socket(self)

    class Owner(QWidget):
        device_sample = Signal(object)

        def __init__(self):
            super().__init__()
            self.snapshot = device(21, 'dualsense', touch=True, buttons=list(range(21)))
            self.remote = options.get('remote', False)
            self.client = Client(self)
            self.store = ConfigStore(tmp_path)
            self.store.activate_controller(self.snapshot)
            self.config = self.store.data
            self.profile = self.store.profiles_for(self.snapshot, mode='kbm')[0]
            self.config['profiles'][self.profile]['0'] = {
                **entry('hold', 'Space', 'shortcut', 'Ctrl+F'), 'long_press': .81}
            self.config['profiles'][self.profile]['1'] = {
                **entry('mouse_click', 'left', 'hold', 'Shift+W'), 'long_press': 1.24}
            self.store.save()
            self.changes = []
            self.fail = False
            self.notice = QLabel('', self)

        def mapping_change(self, change):
            self.changes.append(copy.deepcopy(change))
            if self.fail:
                self.notice.setText('保存失败：磁盘不可写')
                return False
            try:
                self.store.apply_mapping_change(change, self.snapshot)
            except (ValueError, OSError) as exc:
                self.notice.setText(str(exc))
                return False
            return True

    owner = Owner()
    dialogs = []

    def open_dialog(first='0', profile=None):
        from gamepadstudio.mapping_swap_ui import MappingSwapDialog
        dialog = MappingSwapDialog(owner, profile or owner.profile, first)
        if hasattr(dialog, 'timer'):
            dialog.timer.stop()
        dialogs.append(dialog)
        return dialog

    yield owner, hotkey_app, open_dialog
    for dialog in dialogs:
        dialog.close()
        dialog.deleteLater()
    owner.close()
    owner.deleteLater()
    hotkey_app.sendPostedEvents(None, QEvent.DeferredDelete)


def screenshot(widget, name):
    root = Path(__file__).resolve().parents[1] / 'artifacts' / 'mapping-swap-ui'
    root.mkdir(parents=True, exist_ok=True)
    assert widget.grab().save(str(root / name))


def select_state(window, app, fake_device, state):
    fake_device.state = state
    window.snapshot = state
    window.update_controller_ui(state)
    window.tester.update_state(state)
    app.processEvents()


def choose(dialog, first='0', second='1'):
    first_index = dialog.first_combo.findData(first)
    second_index = dialog.second_combo.findData(second)
    assert first_index >= 0 and second_index >= 0
    dialog.first_combo.setCurrentIndex(first_index)
    dialog.second_combo.setCurrentIndex(second_index)


def preview_text(widget):
    return '\n'.join(label.text() for label in widget.findChildren(QLabel))


def test_opening_and_changing_selection_never_write_configuration(swap_owner):
    owner, _, open_dialog = swap_owner
    before, file_before = copy.deepcopy(owner.config), owner.store.path.read_bytes()
    dialog = open_dialog()
    choose(dialog, '1', '0')
    assert owner.changes == [] and owner.config == before
    assert owner.store.path.read_bytes() == file_before
    assert 'Space' in preview_text(dialog.second_preview)
    assert 'Ctrl+F' in preview_text(dialog.second_preview)


@pytest.mark.parametrize('save_selection', [('0', '1'), ('1', '0')])
def test_cancel_discards_swap_draft(swap_owner, save_selection):
    owner, _, open_dialog = swap_owner
    before, file_before = copy.deepcopy(owner.config), owner.store.path.read_bytes()
    dialog = open_dialog()
    choose(dialog, *save_selection)
    dialog.buttons.rejected.emit()
    assert dialog.result() == QDialog.Rejected
    assert owner.changes == [] and owner.config == before
    assert owner.store.path.read_bytes() == file_before


def test_saved_swap_preserves_full_short_long_and_threshold_entries(swap_owner):
    from gamepadstudio.studio_core import mapping_input_signature, swap_binding_entry
    owner, _, open_dialog = swap_owner
    before = copy.deepcopy(owner.config)
    first = swap_binding_entry(before, owner.snapshot, owner.profile, '0')
    second = swap_binding_entry(before, owner.snapshot, owner.profile, '1')
    dialog = open_dialog()
    choose(dialog)
    assert dialog.swap_button.isEnabled()
    dialog.save()
    expected = copy.deepcopy(before)
    expected['profiles'][owner.profile]['0'] = second
    expected['profiles'][owner.profile]['1'] = first
    expected['mapping_revision'] += 1
    assert owner.config == expected
    assert dialog.result() == QDialog.Accepted
    assert owner.changes == [{
        'op': 'swap_bindings', 'profile': owner.profile, 'first': '0', 'second': '1',
        'expected_inputs': mapping_input_signature(owner.snapshot),
        'expected_first': first, 'expected_second': second}]
    restored = ConfigStore(owner.store.root).data
    for key in ('profiles', 'profile_options', 'active_profile', 'controller_profiles',
                'device_settings', 'mapping_revision'):
        assert restored[key] == expected[key]


@pytest.mark.parametrize('explicit_none', [False, True])
def test_implicit_shield_default_or_explicit_none_preview_matches_commit(swap_owner, explicit_none):
    from gamepadstudio.studio_core import swap_binding_entry
    owner, _, open_dialog = swap_owner
    owner.snapshot = device(21, 'xbox', buttons=list(range(16)))
    owner.config['gamebar_shield_enabled'] = True
    if explicit_none:
        owner.config['profiles'][owner.profile]['15'] = entry()
    else:
        owner.config['profiles'][owner.profile].pop('15', None)
    owner.config['profiles'][owner.profile]['0'] = entry('hold', 'Space')
    owner.store.save()
    original = swap_binding_entry(owner.config, owner.snapshot, owner.profile, '15')
    assert original['short']['action'] == ('none' if explicit_none else 'capture')
    dialog = open_dialog('15')
    choose(dialog, '15', '0')
    assert dialog.swap_button.isEnabled()
    dialog.save()
    assert owner.config['profiles'][owner.profile]['0'] == original
    assert effective_mappings(owner.config, owner.snapshot, owner.profile)['0'] == original


@pytest.mark.parametrize('case', ['same_source', 'identical_entries', 'both_empty', 'empty_different_thresholds'])
def test_meaningless_swap_is_disabled(swap_owner, case):
    owner, _, open_dialog = swap_owner
    if case == 'identical_entries':
        owner.config['profiles'][owner.profile]['1'] = copy.deepcopy(owner.config['profiles'][owner.profile]['0'])
    elif case == 'both_empty':
        owner.config['profiles'][owner.profile]['0'] = entry()
        owner.config['profiles'][owner.profile]['1'] = entry()
    elif case == 'empty_different_thresholds':
        owner.config['profiles'][owner.profile]['0'] = {**entry(), 'long_press': .81}
        owner.config['profiles'][owner.profile]['1'] = {**entry(), 'long_press': 1.24}
    owner.store.save()
    dialog = open_dialog()
    choose(dialog, '0', '0' if case == 'same_source' else '1')
    assert not dialog.swap_button.isEnabled()
    dialog.save()
    assert owner.changes == []


def test_actual_sources_and_existing_combos_are_candidates(swap_owner):
    owner, _, open_dialog = swap_owner
    owner.config['profiles'][owner.profile]['0+9'] = entry('shortcut', 'Ctrl+M')
    owner.config['profiles'][owner.profile]['40'] = entry('hold', 'F')
    owner.store.save()
    dialog = open_dialog('0+9')
    candidates = {dialog.first_combo.itemData(i) for i in range(dialog.first_combo.count())}
    assert {'0+9', '0', 'LT', 'RT', 'TP:tap'} <= candidates
    assert '40' not in candidates
    assert dialog.first_combo.currentData() == '0+9'
    choose(dialog, '0+9', '1')
    assert dialog.swap_button.isEnabled()


@pytest.mark.parametrize('first,second', [('TP:tap', '0'), ('0', 'TP:tap')])
def test_touch_and_continuous_sources_cannot_swap(swap_owner, first, second):
    owner, _, open_dialog = swap_owner
    owner.config['profiles'][owner.profile]['TP:tap'] = entry('shortcut', 'F')
    owner.store.save()
    dialog = open_dialog(first)
    choose(dialog, first, second)
    assert not dialog.swap_button.isEnabled()
    assert dialog.message.text()
    dialog.save()
    assert owner.changes == []


def test_two_touch_gestures_can_swap_without_changing_device_preferences(swap_owner):
    owner, _, open_dialog = swap_owner
    first, second = 'TP:tap', 'TP:swipe_up'
    owner.config['profiles'][owner.profile][first] = entry('shortcut', 'F')
    owner.config['profiles'][owner.profile][second] = entry('wheel', 'up')
    owner.store.save()
    preferences = copy.deepcopy(owner.config['device_settings'])
    dialog = open_dialog(first)
    choose(dialog, first, second)
    assert dialog.swap_button.isEnabled()
    dialog.save()
    assert owner.config['profiles'][owner.profile][first]['short'] == {'action': 'wheel', 'value': 'up'}
    assert owner.config['profiles'][owner.profile][second]['short'] == {'action': 'shortcut', 'value': 'F'}
    assert owner.config['device_settings'] == preferences


@pytest.mark.parametrize('action', ['launch', 'gamepad_macro'])
def test_local_swap_preserves_action_metadata_without_running_it(swap_owner, action):
    from gamepadstudio.studio_core import swap_binding_entry
    owner, _, open_dialog = swap_owner
    profile = owner.profile if action == 'launch' else owner.store.profiles_for(owner.snapshot, 'gamepad')[0]
    binding = ({'action': 'launch', 'executable': 'D:\\游戏\\启动器.exe',
                'arguments': '--profile "中文"', 'working_directory': 'D:\\游戏'}
               if action == 'launch' else {'action': 'gamepad_macro', 'sequence': [
                   {'buttons': ['0'], 'hold_ms': 50, 'gap_ms': 40}], 'repeat': False})
    owner.config['profiles'][profile]['0'] = {'short': binding, 'long': {'action': 'none'}, 'long_press': .81}
    owner.config['profiles'][profile]['1'] = entry('capture')
    owner.store.save()
    before = swap_binding_entry(owner.config, owner.snapshot, profile, '0')
    dialog = open_dialog(profile=profile)
    choose(dialog)
    dialog.save()
    assert dialog.result() == QDialog.Accepted
    assert owner.config['profiles'][profile]['1'] == before
    assert owner.config['profiles'][profile]['1']['short'] == binding


def scroll_settings(owner, gestures):
    owner.store.set_setting('touch_scroll', True, owner.snapshot)
    owner.store.set_setting('touch_gestures_enabled', gestures, owner.snapshot)
    owner.config['profiles'][owner.profile]['TP:scroll_up'] = entry()
    owner.config['profiles'][owner.profile]['TP:tap'] = entry()
    owner.store.save()


def test_direct_scroll_gate_affects_only_selected_scroll_pair(swap_owner):
    owner, _, open_dialog = swap_owner
    scroll_settings(owner, False)
    preferences = copy.deepcopy(owner.config['device_settings'])
    dialog = open_dialog('0')
    choose(dialog)
    assert dialog.swap_button.isEnabled()
    choose(dialog, 'TP:scroll_up', 'TP:tap')
    assert not dialog.swap_button.isEnabled()
    assert '手势' in dialog.message.text()
    dialog.save()
    assert owner.changes == [] and owner.config['device_settings'] == preferences


def test_enabled_scroll_default_moves_and_leaves_explicit_suppress(swap_owner):
    owner, _, open_dialog = swap_owner
    scroll_settings(owner, True)
    preferences = copy.deepcopy(owner.config['device_settings'])
    dialog = open_dialog('TP:scroll_up')
    choose(dialog, 'TP:scroll_up', 'TP:tap')
    assert dialog.swap_button.isEnabled()
    dialog.save()
    assert dialog.result() == QDialog.Accepted
    assert owner.config['profiles'][owner.profile]['TP:tap']['short'] == {'action': 'wheel', 'value': 'up'}
    assert owner.config['profiles'][owner.profile]['TP:scroll_up']['short']['action'] == 'suppress'
    assert owner.config['device_settings'] == preferences


@pytest.mark.parametrize('swap_owner', [{}, {'remote': True}], indirect=True)
@pytest.mark.parametrize('change', ['disconnect', 'instance', 'physical', 'axes', 'buttons', 'family'])
def test_device_and_input_changes_make_draft_permanently_invalid(swap_owner, change):
    owner, _, open_dialog = swap_owner
    original = copy.deepcopy(owner.snapshot)
    dialog = open_dialog()
    choose(dialog)
    changed = copy.deepcopy(original)
    if change == 'disconnect':
        changed = None
    elif change == 'instance':
        changed['instance_id'] += 1
    elif change == 'physical':
        changed['device_key'] = 'fixture:other'
    elif change == 'axes':
        changed['available_axes'] = [0, 1, 4, 5]
    elif change == 'buttons':
        changed['available_buttons'] = list(range(14))
    else:
        changed['family'] = 'xbox'
    owner.snapshot = changed
    if owner.remote:
        owner.client.event.emit({'type': 'state', 'device': changed})
    else:
        owner.device_sample.emit(changed)
    assert dialog.invalidated and not dialog.swap_button.isEnabled()
    owner.snapshot = original
    dialog.check_context()
    dialog.save()
    assert dialog.invalidated and owner.changes == []


@pytest.mark.parametrize('swap_owner', [{'remote': True}], indirect=True)
def test_rapid_backend_reconnect_keeps_draft_invalid(swap_owner):
    owner, _, open_dialog = swap_owner
    dialog = open_dialog()
    choose(dialog)
    owner.client.connected = False
    owner.client.socket.disconnected.emit()
    owner.client.connected = True
    dialog.check_context()
    dialog.save()
    assert dialog.invalidated and not dialog.swap_button.isEnabled()
    assert owner.changes == []


@pytest.mark.parametrize('change', ['first', 'second', 'delete'])
def test_modified_or_deleted_preset_is_not_overwritten_by_old_draft(swap_owner, change):
    owner, _, open_dialog = swap_owner
    dialog = open_dialog()
    choose(dialog)
    if change == 'delete':
        owner.config['profiles'].pop(owner.profile)
    else:
        key = '0' if change == 'first' else '1'
        owner.config['profiles'][owner.profile][key] = entry('hold', 'M')
    owner.store.save()
    before = copy.deepcopy(owner.config)
    dialog.check_context()
    dialog.save()
    assert owner.config == before
    assert not dialog.swap_button.isEnabled() or dialog.result() == QDialog.Rejected


def test_unrelated_binding_and_options_update_survives_open_draft(swap_owner):
    owner, _, open_dialog = swap_owner
    dialog = open_dialog()
    choose(dialog)
    owner.config['profiles'][owner.profile]['2'] = entry('shortcut', 'Alt+M')
    owner.config['profile_options'][owner.profile]['mouse']['invert_y'] = True
    owner.store.save()
    dialog.check_context()
    assert dialog.swap_button.isEnabled() and not dialog.invalidated
    dialog.save()
    assert dialog.result() == QDialog.Accepted
    assert owner.config['profiles'][owner.profile]['2'] == entry('shortcut', 'Alt+M')
    assert owner.config['profile_options'][owner.profile]['mouse']['invert_y'] is True


def test_changed_binding_on_disk_rejects_stale_confirmation(swap_owner):
    owner, _, open_dialog = swap_owner
    dialog = open_dialog()
    choose(dialog)
    dialog.show()
    external = ConfigStore(owner.store.root)
    external.apply_mapping_change({'op': 'binding', 'profile': owner.profile, 'trigger': '0',
                                   'mapping': entry('hold', 'M')}, owner.snapshot)
    latest = owner.store.path.read_bytes()
    dialog.save()
    assert dialog.result() == QDialog.Rejected and dialog.isVisible()
    assert owner.store.path.read_bytes() == latest
    assert '变化' in dialog.message.text()


@pytest.mark.parametrize('swap_owner', [{}, {'remote': True}], indirect=True)
def test_power_updates_do_not_invalidate_mapping_capabilities(swap_owner):
    owner, _, open_dialog = swap_owner
    dialog = open_dialog()
    choose(dialog)
    owner.snapshot['power'] = 0
    if owner.remote:
        owner.client.event.emit({'type': 'state', 'device': owner.snapshot})
    else:
        owner.device_sample.emit(owner.snapshot)
    assert not dialog.invalidated and dialog.swap_button.isEnabled()


def test_apply_failure_retains_selection_and_preview_for_retry(swap_owner):
    owner, _, open_dialog = swap_owner
    dialog = open_dialog()
    choose(dialog)
    dialog.show()
    before = copy.deepcopy(owner.config)
    first, second = preview_text(dialog.first_preview), preview_text(dialog.second_preview)
    owner.fail = True
    dialog.save()
    assert dialog.isVisible() and dialog.result() == QDialog.Rejected
    assert owner.config == before and '磁盘不可写' in dialog.message.text()
    assert preview_text(dialog.first_preview) == first and preview_text(dialog.second_preview) == second
    assert dialog.first_combo.currentData() == '0' and dialog.second_combo.currentData() == '1'
    owner.fail = False
    dialog.save()
    assert dialog.result() == QDialog.Accepted


@pytest.mark.parametrize('swap_owner', [{}, {'remote': True}], indirect=True)
def test_close_unbinds_device_and_transport_signals(swap_owner):
    owner, _, open_dialog = swap_owner
    dialog = open_dialog()
    choose(dialog)
    dialog.show()
    dialog.close()
    owner.snapshot = None
    owner.device_sample.emit(None)
    owner.client.event.emit({'type': 'state', 'device': None})
    owner.client.socket.disconnected.emit()
    assert not dialog.invalidated
    if hasattr(dialog, 'timer'):
        assert not dialog.timer.isActive()


@pytest.mark.parametrize('lang,width', [('zh', 490), ('en', 350)])
def test_preview_and_actions_fit_small_localized_dialog(swap_owner, lang, width):
    owner, app, open_dialog = swap_owner
    init_language(lang)
    owner.setStyleSheet(STYLE)
    dialog = open_dialog()
    choose(dialog)
    dialog.resize(width, 510)
    dialog.show()
    app.processEvents()
    assert dialog.width() == width
    for widget in (dialog.first_combo, dialog.second_combo, dialog.swap_button,
                   dialog.buttons.button(QDialogButtonBox.Cancel)):
        assert dialog.rect().contains(widget.mapTo(dialog, QPoint(0, 0)))
        assert dialog.rect().contains(widget.mapTo(dialog, widget.rect().bottomRight()))
    assert 'Space' in preview_text(dialog.first_preview)
    assert 'Shift+W' in preview_text(dialog.second_preview)
    screenshot(dialog, f'dialog-{lang}-{width}.png')


def studio_swap_draft(window, first='0'):
    from gamepadstudio.mapping_swap_ui import MappingSwapDialog
    profile = window.store.profiles_for(window.snapshot, mode='kbm')[0]
    window.config['profiles'][profile]['0'] = {**entry('hold', 'Space', 'shortcut', 'Ctrl+F'),
                                              'long_press': .81}
    window.config['profiles'][profile]['1'] = {**entry('mouse_click', 'left', 'hold', 'Shift+W'),
                                              'long_press': 1.24}
    window.store.save()
    dialog = MappingSwapDialog(window, profile, first)
    if hasattr(dialog, 'timer'):
        dialog.timer.stop()
    choose(dialog)
    return dialog, profile


def cleanup_dialog(dialog, app):
    dialog.close()
    dialog.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def test_standalone_studio_swap_does_not_activate_target_or_change_other_settings(hotkey_workspace):
    window, app, fake_device, _ = hotkey_workspace
    select_state(window, app, fake_device, device(21))
    dialog, profile = studio_swap_draft(window)
    try:
        before = copy.deepcopy(window.config)
        assert before['active_profile'] != profile
        dialog.save()
        assert dialog.result() == QDialog.Accepted
        assert window.config['profiles'][profile]['0'] == before['profiles'][profile]['1']
        assert window.config['profiles'][profile]['1'] == before['profiles'][profile]['0']
        for key in ('active_profile', 'controller_profiles', 'profile_options', 'device_settings',
                    'application_profiles'):
            assert window.config[key] == before[key]
        assert window.actions.calls == []
    finally:
        cleanup_dialog(dialog, app)


def test_swapped_short_and_long_bindings_drive_real_mapping_runtime_with_fake_output(hotkey_workspace):
    from gamepadstudio.studio_core import device_config
    window, app, fake_device, _ = hotkey_workspace
    state = device(21)
    state['axes'][4:6] = [-1., -1.]
    select_state(window, app, fake_device, state)
    dialog, profile = studio_swap_draft(window)
    try:
        window.store.remember_profile(state, profile)
        window.store.save()
        dialog.save()
        assert dialog.result() == QDialog.Accepted
        config = device_config(window.config, state)

        def frame(buttons, now):
            current = copy.deepcopy(state)
            current['buttons'] = buttons
            window.engine.update(current, config, now=now)

        frame([], 100.)
        frame([0], 100.1)
        frame([], 100.2)
        frame([], 100.3)
        assert ('mouse', 'left', True) in window.actions.calls
        assert ('mouse', 'left', False) in window.actions.calls
        assert not any(call[:2] == ('key', 'Space') for call in window.actions.calls)
        window.actions.calls.clear()
        frame([1], 101.)
        frame([1], 101.9)
        frame([], 102.)
        frame([], 102.1)
        assert ('key', 'Ctrl+F', True) in window.actions.calls
        assert ('key', 'Ctrl+F', False) in window.actions.calls
        assert not window.actions.mouse and not any(window.actions.keys.values())
    finally:
        cleanup_dialog(dialog, app)


@pytest.mark.parametrize('hotkey_workspace', [{'remote': True}], indirect=True)
@pytest.mark.parametrize('success', [False, True])
def test_remote_confirmation_passes_scope_instance_and_authoritative_snapshots(hotkey_workspace, monkeypatch, success):
    from gamepadstudio.studio_core import mapping_input_signature, swap_binding_entry
    window, app, fake_device, _ = hotkey_workspace
    state = device(21)
    select_state(window, app, fake_device, state)
    dialog, profile = studio_swap_draft(window)
    captured = []
    before = copy.deepcopy(window.config)

    def backend_request(root, command, **kwargs):
        captured.append((command, copy.deepcopy(kwargs)))
        if not success:
            return {'ok': False, 'error': '手柄连接已变化，请重新打开交换绑定'}
        backend = ConfigStore(root)
        config = backend.apply_mapping_change(kwargs['change'], state)
        return {'ok': True, 'config': config}

    monkeypatch.setattr('gamepadstudio.studio.request', backend_request)
    try:
        dialog.show()
        dialog.save()
        command, context = captured[-1]
        assert command == 'mapping_change'
        assert context['device_scope'] == profile_scope(state) and context['instance_id'] == 21
        assert context['change'] == {
            'op': 'swap_bindings', 'profile': profile, 'first': '0', 'second': '1',
            'expected_inputs': mapping_input_signature(state),
            'expected_first': swap_binding_entry(before, state, profile, '0'),
            'expected_second': swap_binding_entry(before, state, profile, '1')}
        if success:
            assert dialog.result() == QDialog.Accepted
            assert window.config['profiles'][profile]['0'] == before['profiles'][profile]['1']
            assert window.config['active_profile'] == before['active_profile']
        else:
            assert window.config == before and dialog.isVisible()
            assert '连接已变化' in dialog.message.text()
        assert window.actions.calls == []
    finally:
        cleanup_dialog(dialog, app)


@pytest.mark.parametrize('hotkey_workspace', [{}, {'remote': True}], indirect=True)
def test_opening_swap_releases_output_and_closes_remote_suspend_lease(hotkey_workspace, monkeypatch):
    from gamepadstudio.mapping_swap_ui import MappingSwapDialog
    window, app, fake_device, _ = hotkey_workspace
    select_state(window, app, fake_device, device(21))
    profile = window.store.profiles_for(window.snapshot, 'kbm')[0]
    window.actions.hold('E', True)
    window.actions.mouse_button('left', True)
    opened = []

    def inspect_dialog(dialog):
        opened.append(dialog)
        assert not window.actions.keys and not window.actions.mouse
        assert dialog.owner is window
        assert dialog.first_combo.currentData() == '1'
        if window.remote:
            assert window.client.sent[-1] == ('suspend', {'seconds': 2})
        dialog.reject()
        return QDialog.Rejected

    monkeypatch.setattr(MappingSwapDialog, 'exec', inspect_dialog)
    window.open_mapping_swap(profile, '1')
    assert len(opened) == 1
    if window.remote:
        assert window.client.sent[-1] == ('suspend', {'seconds': 0})
    app.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.mark.parametrize('reason', ['disconnected', 'foreign_profile', 'offline_backend'])
@pytest.mark.parametrize('hotkey_workspace', [{'remote': True}], indirect=True)
def test_invalid_workspace_context_cannot_open_swap(hotkey_workspace, monkeypatch, reason):
    from gamepadstudio.mapping_swap_ui import MappingSwapDialog
    window, app, fake_device, notices = hotkey_workspace
    state = device(21)
    select_state(window, app, fake_device, state)
    profile = window.store.profiles_for(state, 'kbm')[0]
    if reason == 'disconnected':
        window.snapshot = None
    elif reason == 'foreign_profile':
        profile = window.store.profiles_for(device(22), 'kbm')[0]
    else:
        window.client.connected = False
    monkeypatch.setattr(MappingSwapDialog, 'exec', lambda _: pytest.fail('Invalid context opened dialog'))
    window.open_mapping_swap(profile, '0')
    assert notices and window.client.sent == []


def test_controller_deck_and_kbm_menu_use_current_profile_and_source(hotkey_workspace, monkeypatch):
    window, app, fake_device, _ = hotkey_workspace
    select_state(window, app, fake_device, device(21))
    calls = []
    monkeypatch.setattr(window, 'open_mapping_swap', lambda profile, first: calls.append((profile, first)))
    gamepad = window.current_gamepad_profile()
    window.mapping_deck.set_trigger('LT')
    assert window.mapping_deck.swap_btn.isEnabled()
    window.mapping_deck.swap_btn.click()
    assert calls[-1] == (gamepad, 'LT')
    page = window.virtual_kbm_page
    page.bindings.refresh()
    assert not page.bindings.swap_button.isEnabled()
    page.bindings.list.setCurrentItem(page.bindings.rows['1'])
    assert page.bindings.swap_button.isEnabled()
    page.bindings.swap_button.click()
    assert calls[-1] == (page.current_scheme(), '1')
    assert page.swap_bindings_action.isEnabled()
    page.swap_bindings_action.trigger()
    assert calls[-1] == (page.current_scheme(), '1')
    menus = [button.menu() for button in page.preset_actions.findChildren(QPushButton) if button.menu()]
    assert any(page.swap_bindings_action in menu.actions() for menu in menus)


@pytest.mark.parametrize('hotkey_workspace', [{}, {'remote': True}], indirect=True)
def test_compact_entries_are_disabled_after_disconnect(hotkey_workspace):
    window, app, fake_device, _ = hotkey_workspace
    select_state(window, app, fake_device, device(21))
    page = window.virtual_kbm_page
    page.bindings.refresh()
    page.bindings.list.setCurrentItem(page.bindings.rows['0'])
    assert window.mapping_deck.swap_btn.isEnabled() and page.bindings.swap_button.isEnabled()
    select_state(window, app, fake_device, None)
    window.mapping_deck.refresh()
    page.bindings.refresh()
    assert not window.mapping_deck.swap_btn.isEnabled()
    assert not page.bindings.swap_button.isEnabled()
    assert not page.swap_bindings_action.isEnabled()


@pytest.mark.parametrize('hotkey_workspace', [{'remote': True}], indirect=True)
def test_offline_backend_disables_all_entries_on_refresh(hotkey_workspace):
    window, app, fake_device, _ = hotkey_workspace
    select_state(window, app, fake_device, device(21))
    page = window.virtual_kbm_page
    page.bindings.refresh()
    page.bindings.list.setCurrentItem(page.bindings.rows['0'])
    window.client.connected = False
    window.refresh_mappings()
    assert not window.mapping_deck.swap_btn.isEnabled()
    assert not page.bindings.swap_button.isEnabled()
    assert not page.swap_bindings_action.isEnabled()


@pytest.mark.parametrize('hotkey_workspace', ['zh', 'en'], indirect=True)
@pytest.mark.parametrize('width', [960, 1440])
def test_compact_swap_entries_fit_localized_full_workspace(hotkey_workspace, width):
    window, app, fake_device, _ = hotkey_workspace
    window.setStyleSheet(STYLE)
    window.resize(width, 900)
    state = device(21)
    state['axes'][4:6] = [-1., -1.]
    select_state(window, app, fake_device, state)
    window.poll()
    window.navigate(1)
    window.show()
    app.processEvents()
    page = window.stack.currentWidget()
    assert isinstance(page, QScrollArea)
    assert page.horizontalScrollBar().maximum() == 0
    button = window.mapping_deck.swap_btn
    viewport = page.viewport()
    assert button.isVisible() and button.isEnabled()
    assert viewport.rect().contains(button.mapTo(viewport, QPoint(0, 0)))
    assert viewport.rect().contains(button.mapTo(viewport, button.rect().bottomRight()))
    assert button.accessibleName() == ('交换绑定' if get_language() == 'zh' else 'Swap bindings')
    screenshot(window, f'controller-{get_language()}-{width}.png')
    window.navigate(6)
    window.virtual_kbm_page.toggle_bindings_list()
    app.processEvents()
    kbm = window.virtual_kbm_page
    assert kbm.area.horizontalScrollBar().maximum() == 0
    row = kbm.bindings
    row.list.setCurrentItem(row.rows['0'])
    kbm.area.ensureWidgetVisible(row, 0, 12)
    app.processEvents()
    swap = row.swap_button
    assert swap.isVisible() and swap.isEnabled()
    position = swap.mapTo(row, QPoint(0, 0))
    assert position.x() >= 0 and position.x() + swap.width() <= row.width()
    assert row.list.horizontalScrollBar().maximum() == 0
    screenshot(window, f'keyboard-list-{get_language()}-{width}.png')
