"""Each gesture card opens a second-level page that owns only its own content."""
import os

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from gamepadstudio.mapping_ui import BindingDialog
from tests.mapping_fixtures import MappingOwner

BINDING = {'short': {'action': 'gamepad_button', 'value': '2'},
           'long': {'action': 'gamepad_macro', 'sequence': [{'buttons': ['9'], 'hold_ms': 50, 'gap_ms': 0}],
                    'interrupt': False, 'loop': False},
           'long_press': 0.8}


@pytest.fixture
def owner(tmp_path):
    QApplication.instance() or QApplication([])
    owner = MappingOwner(tmp_path)
    yield owner
    owner.close()


def build(owner, trigger='0', mapping=None, new=False, gesture=None):
    dialog = BindingDialog(owner, trigger, mapping if mapping is not None else dict(BINDING),
                           new=new, mode='gamepad', gesture=gesture)
    dialog.timer.stop()
    return dialog


def test_short_press_page_shows_only_the_short_press(owner):
    dialog = build(owner, gesture='short')
    assert dialog.action_boxes['short'].isHidden() is False
    assert dialog.action_boxes['long'].isHidden()
    assert dialog.timing_box.isHidden()
    assert dialog.source_group.isHidden()
    assert dialog.source_summary.isVisibleTo(dialog.body)
    assert '当前按键' in dialog.source_summary.text()
    assert dialog.windowTitle() == '编辑短按映射'


def test_long_press_page_owns_the_threshold_and_nothing_else(owner):
    dialog = build(owner, gesture='long')
    assert dialog.action_boxes['long'].isHidden() is False
    assert dialog.action_boxes['short'].isHidden()
    assert dialog.timing_box.isHidden() is False
    assert dialog.source_group.isHidden()
    assert dialog.windowTitle() == '编辑长按映射'


def test_combination_page_defines_the_input_only(owner):
    dialog = build(owner, '9+10', mapping={}, new=True, gesture='combo')
    assert dialog.source_group.isHidden() is False
    assert dialog.action_boxes['short'].isHidden() and dialog.action_boxes['long'].isHidden()
    assert dialog.timing_box.isHidden()
    assert dialog.windowTitle() == '添加组合'
    assert [combo.currentData() for combo in dialog.inputs[:2]] == ['9', '10']


def test_a_combination_needs_a_second_button(owner):
    dialog = build(owner, '9+10', mapping={}, new=True, gesture='combo')
    dialog.inputs[1].setCurrentIndex(0)
    dialog.validate()
    assert '至少' in dialog.error.text()
    assert dialog.result() != QDialog.Accepted


def test_a_scoped_page_cannot_rewrite_the_gesture_it_does_not_show(owner):
    for gesture in ('short', 'long', 'combo'):
        dialog = build(owner, gesture=gesture)
        value = dialog.value()
        assert value['short'] == BINDING['short'], gesture
        assert value['long'] == BINDING['long'], gesture
        assert value['long_press'] == 0.8, gesture


def test_the_unscoped_editor_still_carries_every_section(owner):
    """Other entry points (识别按键, 虚拟键鼠列表) keep the combined page."""
    dialog = build(owner)
    assert not any(box.isHidden() for box in dialog.action_boxes.values())
    assert not dialog.timing_box.isHidden()
    assert not dialog.source_group.isHidden()
    assert dialog.source_summary.isHidden()
