"""Visual input-to-output cards for the controller mapping workspace."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QRect, QSize
from PySide6.QtWidgets import (QWidget, QLabel, QPushButton, QVBoxLayout,
                              QHBoxLayout, QLayout, QSizePolicy)

from .glass import TOKENS
from .i18n import tr
from .controller_glyphs import display_parts, make_token_label
from .mapping_engine import (canonical_trigger, trigger_label, effective_mappings,
                             profile_family, binding_label)
from .response_curves import curve_capabilities


class FlowLayout(QLayout):
    """A natural-width token row which wraps within its available width."""
    def __init__(self, parent=None, spacing=5):
        super().__init__(parent)
        self.items = []
        self.offered_width = 0
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item):
        self.items.append(item)

    def count(self):
        return len(self.items)

    def itemAt(self, index):
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):
        return self.items.pop(index) if 0 <= index < len(self.items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self.offered_width = max(0, rect.width())
        self._arrange(rect, False)

    def sizeHint(self):
        flat = self.minimumSize()
        if self.offered_width <= 0:
            return flat
        return QSize(flat.width(), self.heightForWidth(self.offered_width))

    def minimumSize(self):
        size = QSize()
        for item in self.items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect, measure):
        x, y, line_height = rect.x(), rect.y(), 0
        for item in self.items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            width = min(hint.width(), max(1, rect.width()))
            if x > rect.x() and x + width > rect.right() + 1:
                x, y, line_height = rect.x(), y + line_height + self.spacing(), 0
            if not measure:
                item.setGeometry(QRect(x, y, width, hint.height()))
            x += width + self.spacing()
            line_height = max(line_height, hint.height())
        return y - rect.y() + line_height


def _clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout():
            _clear(item.layout())


def _chip(text, parent, accent=False, key=None):
    chip = QLabel(text, parent)
    chip.setAlignment(Qt.AlignCenter)
    chip.setAttribute(Qt.WA_TransparentForMouseEvents)
    chip.setProperty('keyToken', key)
    chip.setStyleSheet(
        f"color: {TOKENS['ink'] if accent else TOKENS['ink_2']}; "
        "background: transparent; border: none; padding: 0 1px; "
        "min-height: 18px; font-weight: 600; font-size: 13px;")
    chip.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    return chip


def _text(text, parent, muted=True):
    label = QLabel(text, parent)
    label.setAttribute(Qt.WA_TransparentForMouseEvents)
    label.setStyleSheet(f"color: {TOKENS['ink_3'] if muted else TOKENS['ink']}; font-size: 11px;")
    return label


def _add_keys(layout, trigger, family, parent, accent=False):
    try:
        keys = display_parts(trigger)
    except ValueError:
        layout.addWidget(_chip(str(trigger), parent, accent))
        return
    for i, key in enumerate(keys):
        if i:
            layout.addWidget(_text('+', parent))
        token = make_token_label(
            key, family, parent=parent,
            color=TOKENS['ink'] if accent else TOKENS['ink_2'], size=13)
        token.setAttribute(Qt.WA_TransparentForMouseEvents)
        token.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        layout.addWidget(token)


class ActionCard(QPushButton):
    """One keyboard-accessible gesture card with compact button symbols."""
    def __init__(self, gesture, parent=None):
        super().__init__(parent)
        self.gesture = gesture
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumHeight(84)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 10)
        layout.setSpacing(7)
        top = QHBoxLayout()
        self.title = _text('', self, False)
        self.title.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {TOKENS['ink_2']};")
        top.addWidget(self.title)
        top.addStretch()
        self.edit_hint = _text(tr('编辑映射') + '  ›', self)
        top.addWidget(self.edit_hint)
        layout.addLayout(top)
        self.output = FlowLayout(spacing=5)
        layout.addLayout(self.output)
        self.active = False
        self._style()

    def _style(self):
        color = TOKENS['border_acc'] if self.active else TOKENS['border']
        bg = TOKENS['accent_bg'] if self.active else TOKENS['surface']
        self.setStyleSheet(
            f"QPushButton {{ text-align: left; background: {bg}; border: 1px solid {color}; border-radius: 9px; padding: 0; min-height: 82px; }}"
            f"QPushButton:hover {{ border-color: {TOKENS['border_hi']}; background: {TOKENS['elevated']}; }}"
            f"QPushButton:focus {{ border-color: {TOKENS['accent']}; }}")

    def set_active(self, active):
        if self.active != active:
            self.active = active
            self._style()

    def set_binding(self, binding, trigger, family, threshold):
        _clear(self.output)
        title = tr('短按') if self.gesture == 'short' else tr('长按') + f' · {threshold:g} ' + tr('秒')
        self.title.setText(title)
        self.edit_hint.setText(tr('编辑映射') + '  ›')
        _add_keys(self.output, trigger, family, self)
        self.output.addWidget(_text('→', self))
        action = binding.get('action', 'none')
        if action in ('gamepad_button', 'gamepad_chord', 'gamepad_turbo'):
            _add_keys(self.output, binding.get('value', ''), family, self, True)
            if action == 'gamepad_turbo':
                self.output.addWidget(_chip(f"{binding.get('rate_hz', 15):g} Hz", self, True))
            elif action == 'gamepad_chord':
                self.output.addWidget(_text(tr('同时触发'), self))
        elif action == 'gamepad_macro':
            sequence = binding.get('sequence', [])
            for index, step in enumerate(sequence[:8]):
                if index:
                    self.output.addWidget(_text('›', self))
                if isinstance(step, dict):
                    kind = step.get('action', step.get('type', ''))
                    value = step.get('value', step.get('button', step.get('buttons', '')))
                    if isinstance(value, list):
                        value = '+'.join(map(str, value))
                    if value:
                        if kind in ('down', 'press', 'button_down') or step.get('down') is True:
                            self.output.addWidget(_text(tr('按下'), self))
                        elif kind in ('up', 'release', 'button_up') or step.get('down') is False:
                            self.output.addWidget(_text(tr('松开'), self))
                        _add_keys(self.output, value, family, self, True)
                    else:
                        wait = step.get('delay_ms', step.get('duration_ms', step.get('delay', step.get('duration', 0))))
                        self.output.addWidget(_chip(str(wait) + ' ms', self))
                else:
                    _add_keys(self.output, step, family, self, True)
            if len(sequence) > 8:
                self.output.addWidget(_chip(f'+{len(sequence) - 8}', self))
            if not sequence:
                self.output.addWidget(_text(tr('未设置'), self))
        elif action == 'none':
            self.output.addWidget(_text(tr('原始输入') if self.gesture == 'short' and '+' not in trigger else tr('未设置'), self))
        else:
            self.output.addWidget(_chip(tr(binding_label(binding, family)), self, action != 'suppress'))
        self.setAccessibleName(trigger_label(trigger, family) + ' · ' + title + ' · ' + binding_label(binding, family))
        self.setToolTip(self.accessibleName())
        self.updateGeometry()


class ComboButton(QPushButton):
    """Use the button symbols to size the combination's shared hit target."""
    def sizeHint(self):
        return self.layout().sizeHint() if self.layout() else super().sizeHint()

    def minimumSizeHint(self):
        return self.sizeHint()


class MappingDeck(QWidget):
    """Selected input, gesture cards and a compact collection of combinations."""
    trigger_selected = Signal(str)

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.selected_trigger = '0'
        self.active = set()
        self._feedback_data = {}
        self.combo_buttons = {}
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)
        header = QHBoxLayout()
        self.input_heading = _text(tr('当前按键'), self)
        header.addWidget(self.input_heading)
        header.addStretch()
        self.swap_btn = QPushButton(tr('交换'))
        self.swap_btn.setCursor(Qt.PointingHandCursor)
        self.swap_btn.setStyleSheet(f"font-size: 11px; color: {TOKENS['ink_2']}; padding: 2px 6px; min-height: 22px; border: none;")
        self.swap_btn.setAccessibleName(tr('交换绑定'))
        self.swap_btn.setToolTip(tr('与另一输入交换完整映射'))
        self.swap_btn.clicked.connect(self.swap)
        header.addWidget(self.swap_btn)
        self.clear_btn = QPushButton(tr('清除映射'))
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.setStyleSheet(f"font-size: 11px; color: {TOKENS['ink_3']}; padding: 2px 6px; min-height: 22px; border: none;")
        self.clear_btn.clicked.connect(self.clear)
        header.addWidget(self.clear_btn)
        layout.addLayout(header)
        self.input_keys = FlowLayout()
        layout.addLayout(self.input_keys)
        # Response settings are directly beside the input they affect.
        self.curve_controls = QWidget(self)
        self.curve_layout = FlowLayout(self.curve_controls, spacing=6)
        self.curve_buttons = {}
        for kind, title in (('trigger', '扳机输入曲线'), ('rumble', '双马达振动曲线'),
                            ('trigger_rumble', '扳机振动曲线')):
            control = QPushButton(tr(title), self.curve_controls)
            control.setObjectName('pill')
            control.setCursor(Qt.PointingHandCursor)
            control.setStyleSheet('font-size: 12px; padding: 5px 10px; min-height: 24px;')
            control.clicked.connect(lambda checked=False, kind=kind: self.open_curve(kind))
            self.curve_layout.addWidget(control)
            self.curve_buttons[kind] = control
        layout.addWidget(self.curve_controls)
        self.short_card = ActionCard('short', self)
        self.long_card = ActionCard('long', self)
        self.short_card.clicked.connect(self.edit)
        self.long_card.clicked.connect(self.edit)
        layout.addWidget(self.short_card)
        layout.addWidget(self.long_card)
        combo_header = QHBoxLayout()
        self.combo_heading = _text(tr('组合映射'), self, False)
        combo_header.addWidget(self.combo_heading)
        combo_header.addStretch()
        self.combo_btn = QPushButton('+ ' + tr('添加组合'))
        self.combo_btn.setCursor(Qt.PointingHandCursor)
        self.combo_btn.setStyleSheet(f"font-size: 11px; color: {TOKENS['ink_2']}; padding: 2px 6px; min-height: 22px;")
        self.combo_btn.clicked.connect(self.add)
        combo_header.addWidget(self.combo_btn)
        layout.addLayout(combo_header)
        self.combo_layout = FlowLayout(spacing=6)
        layout.addLayout(self.combo_layout)
        self.refresh()

    def _profile(self):
        return self.owner.current_gamepad_profile()

    def set_trigger(self, trigger):
        self.selected_trigger = canonical_trigger(trigger)
        self.refresh()

    def _choose_combo(self, trigger):
        self.set_trigger(trigger)
        self.trigger_selected.emit(trigger)

    def refresh(self):
        profile = self._profile()
        config = self.owner.config
        state = getattr(self.owner, 'snapshot', None)
        family = profile_family(config, state, profile)
        mappings = effective_mappings(config, state, profile)
        entry = mappings.get(self.selected_trigger, {})
        _clear(self.input_keys)
        self.input_heading.setText(tr('组合输入') if '+' in self.selected_trigger else tr('当前按键'))
        _add_keys(self.input_keys, self.selected_trigger, family, self, True)
        from .studio_core import device_config
        threshold = entry.get('long_press', device_config(config, state).get('long_press', .65))
        self.short_card.set_binding(entry.get('short', {}), self.selected_trigger, family, threshold)
        self.long_card.set_binding(entry.get('long', {}), self.selected_trigger, family, threshold)
        self.refresh_curve_controls()
        self.clear_btn.setEnabled(any(entry.get(g, {}).get('action', 'none') != 'none' for g in ('short', 'long')))
        self.clear_btn.setText(tr('清除映射'))
        from .mapping_swap_ui import can_swap_bindings
        self.swap_btn.setEnabled(can_swap_bindings(self.owner, profile, self.selected_trigger))
        self.swap_btn.setText(tr('交换'))
        self.combo_heading.setText(tr('组合映射'))
        self.combo_btn.setText('+ ' + tr('添加组合'))
        _clear(self.combo_layout)
        self.combo_buttons = {}
        for raw_trigger, mapping in mappings.items():
            if '+' not in raw_trigger or all(mapping.get(g, {}).get('action', 'none') == 'none' for g in ('short', 'long')):
                continue
            trigger = canonical_trigger(raw_trigger)
            button = ComboButton(self)
            button.setCursor(Qt.PointingHandCursor)
            button.setCheckable(True)
            button.setChecked(trigger == self.selected_trigger)
            button.setProperty('mappingTrigger', trigger)
            button.setAccessibleName(trigger_label(trigger, family))
            button.setToolTip(trigger_label(trigger, family))
            row = QHBoxLayout(button)
            row.setContentsMargins(8, 4, 8, 4)
            row.setSpacing(4)
            _add_keys(row, trigger, family, button)
            button._base_style = (
                f"QPushButton {{ padding: 0; min-height: 0; border: 1px solid {TOKENS['border']}; border-radius: 8px; background: {TOKENS['base']}; }}"
                f"QPushButton:checked {{ border-color: {TOKENS['border_acc']}; background: {TOKENS['accent_bg']}; }}"
                f"QPushButton:hover, QPushButton:focus {{ border-color: {TOKENS['accent']}; }}")
            button.setStyleSheet(button._base_style)
            button.clicked.connect(lambda checked=False, key=trigger: self._choose_combo(key))
            self.combo_layout.addWidget(button)
            self.combo_buttons[trigger] = button
        if not self.combo_buttons:
            self.combo_layout.addWidget(_text(tr('暂无组合映射'), self))
        self.updateGeometry()
        self.feedback(self._feedback_data)

    def curve_channel(self, kind):
        if kind == 'rumble':
            return None
        members = set(self.selected_trigger.split('+'))
        if 'RT' in members and 'LT' not in members:
            return 'right'
        if 'LT' in members:
            return 'left'
        return None

    def refresh_curve_controls(self):
        state = getattr(self.owner, 'snapshot', None)
        capabilities = curve_capabilities(state)
        can_open = callable(getattr(self.owner, 'open_curve_editor', None))
        family = profile_family(self.owner.config, state, self._profile())
        any_visible = False
        for kind, title in (('trigger', '扳机输入曲线'), ('rumble', '双马达振动曲线'),
                            ('trigger_rumble', '扳机振动曲线')):
            supported = bool(capabilities['trigger_axes']) if kind == 'trigger' else capabilities[kind]
            control = self.curve_buttons[kind]
            visible = can_open and supported
            control.setVisible(visible)
            control.setEnabled(visible)
            channel = self.curve_channel(kind)
            if kind == 'trigger' and ((channel == 'left' and 4 not in capabilities['trigger_axes']) or
                                      (channel == 'right' and 5 not in capabilities['trigger_axes'])):
                channel = None
            prefix = trigger_label('LT' if channel == 'left' else 'RT', family) + ' · ' if channel else ''
            control.setText(prefix + tr(title))
            control.setAccessibleName(control.text())
            control.setToolTip(control.text())
            any_visible |= visible
        self.curve_controls.setVisible(any_visible)
        self.curve_controls.updateGeometry()

    def open_curve(self, kind):
        # Recheck at the time of the click; capability metadata can update live.
        self.refresh_curve_controls()
        if not self.curve_buttons[kind].isHidden():
            self.owner.open_curve_editor(kind, channel=self.curve_channel(kind))

    def edit(self):
        self.owner.edit_mapping(self.selected_trigger, profile=self._profile(), mode='gamepad')

    def add(self):
        self.owner.edit_mapping('9+10', new=True, profile=self._profile(), mode='gamepad')

    def clear(self):
        self.owner.mapping_change({'op': 'unbind', 'trigger': self.selected_trigger, 'profile': self._profile()})

    def swap(self):
        from .mapping_swap_ui import can_swap_bindings
        if can_swap_bindings(self.owner, self._profile(), self.selected_trigger):
            self.owner.open_mapping_swap(self._profile(), self.selected_trigger)

    def feedback(self, data):
        self._feedback_data = data
        displayed_active = self._profile() == self.owner.config.get('active_profile')
        self.active = set(data.get('active', [])) if displayed_active else set()
        selected_active = self.selected_trigger in self.active
        gesture = next((event.get('gesture') for event in reversed(data.get('events', []))
                        if event.get('trigger') == self.selected_trigger), None)
        self.short_card.set_active(selected_active and gesture != 'long')
        self.long_card.set_active(selected_active and gesture == 'long')
        for trigger, button in self.combo_buttons.items():
            active = trigger in self.active
            if button.property('activeMapping') != active:
                button.setProperty('activeMapping', active)
                button.setStyleSheet(button._base_style + (
                    f" QPushButton {{ border-color: {TOKENS['accent']}; }}" if active else ''))
