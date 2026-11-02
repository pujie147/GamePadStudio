"""Regression checks for usable fullscreen and restored workspaces."""
import pytest

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QLabel, QMessageBox, QScrollArea

from gamepadstudio.mapping_ui import BindingDialog
from gamepadstudio.studio import Studio, STYLE
from gamepadstudio.kbm_mapper import NIKKI_PROFILE_NAME


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    class Disconnected:
        available = []
        def scan(self): pass
        def read(self): return None
        def close(self): pass

    monkeypatch.setattr('gamepadstudio.studio.Device', Disconnected)
    monkeypatch.setattr(QMessageBox, 'information', lambda *args, **kwargs: pytest.fail('Unexpected informational dialog'))
    app = QApplication.instance() or QApplication([])
    QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    app.setStyleSheet(STYLE)
    window = Studio(tmp_path, standalone=True, lang='zh')
    window.enabled = False
    for timer in (window.timer, window.scan_timer, window.gallery_timer):
        timer.stop()
    window.poll()
    window.show()
    QTest.qWait(20)
    try:
        yield window
    finally:
        window.cleanup()
        window.hide()


def test_fullscreen_shortcuts_restore_the_previous_window_mode(workspace):
    window = workspace
    window.showFullScreen()
    QTest.qWait(20)
    assert window.isFullScreen()
    dialog = QDialog(window)
    dialog.setModal(True)
    dialog.show()
    QTest.qWait(20)
    QTest.keyClick(dialog, Qt.Key_Escape)
    QTest.qWait(20)
    assert not dialog.isVisible() and window.isFullScreen()
    window.activateWindow()
    QTest.qWait(20)
    QTest.keyClick(window, Qt.Key_Escape)
    QTest.qWait(20)
    assert not window.isFullScreen() and not window.isMaximized()
    window.showMaximized()
    QTest.qWait(20)
    QTest.keyClick(window, Qt.Key_F11)
    QTest.qWait(20)
    assert window.isFullScreen()
    QTest.keyClick(window, Qt.Key_F11)
    QTest.qWait(20)
    assert not window.isFullScreen() and window.isMaximized()


def test_pages_fit_after_shrinking_and_keep_navigation_clickable(workspace):
    window = workspace
    for width, height in [(1920, 1080), (960, 640), (1600, 900), (1200, 780)]:
        window.resize(width, height)
        QTest.qWait(25)
        for index in range(7):
            window.navigate(index)
            QTest.qWait(20)
            page = window.stack.currentWidget()
            areas = page.findChildren(QScrollArea)
            if isinstance(page, QScrollArea):
                areas.append(page)
            assert all(area.horizontalScrollBar().maximum() == 0 for area in areas), (width, index)
        for control in window.nav.values():
            assert control.height() >= 40
            assert window.rail.rect().contains(control.geometry())
        assert all(box.height() >= 48 for box, _ in window.mapping_boxes.values() if not box.isHidden())
    window.navigate(1)
    window.select_mapping(2)
    assert window.mapping_deck.selected_trigger == '2'
    assert window.mapping_boxes[2][0].isChecked()
    assert not hasattr(window, 'binding_list')


def test_keyboard_profile_actions_use_the_displayed_preset_offline(workspace, monkeypatch):
    import copy
    window = workspace
    active = window.config['active_profile']
    native_before = copy.deepcopy(window.config['profiles'][active])
    window.navigate(6)
    assert window.virtual_kbm_page.current_scheme() == NIKKI_PROFILE_NAME
    assert window.config['active_profile'] == active
    window.config['profiles'][NIKKI_PROFILE_NAME]['0']['short'] = {'action': 'hold', 'value': 'F11'}
    monkeypatch.setattr(QInputDialog, 'getText', lambda *args: ('暖暖副本', True))
    window.duplicate_profile('kbm')
    assert window.config['profiles']['暖暖副本'] == window.config['profiles'][NIKKI_PROFILE_NAME]
    assert window.config['profile_modes']['暖暖副本'] == 'kbm'
    window.change_profile(active)
    window.virtual_kbm_page.scheme_combo.blockSignals(True)
    window.virtual_kbm_page.scheme_combo.setCurrentText(NIKKI_PROFILE_NAME)
    window.virtual_kbm_page.scheme_combo.blockSignals(False)
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.Yes)
    window.reset_profile()
    assert window.config['profiles'][NIKKI_PROFILE_NAME]['0']['short']['value'] != 'F11'
    assert window.config['profiles'][active] == native_before
    assert window.config['active_profile'] == active


def test_combination_cards_and_controller_selection_follow_the_displayed_gamepad_profile(workspace, monkeypatch):
    window = workspace
    # This fixture intentionally moves the sample macro into the offline input
    # device's library; profiles belonging to another device stay hidden.
    window.config['profile_devices']['动作与格斗宏'] = 'offline:xinput'
    window.config['profile_families']['动作与格斗宏'] = 'generic'
    window.change_profile('动作与格斗宏')
    window.navigate(1)
    edits = []
    monkeypatch.setattr(window, 'edit_mapping', lambda *args, **kwargs: edits.append((args, kwargs)))
    deck = window.mapping_deck
    QTest.mouseClick(deck.combo_buttons['9+10'], Qt.LeftButton)
    assert deck.selected_trigger == '9+10'
    assert window.mapping_art.selected_buttons == {9, 10}
    assert {key for key, (box, _) in window.mapping_boxes.items() if box.isChecked()} == {9, 10}
    QTest.mouseClick(deck.short_card, Qt.LeftButton)
    assert edits[-1] == (('9+10',), {'profile': '动作与格斗宏', 'mode': 'gamepad', 'gesture': 'short'})
    window.refresh_mappings()
    assert deck.selected_trigger == '9+10'
    window.update_controller_ui(None)
    assert deck.selected_trigger == '9+10'
    deck.clear_btn.click()
    assert all(action['action'] == 'none' for action in window.config['profiles']['动作与格斗宏']['9+10'].values())
    assert deck.selected_trigger == '9+10'
    assert '9+10' not in deck.combo_buttons


def test_a_new_combination_lands_on_the_cards_that_give_it_an_action(workspace, monkeypatch):
    window = workspace
    window.navigate(1)
    monkeypatch.setattr(BindingDialog, 'exec', lambda dialog: QDialog.Accepted)
    deck = window.mapping_deck
    deck.combo_btn.click()
    assert deck.selected_trigger == '9+10'
    assert '9+10' in deck.combo_buttons
    assert any('新组合已创建' in label.text() for label in deck.findChildren(QLabel))
    assert all(window.config['profiles'][deck._profile()]['9+10'][g]['action'] == 'none'
               for g in ('short', 'long'))


def test_deleting_displayed_gamepad_profile_keeps_active_keyboard_profile(workspace, monkeypatch):
    window = workspace
    window.navigate(1)
    # Retain a native fallback for this input device while deleting the visible
    # native preset with the keyboard preset active.
    assert window.mapping_change({'op': 'create', 'profile': '离线手柄副本', 'mode': 'gamepad',
                                  'source': window.current_gamepad_profile()})
    window.change_profile(NIKKI_PROFILE_NAME)
    displayed = window.current_gamepad_profile()
    keyboard_before = window.config['profiles'][NIKKI_PROFILE_NAME].copy()
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.Yes)
    window.delete_profile()
    assert displayed not in window.config['profiles']
    assert window.config['active_profile'] == NIKKI_PROFILE_NAME
    assert window.config['profiles'][NIKKI_PROFILE_NAME] == keyboard_before
