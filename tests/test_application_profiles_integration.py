"""Real Agent/Studio contracts with deterministic foregrounds and no OS input."""
import os

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from gamepadstudio.agent import Agent
from gamepadstudio.application_profiles_ui import ApplicationProfilesDialog
from gamepadstudio.i18n import get_language_preference, init_language
from gamepadstudio.mapping_engine import MappingRuntime
from gamepadstudio.studio import Studio
from gamepadstudio.studio_core import ConfigStore, profile_scope
from tests.test_device_scope_ui import device, select, workspace
from tests.test_unified_mapping import Actions, entry


@pytest.fixture
def remote_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr('gamepadstudio.studio.WINDOWS_FEATURES', True)
    monkeypatch.setattr('gamepadstudio.virtual_kbm_ui.WINDOWS_FEATURES', True)
    app = QApplication.instance() or QApplication([])
    previous = get_language_preference()
    init_language('zh')
    state = device(707)
    foreground = {'hwnd': 80, 'pid': 987654, 'executable': r'C:\Games\InfinityNikki.exe'}

    class Device:
        available = []
        def scan(self): pass
        def read(self): return state
        def close(self): pass

    monkeypatch.setattr('gamepadstudio.application_profiles.foreground_application', lambda: dict(foreground))
    monkeypatch.setattr('gamepadstudio.agent.foreground_application', lambda: dict(foreground))
    backend_actions, ui_actions = Actions(), Actions()
    agent = Agent(tmp_path, Device(), backend_actions)
    agent.timer.stop(); agent.scan_timer.stop(); agent.broadcast_timer.stop()
    agent.poll()
    native = agent.store.profiles_for(state, 'gamepad')[0]
    nikki = agent.store.profiles_for(state, 'kbm')[0]
    agent.handle({'command': 'mapping_change', 'change': {
        'op': 'application_profiles', 'settings': {'enabled': True, 'rules': [
            {'executable': foreground['executable'], 'profile': nikki}]}}})

    class Client(QObject):
        event = Signal(dict)
        def __init__(self, root, parent=None):
            super().__init__(parent)
            self.connected = True
            self.status = agent.status()
            self.state = state
        def send(self, command, **kwargs):
            agent.handle({'command': command, **kwargs})
            self.status = agent.status()
            return True
        def close(self): pass

    def request(root, command, **kwargs):
        result = agent.handle({'command': command,
                              **{key: value for key, value in kwargs.items() if key not in ('role', 'timeout')}})
        return {'ok': True, **(result if isinstance(result, dict) else {})}

    monkeypatch.setattr('gamepadstudio.studio.AgentClient', Client)
    monkeypatch.setattr('gamepadstudio.studio.create_actions', lambda: ui_actions)
    monkeypatch.setattr('gamepadstudio.studio.request', request)
    window = Studio(tmp_path, lang='zh')
    window.timer.stop(); window.scan_timer.stop(); window.gallery_timer.stop()
    window.client.status = agent.status()
    window.poll()
    yield window, agent, app, state, foreground, native, nikki, backend_actions, ui_actions
    window.cleanup(); window.hide(); agent.close()
    init_language(previous)


def sync_remote(window, agent, app):
    window.client.status = agent.status()
    window.client.state = agent.state
    window.poll()
    app.processEvents()


def test_remote_automatic_profile_survives_controller_refresh_and_matches_badge(remote_workspace):
    window, agent, app, state, _, native, nikki, _, ui_actions = remote_workspace
    assert agent.config['active_profile'] == nikki
    assert agent.config['controller_profiles'][profile_scope(state)] == native
    for capabilities in ({}, {'rumble': False}, {'available_axes': [0, 1]}):
        state.update(capabilities)
        window.update_controller_ui(state)
        sync_remote(window, agent, app)
        assert window.config['active_profile'] == nikki
        assert window.profile_combo.currentText() == nikki
        assert window.virtual_kbm_page.current_scheme() == nikki
        assert window.virtual_kbm_page.scheme_active_badge.text() == '应用自动生效'
        assert 'InfinityNikki.exe' in window.profile_combo.toolTip()
    persisted = ConfigStore(agent.root)
    assert persisted.data['active_profile'] == nikki
    assert persisted.data['controller_profiles'][profile_scope(state)] == native
    assert not ui_actions.calls


def test_remote_manual_selection_of_same_active_profile_suspends_rule_until_foreground_changes(remote_workspace):
    window, agent, app, state, foreground, _, nikki, _, _ = remote_workspace
    assert agent.application_profile['automatic']
    window.change_profile(nikki)  # The active value still needs an explicit manual override.
    sync_remote(window, agent, app)
    assert not agent.application_profile['automatic']
    assert agent.config['controller_profiles'][profile_scope(state)] == nikki
    assert window.virtual_kbm_page.scheme_active_badge.text() == '✓ 已加载生效'
    agent.update_application_profile(force=True)
    assert not agent.application_profile['automatic']
    foreground.update(hwnd=81, pid=987655)
    agent.update_application_profile(force=True)
    sync_remote(window, agent, app)
    assert agent.application_profile['automatic']
    assert window.virtual_kbm_page.scheme_active_badge.text() == '应用自动生效'


def test_remote_reselecting_same_active_combo_item_is_a_manual_choice(remote_workspace):
    window, agent, app, _, _, _, nikki, _, _ = remote_workspace
    assert window.profile_combo.currentText() == nikki
    assert agent.application_profile['automatic']
    # Qt emits activated for a user choosing the current item even though the
    # text did not change. The visible control must reach the manual contract.
    window.profile_combo.activated.emit(window.profile_combo.currentIndex())
    sync_remote(window, agent, app)
    assert not agent.application_profile['automatic']
    assert window.virtual_kbm_page.scheme_active_badge.text() == '✓ 已加载生效'


def test_remote_unmatched_application_restores_manual_profile_without_ui_disagreement(remote_workspace):
    window, agent, app, state, foreground, native, _, _, _ = remote_workspace
    foreground.update(hwnd=90, pid=123456, executable=r'C:\Tools\Editor.exe')
    agent.update_application_profile(force=True)
    sync_remote(window, agent, app)
    assert agent.config['active_profile'] == native
    assert agent.config['controller_profiles'][profile_scope(state)] == native
    assert window.config['active_profile'] == window.profile_combo.currentText() == native
    assert window.mapping_combo.currentText() == native
    assert window.mapping_active_badge.text() == '已生效'
    assert not window.profile_combo.toolTip()


def test_remote_app_switch_releases_output_and_requires_physical_release_before_new_hold(remote_workspace):
    window, agent, app, state, foreground, native, nikki, backend_actions, ui_actions = remote_workspace
    foreground.update(hwnd=90, pid=123456, executable=r'C:\Tools\Editor.exe')
    agent.update_application_profile(force=True)
    for profile, output in ((native, 'E'), (nikki, 'F')):
        agent.handle({'command': 'mapping_change', 'change': {
            'op': 'binding', 'profile': profile, 'trigger': '0', 'mapping': entry('hold', output)}})
    agent.engine.update(state, agent.config, now=0)
    state['buttons'] = [0]
    agent.engine.update(state, agent.config, now=1)
    agent.engine.update(state, agent.config, now=1.1)
    assert any(backend_actions.keys.values())
    foreground.update(hwnd=80, pid=987654, executable=r'C:\Games\InfinityNikki.exe')
    agent.update_application_profile(force=True)
    assert not any(backend_actions.keys.values())
    agent.engine.update(state, agent.config, now=2)
    agent.engine.update(state, agent.config, now=2.1)
    assert not any(backend_actions.keys.values())
    state['buttons'] = []
    agent.engine.update(state, agent.config, now=3)
    state['buttons'] = [0]
    agent.engine.update(state, agent.config, now=4)
    agent.engine.update(state, agent.config, now=4.1)
    assert backend_actions.keys.get(ord('F')) == 1
    sync_remote(window, agent, app)
    assert window.profile_combo.currentText() == nikki
    assert not ui_actions.calls


def test_application_rule_entries_disable_without_device_in_both_mapping_pages(workspace):
    window, app = workspace
    select(window, app, None)
    assert not window.application_profiles_action.isEnabled()
    assert not window.virtual_kbm_page.application_profiles_action.isEnabled()
    select(window, app, device(101))
    assert window.application_profiles_action.isEnabled()
    assert window.virtual_kbm_page.application_profiles_action.isEnabled()
    select(window, app, None)
    assert not window.application_profiles_action.isEnabled()
    assert not window.virtual_kbm_page.application_profiles_action.isEnabled()


def test_standalone_application_switch_preserves_manual_baseline_and_current_views(workspace, monkeypatch):
    window, app = workspace
    state = device(101)
    select(window, app, state)
    native = window.config['active_profile']
    nikki = window.store.profiles_for(state, 'kbm')[0]
    foreground = {'hwnd': 80, 'pid': 987654, 'executable': r'C:\Games\InfinityNikki.exe'}
    monkeypatch.setattr('gamepadstudio.application_profiles.foreground_application', lambda: dict(foreground))
    assert window.mapping_change({'op': 'application_profiles', 'settings': {'enabled': True, 'rules': [
        {'executable': foreground['executable'], 'profile': nikki}]}})
    window.refresh_application_profile_status()
    assert window.config['active_profile'] == nikki
    assert window.config['controller_profiles'][profile_scope(state)] == native
    assert window.profile_combo.currentText() == nikki
    assert window.virtual_kbm_page.scheme_active_badge.text() == '应用自动生效'
    assert not window.enabled  # Switching profiles cannot resume paused output.
    foreground.update(hwnd=90, pid=123456, executable=r'C:\Tools\Editor.exe')
    assert window.update_application_profile(force=True)
    assert window.config['active_profile'] == window.profile_combo.currentText() == native
    assert window.mapping_combo.currentText() == native
    assert window.mapping_active_badge.text() == '已生效'


def test_standalone_same_active_manual_selection_clears_automatic_badge(workspace, monkeypatch):
    window, app = workspace
    state = device(101)
    select(window, app, state)
    nikki = window.store.profiles_for(state, 'kbm')[0]
    foreground = {'hwnd': 80, 'pid': 987654, 'executable': r'C:\Games\InfinityNikki.exe'}
    monkeypatch.setattr('gamepadstudio.application_profiles.foreground_application', lambda: dict(foreground))
    assert window.mapping_change({'op': 'application_profiles', 'settings': {'enabled': True, 'rules': [
        {'executable': foreground['executable'], 'profile': nikki}]}})
    window.change_profile(nikki)
    assert window.config['controller_profiles'][profile_scope(state)] == nikki
    assert window.virtual_kbm_page.scheme_active_badge.text() == '✓ 已加载生效'
    assert not window.application_profile_status()['automatic']
    assert not window.update_application_profile(force=True)
    assert not window.application_profile_status()['automatic']


def test_standalone_capability_refresh_preserves_automatic_profile_while_workspace_is_foreground(workspace, monkeypatch):
    window, app = workspace
    state = device(101)
    select(window, app, state)
    native = window.config['active_profile']
    nikki = window.store.profiles_for(state, 'kbm')[0]
    foreground = {'hwnd': 80, 'pid': 987654, 'executable': r'C:\Games\InfinityNikki.exe'}
    monkeypatch.setattr('gamepadstudio.application_profiles.foreground_application', lambda: dict(foreground))
    assert window.mapping_change({'op': 'application_profiles', 'settings': {'enabled': True, 'rules': [
        {'executable': foreground['executable'], 'profile': nikki}]}})
    foreground.update(hwnd=90, pid=os.getpid(), executable=r'C:\Python\python.exe')
    for capabilities in ({}, {'rumble': False}, {'available_axes': [0, 1]}):
        state.update(capabilities)
        window.update_controller_ui(state)
        assert not window.update_application_profile(force=True)
        window.refresh_application_profile_status()
        assert window.config['active_profile'] == window.profile_combo.currentText() == nikki
        assert window.config['controller_profiles'][profile_scope(state)] == native
        assert window.virtual_kbm_page.scheme_active_badge.text() == '应用自动生效'
    persisted = ConfigStore(window.store.root)
    assert persisted.data['active_profile'] == nikki
    assert persisted.data['controller_profiles'][profile_scope(state)] == native


@pytest.mark.parametrize('changed_identity', ['different_device', 'same_device_reconnected'])
def test_standalone_real_device_change_restores_its_manual_baseline(workspace, monkeypatch, changed_identity):
    window, app = workspace
    first = device(101)
    select(window, app, first)
    first_native = window.config['active_profile']
    nikki = window.store.profiles_for(first, 'kbm')[0]
    foreground = {'hwnd': 80, 'pid': 987654, 'executable': r'C:\Games\InfinityNikki.exe'}
    monkeypatch.setattr('gamepadstudio.application_profiles.foreground_application', lambda: dict(foreground))
    assert window.mapping_change({'op': 'application_profiles', 'settings': {'enabled': True, 'rules': [
        {'executable': foreground['executable'], 'profile': nikki}]}})
    if changed_identity == 'different_device':
        second = device(202)
    else:
        second = dict(first, instance_id=999)
        assert profile_scope(second) == profile_scope(first)
    select(window, app, second)
    assert window.config['active_profile'] == window.config['controller_profiles'][profile_scope(second)]
    assert window.config['active_profile'] in window.store.profiles_for(second)
    assert window.config['active_profile'] != nikki
    if changed_identity == 'same_device_reconnected':
        assert window.config['active_profile'] == first_native


def test_standalone_modal_draft_freezes_switching_and_cancel_restores_application_flow(workspace, monkeypatch):
    window, app = workspace
    state = device(101)
    select(window, app, state)
    native = window.config['active_profile']
    nikki = window.store.profiles_for(state, 'kbm')[0]
    foreground = {'hwnd': 80, 'pid': 987654, 'executable': r'C:\Games\InfinityNikki.exe'}
    monkeypatch.setattr('gamepadstudio.application_profiles.foreground_application', lambda: dict(foreground))
    settings = {'enabled': True, 'rules': [{'executable': foreground['executable'], 'profile': nikki}]}
    assert window.mapping_change({'op': 'application_profiles', 'settings': settings})
    dialog = ApplicationProfilesDialog(window)
    try:
        dialog.setModal(True)
        dialog.show()
        app.processEvents()
        assert QApplication.activeModalWidget() is dialog
        dialog.enabled_box.setChecked(False)
        dialog.rows[0].profile_combo.setCurrentIndex(0)
        foreground.update(hwnd=90, pid=123456, executable=r'C:\Tools\Editor.exe')
        assert not window.update_application_profile(force=True)
        assert window.config['active_profile'] == nikki
        assert dialog.buttons.button(QDialogButtonBox.Save).isEnabled()
        dialog.reject()
        app.processEvents()
        assert window.store.application_settings(state) == settings
        assert window.update_application_profile(force=True)
        assert window.config['active_profile'] == window.profile_combo.currentText() == native
    finally:
        dialog.reject()


def test_standalone_switch_while_holding_releases_old_key_without_pressing_new_key(workspace, monkeypatch):
    window, app = workspace
    state = device(101)
    select(window, app, state)
    native = window.config['active_profile']
    nikki = window.store.profiles_for(state, 'kbm')[0]
    # Replace the complete output path, including its mouse thread, so this
    # standalone integration check cannot emit input to another Windows app.
    window.engine.close()
    window.actions = Actions()
    window.engine = MappingRuntime(window.actions, window.dispatch, start_mouse=False)
    foreground = {'hwnd': 90, 'pid': 123456, 'executable': r'C:\Tools\Editor.exe'}
    monkeypatch.setattr('gamepadstudio.application_profiles.foreground_application', lambda: dict(foreground))
    for profile, output in ((native, 'E'), (nikki, 'F')):
        assert window.mapping_change({'op': 'binding', 'profile': profile,
                                      'trigger': '0', 'mapping': entry('hold', output)})
    assert window.mapping_change({'op': 'application_profiles', 'settings': {'enabled': True, 'rules': [
        {'executable': r'C:\Games\InfinityNikki.exe', 'profile': nikki}]}})
    window.engine.update(state, window.config, now=0)
    state['buttons'] = [0]
    window.engine.update(state, window.config, now=1)
    window.engine.update(state, window.config, now=1.1)
    assert window.actions.keys.get(ord('E')) == 1
    foreground.update(hwnd=80, pid=987654, executable=r'C:\Games\InfinityNikki.exe')
    assert window.update_application_profile(force=True)
    assert not any(window.actions.keys.values())
    window.engine.update(state, window.config, now=2)
    window.engine.update(state, window.config, now=2.1)
    assert not any(window.actions.keys.values())
    state['buttons'] = []
    window.engine.update(state, window.config, now=3)
    state['buttons'] = [0]
    window.engine.update(state, window.config, now=4)
    window.engine.update(state, window.config, now=4.1)
    assert window.actions.keys.get(ord('F')) == 1
    assert window.profile_combo.currentText() == nikki
