"""Shared mapping editors and live binding lists."""
import copy
import sys
import time
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QColor
from PySide6.QtWidgets import (QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QLineEdit, QDialogButtonBox, QListWidget, QListWidgetItem,
    QFormLayout, QFileDialog, QMessageBox, QDoubleSpinBox, QGridLayout, QSpinBox, QGroupBox,
    QCheckBox)
from .mapping_engine import (INPUTS, GAMEPAD_TARGETS, InputNormalizer, canonical_trigger, trigger_label,
                             binding_label, validate_mappings, effective_mappings, profile_family, input_sources)
from .mapping_deck import FlowLayout
from .studio_core import ACTION_NAMES, device_config
from .glass import TOKENS
from .touch_gestures import TouchGestureRecognizer, touch_sources
from .i18n import tr

MACRO_DELAY_PRESET_MS = 100
MACRO_STEP_LIMIT = 64
MACRO_HOLD_LIMITS = (10, 500)


def _macro_label(text, parent=None, muted=False):
    label = QLabel(text, parent)
    label.setStyleSheet(f"color: {TOKENS['ink_3'] if muted else TOKENS['ink_2']}; font-size: 11px; font-weight: 600;")
    return label


def _macro_flat(text, parent=None):
    button = QPushButton(text, parent)
    button.setFlat(True)
    button.setCursor(Qt.PointingHandCursor)
    button.setFixedSize(20, 20)
    button.setStyleSheet(
        f"QPushButton {{ background: transparent; border: none; color: {TOKENS['ink_3']}; "
        f"font-size: 12px; font-weight: 700; }}"
        f"QPushButton:hover {{ color: {TOKENS['red']}; }}")
    return button


def _drain(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout():
            _drain(item.layout())


def _ms(value, fallback):
    # Sequence entries come from a persisted profile, so tolerate junk here.
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return fallback


class GamepadTargetSelector(QWidget):
    """Tactile target button and chord selector for pure Gamepad-to-Gamepad actions."""

    def __init__(self, family='generic', parent=None, gesture='short'):
        super().__init__(parent)
        self.family = family
        self.gesture = gesture
        self.mode = 'single'  # 'single', 'chord', 'turbo', 'macro'
        self.selected = {'0'}
        self.turbo_rate = 15
        self.macro_steps = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        # 2-row button grid
        self.grid = QGridLayout()
        self.grid.setSpacing(6)
        self.buttons = {}

        row1 = ['0', '1', '2', '3', '9', '10', 'LT', 'RT', '15']
        row2 = ['11', '12', '13', '14', '7', '8', '4', '6', '5']

        for col, k in enumerate(row1):
            btn = QPushButton(self._clean_label(k))
            btn.setCheckable(True)
            btn.setFixedSize(54, 30)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda chk=False, key=k: self._on_btn_clicked(key))
            self.grid.addWidget(btn, 0, col)
            self.buttons[k] = btn

        for col, k in enumerate(row2):
            btn = QPushButton(self._clean_label(k))
            btn.setCheckable(True)
            btn.setFixedSize(54, 30)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda chk=False, key=k: self._on_btn_clicked(key))
            self.grid.addWidget(btn, 1, col)
            self.buttons[k] = btn

        layout.addLayout(self.grid)

        # Status & Turbo Controls
        self.info_row = QHBoxLayout()
        self.info_row.setSpacing(10)
        self.info_label = QLabel(self._summary_text())
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet(f"color: {TOKENS['ink_2']}; font-size: 11.5px; font-weight: 600;")
        self.info_row.addWidget(self.info_label, 1)

        self.turbo_box = QWidget()
        tb_l = QHBoxLayout(self.turbo_box)
        tb_l.setContentsMargins(0, 0, 0, 0)
        tb_l.setSpacing(6)
        tb_title = QLabel('连发速率:')
        tb_title.setStyleSheet(f"color: {TOKENS['ink_2']}; font-size: 11px;")
        tb_l.addWidget(tb_title)
        self.turbo_spin = QSpinBox()
        self.turbo_spin.setRange(5, 30)
        self.turbo_spin.setValue(15)
        self.turbo_spin.setSuffix(' Hz')
        self.turbo_spin.setToolTip(tr('手柄连发速率（5–30 Hz），通过虚拟 XInput 总线实时输出高频脉冲'))
        self.turbo_spin.valueChanged.connect(self._on_turbo_changed)
        tb_l.addWidget(self.turbo_spin)
        self.info_row.addWidget(self.turbo_box)
        self.turbo_box.hide()

        layout.addLayout(self.info_row)

        # Macro timeline (gamepad_macro)
        self.macro_box = QWidget()
        mb_l = QVBoxLayout(self.macro_box)
        mb_l.setContentsMargins(0, 0, 0, 0)
        mb_l.setSpacing(6)

        mb_head = QHBoxLayout()
        mb_head.setSpacing(6)
        mb_head.addWidget(_macro_label('宏流程:'))
        self.macro_hint = _macro_label(tr('点击上方按键录制连招'), muted=True)
        mb_head.addWidget(self.macro_hint)
        mb_head.addStretch()
        self.macro_delay_btn = QPushButton('+ 延迟')
        self.macro_delay_btn.setCursor(Qt.PointingHandCursor)
        self.macro_delay_btn.setToolTip(tr('在上一个按键之后插入一段延迟'))
        self.macro_delay_btn.setStyleSheet(self._chip_style(accent=False))
        self.macro_delay_btn.clicked.connect(self._append_delay)
        mb_head.addWidget(self.macro_delay_btn)
        self.macro_clear_btn = QPushButton('清空')
        self.macro_clear_btn.setCursor(Qt.PointingHandCursor)
        self.macro_clear_btn.setStyleSheet(self._chip_style(accent=False))
        self.macro_clear_btn.clicked.connect(self._clear_macro)
        mb_head.addWidget(self.macro_clear_btn)
        mb_head.addWidget(_macro_label(tr('每步按住')))
        self.macro_hold_spin = QSpinBox()
        self.macro_hold_spin.setRange(*MACRO_HOLD_LIMITS)
        self.macro_hold_spin.setValue(50)
        self.macro_hold_spin.setSuffix(' ms')
        self.macro_hold_spin.setFocusPolicy(Qt.ClickFocus | Qt.TabFocus)
        self.macro_hold_spin.setToolTip(tr('宏中每个按键的按住时长，写入所有步骤'))
        self.macro_hold_spin.valueChanged.connect(self._on_hold_changed)
        mb_head.addWidget(self.macro_hold_spin)
        mb_l.addLayout(mb_head)

        self.macro_flow = FlowLayout(spacing=5)
        mb_l.addLayout(self.macro_flow)

        mb_opts = QHBoxLayout()
        mb_opts.setSpacing(14)
        self.macro_interrupt = QCheckBox(tr('可打断'))
        self.macro_interrupt.setToolTip(tr('勾选后，其他手柄动作被触发的瞬间会停止并释放正在执行的宏'))
        self.macro_interrupt.toggled.connect(lambda _=False: self._update_summary())
        mb_opts.addWidget(self.macro_interrupt)
        self.macro_loop = QCheckBox(tr('连续（按住循环）'))
        self.macro_loop.setToolTip(tr('勾选后长按不放会反复执行宏，松开按键即停止'))
        self.macro_loop.toggled.connect(lambda _=False: self._update_summary())
        mb_opts.addWidget(self.macro_loop)
        mb_opts.addStretch()
        mb_l.addLayout(mb_opts)
        # Looping is a held-gesture property; a short press has nothing to hold.
        self.macro_loop.setVisible(self.gesture == 'long')
        layout.addWidget(self.macro_box)
        self.macro_box.hide()

        self._rebuild_macro()
        self._update_styles()

    @staticmethod
    def _chip_style(accent=True):
        if accent:
            return ("QPushButton { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #ff6d3b, stop:1 #ff5722);"
                    "  color: #ffffff; font-weight: 700; border: 1px solid rgba(255,255,255,0.45); border-radius: 6px; }")
        return ("QPushButton {"
                f"  background: {TOKENS['surface']}; color: {TOKENS['ink_2']}; font-weight: 600;"
                f"  border: 1px solid {TOKENS['border']}; border-radius: 6px; padding: 0 8px; }}"
                f"QPushButton:hover {{ border-color: {TOKENS['border_hi']}; color: {TOKENS['ink']}; }}"
                f"QPushButton:disabled {{ color: {TOKENS['ink_dim']}; border-color: {TOKENS['border_subtle']}; }}")

    @staticmethod
    def _key_chip_style():
        # Red hover is the delete affordance: clicking a recorded key removes that step.
        return (GamepadTargetSelector._chip_style(accent=True)
                + f"QPushButton:hover {{ background: {TOKENS['red']}; border-color: {TOKENS['red']}; }}")

    def _clean_label(self, k):
        raw = trigger_label(k, self.family)
        return (raw.replace('方向键 ', '').replace('D-Pad ', '')
                   .replace('  交叉', '').replace('  Cross', '')
                   .replace('  圆圈', '').replace('  Circle', '')
                   .replace('  方块', '').replace('  Square', '')
                   .replace('  三角', '').replace('  Triangle', '')
                   .replace('左摇杆按下', 'L3').replace('右摇杆按下', 'R3')
                   .replace('Left Stick Click', 'L3').replace('Right Stick Click', 'R3')
                   .replace('左缓冲键', 'LB').replace('右缓冲键', 'RB')
                   .replace('Left Bumper', 'LB').replace('Right Bumper', 'RB')
                   .replace('触摸板按键', 'TP').replace('Touchpad Click', 'TP'))

    def _on_turbo_changed(self, v):
        self.turbo_rate = v
        self.info_label.setText(self._summary_text())

    def _on_btn_clicked(self, key):
        if self.mode == 'macro':
            self._append_step(key)
            return
        if self.mode in ('single', 'turbo'):
            self.selected = {key}
        else:
            if key in self.selected:
                if len(self.selected) > 1:
                    self.selected.remove(key)
            else:
                if len(self.selected) < 4:
                    self.selected.add(key)
        self._update_styles()

    def _update_styles(self):
        for k, btn in self.buttons.items():
            is_sel = k in self.selected and self.mode != 'macro'
            btn.blockSignals(True)
            btn.setChecked(is_sel)
            btn.blockSignals(False)
            if is_sel:
                btn.setStyleSheet(self._chip_style(accent=True))
            else:
                btn.setStyleSheet(
                    f"background: {TOKENS['surface']}; color: {TOKENS['ink_2']}; font-weight: 600; "
                    f"border: 1px solid {TOKENS['border']}; border-radius: 6px;"
                )
        self.info_label.setText(self._summary_text())

    def _summary_text(self):
        if self.mode == 'macro':
            if not self.macro_steps:
                return '🎯 目标手柄宏: ' + tr('未录制 · 点击上方按键添加连招步骤')
            hold = int(self.macro_hold_spin.value())
            total = sum(hold + int(step['gap_ms']) for step in self.macro_steps)
            flow = ' › '.join(self._clean_label(step['button']) for step in self.macro_steps)
            flags = [tr('可打断')] if self.macro_interrupt.isChecked() else []
            if self.gesture == 'long' and self.macro_loop.isChecked():
                flags.append(tr('循环'))
            suffix = (' · ' + ' + '.join(flags)) if flags else ''
            return (f'🎯 目标手柄宏: {flow}  ({len(self.macro_steps)} 步 · 约 {total} ms{suffix})')
        if not self.selected:
            return '请在上方选择目标手柄按键'
        names = [trigger_label(k, self.family) for k in sorted(self.selected, key=lambda x: str(x))]
        if self.mode == 'chord':
            return '🎯 目标手柄组合宏: ' + ' + '.join(names) + ' (一键同时触发)'
        elif self.mode == 'turbo':
            from .virtual_xinput import virtual_gamepad_backend_available
            if virtual_gamepad_backend_available():
                return ('⚡ 目标手柄连发: ' + names[0] + f' ({self.turbo_rate} 次/秒) · ' +
                        tr('通过虚拟 XInput / HID 总线实时输出高频脉冲'))
            return ('⚡ 目标手柄连发: ' + names[0] + f' ({self.turbo_rate} 次/秒) · ' +
                    tr('未接入虚拟手柄驱动；游戏内连发请改用虚拟键鼠模式的“按住高频连发 (Turbo)”'))
        else:
            return '🎯 映射为目标手柄按键: ' + names[0]

    def set_mode(self, mode):
        self.mode = mode
        self.turbo_box.setVisible(mode == 'turbo')
        self.macro_box.setVisible(mode == 'macro')
        if mode in ('single', 'turbo') and len(self.selected) > 1:
            self.selected = {next(iter(sorted(self.selected)))}
        self._update_styles()

    def set_value(self, val):
        if not val:
            return
        parts = str(val).split('+')
        self.selected = set(parts)
        self._update_styles()

    def get_selected_single(self):
        return next(iter(self.selected), '0')

    def get_selected_chord(self):
        return '+'.join(sorted(self.selected, key=lambda x: str(x)))

    def get_turbo_rate(self):
        return self.turbo_spin.value()

    def _update_summary(self):
        self.info_label.setText(self._summary_text())

    def _append_step(self, key):
        if len(self.macro_steps) >= MACRO_STEP_LIMIT:
            self.macro_hint.setText(tr('宏最多 %d 步') % MACRO_STEP_LIMIT)
            return
        self.macro_steps.append({'button': str(key), 'gap_ms': 0})
        self._rebuild_macro()

    def _remove_step(self, index):
        if 0 <= index < len(self.macro_steps):
            del self.macro_steps[index]
            self._rebuild_macro()

    def _append_delay(self):
        if not self.macro_steps:
            return
        self.macro_steps[-1]['gap_ms'] = MACRO_DELAY_PRESET_MS
        self._rebuild_macro()

    def _set_gap(self, index, value):
        if 0 <= index < len(self.macro_steps):
            self.macro_steps[index]['gap_ms'] = int(value)
            self.macro_delay_btn.setEnabled(
                bool(self.macro_steps) and int(self.macro_steps[-1]['gap_ms']) <= 0)
            self._update_summary()

    def _clear_macro(self):
        self.macro_steps = []
        self._rebuild_macro()

    def _on_hold_changed(self, _value):
        self._update_summary()

    def _rebuild_macro(self):
        _drain(self.macro_flow)
        last = len(self.macro_steps) - 1
        for index, step in enumerate(self.macro_steps):
            if index:
                self.macro_flow.addWidget(_macro_label('›', muted=True))
            self.macro_flow.addWidget(self._key_chip(index, step['button']))
            if int(step['gap_ms']) > 0:
                self.macro_flow.addWidget(self._delay_chip(index, step['gap_ms']))
        empty = not self.macro_steps
        self.macro_hint.setVisible(empty)
        self.macro_hint.setText(tr('点击上方按键录制连招'))
        self.macro_delay_btn.setEnabled(not empty and int(self.macro_steps[-1]['gap_ms']) <= 0)
        self.macro_clear_btn.setEnabled(not empty)
        self._update_summary()

    def _key_chip(self, index, button):
        chip = QPushButton(self._clean_label(button), self.macro_box)
        chip.setCursor(Qt.PointingHandCursor)
        chip.setFixedSize(62, 28)
        chip.setToolTip(tr('点击删除此按键'))
        chip.setStyleSheet(self._key_chip_style())
        chip.clicked.connect(lambda checked=False, i=index: self._remove_step(i))
        return chip

    def _delay_chip(self, index, ms):
        wrap = QWidget(self.macro_box)
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(1)
        spin = QSpinBox()
        spin.setRange(1, 5000)
        spin.setValue(max(1, _ms(ms, MACRO_DELAY_PRESET_MS)))
        spin.setSuffix(' ms')
        spin.setPrefix('⏱ ')
        spin.setButtonSymbols(QSpinBox.NoButtons)
        spin.setAlignment(Qt.AlignCenter)
        spin.setFocusPolicy(Qt.ClickFocus | Qt.TabFocus)
        spin.setToolTip(tr('点击修改延迟时长'))
        spin.setStyleSheet(
            f"background: {TOKENS['overlay']}; color: {TOKENS['cyan']}; font-weight: 700; font-size: 11px;"
            f"border: 1px solid {TOKENS['border']}; border-radius: 6px; padding: 0 4px;"
            f"QSpinBox:focus {{ border-color: {TOKENS['cyan']}; }}")
        spin.valueChanged.connect(lambda v, i=index: self._set_gap(i, v))
        row.addWidget(spin)
        remove = _macro_flat('✕', wrap)
        remove.setToolTip(tr('删除此延迟'))
        remove.clicked.connect(lambda checked=False, i=index: self._remove_delay(i))
        row.addWidget(remove)
        return wrap

    def _remove_delay(self, index):
        if 0 <= index < len(self.macro_steps):
            self.macro_steps[index]['gap_ms'] = 0
            self._rebuild_macro()

    def set_sequence(self, sequence):
        steps = []
        hold = 50
        for step in (sequence if isinstance(sequence, list) else ()):
            if not isinstance(step, dict) or len(steps) >= MACRO_STEP_LIMIT:
                continue
            buttons = step.get('buttons')
            if isinstance(buttons, (list, tuple)) and buttons:
                key = str(buttons[0]).strip()
            else:
                key = str(step.get('value', '') or '').split('+')[0].strip()
            if key not in GAMEPAD_TARGETS:
                continue
            hold = _ms(step.get('hold_ms'), hold) or hold
            steps.append({'button': key, 'gap_ms': _ms(step.get('gap_ms'), 0)})
        self.macro_steps = steps
        self.macro_hold_spin.setValue(hold)   # clamped to MACRO_HOLD_LIMITS by the spin box
        self._rebuild_macro()

    def get_selected_macro(self):
        hold = int(self.macro_hold_spin.value())
        return [{'buttons': [str(step['button'])], 'hold_ms': hold,
                 'gap_ms': int(step['gap_ms'])} for step in self.macro_steps]

    def set_macro_options(self, interrupt=False, loop=False):
        self.macro_interrupt.setChecked(bool(interrupt))
        self.macro_loop.setChecked(bool(loop) and self.gesture == 'long')

    def get_macro_interrupt(self):
        return bool(self.macro_interrupt.isChecked())

    def get_macro_loop(self):
        return bool(self.macro_loop.isChecked()) if self.gesture == 'long' else False


class KeySequenceField(QLineEdit):
    """Fallback field for legacy KBM mappings."""
    def __init__(self, value='', parent=None):
        super().__init__(value, parent)
        self.recording = False
        self.held = set()
        self.keys = []
        self.setPlaceholderText('Cmd+Shift+S / Ctrl / W+Space' if sys.platform == 'darwin' else 'Ctrl+Shift+S / W+Space')

    def start_recording(self):
        self.recording = True; self.held.clear(); self.keys.clear()
        self.setPlaceholderText('同时按下目标按键，再松开'); self.clear(); self.setFocus()

    def keyPressEvent(self, event):
        if not self.recording:
            return super().keyPressEvent(event)
        if event.isAutoRepeat():
            return
        key = event.key()
        # Qt maps Key_Control to physical Command and Key_Meta to physical
        # Control on macOS. Persist actual modifiers, not Qt's aliases.
        modifiers = {Qt.Key_Control: 'Cmd' if sys.platform == 'darwin' else 'Ctrl',
                     Qt.Key_Meta: 'Ctrl' if sys.platform == 'darwin' else 'Win',
                     Qt.Key_Shift: 'Shift', Qt.Key_Alt: 'Alt'}
        name = modifiers.get(key, QKeySequence(key).toString(QKeySequence.PortableText))
        if name and name not in self.keys:
            self.keys.append(name)
        self.held.add(key)
        self.setText('+'.join(self.keys))
        event.accept()

    def keyReleaseEvent(self, event):
        if not self.recording:
            return super().keyReleaseEvent(event)
        if event.isAutoRepeat():
            return
        self.held.discard(event.key())
        if not self.held:
            self.recording = False
        event.accept()


class BindingDialog(QDialog):
    """Configuration dialog supporting either pure Gamepad macro editing or pure Virtual KBM editing."""

    def __init__(self, owner, trigger='0', mapping=None, output=None, new=False, profile=None, mode=None):
        super().__init__(owner)
        self.owner = owner
        self.new_binding = new
        self.profile = profile or owner.config.get('active_profile')
        self.family = profile_family(owner.config, owner.snapshot, self.profile)
        self.device_context = self._device_context(owner.snapshot)
        self.context_changed = False
        self.original_trigger = canonical_trigger(trigger)
        self.touch_gesture = self.original_trigger.startswith('TP:')

        # Resolve mode: 'gamepad' or 'kbm'
        mapping = copy.deepcopy(mapping or {})
        if mode is None:
            if output or any(mapping.get(g, {}).get('action') in ('hold', 'shortcut', 'mouse_hold', 'mouse_click', 'wheel', 'launch') for g in ('short', 'long')):
                mode = 'kbm'
            else:
                from .studio_core import profile_mode
                mode = profile_mode(owner.config, self.profile)
        self.base_mode = mode
        self.mode = 'kbm' if self.touch_gesture else mode
        self.available_inputs = input_sources(owner.snapshot)

        from .i18n import tr
        title = tr('编辑触摸板手势') if self.touch_gesture else tr('编辑手柄输出配置' if self.mode == 'gamepad' else '编辑虚拟键鼠映射')
        self.setWindowTitle(title)
        self.setMinimumWidth(520 if self.touch_gesture else 660)
        self.normalizer = InputNormalizer()
        self.touch_recognizer = TouchGestureRecognizer()
        self.capturing = False
        self.ready = False
        self.best = set()
        self.finished_editing = False

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        # Profile & Mode Header
        top_bar = QHBoxLayout()
        icon_char = '🎮' if self.mode == 'gamepad' else '⌨️'
        prof_title = QLabel(f"{icon_char} 当前配置：{self.profile}")
        prof_title.setStyleSheet(f"font-size: 13.5px; font-weight: 700; color: {TOKENS['accent'] if self.mode == 'gamepad' else TOKENS['cyan']};")
        top_bar.addWidget(prof_title, 1)

        badge_text = tr('手柄输出配置') if self.mode == 'gamepad' else '手柄 ➔ 虚拟键鼠 映射生产'
        if self.touch_gesture:
            badge_text = tr('触摸板 → 键鼠与快捷动作')
        macro_badge = QLabel(badge_text)
        self.mode_badge = macro_badge
        macro_badge.setStyleSheet(f"background: {TOKENS['surface']}; color: {TOKENS['cyan'] if self.mode == 'gamepad' else TOKENS['accent']}; padding: 3px 8px; border-radius: 4px; font-weight: 600; font-size: 11px;")
        top_bar.addWidget(macro_badge)
        layout.addLayout(top_bar)

        # Trigger Input Selection
        source_group = QGroupBox(tr('输入来源：手柄按键或触摸手势') if touch_sources(owner.snapshot)
                                 else "触发源 (物理手柄按键 / 组合键)")
        source_group.setStyleSheet(f"QGroupBox {{ font-weight: 700; color: {TOKENS['ink']}; }}")
        sg_l = QVBoxLayout(source_group)
        sg_l.setSpacing(8)

        source = QHBoxLayout()
        self.inputs = []
        parts = self.original_trigger.split('+')
        for i in range(4):
            combo = QComboBox()
            combo.addItem('—', '')
            for key in self.available_inputs:
                if i and key.startswith('TP:'):
                    continue
                combo.addItem(trigger_label(key, self.family), key)
            value = parts[i] if i < len(parts) else ''
            if new and i == 0 and value not in self.available_inputs:
                value = next(iter(self.available_inputs), '')
            combo.setCurrentIndex(max(0, combo.findData(value)))
            self.inputs.append(combo)
            source.addWidget(combo)
            if self.touch_gesture and i:
                combo.setEnabled(False)
                combo.hide()
        sg_l.addLayout(source)

        capture_row = QHBoxLayout()
        self.capture_button = QPushButton(tr('录入手柄按键或触摸手势') if touch_sources(owner.snapshot)
                                         else '从手柄录入组合键')
        self.capture_button.clicked.connect(self.start_capture)
        capture_row.addWidget(self.capture_button)

        self.live = QLabel('')
        self.live.setStyleSheet(f"color: {TOKENS['ink_2']}; font-weight: 600;")
        capture_row.addWidget(self.live, 1)
        sg_l.addLayout(capture_row)
        layout.addWidget(source_group)

        if output:
            from .mapping_engine import legacy_action
            mapping['short'] = legacy_action(output)

        # Action Options
        GAMEPAD_UI_ACTIONS = [
            ('none', '保留原始输入 (Pass-through)'),
            ('gamepad_button', '映射为手柄按键 (Remap)'),
            ('gamepad_chord', '手柄多键组合宏 (一键出招)'),
            ('gamepad_macro', '手柄动作宏 (连招序列)'),
            ('gamepad_turbo', '手柄高频连发 (Turbo)'),
            ('capture', '保存截图 (Screenshot)'),
            ('replay_record', '保存精彩瞬间 (回放录制)'),
            ('home', '打开控制中心 (Home)'),
            ('gallery', '打开截图资料库 (Gallery)'),
            ('suppress', '屏蔽此按键 (Suppress)'),
        ]

        KBM_UI_ACTIONS = [
            ('hold', '持续按住按键 (Hold Key)'),
            ('shortcut', '触发按键/组合键 (Shortcut)'),
            ('mouse_hold', '按住鼠标键 (Mouse Hold)'),
            ('mouse_click', '单击鼠标键 (Mouse Click)'),
            ('wheel', '鼠标滚轮滚动 (Mouse Wheel)'),
            ('none', '保留原始输入 (Pass-through)'),
            ('suppress', '屏蔽此按键 (Suppress)'),
            ('launch', '启动应用程序 (Launch App)'),
            ('capture', tr('保存截图')),
            ('replay_record', tr('保存精彩瞬间')),
            ('record_toggle', tr('开始或结束录屏')),
            ('home', tr('打开控制中心')),
            ('gallery', tr('打开截图资料库')),
        ]
        TOUCH_UI_ACTIONS = [
                ('none', tr('未设置')),
                ('shortcut', tr('键盘按键或组合键')),
                ('mouse_click', tr('鼠标单击')),
                ('wheel', tr('鼠标滚轮滚动')),
                ('capture', tr('保存截图')),
                ('replay_record', tr('保存精彩瞬间')),
                ('record_toggle', tr('开始或结束录屏')),
                ('home', tr('打开控制中心')),
                ('gallery', tr('打开截图资料库')),
                ('launch', tr('启动应用程序')),
            ]
        self.keyboard_action_options = KBM_UI_ACTIONS
        self.touch_action_options = TOUCH_UI_ACTIONS
        self.gamepad_action_options = GAMEPAD_UI_ACTIONS

        self.action_combos = {}
        self.target_selectors = {}
        self.kbm_fields = {}
        self.mouse_combos = {}
        self.hold_modes = {}
        self.hold_mode_rows = {}
        self.turbo_spins = {}
        self.launch_paths = {}
        self.launch_args = {}
        self.action_boxes = {}
        self.action_mode_memory = {}

        gestures = [('short', '短按 (SHORT PRESS)'), ('long', '长按 (LONG PRESS)')]
        for gesture, title in gestures:
            box = QGroupBox(title)
            box.setStyleSheet(f"QGroupBox {{ font-weight: 700; color: {TOKENS['ink']}; }}")
            b_l = QVBoxLayout(box)
            b_l.setSpacing(8)

            act_row = QHBoxLayout()
            act_label = QLabel('目标动作:')
            act_label.setStyleSheet(f"color: {TOKENS['ink_2']}; font-weight: 600;")
            act_row.addWidget(act_label)

            action = QComboBox()
            action.setMinimumWidth(240)
            binding = mapping.get(gesture, {})
            current_act = binding.get('action', 'none')

            options = GAMEPAD_UI_ACTIONS if self.mode == 'gamepad' else KBM_UI_ACTIONS
            for key, name in options:
                action.addItem(name, key)

            idx = action.findData(current_act)
            action.setCurrentIndex(max(0, idx))
            act_row.addWidget(action, 1)
            b_l.addLayout(act_row)

            if self.base_mode == 'gamepad':
                selector = GamepadTargetSelector(self.family, gesture=gesture)
                if current_act == 'gamepad_chord':
                    selector.set_mode('chord')
                    selector.set_value(binding.get('value', ''))
                elif current_act == 'gamepad_macro':
                    selector.set_mode('macro')
                    selector.set_sequence(binding.get('sequence', []))
                    selector.set_macro_options(binding.get('interrupt', False),
                                               binding.get('loop', False))
                elif current_act == 'gamepad_turbo':
                    selector.set_mode('turbo')
                    selector.set_value(binding.get('value', '0'))
                    if 'rate_hz' in binding:
                        selector.turbo_spin.setValue(int(binding['rate_hz']))
                elif current_act == 'gamepad_button':
                    selector.set_mode('single')
                    selector.set_value(binding.get('value', '0'))
                b_l.addWidget(selector)
                self.target_selectors[gesture] = selector

                def make_gamepad_updater(a=action, s=selector):
                    def update():
                        k = a.currentData()
                        if k in ('gamepad_button', 'gamepad_chord', 'gamepad_macro', 'gamepad_turbo'):
                            s.show()
                            if k == 'gamepad_button':
                                s.set_mode('single')
                            elif k == 'gamepad_chord':
                                s.set_mode('chord')
                            elif k == 'gamepad_macro':
                                s.set_mode('macro')
                            elif k == 'gamepad_turbo':
                                s.set_mode('turbo')
                        else:
                            s.hide()
                    return update
                updater = make_gamepad_updater(action, selector)
                action.currentIndexChanged.connect(updater)
                updater()
            # KBM Mode Controls
            kbm_row = QWidget()
            k_layout = QHBoxLayout(kbm_row)
            k_layout.setContentsMargins(0, 0, 0, 0)
            field = KeySequenceField(binding.get('value', ''))
            record_btn = QPushButton('录入键盘按键')
            record_btn.clicked.connect(field.start_recording)
            k_layout.addWidget(field, 1)
            k_layout.addWidget(record_btn)
            b_l.addWidget(kbm_row)
            self.kbm_fields[gesture] = field

            mouse_combo = QComboBox()
            for v, m_name in [('left', '鼠标左键'), ('right', '鼠标右键'), ('middle', '鼠标中键'), ('up', '滚轮向上'), ('down', '滚轮向下')]:
                mouse_combo.addItem(m_name, v)
            m_idx = mouse_combo.findData(binding.get('value', 'left'))
            mouse_combo.setCurrentIndex(max(0, m_idx))
            b_l.addWidget(mouse_combo)
            self.mouse_combos[gesture] = mouse_combo

            hold_mode_row = QWidget()
            hold_mode_layout = QHBoxLayout(hold_mode_row)
            hold_mode_layout.setContentsMargins(0, 0, 0, 0)
            hold_mode_layout.addWidget(QLabel(tr('触发方式')))
            hold_mode = QComboBox()
            hold_mode.addItem(tr('按住时生效'), 'hold')
            hold_mode.addItem(tr('按一次保持，再按一次取消'), 'toggle')
            hold_mode.addItem(tr('按住高频连发 (Turbo)'), 'turbo')
            hold_mode.addItem(tr('双击并按住 (冲刺/奔跑)'), 'double_tap_hold')
            hold_mode.setToolTip(tr('切换保持会在暂停、断线、切换预设或退出时自动释放'))
            hold_mode.setCurrentIndex(max(0, hold_mode.findData(binding.get('mode', 'hold'))))
            hold_mode_layout.addWidget(hold_mode, 1)

            turbo_spin = QSpinBox()
            turbo_spin.setRange(5, 30)
            try:
                raw_rate = binding.get('rate_hz', 15)
                rate_val = 15 if isinstance(raw_rate, bool) else int(raw_rate)
            except (TypeError, ValueError):
                rate_val = 15
            turbo_spin.setValue(max(5, min(30, rate_val)))
            turbo_spin.setSuffix(' Hz')
            turbo_spin.setAccessibleName(tr('连发速率'))
            turbo_spin.setToolTip(tr('设置按住连发频率（5 - 30 Hz）'))
            hold_mode_layout.addWidget(turbo_spin)
            self.turbo_spins[gesture] = turbo_spin

            def make_turbo_updater(hm=hold_mode, ts=turbo_spin, a=action):
                def update():
                    ts.setVisible(not self.touch_gesture and self.mode == 'kbm' and a.currentData() in ('hold', 'mouse_hold') and hm.currentData() == 'turbo')
                return update
            t_updater = make_turbo_updater(hold_mode, turbo_spin, action)
            hold_mode.currentIndexChanged.connect(t_updater)
            t_updater()

            b_l.addWidget(hold_mode_row)
            self.hold_modes[gesture] = hold_mode
            self.hold_mode_rows[gesture] = hold_mode_row

            launch_row = QWidget()
            l_layout = QHBoxLayout(launch_row)
            l_layout.setContentsMargins(0, 0, 0, 0)
            path_edit = QLineEdit(binding.get('executable', ''))
            path_edit.setPlaceholderText('应用程序路径 (.app / 可执行文件)' if sys.platform == 'darwin' else '应用程序路径 (.exe)')
            browse_btn = QPushButton('浏览...')
            browse_btn.clicked.connect(lambda chk=False, p=path_edit: self.browse_app(p))
            args_edit = QLineEdit(binding.get('arguments', ''))
            args_edit.setPlaceholderText('启动参数 (可选)')
            l_layout.addWidget(path_edit, 2)
            l_layout.addWidget(browse_btn)
            l_layout.addWidget(args_edit, 1)
            b_l.addWidget(launch_row)
            self.launch_paths[gesture] = path_edit
            self.launch_args[gesture] = args_edit

            def make_kbm_updater(a=action, kr=kbm_row, mc=mouse_combo, lr=launch_row, mr=hold_mode_row, tu=t_updater):
                def update():
                    k = a.currentData()
                    kr.setVisible(k in ('hold', 'shortcut'))
                    mc.setVisible(k in ('mouse_hold', 'mouse_click', 'wheel'))
                    mr.setVisible(not self.touch_gesture and self.mode == 'kbm' and k in ('hold', 'mouse_hold'))
                    lr.setVisible(k == 'launch')
                    tu()
                return update
            updater = make_kbm_updater(action, kbm_row, mouse_combo, launch_row)
            action.currentIndexChanged.connect(updater)
            updater()

            self.action_combos[gesture] = action
            self.action_boxes[gesture] = box
            layout.addWidget(box)

        # Long press timing
        self.timing_box = QWidget()
        timing = QHBoxLayout(self.timing_box)
        timing.setContentsMargins(0, 0, 0, 0)
        timing.addWidget(QLabel('长按识别阈值:'))
        self.long_press = QDoubleSpinBox(self)
        self.long_press.setRange(.15, 3.)
        self.long_press.setSingleStep(.05)
        self.long_press.setSuffix(' 秒')
        from .studio_core import device_config
        self.long_press.setValue(mapping.get('long_press', device_config(owner.config, owner.snapshot).get('long_press', .65)))
        timing.addWidget(self.long_press)
        timing.addStretch()
        layout.addWidget(self.timing_box)

        # Tip banner
        from .virtual_xinput import virtual_gamepad_backend_available
        has_vpad = virtual_gamepad_backend_available()
        if self.touch_gesture:
            tip_text = tr('每个手势触发一次动作；按下触摸板仍使用独立按键映射。')
        elif self.mode == 'gamepad':
            tip_text = (
                tr('手柄输出配置；已检测到虚拟手柄后端（ViGEmBus / VirtualHID），实时合成标准 XInput 报告。')
                if has_vpad else
                tr('手柄输出配置；当前未接入系统虚拟手柄驱动后端。若需在游戏内高频自动连发，请切换到虚拟键鼠模式使用“按住高频连发 (Turbo)”。')
            )
        else:
            tip_text = '💡 虚拟键鼠模拟：将物理手柄输入无缝转换为真实键盘按键、鼠标点击、滚轮或应用快捷键。'
        tip_lbl = QLabel(tip_text)
        tip_lbl.setWordWrap(True)
        tip_lbl.setStyleSheet(f"color: {TOKENS['ink_3']}; font-size: 11px; padding: 4px 0;")
        self.tip_label = tip_lbl
        layout.addWidget(tip_lbl)

        self.error = QLabel('')
        self.error.setStyleSheet(f"color: {TOKENS['amber']}; font-weight: 600;")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)

        controls = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_button = controls.button(QDialogButtonBox.Save)
        controls.button(QDialogButtonBox.Save).setText('保存映射')
        controls.button(QDialogButtonBox.Cancel).setText('取消')
        controls.accepted.connect(self.validate)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)

        self.inputs[0].currentIndexChanged.connect(self.sync_source_kind)
        self.sync_source_kind(preserve_action=True)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(16)

    def stop_capture_session(self):
        """Discard live input when this editor leaves its modal loop."""
        self.timer.stop()
        self.capturing = False
        self.ready = False
        self.best.clear()
        self.normalizer.reset()
        self.touch_recognizer.reset(block_until_release=True)
        self.finished_editing = True

    def done(self, result):
        self.stop_capture_session()
        super().done(result)

    def closeEvent(self, event):
        # QDialog only rejects a visible dialog. Tests and other nonmodal owners
        # can close an editor before showing it, and must also stop its timer.
        self.stop_capture_session()
        super().closeEvent(event)

    def sync_source_kind(self, _index=None, preserve_action=False):
        """A touch source is a completed action, rather than a held chord."""
        from .i18n import tr
        touch = str(self.inputs[0].currentData() or '').startswith('TP:')
        previous_touch = self.touch_gesture
        previous_kind = 'touch' if previous_touch else self.base_mode
        kind = 'touch' if touch else self.base_mode
        if touch != previous_touch:
            self.action_mode_memory[previous_kind] = {
                gesture: action.currentData() for gesture, action in self.action_combos.items()}
        self.touch_gesture = touch
        self.mode = 'kbm' if touch else self.base_mode
        for combo in self.inputs[1:]:
            if touch:
                combo.setCurrentIndex(0)
            combo.setVisible(not touch)
            combo.setEnabled(not touch and not self.context_changed)
        self.action_boxes['short'].setTitle(tr('触发动作') if touch else '短按 (SHORT PRESS)')
        self.action_boxes['long'].setVisible(not touch)
        self.timing_box.setVisible(not touch)
        self.long_press.setVisible(not touch)
        if touch != previous_touch or preserve_action:
            for gesture, action in self.action_combos.items():
                selected = self.action_mode_memory.get(kind, {}).get(gesture, action.currentData())
                if touch and gesture == 'short' and not preserve_action:
                    selected = {'hold': 'shortcut', 'mouse_hold': 'mouse_click', 'suppress': 'none'}.get(selected, selected)
                options = list(self.touch_action_options if touch else self.gamepad_action_options
                               if self.mode == 'gamepad' else self.keyboard_action_options)
                # Existing one-shot mappings may use the bounded hold representation.
                if touch and selected in ('hold', 'mouse_hold'):
                    options.append((selected, tr('按键短按') if selected == 'hold' else tr('鼠标单击')))
                action.blockSignals(True)
                action.clear()
                for key, name in options:
                    action.addItem(name, key)
                action.setCurrentIndex(max(0, action.findData(selected)))
                action.blockSignals(False)
                action.currentIndexChanged.emit(action.currentIndex())
        self.setWindowTitle(tr('编辑触摸板手势') if touch else
                            tr('编辑手柄输出配置' if self.mode == 'gamepad' else '编辑虚拟键鼠映射'))
        self.mode_badge.setText(tr('触摸板 → 键鼠与快捷动作') if touch else
                                tr('手柄输出配置') if self.mode == 'gamepad' else '手柄 ➔ 虚拟键鼠 映射生产')
        if touch:
            self.tip_label.setText(tr('每个手势触发一次动作；按下触摸板仍使用独立按键映射。'))
        elif self.mode == 'kbm':
            self.tip_label.setText(tr('选择手柄按键或触摸手势，也可以点击录入后实际操作。'))
        else:
            from .virtual_xinput import virtual_gamepad_backend_available
            self.tip_label.setText(
                tr('手柄输出配置；已检测到虚拟手柄后端（ViGEmBus / VirtualHID），实时合成标准 XInput 报告。')
                if virtual_gamepad_backend_available() else
                tr('手柄输出配置；当前未接入系统虚拟手柄驱动后端。若需在游戏内高频自动连发，请切换到虚拟键鼠模式使用“按住高频连发 (Turbo)”。')
            )

    @staticmethod
    def _device_context(state):
        from .studio_core import profile_scope
        # Anonymous input samples are used by preview/capture harnesses. Real
        # hardware always has an identity, supplied by Device.read().
        identified = state and any(key in state for key in ('device_key', 'profile_key', 'instance_id', 'family'))
        return (profile_scope(state) if identified else 'offline:xinput',
                state.get('instance_id') if state else None,
                (state or {}).get('family', 'generic'), tuple(input_sources(state)))

    def check_device_context(self):
        from .i18n import tr
        if self.context_changed or self._device_context(self.owner.snapshot) != self.device_context:
            self.context_changed = True
            self.capturing = False
            self.best.clear()
            self.normalizer.reset()
            self.touch_recognizer.reset(block_until_release=True)
            self.timer.stop()
            self.capture_button.setEnabled(False)
            self.save_button.setEnabled(False)
            for combo in self.inputs:
                combo.setEnabled(False)
            self.error.setText(tr('输入设备已变化，请重新打开映射编辑。'))
            return False
        return True

    def browse_app(self, field):
        from PySide6.QtWidgets import QFileDialog
        application_filter = '应用程序 (*.app);;所有文件 (*)' if sys.platform == 'darwin' else '程序 (*.exe);;所有文件 (*)'
        value, _ = QFileDialog.getOpenFileName(self, '选择应用', '', application_filter)
        if value:
            field.setText(value)

    def start_capture(self):
        if self.finished_editing:
            return
        if not self.check_device_context():
            return
        self.capturing = True
        self.ready = False
        self.best = set()
        self.normalizer.reset()
        self.touch_recognizer.reset(block_until_release=True)
        from .i18n import tr
        self.capture_button.setText(tr('先松开按键并抬起手指，再输入按键或手势'))
        self.error.clear()

    def finish_capture(self, trigger):
        from .i18n import tr
        parts = canonical_trigger(trigger).split('+')
        for i, combo in enumerate(self.inputs):
            combo.setCurrentIndex(max(0, combo.findData(parts[i] if i < len(parts) else '')))
        self.capturing = False
        self.best.clear()
        self.touch_recognizer.reset(block_until_release=True)
        self.live.setText(trigger_label(trigger, self.family))
        self.capture_button.setText(tr('重新录入来源'))

    def poll(self):
        if self.finished_editing:
            return
        if not self.check_device_context():
            return
        state = self.owner.snapshot
        options = self.owner.config.get('profile_options', {}).get(self.profile, {})
        inputs = copy.deepcopy(options.get('input') or {})
        inputs['trigger_curves'] = device_config(self.owner.config, state).get('trigger_curves', {})
        current = self.normalizer.update(state, inputs)
        current.difference_update({
            'LS:outer', 'LS:inner',
            'LS:up:outer', 'LS:down:outer', 'LS:left:outer', 'LS:right:outer'
        })
        if not self.capturing:
            self.live.setText(' + '.join(trigger_label(k, self.family) for k in sorted(current)) if current else
                              trigger_label(self.inputs[0].currentData(), self.family) if self.touch_gesture else
                              '等待输入' if state else '未连接 · 可手动在上方选择按键')
            return
        touch = self.touch_recognizer.update(state, settings=device_config(self.owner.config, state))
        if not self.ready:
            self.ready = not current and not touch['contacts']
            if self.ready:
                self.touch_recognizer.reset(block_until_release=False)
            return
        if current or self.best:
            # A completed touch action cannot join a held physical chord.
            self.touch_recognizer.reset(block_until_release=True)
            if not current and touch['contacts']:
                from .i18n import tr
                self.live.setText(tr('抬起手指后完成按键录入'))
                return
        else:
            events = [event for event in touch['events'] if event in self.available_inputs]
            if touch['scroll_steps']:
                direction = 'TP:scroll_up' if touch['scroll_steps'] > 0 else 'TP:scroll_down'
                if direction in self.available_inputs:
                    events.append(direction)
            if events:
                self.finish_capture(events[-1])
                return
            if touch['contacts']:
                from .i18n import tr
                self.live.setText(tr('正在识别触摸手势'))
                return
        if len(current) > len(self.best):
            self.best = set(current)
        if current:
            self.live.setText(trigger_label('+'.join(current), self.family) if len(current) <= 4 else '最多支持 4 个按键')
        elif self.best:
            if len(self.best) > 4:
                self.error.setText('最多支持 4 个按键，请重新录入')
                self.best.clear()
                return
            self.finish_capture('+'.join(self.best))

    def trigger(self):
        parts = [c.currentData() for c in self.inputs if c.currentData()]
        if not parts:
            raise ValueError('请选择当前手柄支持的输入按键')
        return canonical_trigger('+'.join(parts))

    def value(self):
        mapping = {}
        for g in (('short',) if self.touch_gesture else ('short', 'long')):
            action_code = self.action_combos[g].currentData()
            binding = {'action': action_code}
            if self.mode == 'gamepad':
                if action_code == 'gamepad_button':
                    binding['value'] = self.target_selectors[g].get_selected_single()
                elif action_code == 'gamepad_chord':
                    binding['value'] = self.target_selectors[g].get_selected_chord()
                elif action_code == 'gamepad_macro':
                    binding['sequence'] = self.target_selectors[g].get_selected_macro()
                    binding['interrupt'] = self.target_selectors[g].get_macro_interrupt()
                    binding['loop'] = self.target_selectors[g].get_macro_loop()
                elif action_code == 'gamepad_turbo':
                    binding['value'] = self.target_selectors[g].get_selected_single()
                    binding['rate_hz'] = self.target_selectors[g].get_turbo_rate()
            else:
                if action_code in ('hold', 'shortcut'):
                    binding['value'] = self.kbm_fields[g].text().strip()
                elif action_code in ('mouse_hold', 'mouse_click', 'wheel'):
                    binding['value'] = self.mouse_combos[g].currentData()
                elif action_code == 'launch':
                    binding.update(executable=self.launch_paths[g].text().strip(), arguments=self.launch_args[g].text().strip())
                if not self.touch_gesture and action_code in ('hold', 'mouse_hold'):
                    mode = self.hold_modes[g].currentData()
                    if mode in ('toggle', 'turbo', 'double_tap_hold'):
                        binding['mode'] = mode
                        if mode == 'turbo':
                            binding['rate_hz'] = self.turbo_spins[g].value()
            mapping[g] = binding
        if self.touch_gesture:
            mapping['long'] = {'action': 'none'}
        else:
            mapping['long_press'] = self.long_press.value()
        return mapping

    def validate(self):
        try:
            if not self.check_device_context():
                return
            if self.capturing:
                from .i18n import tr
                raise ValueError(tr('请先完成输入来源录入'))
            val = self.value()
            for gesture in (('short',) if self.touch_gesture else ('short', 'long')):
                if val[gesture].get('action') == 'gamepad_macro' and not val[gesture].get('sequence'):
                    raise ValueError(tr('宏流程为空：请先点击上方按键录制至少一个步骤'))
            trig = self.trigger()
            if not set(trig.split('+')) <= set(input_sources(self.owner.snapshot)):
                raise ValueError('请选择当前手柄支持的输入按键')
            validate_mappings({trig: val})
            if sys.platform == 'darwin':
                from .actions import supports_key
                for binding in (val.get('short', {}), val.get('long', {})):
                    if binding.get('action') in ('hold', 'shortcut') and not supports_key(binding.get('value', '')):
                        from .macos_actions import MacActions
                        raise ValueError(MacActions.key_capability(binding.get('value',''))['reason'])
            if self.new_binding or trig != self.original_trigger:
                existing = self.owner.config['profiles'].get(self.profile, {}).get(trig, {})
                if any(existing.get(g, {}).get('action', 'none') != 'none' for g in ('short', 'long')):
                    if QMessageBox.question(self, '替换映射', f'组合键 {trigger_label(trig, self.family)} 已有映射，是否替换？') != QMessageBox.Yes:
                        return
            self.accept()
        except ValueError as exc:
            self.error.setText(str(exc))


GamepadBindingDialog = BindingDialog
KbmBindingDialog = BindingDialog


class BindingList(QWidget):
    """Live list of active mappings for the current profile."""

    def __init__(self, owner, profile_getter=None, mode='gamepad', parent=None):
        from PySide6.QtWidgets import QSizePolicy
        from .glass import glyph
        from .i18n import tr

        super().__init__(parent)
        self.owner = owner
        self.profile_getter = profile_getter
        self.mode = mode
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        tools = QHBoxLayout()
        tools.setSpacing(8)
        header_text = '当前手柄宏映射列表' if mode == 'gamepad' else '当前虚拟键鼠映射列表'
        self.heading = QLabel(tr(header_text))
        self.heading.setObjectName('section')
        self.heading.setWordWrap(True)
        self.heading.setMinimumWidth(0)
        self.heading.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        tools.addWidget(self.heading, 1)
        button_specs = [
            ('add_button', '添加映射', 'plus', self.add),
            ('edit_button', '编辑', 'edit', self.edit),
            ('swap_button', '交换', None, self.swap),
            ('clear_button', '清除', 'trash', self.clear),
        ]
        if mode == 'gamepad':
            button_specs.insert(1, ('allocate_unused_button', '分配余键序列', None, self.allocate_unused))
        for name, title, symbol, callback in button_specs:
            btn = QPushButton(tr(title))
            btn.setObjectName('pill')
            btn.setCursor(Qt.PointingHandCursor)
            if symbol:
                btn.setIcon(glyph(symbol, TOKENS['ink_2']))
            btn.setAccessibleName(tr(title))
            btn.setToolTip(tr(title))
            btn.clicked.connect(callback)
            tools.addWidget(btn)
            setattr(self, name, btn)
        layout.addLayout(tools)
        self.instruction = QLabel(tr('单击选择 · 双击编辑 · 实时按键高亮'))
        self.instruction.setObjectName('caption')
        self.instruction.setWordWrap(True)
        self.instruction.setMinimumWidth(0)
        self.instruction.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.instruction)
        self.list = QListWidget()
        self.list.setAccessibleName(tr(header_text))
        self.list.setAccessibleDescription(self.instruction.text())
        self.list.setMinimumWidth(0)
        self.list.setMinimumHeight(110)
        self.list.setWordWrap(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.itemSelectionChanged.connect(self._sync_controls)
        self.list.itemDoubleClicked.connect(lambda item: self.edit())
        layout.addWidget(self.list)
        self.rows = {}
        self.active = set()
        self._sync_controls()

    def _sync_controls(self):
        selected = bool(self.list.selectedItems())
        self.edit_button.setEnabled(selected)
        self.clear_button.setEnabled(selected)
        from .mapping_swap_ui import can_swap_bindings
        first = self.list.currentItem().data(Qt.UserRole) if self.list.currentItem() else None
        self.swap_button.setEnabled(selected and can_swap_bindings(self.owner, self.get_profile_name(), first))

    def get_profile_name(self):
        if self.profile_getter:
            return self.profile_getter()
        if hasattr(self.owner, 'current_gamepad_profile') and self.mode == 'gamepad':
            return self.owner.current_gamepad_profile()
        return self.owner.config.get('active_profile')

    def refresh(self):
        from .i18n import tr

        selected = self.list.currentItem().data(Qt.UserRole) if self.list.currentItem() else None
        self.list.clear()
        self.rows.clear()
        profile_name = self.get_profile_name()
        family = profile_family(self.owner.config, self.owner.snapshot, profile_name)
        for trigger, entry in effective_mappings(self.owner.config, self.owner.snapshot, profile_name).items():
            if all(entry.get(g, {}).get('action', 'none') == 'none' for g in ('short', 'long')):
                continue
            text = trigger_label(trigger, family) + '  →  ' + binding_label(entry.get('short', {}), family)
            if entry.get('long', {}).get('action', 'none') != 'none':
                text += '    ' + tr('长按：') + binding_label(entry['long'], family)
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, trigger)
            item.setData(Qt.AccessibleTextRole, text)
            item.setToolTip(text)
            item.setForeground(QColor(TOKENS['accent'] if trigger in self.active else TOKENS['ink']))
            self.list.addItem(item)
            self.rows[trigger] = item
            if trigger == selected:
                self.list.setCurrentItem(item)
        self._sync_controls()

    def add(self):
        profile_name = self.get_profile_name()
        self.owner.edit_mapping('0', new=True, profile=profile_name, mode=self.mode)

    def allocate_unused(self, overwrite=False, use_canonical_slots=False):
        profile_name = self.get_profile_name()
        self.owner.mapping_change({
            'op': 'allocate_unused_xinput',
            'profile': profile_name,
            'overwrite': bool(overwrite),
            'use_canonical_slots': bool(use_canonical_slots),
        })

    def edit(self):
        if self.list.currentItem():
            profile_name = self.get_profile_name()
            self.owner.edit_mapping(self.list.currentItem().data(Qt.UserRole), profile=profile_name, mode=self.mode)

    def clear(self):
        if self.list.currentItem():
            profile_name = self.get_profile_name()
            self.owner.mapping_change({'op': 'unbind', 'trigger': self.list.currentItem().data(Qt.UserRole), 'profile': profile_name})

    def swap(self):
        from .mapping_swap_ui import can_swap_bindings
        if self.list.currentItem():
            profile_name = self.get_profile_name()
            first = self.list.currentItem().data(Qt.UserRole)
            if can_swap_bindings(self.owner, profile_name, first):
                self.owner.open_mapping_swap(profile_name, first)

    def feedback(self, data):
        active = set(data.get('active', []))
        if active == self.active:
            return
        self.active = active
        for trigger, item in self.rows.items():
            item.setForeground(QColor(TOKENS['accent'] if trigger in active else TOKENS['ink']))


def press_verdicts(mappings):
    """Split buttons into what dispatches, what only leads a chord, and what is blocked.

    A bare press of a dispatchable button gets no log row: its dispatched action
    already earns one. Buttons that merely appear inside a chord, or that are
    deliberately suppressed, would otherwise vanish from the log entirely.
    """
    dispatchable, prefix, blocked = set(), set(), set()
    for trigger, entry in (mappings or {}).items():
        if not isinstance(entry, dict):
            continue
        codes = {entry[g].get('action', 'none') for g in ('short', 'long')
                 if isinstance(entry.get(g), dict)}
        parts = str(trigger).split('+')
        if len(parts) > 1:
            prefix.update(parts)
        elif codes - {'none', 'suppress'}:
            dispatchable.add(parts[0])
        elif 'suppress' in codes:
            blocked.add(parts[0])
    return dispatchable, prefix, blocked


class TriggerLog(QWidget):
    """Rolling record of dispatched mapping actions and bare presses, newest first."""

    MAX_ROWS = 200
    ACTION, BARE = 'action', 'bare'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cursor = 0
        self.macro_cursor = 0
        self.rows = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        head = QHBoxLayout()
        head.setSpacing(8)
        heading = QLabel(tr('触发日志'))
        heading.setObjectName('section')
        head.addWidget(heading)
        self.count_label = QLabel(tr('暂无触发'))
        self.count_label.setStyleSheet(f"color: {TOKENS['ink_3']}; font-size: 10.5px;")
        head.addWidget(self.count_label)
        self.preview_badge = QLabel(tr('安全试按中 · 不产生真实输出'))
        self.preview_badge.setStyleSheet(f"color: {TOKENS['amber']}; font-size: 10.5px; font-weight: 700;")
        self.preview_badge.hide()
        head.addWidget(self.preview_badge, 1)
        self.only_actions = QCheckBox(tr('仅动作'))
        self.only_actions.setToolTip(tr('只显示已映射动作的触发结果，隐藏裸按键与未映射记录'))
        self.only_actions.toggled.connect(self._render)
        head.addWidget(self.only_actions)
        self.clear_button = QPushButton(tr('清空'))
        self.clear_button.setObjectName('pill')
        self.clear_button.setCursor(Qt.PointingHandCursor)
        self.clear_button.clicked.connect(self.clear)
        head.addWidget(self.clear_button)
        self.toggle_button = QPushButton(tr('收起'))
        self.toggle_button.setObjectName('pill')
        self.toggle_button.setCursor(Qt.PointingHandCursor)
        self.toggle_button.setCheckable(True)
        self.toggle_button.toggled.connect(self._toggle)
        head.addWidget(self.toggle_button)
        layout.addLayout(head)

        self.list = QListWidget()
        self.list.setAccessibleName(tr('触发日志'))
        self.list.setMinimumHeight(92)
        self.list.setMaximumHeight(150)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setStyleSheet(
            f"QListWidget {{ background: {TOKENS['base']}; border: 1px solid {TOKENS['border']};"
            f" border-radius: {TOKENS['r_sm']}px; padding: 3px; }}"
            f"QListWidget::item {{ font-size: 11px; border: none; padding: 1px 4px; min-height: 16px; }}")
        layout.addWidget(self.list)
        self.toggle_button.setChecked(True)   # collapsed until the user opens it

    def _toggle(self, collapsed):
        self.list.setVisible(not collapsed)
        self.toggle_button.setText(tr('展开') if collapsed else tr('收起'))

    def clear(self):
        self.rows = []
        self._render()

    def _color(self, row):
        if row['preview']:
            return TOKENS['amber']
        return TOKENS['ink_dim'] if row['kind'] == self.BARE else TOKENS['ink_2']

    def _render(self, *_ignored):
        hide_bare = self.only_actions.isChecked()
        self.list.clear()
        for row in reversed(self.rows):        # newest on top
            if hide_bare and row['kind'] == self.BARE:
                continue
            item = QListWidgetItem(('⚠ ' if row['preview'] else '') + row['text'])
            item.setForeground(QColor(self._color(row)))
            item.setToolTip(row['text'])
            self.list.addItem(item)
        shown = self.list.count()
        self.count_label.setText(f'{shown} 条' if shown else tr('暂无触发'))
        self.clear_button.setEnabled(bool(self.rows))

    @staticmethod
    def _stamp():
        now = time.time()
        return time.strftime('%H:%M:%S', time.localtime(now)) + f'.{int(now % 1 * 1000):03d}'

    def _action_row(self, event, preview, family):
        gesture = tr('长按') if event.get('gesture') == 'long' else tr('短按')
        text = (f"{self._stamp()}   {trigger_label(event.get('trigger', ''), family)} {gesture} → "
                + str(event.get('action', '')))
        return {'text': text, 'kind': self.ACTION, 'preview': preview}

    def _bare_row(self, key, verdict, preview, family):
        text = f"{self._stamp()}   {trigger_label(str(key), family)} {tr('按下')} · {verdict}"
        return {'text': text, 'kind': self.BARE, 'preview': preview}

    def _macro_row(self, event, family, preview):
        name = trigger_label(event.get('trigger', ''), family)
        reason = event.get('reason')
        steps, reached = event.get('steps', 0), event.get('reached', 0)
        if reason == 'complete':
            detail = f"{tr('宏完成')} · {steps} {tr('步')} · {event.get('elapsed', 0.):.2f}s"
        elif reason == 'interrupted':
            held_keys = event.get('held') or []
            held = trigger_label('+'.join(held_keys), family) if held_keys else ''
            detail = (f"{tr('宏被打断')} · {tr('停在第')} {reached}/{steps} {tr('步')}"
                      + (f" · {tr('已释放')} {held}" if held else ''))
        elif event.get('loop'):
            detail = f"{tr('宏循环')} {event.get('cycles', 1)} {tr('轮后松手停止')} · {event.get('elapsed', 0.):.2f}s"
        else:
            detail = f"{tr('宏松手停止')} · {tr('停在第')} {reached}/{steps} {tr('步')}"
        return {'text': f"{self._stamp()}   {name} {detail}", 'kind': self.ACTION, 'preview': preview}

    def record(self, events, sequence=0, preview=False, family='generic', presses=(), mappings=None,
               macro_events=(), macro_sequence=0):
        self.preview_badge.setVisible(bool(preview))
        if sequence < self.cursor:      # a restarted agent renumbers from zero
            self.cursor = 0
        changed = False
        fresh = [event for event in events if event.get('seq', 0) > self.cursor]
        if fresh:
            self.cursor = max(event.get('seq', 0) for event in fresh)
            self.rows.extend(self._action_row(event, preview, family) for event in fresh)
            changed = True
        if macro_sequence < self.macro_cursor:
            self.macro_cursor = 0
        fresh_macro = [event for event in macro_events if event.get('seq', 0) > self.macro_cursor]
        if fresh_macro:
            self.macro_cursor = max(event.get('seq', 0) for event in fresh_macro)
            self.rows.extend(self._macro_row(event, family, preview) for event in fresh_macro)
            changed = True
        if presses:
            dispatchable, prefix, blocked = press_verdicts(mappings)
            for key in sorted(presses, key=str):
                if key in dispatchable:
                    continue
                verdict = (tr('组合前缀') if key in prefix else
                           tr('已屏蔽') if key in blocked else tr('未映射'))
                self.rows.append(self._bare_row(key, verdict, preview, family))
            changed = True
        if changed:
            del self.rows[:-self.MAX_ROWS]
            self._render()
