"""The controller visual deck routes real triggers, gestures and profile state."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from gamepadstudio.mapping_deck import FlowLayout, MappingDeck
from gamepadstudio.studio_core import default_config
from gamepadstudio.kbm_mapper import NIKKI_PROFILE_NAME


class Owner:
    def __init__(self, root):
        self.config = default_config(root)
        self.snapshot = {'family': 'xbox'}
        self.profile = '主机体验'
        self.config['profiles'][self.profile] = {
            '0': {'short': {'action': 'gamepad_button', 'value': '1'},
                  'long': {'action': 'gamepad_turbo', 'value': '2', 'rate_hz': 18}},
            '9+10': {'short': {'action': 'gamepad_chord', 'value': '2+3'},
                     'long': {'action': 'none'}},
            '0+1+2+3': {'short': {'action': 'gamepad_chord', 'value': '9+10+LT+RT'},
                        'long': {'action': 'none'}},
            '1+2': {'short': {'action': 'none'}, 'long': {'action': 'none'}},
        }
        self.edits = []
        self.changes = []

    def current_gamepad_profile(self):
        return self.profile

    def edit_mapping(self, trigger, **kwargs):
        self.edits.append((trigger, kwargs))

    def mapping_change(self, change):
        self.changes.append(change)
        self.config['profiles'][change['profile']][change['trigger']] = {
            'short': {'action': 'none'}, 'long': {'action': 'none'}}


def app():
    return QApplication.instance() or QApplication([])


def flush():
    app().processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def key_tokens(widget):
    return [label.property('keyToken') for label in widget.findChildren(QLabel)
            if label.property('keyToken') is not None]


def test_combo_chips_use_canonical_triggers_and_edit_current_profile(tmp_path):
    app()
    owner = Owner(tmp_path)
    deck = MappingDeck(owner)
    selected = []
    deck.trigger_selected.connect(selected.append)
    assert set(deck.combo_buttons) == {'9+10', '0+1+2+3'}
    deck.combo_buttons['9+10'].click()
    flush()
    assert selected == ['9+10']
    assert deck.selected_trigger == '9+10'
    assert key_tokens(deck.short_card) == ['9', '10', '2', '3']
    deck.long_card.click()
    assert owner.edits[-1] == ('9+10', {'profile': '主机体验', 'mode': 'gamepad'})
    deck.combo_btn.click()
    assert owner.edits[-1] == ('9+10', {'new': True, 'profile': '主机体验', 'mode': 'gamepad'})
    deck.set_trigger('RB+LB')
    assert selected == ['9+10']  # External synchronisation does not loop back.
    assert deck.selected_trigger == '9+10'
    deck.refresh()
    assert deck.selected_trigger == '9+10'
    deck.clear_btn.click()
    deck.refresh()
    assert owner.changes[-1] == {'op': 'unbind', 'trigger': '9+10', 'profile': '主机体验'}
    assert '9+10' not in deck.combo_buttons
    assert deck.selected_trigger == '9+10'
    assert not deck.clear_btn.isEnabled()


def test_feedback_cannot_light_an_inactive_profile(tmp_path):
    app()
    owner = Owner(tmp_path)
    deck = MappingDeck(owner)
    deck.set_trigger('0')
    data = {'active': ['0', '9+10'], 'events': [{'trigger': '0', 'gesture': 'long'}]}
    deck.feedback(data)
    assert deck.long_card.active and not deck.short_card.active
    assert deck.combo_buttons['9+10'].property('activeMapping')
    deck.refresh()
    assert deck.long_card.active and not deck.short_card.active
    owner.config['active_profile'] = NIKKI_PROFILE_NAME
    deck.feedback(data)
    assert not deck.long_card.active and not deck.short_card.active
    assert not deck.combo_buttons['9+10'].property('activeMapping')
    owner.config['active_profile'] = owner.profile
    deck.feedback({'active': []})
    assert not deck.long_card.active


def test_modifier_appears_first_without_changing_edit_target(tmp_path):
    app()
    owner = Owner(tmp_path)
    owner.config['profiles'][owner.profile]['0+9'] = {
        'short': {'action': 'gamepad_button', 'value': '0'},
        'long': {'action': 'none'}}
    deck = MappingDeck(owner)
    deck.set_trigger('LB+A')
    flush()
    assert deck.selected_trigger == '0+9'
    assert key_tokens(deck.short_card) == ['9', '0', '0']
    assert key_tokens(deck.combo_buttons['0+9']) == ['9', '0']
    deck.short_card.click()
    assert owner.edits[-1] == ('0+9', {'profile': '主机体验', 'mode': 'gamepad'})


def test_four_key_input_and_output_wrap_within_narrow_deck(tmp_path):
    app()
    window = QWidget()
    layout = QVBoxLayout(window)
    owner = Owner(tmp_path)
    deck = MappingDeck(owner)
    layout.addWidget(deck)
    layout.addStretch()
    deck.set_trigger('0+1+2+3')
    window.resize(350, 560)
    window.show()
    flush()
    try:
        assert key_tokens(deck.short_card) == ['0', '1', '2', '3', '9', '10', 'LT', 'RT']
        assert deck.width() <= 350
        assert deck.short_card.height() >= 84
        for child in deck.short_card.findChildren(QLabel):
            corner = child.mapTo(deck.short_card, child.rect().bottomRight())
            assert corner.x() < deck.short_card.width()
        for button in deck.combo_buttons.values():
            assert button.width() >= button.layout().sizeHint().width()
            for child in button.findChildren(QLabel):
                corner = child.mapTo(button, child.rect().bottomRight())
                assert corner.x() < button.width()
        assert deck.height() < 380
    finally:
        window.hide()


def test_cards_show_turbo_rate_and_macro_steps_as_widgets(tmp_path):
    app()
    owner = Owner(tmp_path)
    deck = MappingDeck(owner)
    deck.set_trigger('0')
    flush()
    assert any(label.text() == '18 Hz' for label in deck.long_card.findChildren(QLabel))
    owner.config['profiles'][owner.profile]['0']['short'] = {
        'action': 'gamepad_macro',
        'sequence': [{'value': '0'}, {'delay_ms': 80}, {'value': '1+2'}]}
    deck.refresh()
    flush()
    assert key_tokens(deck.short_card) == ['0', '0', '1', '2']
    assert any(label.text() == '80 ms' for label in deck.short_card.findChildren(QLabel))
    assert all('{' not in label.text() for label in deck.short_card.findChildren(QLabel))


def test_curve_controls_wrap_and_recheck_actual_capabilities(tmp_path):
    app()
    owner = Owner(tmp_path)
    owner.snapshot = {'family': 'dualsense', 'available_axes': list(range(6)),
                      'rumble': True, 'trigger_rumble': True}
    owner.curve_calls = []
    owner.open_curve_editor = lambda kind, channel=None: owner.curve_calls.append((kind, channel))
    window = QWidget()
    layout = QVBoxLayout(window)
    deck = MappingDeck(owner)
    layout.addWidget(deck)
    deck.set_trigger('R2')
    window.resize(350, 650)
    window.show()
    flush()
    try:
        assert all(not control.isHidden() for control in deck.curve_buttons.values())
        for control in deck.curve_buttons.values():
            assert control.geometry().right() < deck.curve_controls.width()
        deck.curve_buttons['trigger'].click()
        assert owner.curve_calls == [('trigger', 'right')]
        owner.snapshot = {'family': 'switch', 'available_axes': list(range(6)), 'rumble': True}
        deck.refresh()
        assert deck.curve_buttons['trigger'].isHidden()
        assert deck.curve_buttons['trigger_rumble'].isHidden()
        deck.open_curve('trigger')
        assert owner.curve_calls == [('trigger', 'right')]
    finally:
        window.hide()


def test_flow_layout_asks_for_every_row_it_wraps():
    """A wrapping row must report its wrapped height, not the height of one row."""
    app = QApplication.instance() or QApplication([])
    host = QWidget()
    flow = FlowLayout(spacing=5)
    host.setLayout(flow)
    for _ in range(10):
        chip = QWidget()
        chip.setFixedSize(62, 28)
        flow.addWidget(chip)
    assert flow.sizeHint().height() == 28          # no width offered yet: still one row
    host.resize(700, 600)
    host.show()
    app.processEvents()
    assert flow.sizeHint().height() == 28          # 700px fits every chip on one row
    host.resize(200, 600)
    app.processEvents()
    wrapped = flow.heightForWidth(flow.geometry().width())
    assert wrapped > 28
    assert flow.sizeHint().height() == wrapped     # the same width drives both contracts
    assert flow.minimumSize().height() == 28       # and it may still shrink to a single row
    host.hide()
