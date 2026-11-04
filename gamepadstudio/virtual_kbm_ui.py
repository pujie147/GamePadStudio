"""
Virtual KBM Studio UI Component (标准布局虚拟键鼠与外设捕获工作台)
1. 完整展示标准 ANSI 布局的高精细度虚拟键盘与虚拟鼠标面板
2. 键帽上直接清晰展示绑定的手柄按键，明确区分【短按 (短)】与【长按 (长)】
3. 键盘全画幅自适应缩放（46px ~ 76px 基准），铺满整个视口，不浪费页面空间
4. 目标驱动式捕获流：“点击虚拟键鼠按键 -> 触发捕获监听 -> 按下任意手柄/外设 -> 即刻绑定”
5. 悬停卡片详情与右键快速解绑，集成预设方案与设备隐身
"""

import copy
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set, Union
from PySide6.QtCore import Qt, QSize, Signal, QUrl, QRect, QEvent, QTimer
from PySide6.QtGui import (
    QColor, QFont, QDesktopServices, QPainter, QPen, QBrush, QFontMetrics
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QComboBox, QLineEdit, QScrollArea,
    QFrame, QSlider, QInputDialog, QMessageBox, QSizePolicy, QCheckBox, QMenu, QLayout,
    QDialog, QDialogButtonBox, QFormLayout, QDoubleSpinBox, QSpinBox
)
from shiboken6 import isValid

from .glass import (
    TOKENS, GlassPanel, IconButton, Toggle,
    SquircleBadge, glyph
)
from .i18n import tr, tr_profile
from .controller_glyphs import (button_text, display_parts, draw_token,
                                token_advance, make_token_label)
from .response_curves import curve_capabilities
from .touch_ui import supports_touch
from .mapping_deck import FlowLayout
from .application_profiles import application_profiles_supported

WINDOWS_FEATURES = sys.platform == 'win32'

def label(text, kind=None, wrap=False):
    w = QLabel(text)
    if kind: w.setObjectName(kind)
    w.setWordWrap(wrap)
    return w


def button(text, callback, primary=False, icon=None, pill=False, danger=False):
    w = QPushButton(text); w.setCursor(Qt.PointingHandCursor)
    if primary: w.setObjectName('primary')
    elif danger: w.setObjectName('danger')
    elif pill: w.setObjectName('pill')
    if icon:
        if isinstance(icon, str):
            icon_color = '#ffffff' if primary else (TOKENS['red'] if danger else TOKENS['accent'])
            w.setIcon(glyph(icon, icon_color))
        else:
            w.setIcon(icon)
        w.setIconSize(QSize(15, 15))
    w.clicked.connect(callback)
    return w


def card(kind='card'):
    frame = GlassPanel(kind=kind)
    layout = QVBoxLayout(frame); layout.setContentsMargins(18, 16, 18, 16); layout.setSpacing(10)
    return frame, layout


def compact_trigger(raw_label: str) -> str:
    """兼容旧调用中的完整按键名称；正式界面使用输入 ID 选择符号"""
    if not raw_label:
        return ""
    s = str(raw_label).strip()
    replacements = [
        ('  交叉', ''), ('  Cross', ''),
        ('  圆圈', ''), ('  Circle', ''),
        ('  方块', ''), ('  Square', ''),
        ('  三角', ''), ('  Triangle', ''),
        ('方向键 ', ''), ('D-Pad ', ''),
        ('左摇杆按下', 'L3'), ('右摇杆按下', 'R3'),
        ('Left Stick Click', 'L3'), ('Right Stick Click', 'R3'),
        ('左摇杆上推满', 'LS↑推满'), ('左摇杆下推满', 'LS↓推满'),
        ('左摇杆左推满', 'LS←推满'), ('左摇杆右推满', 'LS→推满'),
        ('LS:up:outer', 'LS↑推满'), ('LS:down:outer', 'LS↓推满'),
        ('LS:left:outer', 'LS←推满'), ('LS:right:outer', 'LS→推满'),
        ('左摇杆', 'LS:'), ('右摇杆', 'RS:'),
        ('按住', ''), ('（按住）', ''),
    ]
    for a, b in replacements:
        s = s.replace(a, b)
    parts = [p.strip() for p in s.split('+')]
    return "+".join(parts)


def trigger_tokens(raw_label: str) -> Tuple[str, ...]:
    """Display controller modifiers first without changing a chord's identity."""
    aliases = {'触摸板': 'Touchpad', 'Touchpad': 'Touchpad',
               '麦克风': 'MIC', 'Mic': 'MIC', 'LS:推满': 'LS MAX',
               'LS:↑': 'LS↑', 'LS:↓': 'LS↓', 'LS:←': 'LS←', 'LS:→': 'LS→',
               'RS:↑': 'RS↑', 'RS:↓': 'RS↓', 'RS:←': 'RS←', 'RS:→': 'RS→',
               'LS:up:outer': 'LS↑推满', 'LS:down:outer': 'LS↓推满',
               'LS:left:outer': 'LS←推满', 'LS:right:outer': 'LS→推满',
               'LS:上推满': 'LS↑推满', 'LS:下推满': 'LS↓推满',
               'LS:左推满': 'LS←推满', 'LS:右推满': 'LS→推满'}
    parts = [aliases.get(part.strip(), part.strip()) for part in compact_trigger(raw_label).split('+') if part.strip()]
    modifiers = {'L1', 'R1', 'L2', 'R2', 'LB', 'RB', 'LT', 'RT', 'Ctrl', 'Shift', 'Alt', 'Win', 'Cmd', 'Meta',
                 'Create', 'SHARE', 'Share', 'View', 'Back', '−', '-'}
    return tuple([part for part in parts if part in modifiers] + [part for part in parts if part not in modifiers])


class KeyCap(QPushButton):
    """
    虚拟键帽：
    - 直接在键帽上展示绑定的手柄按键
    - 用颜色和细小长按标记区分手势，绑定符号不再套框
    - 支持自适应动态放大铺满屏幕
    - 鼠标悬停发光与详细卡片说明、左键编辑、右键快捷解绑
    """
    left_clicked = Signal(str, str)   # (key_id, display_name)
    right_clicked = Signal(str, str)  # (key_id, display_name)

    def __init__(self, key_id: str, display_name: str, width: Any = 1.0, height: Any = 1.0, is_special: bool = False, parent=None):
        super().__init__(parent)
        self.key_id = key_id
        self.display_name = display_name
        self.is_special = is_special

        # 兼容相对标准单位宽度 (1U = 1.0) 与历史像素宽度
        if isinstance(width, (int, float)) and width > 10:
            self.u_width = float(width) / 33.0
        else:
            self.u_width = float(width)

        if isinstance(height, (int, float)) and height > 10:
            self.u_height = float(height) / 34.0
        else:
            self.u_height = float(height)

        self.short_bindings: List[str] = []
        self.long_bindings: List[str] = []
        self.badges: List[str] = []
        self.binding_sources = {}
        self.is_capturing = False
        self.is_pressed = False
        self.is_hovered = False

        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(tr(display_name))
        self.setAttribute(Qt.WA_Hover, True)

        self.set_unit_size(52, 50, 4)
        self.update_tooltip()

    def set_unit_size(self, u_px: int, h_px: int, spacing: int = 4):
        """按全局自适应单位尺寸刷新实际像素大小"""
        w = max(24, int(round(self.u_width * u_px + (self.u_width - 1.0) * spacing)))
        h = max(24, int(round(self.u_height * h_px + (self.u_height - 1.0) * spacing)))
        self.setFixedSize(w, h)
        self.update()

    def enterEvent(self, event):
        self.is_hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.is_hovered = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.right_clicked.emit(self.key_id, self.display_name)
        elif event.button() == Qt.LeftButton:
            self.left_clicked.emit(self.key_id, self.display_name)
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.left_clicked.emit(self.key_id, self.display_name)
            event.accept()
            return
        super().keyPressEvent(event)

    def set_mapping_info(self, info: List[Dict[str, Any]], capturing: bool = False):
        """传入结构化映射信息，严格区分短按与长按"""
        self.short_bindings = []
        self.long_bindings = []
        self.badges = []
        self.binding_sources = {}
        self.is_capturing = capturing
        for item in info:
            trigger = item.get('trigger', '')
            gesture = item.get('gesture', 'short')
            self.binding_sources[(trigger, gesture == 'long')] = item
            if gesture == 'long':
                if trigger not in self.long_bindings:
                    self.long_bindings.append(trigger)
                self.badges.append(f"{trigger} 长按")
            else:
                if trigger not in self.short_bindings:
                    self.short_bindings.append(trigger)
                self.badges.append(f"{trigger} 短按")
        self.update_tooltip()
        self.update()

    def set_mapping_state(self, badges: List[Union[str, Dict[str, Any]]], capturing: bool = False):
        """兼容通用徽章输入"""
        self.short_bindings = []
        self.long_bindings = []
        self.badges = []
        self.binding_sources = {}
        self.is_capturing = capturing
        for item in badges:
            if isinstance(item, dict):
                trigger = item.get('trigger', '')
                gesture = item.get('gesture', 'short')
                self.binding_sources[(trigger, gesture == 'long')] = item
                if gesture == 'long':
                    if trigger not in self.long_bindings: self.long_bindings.append(trigger)
                    self.badges.append(f"{trigger} 长按")
                else:
                    if trigger not in self.short_bindings: self.short_bindings.append(trigger)
                    self.badges.append(f"{trigger} 短按")
            elif isinstance(item, str):
                self.badges.append(item)
                if '长按' in item or '长' in item:
                    t = item.replace(' 长按', '').replace('长按', '').replace('长', '').strip()
                    if t and t not in self.long_bindings: self.long_bindings.append(t)
                else:
                    t = item.replace(' 短按', '').replace('短按', '').replace('短', '').strip()
                    if t and t not in self.short_bindings: self.short_bindings.append(t)
        self.update_tooltip()
        self.update()

    def update_tooltip(self):
        desc = tr(KEY_DESCRIPTIONS.get(self.key_id, self.display_name))
        platform_tip = getattr(self, 'platform_tooltip', '')
        if not self.isEnabled() and platform_tip:
            self.setToolTip(platform_tip)
            return
        if self.is_pressed:
            self.setToolTip(f"【{desc}】正在触发物理输出...")
            return
        if self.is_capturing:
            self.setToolTip(f"【{desc}】正在等待外设输入...\n请在手柄、飞行摇杆或任意接入设备上按下按键或推轴")
            return
        lines = [f"虚拟按键：【{desc}】({self.key_id})"]
        if platform_tip:
            lines.append(platform_tip)
        if self.short_bindings or self.long_bindings:
            lines.append(f"已绑定手柄触发（{len(self.short_bindings) + len(self.long_bindings)} 项绑定）：")
            if self.short_bindings:
                lines.append(f"  • 短按 (Short Press)：{', '.join(self.short_bindings)}")
            if self.long_bindings:
                lines.append(f"  • 长按 (Long Press)：{', '.join(self.long_bindings)}")
            lines.append("\n💡 点击配置/修改 · 右键快速清除绑定")
        else:
            lines.append("未绑定外设触发\n\n💡 点击开始捕获手柄/外设按键")
        self.setToolTip("\n".join(lines))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        rect = self.rect()
        w, h = rect.width(), rect.height()
        radius = 6.0

        has_short = bool(self.short_bindings)
        has_long = bool(self.long_bindings)
        has_badges = has_short or has_long

        # 1. 背景与发光边框状态判决
        if self.is_pressed:
            # 物理按键触发输出高亮
            bg_brush = QBrush(QColor(255, 159, 10))
            border_pen = QPen(QColor(255, 200, 100), 1.5)
            text_color = QColor(18, 18, 20)
        elif self.is_capturing:
            # 捕获手柄输入脉冲
            bg_brush = QBrush(QColor(245, 158, 11, 40))
            border_pen = QPen(QColor(245, 158, 11), 1.8, Qt.DashLine)
            text_color = QColor(245, 158, 11)
        elif has_badges:
            # 已绑定：根据短按/长按提供高级辨识度光环
            if has_short and has_long:
                # 兼具短按与长按：深紫罗兰光环
                bg_color = QColor(36, 20, 56, 210 if not self.is_hovered else 240)
                border_color = QColor(168, 85, 247, 190 if not self.is_hovered else 255)
            elif has_long:
                # 仅长按：暖琥珀金光环
                bg_color = QColor(44, 26, 12, 210 if not self.is_hovered else 240)
                border_color = QColor(245, 158, 11, 190 if not self.is_hovered else 255)
            else:
                # 仅短按：电光青蓝光环
                bg_color = QColor(14, 32, 52, 210 if not self.is_hovered else 240)
                border_color = QColor(56, 189, 248, 190 if not self.is_hovered else 255)
            bg_brush = QBrush(bg_color)
            border_pen = QPen(border_color, 1.5 if not self.is_hovered else 2.0)
            text_color = QColor(255, 255, 255)
        else:
            # 未绑定默认深色质感
            bg_color = QColor(TOKENS['base'] if self.is_special else TOKENS['elevated'])
            if self.is_hovered:
                bg_color = QColor(TOKENS['overlay'])
            border_color = QColor(TOKENS['border_hi'] if self.is_hovered else TOKENS['border'])
            bg_brush = QBrush(bg_color)
            border_pen = QPen(border_color, 1.0)
            text_color = QColor(TOKENS['ink'] if self.is_hovered else TOKENS['ink_2'])

        if self.hasFocus():
            border_pen = QPen(QColor(TOKENS['accent']), 2.0)

        painter.setBrush(bg_brush)
        painter.setPen(border_pen)
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), radius, radius)

        # 2. 键名与手柄按键文字绘制
        main_font = QFont("Microsoft YaHei", -1)
        main_font.setFamilies(["Microsoft YaHei", "Segoe UI", "PingFang SC", "sans-serif"])
        if len(self.display_name) <= 2:
            main_font.setPixelSize(max(11, min(15, int(h * 0.30))))
        elif len(self.display_name) <= 5:
            main_font.setPixelSize(max(10, min(13, int(h * 0.25))))
        else:
            main_font.setPixelSize(max(9, min(11, int(h * 0.22))))
        main_font.setBold(True)
        display_plan = self.binding_display_plan(w - 6, 22)
        stacked_chord = any(item.get('stacked') for item in display_plan)
        if stacked_chord and h < 40:
            main_font.setPixelSize(min(9, main_font.pixelSize()))
        while main_font.pixelSize() > 7 and QFontMetrics(main_font).horizontalAdvance(self.display_name) > w - 6:
            main_font.setPixelSize(main_font.pixelSize() - 1)
        painter.setFont(main_font)
        painter.setPen(text_color)

        if not (self.is_capturing or has_badges):
            painter.drawText(rect, Qt.AlignCenter, self.display_name)
        else:
            # 键名和徽章组成紧凑内容组；双行高键帽也保持在正中。
            badge_h = max(18, min(24, int(h * 0.40)))
            if stacked_chord:
                badge_h = 22
            gap = 1 if stacked_chord and h < 40 else max(2, min(4, int(h * 0.05)))
            title_min = 8 if stacked_chord and h < 40 else 12
            title_h = min(QFontMetrics(main_font).height(), max(title_min, h - badge_h - gap - 4))
            content_h = title_h + gap + badge_h
            content_y = (h - content_h) // 2
            top_rect = QRect(3, content_y, w - 6, title_h)
            painter.drawText(top_rect, Qt.AlignCenter, self.display_name)

            # 底部空间绘制绑定的手柄按键与长按/短按标识
            badge_rect = QRect(3, content_y + title_h + gap, w - 6, badge_h)
            if self.is_capturing:
                cap_font = QFont("Microsoft YaHei", max(8, min(10, int(h * 0.20))))
                cap_font.setFamilies(["Microsoft YaHei", "Segoe UI", "PingFang SC", "sans-serif"])
                cap_font.setBold(True)
                painter.setFont(cap_font)
                painter.setPen(QColor(245, 158, 11))
                painter.drawText(badge_rect, Qt.AlignCenter, tr("⏳ 捕获中"))
            else:
                self._draw_badges(painter, badge_rect)

    def _draw_badges(self, painter, rect):
        plan = self.binding_display_plan(rect.width(), rect.height())
        if not plan:
            return
        total_w = sum(item['width'] for item in plan) + 8 * (len(plan) - 1)
        x = rect.x() + (rect.width() - total_w) // 2
        painter.save()
        painter.setClipRect(rect)
        for index, item in enumerate(plan):
            area = QRect(x, rect.y(), item['width'], rect.height())
            if item['kind'] == 'count':
                self._draw_binding_count(painter, area, item['count'], item['overflow'])
            else:
                self._draw_binding(painter, area, item)
            x += item['width'] + 8
            if index < len(plan) - 1:
                painter.setPen(QPen(QColor(TOKENS['ink_3']), 1))
                painter.drawLine(x - 4, rect.center().y() - 3, x - 4, rect.center().y() + 3)
        painter.restore()

    @staticmethod
    def _binding_font(size):
        font = QFont('Segoe UI', -1)
        font.setFamilies(['Segoe UI', 'Microsoft YaHei', 'sans-serif'])
        font.setPixelSize(size)
        font.setBold(True)
        return font

    def _binding_items(self):
        items = []
        for bindings, is_long in ((self.short_bindings, False), (self.long_bindings, True)):
            for trigger in bindings:
                source = self.binding_sources.get((trigger, is_long), {})
                family = source.get('family', 'generic')
                keys = display_parts(source['input']) if source.get('input') else ()
                tokens = tuple(button_text(key, family) for key in keys) if keys else trigger_tokens(trigger)
                items.append({'tokens': tokens, 'keys': keys, 'family': family, 'long': is_long})
        return items

    def _token_widths(self, item, font):
        if item['keys']:
            return [token_advance(key, item['family'], font) for key in item['keys']]
        fm = QFontMetrics(font)
        return [max(5, fm.horizontalAdvance(token)) for token in item['tokens']]

    def binding_display_plan(self, width, height):
        """Fit bare button marks first; wrap only when a full chord needs it."""
        items = self._binding_items()
        if not items:
            return []
        minimum = 7 if width < 48 else 8
        candidates = []
        for size in range(min(14, max(8, height - 3)), minimum - 1, -1):
            font = self._binding_font(size)
            measured = []
            for item in items:
                widths = self._token_widths(item, font)
                measured.append(dict(item, kind='binding', font=size, stacked=False,
                                     width=sum(widths) + 6 * (len(widths) - 1) + (5 if item['long'] else 0)))
            candidates.append(measured)
            if sum(item['width'] for item in measured) + 8 * (len(measured) - 1) <= width:
                return measured
        if len(items) == 1:
            return [dict(items[0], kind='binding', font=max(8, min(11, (height - 2) // 2)),
                         stacked=len(items[0]['tokens']) > 1, width=width)]
        # A count describes independent bindings, never the members of a chord.
        count_width = min(width, max(55 if width >= 55 else 0,
            QFontMetrics(self._binding_font(9)).horizontalAdvance(
                self.binding_count_text(len(items), True, width)) + 2))
        shown = []
        for measured in candidates:
            candidate = []
            for item in measured:
                used = sum(entry['width'] for entry in candidate) + 8 * len(candidate)
                if used + item['width'] + 8 + count_width > width:
                    break
                candidate.append(item)
            if len(candidate) > len(shown):
                shown = candidate
        omitted = len(items) - len(shown)
        return shown + [{'kind': 'count', 'count': omitted, 'overflow': bool(shown), 'width': count_width}]

    def _draw_token_row(self, painter, rect, item, start, end, font, color, long_mark=False):
        tokens = item['tokens'][start:end]
        keys = item['keys'][start:end]
        part = dict(item, tokens=tokens, keys=keys)
        widths = self._token_widths(part, font)
        mark_width = 5 if long_mark else 0
        total = sum(widths) + 6 * (len(widths) - 1) + mark_width
        x = rect.x() + (rect.width() - total) // 2
        painter.setFont(font)
        painter.setPen(color)
        if total > rect.width():
            # Keep every member visible in unusual three/four-button chords.
            # The full-width keys use the ordinary size; only a tight row scales.
            painter.save()
            painter.translate(rect.x(), rect.y())
            painter.scale(rect.width() / total, 1)
            self._draw_token_row(painter, QRect(0, 0, total, rect.height()), item,
                                 start, end, font, color, long_mark)
            painter.restore()
            return
        if long_mark:
            painter.setPen(QPen(color, 2, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(x, rect.center().y(), x + 2, rect.center().y())
            x += mark_width
        for index, (token, token_width) in enumerate(zip(tokens, widths)):
            area = QRect(x, rect.y(), token_width, rect.height())
            if keys:
                draw_token(painter, area, keys[index], item['family'], font, color)
            else:
                painter.setFont(font)
                painter.setPen(color)
                painter.drawText(area, Qt.AlignCenter, token)
            x += token_width
            if index < len(tokens) - 1:
                painter.setFont(font)
                painter.setPen(QColor(TOKENS['ink_3']))
                painter.drawText(QRect(x, rect.y(), 6, rect.height()), Qt.AlignCenter, '+')
                x += 6

    def _draw_binding(self, painter, rect, item):
        color = QColor('#fbbf24' if item['long'] else '#7dd3fc')
        if self.is_pressed:
            color = QColor('#ffffff')
        font = self._binding_font(item['font'])
        if item['stacked']:
            row_h = max(8, (rect.height() - 1) // 2)
            prefix_rect = QRect(rect.x(), rect.y(), rect.width() - 5, row_h)
            self._draw_token_row(painter, prefix_rect, item, 0, len(item['tokens']) - 1, font, color)
            painter.setFont(font)
            painter.setPen(QColor(TOKENS['ink_3']))
            painter.drawText(QRect(rect.right() - 4, rect.y(), 5, row_h), Qt.AlignCenter, '+')
            self._draw_token_row(painter, QRect(rect.x(), rect.y() + row_h + 1, rect.width(), row_h),
                                 item, len(item['tokens']) - 1, len(item['tokens']), font, color, item['long'])
        else:
            self._draw_token_row(painter, rect, item, 0, len(item['tokens']), font, color, item['long'])

    @staticmethod
    def binding_count_text(count, overflow, width):
        suffix = tr('组绑定') if width >= 55 else tr('组')
        return ('+' if overflow else '') + str(count) + suffix

    def _draw_binding_count(self, painter, rect, count, overflow):
        text = self.binding_count_text(count, overflow, rect.width())
        font = self._binding_font(9)
        while font.pixelSize() > 7 and QFontMetrics(font).horizontalAdvance(text) > rect.width():
            font.setPixelSize(font.pixelSize() - 1)
        painter.setFont(font)
        painter.setPen(QColor(TOKENS['ink_2']))
        painter.drawText(rect, Qt.AlignCenter, text)


# 标准 108 键主打字区 (60% Typing Block + F-Row) - 6 行每行精准对齐至 15.00U
TYPING_BLOCK_ROWS = [
    # 行 0：Esc 与 F1~F12 功能键区 (1.0 + 0.66 + 4.0 + 0.67 + 4.0 + 0.67 + 4.0 = 15.00U)
    [
        ("Esc", "Esc", 1.0), ("__gap__", "", 0.66),
        ("F1", "F1", 1.0), ("F2", "F2", 1.0), ("F3", "F3", 1.0), ("F4", "F4", 1.0), ("__gap__", "", 0.67),
        ("F5", "F5", 1.0), ("F6", "F6", 1.0), ("F7", "F7", 1.0), ("F8", "F8", 1.0), ("__gap__", "", 0.67),
        ("F9", "F9", 1.0), ("F10", "F10", 1.0), ("F11", "F11", 1.0), ("F12", "F12", 1.0),
    ],
    # 行 1：数字与符号行 (13*1.0 + 2.0 = 15.00U)
    [
        ("`", "` ~", 1.0), ("1", "1", 1.0), ("2", "2", 1.0), ("3", "3", 1.0), ("4", "4", 1.0),
        ("5", "5", 1.0), ("6", "6", 1.0), ("7", "7", 1.0), ("8", "8", 1.0), ("9", "9", 1.0),
        ("0", "0", 1.0), ("-", "- _", 1.0), ("=", "= +", 1.0), ("Backspace", "Back", 2.0),
    ],
    # 行 2：QWERTY 字母行 (1.5 + 12*1.0 + 1.5 = 15.00U)
    [
        ("Tab", "Tab", 1.5), ("Q", "Q", 1.0), ("W", "W", 1.0), ("E", "E", 1.0), ("R", "R", 1.0),
        ("T", "T", 1.0), ("Y", "Y", 1.0), ("U", "U", 1.0), ("I", "I", 1.0), ("O", "O", 1.0),
        ("P", "P", 1.0), ("[", "[ {", 1.0), ("]", "] }", 1.0), ("\\", "\\ |", 1.5),
    ],
    # 行 3：ASDF 基准行 (1.75 + 11*1.0 + 2.25 = 15.00U)
    [
        ("Caps", "Caps", 1.75), ("A", "A", 1.0), ("S", "S", 1.0), ("D", "D", 1.0), ("F", "F", 1.0),
        ("G", "G", 1.0), ("H", "H", 1.0), ("J", "J", 1.0), ("K", "K", 1.0), ("L", "L", 1.0),
        (";", "; :", 1.0), ("'", "' \"", 1.0), ("Enter", "Enter", 2.25),
    ],
    # 行 4：ZXCV 行 (2.25 + 10*1.0 + 2.75 = 15.00U)
    [
        ("Shift", "Shift", 2.25), ("Z", "Z", 1.0), ("X", "X", 1.0), ("C", "C", 1.0), ("V", "V", 1.0),
        ("B", "B", 1.0), ("N", "N", 1.0), ("M", "M", 1.0), (",", ", <", 1.0), (".", ". >", 1.0),
        ("/", "/ ?", 1.0), ("RShift", "Shift", 2.75),
    ],
    # 行 5：控制修饰与空格 (1.25*3 + 6.25 + 1.25*4 = 15.00U)
    [
        ("Ctrl", "Ctrl", 1.25), ("Win", "Win", 1.25), ("Alt", "Alt", 1.25),
        ("Space", "Space (空格)", 6.25),
        ("RAlt", "Alt", 1.25), ("Fn", "Fn", 1.25), ("Menu", "Menu", 1.25), ("RCtrl", "Ctrl", 1.25),
    ],
]

# 独立导航与方向键区 (Navigation & Arrows Block) - 3 列 6 行，精准固定 3.00U
NAV_BLOCK_ROWS = [
    # 行 0：控制功能三键
    [("PrtScn", "PrtSc", 1.0), ("ScrLk", "ScrLk", 1.0), ("Pause", "Pause", 1.0)],
    # 行 1：编辑导航上三键
    [("Insert", "Ins", 1.0), ("Home", "Home", 1.0), ("PgUp", "PgUp", 1.0)],
    # 行 2：编辑导航下三键
    [("Delete", "Del", 1.0), ("End", "End", 1.0), ("PgDn", "PgDn", 1.0)],
    # 行 3：功能留白占位 (高度 1.0U，与 ASDF 基准行齐平)
    [("__empty__", "", 3.0)],
    # 行 4：上方向键居中 (空 1.0U + 上 1.0U + 空 1.0U)
    [("__empty__", "", 1.0), ("Up", "↑", 1.0), ("__empty__", "", 1.0)],
    # 行 5：下左右方向键 (左 1.0U + 下 1.0U + 右 1.0U)
    [("Left", "←", 1.0), ("Down", "↓", 1.0), ("Right", "→", 1.0)],
]

# 标准 108 键数字小键盘区 (Numpad Block) - 6 行 4 列网格布局，严格固定 4.00U
# 每个元组定义：(row, col, rowspan, colspan, key_id, display_name, u_width, u_height)
NUMPAD_BLOCK_GRID = [
    # 行 0：108 键专属顶部多媒体/快捷四键
    (0, 0, 1, 1, "Mute", "Mute", 1.0, 1.0),
    (0, 1, 1, 1, "Vol-", "Vol -", 1.0, 1.0),
    (0, 2, 1, 1, "Vol+", "Vol +", 1.0, 1.0),
    (0, 3, 1, 1, "Calc", "Calc", 1.0, 1.0),
    # 行 1：小键盘控制与基础四则运算
    (1, 0, 1, 1, "NumLock", "Num", 1.0, 1.0),
    (1, 1, 1, 1, "Num/", "/", 1.0, 1.0),
    (1, 2, 1, 1, "Num*", "*", 1.0, 1.0),
    (1, 3, 1, 1, "Num-", "-", 1.0, 1.0),
    # 行 2 & 3：7 8 9 与 2U 高度 + 加号
    (2, 0, 1, 1, "Num7", "7", 1.0, 1.0),
    (2, 1, 1, 1, "Num8", "8", 1.0, 1.0),
    (2, 2, 1, 1, "Num9", "9", 1.0, 1.0),
    (2, 3, 2, 1, "Num+", "+", 1.0, 2.0),
    (3, 0, 1, 1, "Num4", "4", 1.0, 1.0),
    (3, 1, 1, 1, "Num5", "5", 1.0, 1.0),
    (3, 2, 1, 1, "Num6", "6", 1.0, 1.0),
    # 行 4 & 5：1 2 3 与 2U 宽 0、. 以及 2U 高度 Enter
    (4, 0, 1, 1, "Num1", "1", 1.0, 1.0),
    (4, 1, 1, 1, "Num2", "2", 1.0, 1.0),
    (4, 2, 1, 1, "Num3", "3", 1.0, 1.0),
    (4, 3, 2, 1, "NumEnter", "Enter", 1.0, 2.0),
    (5, 0, 1, 2, "Num0", "0", 2.0, 1.0),
    (5, 2, 1, 1, "Num.", ".", 1.0, 1.0),
]

KEY_DESCRIPTIONS = {
    "Mute": "静音 (Mute)",
    "Vol-": "音量- (Vol -)",
    "Vol+": "音量+ (Vol +)",
    "Calc": "计算器 (Calc)",
    "NumLock": "数字键盘锁定 (NumLock)",
    "NumEnter": "小键盘回车 (Num Enter)",
    "Num+": "小键盘加号 (Num +)",
    "Num-": "小键盘减号 (Num -)",
    "Num*": "小键盘乘号 (Num *)",
    "Num/": "小键盘除号 (Num /)",
    "Num.": "小键盘点 (Num .)",
    "Num0": "小键盘 0",
    "Num1": "小键盘 1",
    "Num2": "小键盘 2",
    "Num3": "小键盘 3",
    "Num4": "小键盘 4",
    "Num5": "小键盘 5",
    "Num6": "小键盘 6",
    "Num7": "小键盘 7",
    "Num8": "小键盘 8",
    "Num9": "小键盘 9",
    "mouse:left": "鼠标左键 (普攻/快门/点击)",
    "mouse:right": "鼠标右键 (瞄准/蓄力/交互)",
    "mouse:middle": "鼠标中键",
    "mouse:wheel_up": "滚轮向上",
    "mouse:wheel_down": "滚轮向下",
    "action:capture": "快速截屏 (4K 无损)",
    "action:replay_record": "精彩回放 (回溯录像)",
    "action:record_toggle": "录屏开关",
}

KEYBOARD_LAYOUT = TYPING_BLOCK_ROWS

MOUSE_CONTROLS = [
    ("mouse:left", "左键", 2.4),
    ("mouse:right", "右键", 2.4),
    ("mouse:middle", "中键", 1.8),
    ("mouse:wheel_up", "滚轮 ↑", 1.8),
    ("mouse:wheel_down", "滚轮 ↓", 1.8),
]

ACTION_CONTROLS = [
    ("action:capture", "快速截屏", 3.8),
    ("action:replay_record", "精彩回放", 4.4),
    ("action:record_toggle", "录屏开关", 4.0),
]


class KeyboardCanvas(QWidget):
    """自适应全画幅键盘画布，监听视口缩放并自适应调整键帽基准像素"""
    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self._last_u = 0

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if (isValid(self.page) and hasattr(self.page, 'area') and isValid(self.page.area)
                and not getattr(self.page.owner, 'closed', False)):
            self.fit_width(max(0, self.page.area.viewport().width() - 32))

    def fit_width(self, width):
        if not isValid(self.page) or getattr(self.page.owner, 'closed', False):
            return
        # 按实际视口计算，避免固定键帽的最小尺寸阻止画布在窄窗口中缩小。
        target_u = max(28, min(100, int((width - 140) / 22.4)))
        if target_u != self._last_u:
            self._last_u = target_u
            target_h = max(34, int(round(target_u * 0.94)))
            self.page.recompute_key_sizes(target_u, target_h)


class KbmFeelDialog(QDialog):
    """One profile's pointer, input response and optional walking controls."""

    def __init__(self, owner, profile, parent=None):
        super().__init__(parent or owner)
        from .device_quirks import gyro_protocol_hint
        from .mapping_engine import effective_mappings, input_thresholds, output_tokens, input_sources
        from .studio_core import profile_scope
        self.owner = owner
        self.device_identity = (profile_scope(owner.snapshot), (owner.snapshot or {}).get('instance_id'))
        sources = set(input_sources(owner.snapshot))
        self.has_stick = any(key.startswith(('LS:', 'RS:')) for key in sources)
        self.has_walk = 'LS:inner' in sources
        self.has_trigger = bool(sources & {'LT', 'RT'})
        self.profile = profile
        self.original = copy.deepcopy(owner.config.get('profile_options', {}).get(profile, {}))
        self.mouse = copy.deepcopy(self.original.get('mouse') or {})
        has_rs = bool(sources & {'RS:left', 'RS:right', 'RS:up', 'RS:down'})
        has_ls = bool(sources & {'LS:left', 'LS:right', 'LS:up', 'LS:down'})
        self.has_pointer = has_rs or (self.mouse.get('stick_source') == 'left' and has_ls)
        self.has_gyro = bool((owner.snapshot or {}).get('gyro')) or ('gyro_enabled' in self.mouse)
        self.gyro_protocol_hint = gyro_protocol_hint(owner.snapshot)
        self.input_settings = copy.deepcopy(self.original.get('input') or {})
        thresholds = input_thresholds(self.input_settings)
        entry = effective_mappings(owner.config, owner.snapshot, profile).get('LS:inner', {})
        self.can_walk = any('key:17' in output_tokens(entry.get(g, {})) for g in ('short', 'long'))
        self.setWindowTitle(tr('操作手感'))
        self.setMinimumWidth(440)
        self.resize(min(620, max(440, owner.width() - 80)), min(760, max(480, owner.height() - 100)))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(label(tr_profile(profile), 'section', True))
        layout.addWidget(label(tr('调整当前预设的视角、按键响应与行走手感。'), 'caption', True))

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.NoFrame)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 0)
        body.setSpacing(14)

        pointer, pointer_layout = card()
        pointer_layout.addWidget(label(tr('视角与指针'), 'section'))
        pointer_form = QFormLayout()
        pointer_form.setHorizontalSpacing(18)
        pointer_form.setVerticalSpacing(10)
        self.mode = QComboBox()
        self.mode.addItem(tr('游戏视角'), 'game')
        self.mode.addItem(tr('桌面指针'), 'desktop')
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.mouse.get('mode', 'game'))))
        pointer_form.addRow(tr('用途'), self.mode)
        self.stick_source = QComboBox()
        self.stick_source.addItem(tr('右摇杆 (默认)'), 'right')
        self.stick_source.addItem(tr('左摇杆'), 'left')
        self.stick_source.setCurrentIndex(1 if self.mouse.get('stick_source') == 'left' else 0)
        self.stick_source.setAccessibleName(tr('控制摇杆'))
        pointer_form.addRow(tr('控制摇杆'), self.stick_source)
        self.invert_y = QComboBox()
        self.invert_y.addItem(tr('常规'), False)
        self.invert_y.addItem(tr('反转'), True)
        self.invert_y.setCurrentIndex(1 if self.mouse.get('invert_y') is True else 0)
        self.invert_y.setAccessibleName(tr('垂直视角方向'))
        self.invert_y.setToolTip(tr('只影响游戏视角；桌面指针始终使用常规方向。'))
        self.invert_y.setEnabled(self.mode.currentData() == 'game')
        self.mode.currentIndexChanged.connect(
            lambda _index: self.invert_y.setEnabled(self.mode.currentData() == 'game'))
        pointer_form.addRow(tr('垂直视角方向'), self.invert_y)
        self.mouse_fields = {}
        for key, title, low, high, default in (
            ('sensitivity', '转向速度', 1, 100, 28),
            ('deadzone', '居中容错', 1, 50, 6),
            ('y_ratio', '垂直速度比例', .1, 2, .7),
            ('edge_boost', '推满加速', 1, 3, 1.7),
        ):
            field = QDoubleSpinBox()
            field.setRange(low, high)
            field.setSingleStep(.05 if high <= 3 else 1)
            field.setDecimals(2 if high <= 3 else 0)
            value = self.mouse.get(key, default / 100 if key == 'deadzone' else default)
            field.setValue(value * 100 if key == 'deadzone' else value)
            if key == 'deadzone':
                field.setSuffix(' %')
            pointer_form.addRow(tr(title), field)
            self.mouse_fields[key] = field
        pointer_layout.addLayout(pointer_form)
        pointer_layout.addWidget(label(tr('居中容错越大，越不容易因摇杆漂移而转动视角。'), 'caption', True))
        body.addWidget(pointer)
        pointer.setVisible(self.has_pointer)

        gyro_card, gyro_layout = card()
        gyro_layout.addWidget(label(tr('陀螺仪体感瞄准'), 'section'))
        gyro_layout.addWidget(label(tr('通过手柄陀螺仪微调准星，适合射击与精细瞄准。'), 'caption', True))
        self.gyro_enabled_toggle = Toggle(tr('启用体感瞄准'))
        self.gyro_enabled_toggle.setChecked(bool(self.mouse.get('gyro_enabled', False)))
        gyro_layout.addWidget(self.gyro_enabled_toggle)

        self.gyro_controls = QWidget()
        gyro_form = QFormLayout(self.gyro_controls)
        gyro_form.setContentsMargins(0, 4, 0, 0)
        gyro_form.setHorizontalSpacing(18)
        gyro_form.setVerticalSpacing(10)

        self.gyro_trigger = QComboBox()
        self.gyro_trigger.addItem(tr('按住 L2/LT 激活'), 'LT')
        self.gyro_trigger.addItem(tr('按住 R2/RT 激活'), 'RT')
        self.gyro_trigger.addItem(tr('按住 L1/LB 激活'), '9')
        self.gyro_trigger.addItem(tr('按住 R1/RB 激活'), '10')
        self.gyro_trigger.addItem(tr('常驻开启'), 'always')
        current_trigger = self.mouse.get('gyro_trigger', 'LT')
        idx_t = self.gyro_trigger.findData(current_trigger)
        self.gyro_trigger.setCurrentIndex(max(0, idx_t))
        gyro_form.addRow(tr('激活方式'), self.gyro_trigger)

        self.gyro_sensitivity = QDoubleSpinBox()
        self.gyro_sensitivity.setRange(1.0, 100.0)
        self.gyro_sensitivity.setSingleStep(1.0)
        self.gyro_sensitivity.setDecimals(1)
        self.gyro_sensitivity.setValue(float(self.mouse.get('gyro_sensitivity', 18.0)))
        gyro_form.addRow(tr('体感灵敏度'), self.gyro_sensitivity)

        self.gyro_invert_y = QComboBox()
        self.gyro_invert_y.addItem(tr('常规'), False)
        self.gyro_invert_y.addItem(tr('反转'), True)
        self.gyro_invert_y.setCurrentIndex(1 if self.mouse.get('gyro_invert_y') is True else 0)
        gyro_form.addRow(tr('Y 轴反转'), self.gyro_invert_y)

        gyro_layout.addWidget(self.gyro_controls)
        self.gyro_enabled_toggle.toggled.connect(self.gyro_controls.setEnabled)
        self.gyro_controls.setEnabled(self.gyro_enabled_toggle.isChecked())
        body.addWidget(gyro_card)
        gyro_card.setVisible(self.has_gyro)
        self.gyro_hint_label = label(self.gyro_protocol_hint, 'caption', True)
        body.addWidget(self.gyro_hint_label)
        self.gyro_hint_label.setVisible(bool(self.has_stick and not self.has_gyro and self.gyro_protocol_hint))

        response, response_layout = card()
        response_layout.addWidget(label(tr('按键响应'), 'section'))
        response_layout.addWidget(label(tr('开始响应要高于松开位置，避免边缘反复触发。'), 'caption', True))
        response_form = QFormLayout()
        response_form.setHorizontalSpacing(18)
        response_form.setVerticalSpacing(10)
        self.input_fields = {}
        for group, title in (('stick', '摇杆方向'), ('trigger', '扳机按键'), ('outer', '外圈推满')):
            if not (self.has_stick if group in ('stick', 'outer') else self.has_trigger):
                continue
            pair = QWidget()
            row = QHBoxLayout(pair)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            for suffix, caption in (('press', '开始响应'), ('release', '松开位置')):
                key = group + '_' + suffix
                field = QSpinBox()
                field.setRange(1, 100)
                field.setSuffix(' %')
                field.setValue(round(thresholds[key] * 100))
                field.setAccessibleName(tr(title) + ' · ' + tr(caption))
                column = QVBoxLayout()
                column.setSpacing(4)
                column.addWidget(label(tr(caption), 'caption', True))
                column.addWidget(field)
                row.addLayout(column, 1)
                self.input_fields[key] = field
            response_form.addRow(tr(title), pair)
        if self.has_stick:
            self.directional_outer_toggle = Toggle(tr('分方向独立推满阈值'))
            has_directional_outer = any(f'outer_{d}_press' in self.input_settings or f'outer_{d}_release' in self.input_settings for d in ('up', 'down', 'left', 'right'))
            self.directional_outer_toggle.setChecked(has_directional_outer)
            self.directional_outer_container = QWidget()
            directional_form = QFormLayout(self.directional_outer_container)
            directional_form.setContentsMargins(0, 4, 0, 0)
            directional_form.setHorizontalSpacing(18)
            directional_form.setVerticalSpacing(8)
            for d, d_title in (('up', '上推满'), ('down', '下推满'), ('left', '左推满'), ('right', '右推满')):
                d_pair = QWidget()
                d_row = QHBoxLayout(d_pair)
                d_row.setContentsMargins(0, 0, 0, 0)
                d_row.setSpacing(8)
                for suffix, caption in (('press', '开始响应'), ('release', '松开位置')):
                    key = f'outer_{d}_{suffix}'
                    field = QSpinBox()
                    field.setRange(1, 100)
                    field.setSuffix(' %')
                    field.setValue(round(thresholds[key] * 100))
                    field.setAccessibleName(tr(d_title) + ' · ' + tr(caption))
                    column = QVBoxLayout()
                    column.setSpacing(4)
                    column.addWidget(label(tr(caption), 'caption', True))
                    column.addWidget(field)
                    d_row.addLayout(column, 1)
                    self.input_fields[key] = field
                directional_form.addRow(tr(d_title), d_pair)
            self.directional_outer_toggle.toggled.connect(self.directional_outer_container.setVisible)
            self.directional_outer_container.setVisible(self.directional_outer_toggle.isChecked())
            response_form.addRow(self.directional_outer_toggle)
            response_form.addRow(self.directional_outer_container)
        if self.has_stick:
            self.suppress_ls_up = QComboBox()
            self.suppress_ls_up.addItem(tr('正常响应 (默认)'), 'off')
            self.suppress_ls_up.addItem(tr('搓招防误跳 (横推/斜推时不轻易跳起)'), 'filter')
            self.suppress_ls_up.addItem(tr('禁用摇杆向上 (Hitbox/独立跳跃键流派)'), 'disable')
            cur_suppress = thresholds.get('suppress_ls_up', 'off')
            if cur_suppress is True:
                cur_suppress = 'filter'
            idx = max(0, self.suppress_ls_up.findData(cur_suppress))
            self.suppress_ls_up.setCurrentIndex(idx)
            self.suppress_ls_up.setToolTip(tr('格斗游戏搓招（前下后 236/214）时防止因摇杆划过上半区而误触发向上跳跃。'))
            response_form.addRow(tr('格斗防串跳'), self.suppress_ls_up)
        self.chord_window = QSpinBox()
        self.chord_window.setRange(20, 200)
        self.chord_window.setSuffix(' ms')
        self.chord_window.setValue(round(thresholds['chord_window'] * 1000))
        response_form.addRow(tr('组合识别时间'), self.chord_window)
        response_layout.addLayout(response_form)
        response_layout.addWidget(label(tr('给同时按下的两个按键留一点余量；时间越短，单键响应越快。'), 'caption', True))
        body.addWidget(response)

        walking, walking_layout = card()
        self.walk_toggle = Toggle(tr('轻推慢走'))
        self.walk_toggle.setChecked(self.can_walk and 'walk_press' in thresholds)
        self.walk_toggle.setEnabled(self.can_walk)
        walking_layout.addWidget(self.walk_toggle)
        walking_layout.addWidget(label(tr('轻推时按住 Ctrl，推深后恢复正常跑。冲刺仍由独立按键控制。')
                                       if self.can_walk else tr('先将“左摇杆轻推”映射为 Ctrl，即可启用轻推慢走。'), 'caption', True))
        self.walk_controls = QWidget()
        walk_form = QFormLayout(self.walk_controls)
        walk_form.setContentsMargins(0, 4, 0, 0)
        walk_form.setVerticalSpacing(10)
        for key, title, default in (('walk_press', '回到慢走', .62), ('walk_release', '转为正常跑', .72)):
            field = QSpinBox()
            field.setRange(1, 100)
            field.setSuffix(' %')
            field.setValue(round(thresholds.get(key, default) * 100))
            walk_form.addRow(tr(title), field)
            self.input_fields[key] = field
        walking_layout.addWidget(self.walk_controls)
        self.walk_toggle.toggled.connect(self.walk_controls.setEnabled)
        self.walk_controls.setEnabled(self.walk_toggle.isChecked())
        body.addWidget(walking)
        walking.setVisible(self.has_walk)
        body.addStretch()
        self.area.setWidget(content)
        layout.addWidget(self.area, 1)
        self.error = label('', wrap=True)
        self.error.setStyleSheet(f"color: {TOKENS['amber']};")
        self.error.hide()
        layout.addWidget(self.error)
        self.controls = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.controls.button(QDialogButtonBox.Save).setText(tr('保存'))
        self.controls.button(QDialogButtonBox.Cancel).setText(tr('取消'))
        self.controls.accepted.connect(self.validate)
        self.controls.rejected.connect(self.reject)
        layout.addWidget(self.controls)
        self.device_timer = QTimer(self)
        self.device_timer.timeout.connect(self.check_device)
        self.device_timer.start(200)

    def check_device(self):
        from .studio_core import profile_scope
        state = self.owner.snapshot
        changed = self.device_identity != (profile_scope(state), (state or {}).get('instance_id'))
        self.controls.button(QDialogButtonBox.Save).setEnabled(not changed)
        if changed:
            self.error.setText(tr('设备已改变，请重新打开映射编辑器。'))
            self.error.show()
        return not changed

    def validate(self):
        if not self.check_device():
            return
        values = {key: field.value() for key, field in self.input_fields.items()}
        for group, title in (('stick', '摇杆方向'), ('trigger', '扳机按键'), ('outer', '外圈推满')):
            if group + '_press' not in values:
                continue
            if values[group + '_release'] >= values[group + '_press']:
                self.error.setText(tr(title) + '：' + tr('松开位置必须低于开始响应。'))
                self.error.show()
                return
        if hasattr(self, 'directional_outer_toggle') and self.directional_outer_toggle.isChecked():
            for d, d_title in (('up', '上推满'), ('down', '下推满'), ('left', '左推满'), ('right', '右推满')):
                p_key, r_key = f'outer_{d}_press', f'outer_{d}_release'
                if p_key in values and r_key in values:
                    if values[r_key] >= values[p_key]:
                        self.error.setText(tr(d_title) + '：' + tr('松开位置必须低于开始响应。'))
                        self.error.show()
                        return
        if self.has_walk and self.walk_toggle.isChecked():
            if not values['stick_press'] < values['walk_press'] < values['walk_release']:
                self.error.setText(tr('慢走区间需满足：摇杆开始响应 < 回到慢走 < 转为正常跑。'))
                self.error.show()
                return
        self.error.clear()
        self.accept()

    def options(self):
        mouse = copy.deepcopy(self.mouse)
        if self.has_pointer:
            mouse.update({key: field.value() / 100 if key == 'deadzone' else field.value()
                          for key, field in self.mouse_fields.items()})
            mouse['mode'] = self.mode.currentData()
            mouse['invert_y'] = self.invert_y.currentData() is True
            if self.stick_source.currentData() == 'left':
                mouse['stick_source'] = 'left'
            else:
                mouse.pop('stick_source', None)
        if self.has_gyro:
            mouse['gyro_enabled'] = self.gyro_enabled_toggle.isChecked()
            mouse['gyro_trigger'] = self.gyro_trigger.currentData()
            mouse['gyro_sensitivity'] = round(float(self.gyro_sensitivity.value()), 2)
            mouse['gyro_invert_y'] = self.gyro_invert_y.currentData() is True
        inputs = copy.deepcopy(self.input_settings)
        inputs.update({key: self.input_fields[key].value() / 100
                       for key in ('stick_press', 'stick_release', 'trigger_press', 'trigger_release',
                                   'outer_press', 'outer_release')
                       if key in self.input_fields})
        if hasattr(self, 'directional_outer_toggle') and self.directional_outer_toggle.isChecked():
            for d in ('up', 'down', 'left', 'right'):
                p_key, r_key = f'outer_{d}_press', f'outer_{d}_release'
                if p_key in self.input_fields and r_key in self.input_fields:
                    inputs[p_key] = self.input_fields[p_key].value() / 100
                    inputs[r_key] = self.input_fields[r_key].value() / 100
        else:
            for d in ('up', 'down', 'left', 'right'):
                inputs.pop(f'outer_{d}_press', None)
                inputs.pop(f'outer_{d}_release', None)
        inputs['chord_window'] = self.chord_window.value() / 1000
        if hasattr(self, 'suppress_ls_up'):
            suppress_val = self.suppress_ls_up.currentData()
            if suppress_val and suppress_val != 'off':
                inputs['suppress_ls_up'] = suppress_val
            else:
                inputs.pop('suppress_ls_up', None)
        for key in ('walk_press', 'walk_release'):
            if not self.has_walk:
                continue
            if self.walk_toggle.isChecked():
                inputs[key] = self.input_fields[key].value() / 100
            else:
                inputs.pop(key, None)
        return {'mouse': mouse, 'input': inputs}


class NikkiLayoutDialog(QDialog):
    """A compact controller-first guide built from the selected profile's actions."""

    def __init__(self, owner, profile, parent=None):
        super().__init__(parent or owner)
        self.owner = owner
        self.profile = profile
        self.setWindowTitle(tr('无限暖暖 · 操作布局'))
        self.setMinimumWidth(440)
        self.resize(min(800, max(440, owner.width() - 80)), min(820, max(480, owner.height() - 100)))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(label(tr_profile(profile), 'section', True))
        layout.addWidget(label(tr('按常用动作分组，显示当前预设的实际按键；以游戏内键位和已解锁槽位为准。'), 'caption', True))
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.NoFrame)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.area.viewport().installEventFilter(self)
        layout.addWidget(self.area, 1)
        controls = QDialogButtonBox(QDialogButtonBox.Close)
        controls.button(QDialogButtonBox.Close).setText(tr('关闭'))
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)
        self.refresh()

    def refresh(self):
        from .kbm_mapper import NIKKI_LAYOUT_GROUPS, nikki_binding_hint
        from .mapping_engine import (binding_label, canonical_trigger, effective_mappings,
                                     input_thresholds, profile_family, trigger_label)
        entries = effective_mappings(self.owner.config, self.owner.snapshot, self.profile)
        family = profile_family(self.owner.config, self.owner.snapshot, self.profile)
        self.trigger_cards = {}
        self.group_grids = []
        content = QWidget()
        content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 0)
        body.setSpacing(14)
        for group in NIKKI_LAYOUT_GROUPS:
            triggers = [canonical_trigger(t) for t in group['triggers']]
            triggers = [t for t in triggers if t in entries and any(
                entries[t].get(g, {}).get('action', 'none') not in ('none', 'suppress') for g in ('short', 'long'))]
            if not triggers:
                continue
            section, section_layout = card()
            section_layout.addWidget(label(tr(group['title']), 'section', True))
            description = group['description']
            if group['title'] == '衣柜、任务与社交' and not any(t.startswith('TP:') for t in triggers):
                description = '按住组合辅助键，将常用功能集中在同一区域。'
            section_layout.addWidget(label(tr(description), 'caption', True))
            grid = QGridLayout()
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(10)
            cards = []
            for trigger in triggers:
                tile = QFrame()
                tile.setMinimumWidth(0)
                tile.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
                tile.setStyleSheet(f"QFrame {{ background: {TOKENS['surface']}; border: 1px solid {TOKENS['border']}; border-radius: 8px; }} QLabel {{ border: none; background: transparent; }}")
                tile_layout = QVBoxLayout(tile)
                tile_layout.setContentsMargins(12, 10, 12, 10)
                tile_layout.setSpacing(8)
                head = QHBoxLayout()
                trigger_name = QWidget()
                trigger_name.setMinimumWidth(0)
                trigger_name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
                trigger_row = QHBoxLayout(trigger_name)
                trigger_row.setContentsMargins(0, 0, 0, 0)
                trigger_row.setSpacing(4)
                for part_index, part in enumerate(display_parts(trigger)):
                    if part_index:
                        separator = label('+', 'caption')
                        trigger_row.addWidget(separator)
                    trigger_row.addWidget(make_token_label(part, family, trigger_name, TOKENS['accent'], 15))
                if trigger.startswith('TP:'):
                    trigger_row.addWidget(label(tr(trigger_label(trigger, family)), 'caption', True), 1)
                trigger_row.addStretch()
                head.addWidget(trigger_name, 1)
                edit = button(tr('编辑'), lambda checked=False, t=trigger: self.edit(t))
                edit.setFixedWidth(48 if tr('编辑') == '编辑' else 58)
                head.addWidget(edit, 0, Qt.AlignTop)
                tile_layout.addLayout(head)
                tile.output_labels = {}
                tile.hint_labels = {}
                entry = entries[trigger]
                for gesture in ('short', 'long'):
                    binding = entry.get(gesture, {})
                    if binding.get('action', 'none') in ('none', 'suppress'):
                        continue
                    duration = float(entry.get('long_press', .65))
                    if trigger.startswith('TP:'):
                        gesture_text = tr('每格触发') if trigger.startswith('TP:scroll_') else tr('触发动作')
                    else:
                        gesture_text = tr('短按') if gesture == 'short' else tr('长按') + f' · {duration:g} ' + tr('秒')
                    tile_layout.addWidget(label(gesture_text, 'caption'))
                    output_text = binding_label(binding, family)
                    if binding.get('action') == 'hold':
                        output_text = str(binding.get('value', '')) + ' ' + tr('（按住）')
                    output = label(tr(output_text), wrap=True)
                    output.setMinimumWidth(0)
                    output.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
                    tile_layout.addWidget(output)
                    tile.output_labels[gesture] = output
                    hint_text = nikki_binding_hint(binding)
                    if trigger == 'LS:inner':
                        options = self.owner.config.get('profile_options', {}).get(self.profile, {})
                        if 'walk_press' not in input_thresholds(options.get('input')):
                            hint_text = '轻推慢走已关闭'
                    hint = label(tr(hint_text), 'caption', True)
                    hint.setMinimumWidth(0)
                    hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
                    tile_layout.addWidget(hint)
                    tile.hint_labels[gesture] = hint
                cards.append(tile)
                self.trigger_cards[trigger] = tile
            self.group_grids.append((grid, cards))
            section_layout.addLayout(grid)
            body.addWidget(section)
        if not self.trigger_cards:
            body.addWidget(label(tr('当前预设还没有可显示的游戏操作。'), 'caption', True))
        body.addStretch()
        previous = self.area.takeWidget()
        if previous is not None:
            previous.hide()
            previous.deleteLater()
        self.area.setWidget(content)
        self.reflow(self.area.viewport().width())

    def edit(self, trigger):
        self.owner.edit_mapping(trigger, profile=self.profile, mode='kbm')
        self.refresh()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Resize and isValid(self.area) and watched is self.area.viewport():
            self.reflow(event.size().width())
        return super().eventFilter(watched, event)

    def reflow(self, width):
        columns = 2 if width >= 650 else 1
        for grid, cards in getattr(self, 'group_grids', []):
            for tile in cards:
                grid.removeWidget(tile)
            for column in range(2):
                grid.setColumnStretch(column, 1 if column < columns else 0)
            for index, tile in enumerate(cards):
                grid.addWidget(tile, index // columns, index % columns)


class VirtualKbmPage(QWidget):
    """标准布局虚拟键鼠与手柄按键全映射工作台"""
    def __init__(self, owner, store=None, parent=None):
        super().__init__(parent)
        from .mapping_ui import BindingList
        self.owner = owner
        self.store = store or owner.store
        self.keycaps = {}
        self.is_capturing = False
        self.device_state = None
        self._gaps = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        # 预设操作和输入选项在固定工具区分组，键盘与详情共用下方滚动区。
        toolbar, toolbar_layout = card()
        toolbar_layout.setContentsMargins(16, 14, 16, 14)
        toolbar_layout.setSpacing(12)
        self.preset_controls = QWidget()
        tools = QHBoxLayout(self.preset_controls)
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(10)
        tools.addWidget(label(tr('键鼠预设'), 'section'))
        self.scheme_combo = QComboBox()
        self.scheme_combo.setMinimumWidth(180)
        self.scheme_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.scheme_combo.setAccessibleName(tr('键鼠预设'))
        self.scheme_combo.currentTextChanged.connect(self.on_scheme_changed)
        self.scheme_combo.activated.connect(lambda index: getattr(owner, 'use_current_as_manual', lambda name: None)(self.scheme_combo.itemData(index) or self.scheme_combo.itemText(index)) if self.current_scheme() == owner.config.get('active_profile') else None)
        tools.addWidget(self.scheme_combo, 1)

        self.scheme_active_badge = QLabel(tr('✓ 已加载生效'))
        self.scheme_active_badge.setStyleSheet(f"background: rgba(16, 185, 129, 0.15); color: {TOKENS['green']}; border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 6px; padding: 3px 8px; font-weight: 700; font-size: 11px;")
        tools.addWidget(self.scheme_active_badge)

        self.scheme_activate_btn = button(tr('设为当前生效'), self.activate_current_scheme, primary=True)
        tools.addWidget(self.scheme_activate_btn)
        self.preset_actions = QWidget()
        actions = QHBoxLayout(self.preset_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self.new_preset_btn = button(tr('新建预设'), lambda: owner.duplicate_profile(target_mode='kbm'))
        actions.addWidget(self.new_preset_btn)
        self.btn_toggle_bindings = button(tr('详细列表'), self.toggle_bindings_list)
        self.btn_toggle_bindings.setCheckable(True)
        actions.addWidget(self.btn_toggle_bindings)
        more = QPushButton(tr('更多设置'))
        more.setCursor(Qt.PointingHandCursor)
        more_menu = QMenu(more)
        self.application_profiles_action = more_menu.addAction(tr('应用关联'), lambda: getattr(owner, 'open_application_profiles', lambda: None)())
        self.application_profiles_action.setToolTip('' if application_profiles_supported() else tr('当前平台不支持应用关联'))
        more_menu.addAction(tr('设为手动预设'), lambda: owner.change_profile(self.current_scheme()))
        self.swap_bindings_action = more_menu.addAction(tr('交换绑定'), self.swap_bindings)
        self.scene_templates_menu = more_menu.addMenu(tr('常用玩法一键套用'))
        self.scene_template_actions = {}
        for tpl_id, tpl_title in (
            ('dnf_dash', '横版动作：摇杆推底双击奔跑 (DNF)'),
            ('sprint_3d', '3D 动作：左摇杆推底双击 W 冲刺'),
            ('fps_gyro', '射击体感：按住左扳机开启陀螺仪瞄准'),
            ('left_stick_mouse', '左手鼠标：切换左摇杆控制鼠标指针'),
        ):
            act = self.scene_templates_menu.addAction(tr(tpl_title))
            act.triggered.connect(lambda checked=False, tid=tpl_id: self.apply_scene_template(tid))
            self.scene_template_actions[tpl_id] = act
        more_menu.addSeparator()
        self.import_profile_action = more_menu.addAction(tr('导入预设'), lambda: getattr(owner, 'import_profile', lambda: None)())
        self.export_profile_action = more_menu.addAction(tr('导出此预设'), lambda: getattr(owner, 'export_profile', lambda name: None)(self.current_scheme()))
        more_menu.addSeparator()
        more_menu.addAction(tr('后台设置'), lambda: owner.navigate(4))
        self.cloaking_action = more_menu.addAction(tr('设备隐身'), self.open_cloaking)
        self.cloaking_action.setToolTip('' if WINDOWS_FEATURES else tr('通过 macOS 原生独占访问隔离所选手柄原始输入'))
        if hasattr(owner, 'reset_profile'):
            more_menu.addSeparator()
            more_menu.addAction(tr('恢复默认'), owner.reset_profile)
        more.setMenu(more_menu)
        actions.addWidget(more)
        self.toolbar_grid = QGridLayout()
        self.toolbar_grid.setContentsMargins(0, 0, 0, 0)
        self.toolbar_grid.setHorizontalSpacing(12)
        self.toolbar_grid.setVerticalSpacing(10)
        self.toolbar_grid.setColumnStretch(0, 1)
        self.toolbar_grid.addWidget(self.preset_controls, 0, 0)
        self.toolbar_grid.addWidget(self.preset_actions, 0, 1)
        self._toolbar_stacked = False
        toolbar_layout.addLayout(self.toolbar_grid)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {TOKENS['border']}; border: none;")
        toolbar_layout.addWidget(divider)

        options = FlowLayout(spacing=10)
        option_label = label(tr('输入选项'), 'caption')
        option_label.setFixedHeight(36)
        options.addWidget(option_label)
        self.mouse_toggle = Toggle(tr('右摇杆控制鼠标'))
        self.mouse_toggle.setFixedHeight(36)
        self.mouse_toggle.toggled.connect(lambda enabled: owner.mapping_change({'op': 'options', 'profile': self.current_scheme(), 'options': {'right_stick_mouse': enabled}}))
        options.addWidget(self.mouse_toggle)
        self.mouse_settings_btn = button(tr('操作手感'), self.mouse_settings)
        options.addWidget(self.mouse_settings_btn)
        self.layout_btn = button(tr('操作布局'), self.show_layout)
        options.addWidget(self.layout_btn)
        self.touchpad_btn = button(tr('触摸板'), self.open_touch)
        self.touchpad_btn.setAccessibleName(tr('触摸板手势'))
        options.addWidget(self.touchpad_btn)
        self.refresh_touch_action(owner.snapshot)
        self.curves_btn = QPushButton(tr('曲线'))
        self.curves_btn.setCursor(Qt.PointingHandCursor)
        self.curves_btn.setAccessibleName(tr('编辑曲线'))
        self.curve_menu = QMenu(self.curves_btn)
        self.curve_actions = {}
        for kind, title in (('trigger', '扳机输入曲线'),
                            ('rumble', '双马达振动曲线'),
                            ('trigger_rumble', '扳机振动曲线')):
            action = self.curve_menu.addAction(tr(title))
            action.triggered.connect(lambda checked=False, k=kind: self.open_curve(k))
            self.curve_actions[kind] = action
        self.curves_btn.setMenu(self.curve_menu)
        options.addWidget(self.curves_btn)
        self.refresh_curve_actions(owner.snapshot)

        self.preview_toggle = Toggle(tr('安全试按'))
        self.preview_toggle.setFixedHeight(36)
        self.preview_toggle.setChecked(False)  # 默认关闭安全试按，确保启动即可畅快操作
        self.preview_toggle.setToolTip(tr('开启后，当前映射窗口有焦点时仅回显，不向系统发送键鼠；关闭或切至游戏即可正常输出。'))
        options.addWidget(self.preview_toggle)
        toolbar_layout.addLayout(options)
        layout.addWidget(toolbar)

        feedback = QHBoxLayout()
        feedback.setContentsMargins(4, 0, 4, 0)
        feedback.setSpacing(10)
        feedback.addWidget(label(tr('实时反馈'), 'caption'))
        self.live = QLabel(tr('等待输入'))
        self.live.setObjectName('muted')
        self.live.setWordWrap(True)
        self.live.setMinimumWidth(0)
        self.live.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        feedback.addWidget(self.live, 1)
        layout.addLayout(feedback)

        # 键盘区域保持自然高度，空余空间留在组间，避免各行被拉开。
        content = QWidget()
        content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(16)
        content_layout.setSizeConstraint(QLayout.SetNoConstraint)
        keyboard_card, keyboard_layout = card()
        keyboard_layout.setContentsMargins(16, 16, 16, 16)
        keyboard_layout.setSpacing(16)
        board_header = QHBoxLayout()
        board_header.setSpacing(16)
        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(label(tr('键盘映射'), 'section'))
        heading.addWidget(label(tr('点击按键添加或编辑绑定，右键清除绑定。'), 'caption', wrap=True))
        board_header.addLayout(heading, 1)
        for title, color in (('短按', '#38bdf8'), ('长按', TOKENS['amber']), ('短按 + 长按', TOKENS['purple'])):
            legend = label(tr(title), 'caption')
            legend.setStyleSheet(f"color: {color}; font-size: 11px;")
            board_header.addWidget(legend)
        keyboard_layout.addLayout(board_header)
        self.canvas = KeyboardCanvas(self)
        # 画布宽度始终跟随视口，键帽的自然宽度不会锁住外层最小尺寸。
        self.canvas.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Maximum)
        canvas_layout = QVBoxLayout(self.canvas)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.setSpacing(0)

        # 键盘和下方输出按键作为一个紧凑内容组，在整个卡片中居中。
        # 固定组的自然宽度，使大屏留白落在两侧，避免被拉成零散列。
        self.key_group = QWidget()
        self.key_group.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Maximum)
        group_layout = QVBoxLayout(self.key_group)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setSpacing(20)

        blocks = QHBoxLayout()
        blocks.setContentsMargins(0, 0, 0, 0)
        blocks.setSpacing(18)
        blocks.addStretch(1)

        # 打字区与独立导航区
        for rows in (TYPING_BLOCK_ROWS, NAV_BLOCK_ROWS):
            block = QWidget()
            vertical = QVBoxLayout(block)
            vertical.setContentsMargins(0, 0, 0, 0)
            vertical.setSpacing(4)
            vertical.setAlignment(Qt.AlignTop)
            block.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            for row in rows:
                horizontal = QHBoxLayout()
                horizontal.setContentsMargins(0, 0, 0, 0)
                horizontal.setSpacing(4)
                for key, name, u_width in row:
                    if key.startswith('__'):
                        gap = QWidget()
                        horizontal.addWidget(gap)
                        self._gaps.append((gap, u_width))
                    else:
                        horizontal.addWidget(self.make_key(key, name, u_width, 1.0))
                vertical.addLayout(horizontal)
            blocks.addWidget(block)

        # 独立数字小键盘区
        numpad = QWidget()
        grid = QGridLayout(numpad)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        numpad.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        for row, col, rs, cs, key, name, u_w, u_h in NUMPAD_BLOCK_GRID:
            grid.addWidget(self.make_key(key, name, u_w, u_h), row, col, rs, cs)
        blocks.addWidget(numpad)
        blocks.addStretch(1)
        group_layout.addLayout(blocks)

        # 鼠标和录制动作有各自标题与清晰间隔，相关按键紧凑排列。
        outputs = QHBoxLayout()
        outputs.setContentsMargins(0, 0, 0, 0)
        outputs.setSpacing(20)
        for title, controls in (('鼠标按键', MOUSE_CONTROLS), ('快捷动作', ACTION_CONTROLS)):
            output_group = QVBoxLayout()
            output_group.setSpacing(8)
            output_group.addWidget(label(tr(title), 'caption'))
            output_keys = QHBoxLayout()
            output_keys.setContentsMargins(0, 0, 0, 0)
            output_keys.setSpacing(6)
            for key, name, u_w in controls:
                output_keys.addWidget(self.make_key(key, name, u_w, 1.0))
            output_group.addLayout(output_keys)
            outputs.addLayout(output_group)
        group_layout.addLayout(outputs)
        canvas_layout.addWidget(self.key_group, 0, Qt.AlignHCenter)
        keyboard_layout.addWidget(self.canvas)
        content_layout.addWidget(keyboard_card)

        self.area = QScrollArea()
        self.area.setWidget(content)
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.NoFrame)
        self.area.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }")
        self.area.viewport().installEventFilter(self)
        self.canvas.setStyleSheet("background: transparent;")
        layout.addWidget(self.area, 1)

        # 4. 可选折叠的绑定表格（默认折叠，不浪费主页面空间）
        self.bindings = BindingList(owner, profile_getter=lambda: self.current_scheme(), mode='kbm')
        self.bindings.hide()
        self.bindings.setMinimumHeight(240)
        content_layout.addWidget(self.bindings)
        content_layout.addStretch()

        self.refresh_display()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self.owner, 'closed', False):
            return
        self._arrange_toolbar()

    def _arrange_toolbar(self):
        if not hasattr(self, 'toolbar_grid'):
            return
        needed = self.preset_controls.minimumSizeHint().width() + self.preset_actions.sizeHint().width() + 12
        stacked = needed > self.width() - 32
        if stacked == self._toolbar_stacked:
            return
        self._toolbar_stacked = stacked
        self.toolbar_grid.removeWidget(self.preset_controls)
        self.toolbar_grid.removeWidget(self.preset_actions)
        self.toolbar_grid.addWidget(self.preset_controls, 0, 0, 1, 2 if stacked else 1)
        if stacked:
            self.toolbar_grid.addWidget(self.preset_actions, 1, 0, 1, 2, Qt.AlignRight)
        else:
            self.toolbar_grid.addWidget(self.preset_actions, 0, 1)

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Resize and isValid(self.area) and isValid(self.canvas)
                and not getattr(self.owner, 'closed', False) and watched is self.area.viewport()):
            self.canvas.fit_width(max(0, event.size().width() - 32))
        return super().eventFilter(watched, event)

    def current_scheme(self):
        kbm_items = self.store.profiles_for(self.owner.snapshot, mode='kbm')
        txt = self.scheme_combo.currentText()
        if txt in kbm_items:
            return txt
        active = self.owner.config.get('active_profile', '')
        if active in kbm_items:
            return active
        previous = getattr(self, '_last_kbm_profile', '')
        if previous in kbm_items:
            return previous
        return next(iter(kbm_items), '')

    def on_scheme_changed(self, name):
        if not name:
            return
        self._last_kbm_profile = name
        self.owner.change_profile(name)

    def activate_current_scheme(self):
        scheme = self.current_scheme()
        if scheme:
            self.owner.change_profile(scheme)

    def toggle_bindings_list(self):
        """展开或折叠详细绑定表格"""
        if not self.bindings.isHidden():
            self.bindings.hide()
            self.btn_toggle_bindings.setText(tr('详细列表'))
            self.btn_toggle_bindings.setChecked(False)
        else:
            self.bindings.show()
            self.btn_toggle_bindings.setText(tr('收起列表'))
            self.btn_toggle_bindings.setChecked(True)
            QTimer.singleShot(0, lambda: self.area.ensureWidgetVisible(self.bindings, 0, 12))

    def recompute_key_sizes(self, u_px: int, h_px: int):
        """动态刷新全部键帽像素尺寸"""
        for cap in self.keycaps.values():
            cap.set_unit_size(u_px, h_px, 4)
        for gap, u_gap in self._gaps:
            gap_w = max(4, int(round(u_gap * u_px + (u_gap - 1.0) * 4)))
            gap.setFixedSize(gap_w, h_px)

    def make_key(self, key, name, u_width, u_height=1.0):
        if sys.platform == 'darwin' and not WINDOWS_FEATURES:
            name = {'Win': 'Cmd', 'Alt': 'Option', 'RAlt': 'Option'}.get(key, name)
        cap = KeyCap(key, name, u_width, u_height)
        cap.left_clicked.connect(self.edit_output)
        cap.right_clicked.connect(self.handle_right_click)
        self.keycaps[key] = cap
        if key == 'Fn':
            cap.setEnabled(False)
            cap.platform_tooltip = 'Fn 由键盘硬件处理'
        elif sys.platform == 'darwin' and not WINDOWS_FEATURES and not key.startswith(('mouse:', 'action:')):
            from .actions import supports_key
            if not supports_key(key):
                from .macos_actions import MacActions
                cap.setEnabled(False)
                cap.platform_tooltip = MacActions.key_capability(key)['reason']
            elif key == 'Win':
                cap.platform_tooltip = tr('Command 键；兼容预设中的 Win / Meta 名称')
        cap.update_tooltip()
        return cap

    def handle_right_click(self, key, name):
        """右键点击按键：支持快速清除该键绑定的手柄输入"""
        from .mapping_engine import output_tokens, effective_mappings, trigger_label, profile_family
        token = self.key_token(key)
        scheme = self.current_scheme()
        entries = effective_mappings(self.owner.config, self.owner.snapshot, scheme)
        matches = [trigger for trigger, entry in entries.items()
                   if any(token in output_tokens(entry.get(g, {})) for g in ('short', 'long'))]
        if matches:
            from PySide6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self, '清除绑定',
                f'确定要清除虚拟按键【{name}】的手柄绑定吗？\n涉及手柄按键：{", ".join(matches)}',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                for trigger in matches:
                    self.owner.mapping_change({'op': 'unbind', 'trigger': trigger, 'profile': scheme})
        else:
            self.edit_output(key, name)

    def mouse_settings(self):
        profile = self.current_scheme()
        if not profile:
            return
        dialog = KbmFeelDialog(self.owner, profile, self)
        if dialog.exec() == QDialog.Accepted:
            self.owner.mapping_change({'op': 'options', 'profile': profile, 'options': dialog.options()})

    def show_layout(self):
        from .studio_core import is_nikki_profile
        profile = self.current_scheme()
        if is_nikki_profile(self.owner.config, profile):
            NikkiLayoutDialog(self.owner, profile, self).exec()

    def edit_output(self, key, name):
        from .mapping_engine import output_tokens, effective_mappings, trigger_label, profile_family
        token = self.key_token(key)
        scheme = self.current_scheme()
        entries = effective_mappings(self.owner.config, self.owner.snapshot, scheme)
        matches = [trigger for trigger, entry in entries.items()
                   if any(token in output_tokens(entry.get(g, {})) for g in ('short', 'long'))]
        if len(matches) == 1:
            self.owner.edit_mapping(matches[0], profile=scheme, mode='kbm')
        elif matches:
            from PySide6.QtWidgets import QInputDialog
            family = profile_family(self.owner.config, self.owner.snapshot, scheme)
            options = [tr('添加新的绑定')] + [tr(trigger_label(t, family)) for t in matches]
            value, ok = QInputDialog.getItem(self, name, tr('选择绑定'), options, 0, False)
            if ok:
                index = options.index(value)
                self.owner.edit_mapping(matches[index - 1] if index else '0', new=index == 0,
                                        output=key if value == options[0] else None, profile=scheme, mode='kbm')
        else:
            self.owner.edit_mapping('0', new=True, output=key, profile=scheme, mode='kbm')

    def swap_bindings(self):
        from .mapping_swap_ui import can_swap_bindings
        from .mapping_engine import input_sources
        profile = self.current_scheme()
        if not can_swap_bindings(self.owner, profile):
            return
        current = self.bindings.list.currentItem()
        first = current.data(Qt.UserRole) if current is not None else next(iter(input_sources(self.owner.snapshot)), None)
        if first is not None:
            self.owner.open_mapping_swap(profile, first)

    def apply_scene_template(self, template_id):
        """Apply a one-click gameplay micro-template to the current KBM preset."""
        from .mapping_engine import input_sources
        profile = self.current_scheme()
        if not profile:
            return
        available = set(input_sources(self.owner.snapshot))
        if template_id == 'dnf_dash':
            bindings = {
                'LS:up': {'short': {'action': 'hold', 'value': 'Up'}, 'long': {'action': 'none'}},
                'LS:down': {'short': {'action': 'hold', 'value': 'Down'}, 'long': {'action': 'none'}},
                'LS:left': {'short': {'action': 'hold', 'value': 'Left'}, 'long': {'action': 'none'}},
                'LS:right': {'short': {'action': 'hold', 'value': 'Right'}, 'long': {'action': 'none'}},
                'LS:left:outer': {
                    'short': {'action': 'hold', 'value': 'Left', 'mode': 'double_tap_hold'},
                    'long': {'action': 'none'},
                },
                'LS:right:outer': {
                    'short': {'action': 'hold', 'value': 'Right', 'mode': 'double_tap_hold'},
                    'long': {'action': 'none'},
                },
            }
            for trig, mapping in bindings.items():
                if trig in available:
                    self.owner.mapping_change({'op': 'binding', 'profile': profile, 'trigger': trig, 'mapping': mapping})
            if hasattr(self.owner, 'notify'):
                self.owner.notify(tr('已套用「横版动作：摇杆推底双击奔跑」模板'))
        elif template_id == 'sprint_3d':
            bindings = {
                'LS:up': {'short': {'action': 'hold', 'value': 'W'}, 'long': {'action': 'none'}},
                'LS:down': {'short': {'action': 'hold', 'value': 'S'}, 'long': {'action': 'none'}},
                'LS:left': {'short': {'action': 'hold', 'value': 'A'}, 'long': {'action': 'none'}},
                'LS:right': {'short': {'action': 'hold', 'value': 'D'}, 'long': {'action': 'none'}},
                'LS:up:outer': {
                    'short': {'action': 'hold', 'value': 'W', 'mode': 'double_tap_hold'},
                    'long': {'action': 'none'},
                },
            }
            for trig, mapping in bindings.items():
                if trig in available:
                    self.owner.mapping_change({'op': 'binding', 'profile': profile, 'trigger': trig, 'mapping': mapping})
            if hasattr(self.owner, 'notify'):
                self.owner.notify(tr('已套用「3D 动作：左摇杆推底双击 W 冲刺」模板'))
        elif template_id == 'fps_gyro':
            existing_mouse = copy.deepcopy(
                (self.owner.config.get('profile_options', {}).get(profile, {}) or {}).get('mouse') or {}
            )
            existing_mouse.update({
                'gyro_enabled': True,
                'gyro_trigger': 'LT',
                'gyro_sensitivity': float(existing_mouse.get('gyro_sensitivity', 18.0)),
            })
            self.owner.mapping_change({
                'op': 'options',
                'profile': profile,
                'options': {'right_stick_mouse': True, 'mouse': existing_mouse},
            })
            if hasattr(self.owner, 'notify'):
                self.owner.notify(tr('已套用「射击体感：按住左扳机开启陀螺仪瞄准」模板'))
        elif template_id == 'left_stick_mouse':
            existing_mouse = copy.deepcopy(
                (self.owner.config.get('profile_options', {}).get(profile, {}) or {}).get('mouse') or {}
            )
            existing_mouse['stick_source'] = 'left'
            self.owner.mapping_change({
                'op': 'options',
                'profile': profile,
                'options': {'right_stick_mouse': True, 'mouse': existing_mouse},
            })
            if hasattr(self.owner, 'notify'):
                self.owner.notify(tr('已切换为「左摇杆控制鼠标指针」'))
        self.refresh_display()

    @staticmethod
    def key_token(key):
        from .actions import parse_keys
        if key.startswith(('mouse:', 'action:')): return key
        try: return 'key:' + str(parse_keys(key)[0])
        except (ValueError, IndexError): return ''

    def refresh_display(self):
        from .mapping_engine import effective_mappings, output_tokens, trigger_label, profile_family
        from .studio_core import profile_mode
        config = self.owner.config
        self.refresh_touch_action(self.owner.snapshot)
        kbm_items = self.store.profiles_for(self.owner.snapshot, mode='kbm')
        previous = self.current_scheme()

        self.scheme_combo.blockSignals(True)
        self.scheme_combo.clear()
        self.scheme_combo.setPlaceholderText(tr('选择键鼠预设...'))
        for p_name in kbm_items:
            self.scheme_combo.addItem(p_name)

        active = config.get('active_profile', '')
        if profile_mode(config, active) == 'kbm' and active in kbm_items:
            self.scheme_combo.setCurrentText(active)
            is_active = True
        else:
            selected = previous if previous in kbm_items else next(iter(kbm_items), '')
            if selected:
                self.scheme_combo.setCurrentText(selected)
            else:
                self.scheme_combo.setCurrentIndex(-1)
            is_active = False
        self.scheme_combo.blockSignals(False)

        selected = self.current_scheme()
        if selected:
            self._last_kbm_profile = selected

        if hasattr(self, 'scheme_active_badge'):
            if is_active:
                self.scheme_active_badge.setText(tr('✓ 已加载生效'))
                self.scheme_active_badge.setStyleSheet(f"background: rgba(16, 185, 129, 0.15); color: {TOKENS['green']}; border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 6px; padding: 3px 8px; font-weight: 700; font-size: 11px;")
                self.scheme_activate_btn.hide()
            else:
                self.scheme_active_badge.setText(tr('未激活'))
                self.scheme_active_badge.setStyleSheet(f"background: {TOKENS['surface']}; color: {TOKENS['ink_3']}; border: 1px solid {TOKENS['border']}; border-radius: 6px; padding: 3px 8px; font-weight: 600; font-size: 11px;")
                self.scheme_activate_btn.show()
            has_selection = self.scheme_combo.currentIndex() >= 0
            self.scheme_activate_btn.setEnabled(has_selection)
            for control, primary in ((self.scheme_activate_btn, has_selection), (self.new_preset_btn, not has_selection)):
                control.setObjectName('primary' if primary else '')
                control.style().unpolish(control)
                control.style().polish(control)
            self._arrange_toolbar()

        from .mapping_engine import input_sources
        sources = input_sources(self.owner.snapshot)
        profile_opts = config.get('profile_options', {}).get(selected, {})
        stick_source = (profile_opts.get('mouse') or {}).get('stick_source', 'right')
        stick_axes_sources = (
            {'LS:up', 'LS:down', 'LS:left', 'LS:right'}
            if stick_source == 'left'
            else {'RS:up', 'RS:down', 'RS:left', 'RS:right'}
        )
        self.mouse_toggle.setText(tr('左摇杆控制鼠标') if stick_source == 'left' else tr('右摇杆控制鼠标'))
        self.mouse_toggle.setVisible(bool(stick_axes_sources & set(sources)))
        self.cloaking_action.setEnabled(bool((WINDOWS_FEATURES or sys.platform == 'darwin') and self.owner.snapshot and self.owner.snapshot.get('vendor') and self.owner.snapshot.get('product')))
        self.application_profiles_action.setEnabled(bool(application_profiles_supported() and self.owner.snapshot))
        self.import_profile_action.setEnabled(bool(self.owner.snapshot))
        self.export_profile_action.setEnabled(bool(self.owner.snapshot))
        from .mapping_swap_ui import can_swap_bindings
        self.swap_bindings_action.setEnabled(can_swap_bindings(self.owner, selected))
        self.mouse_toggle.blockSignals(True)
        self.mouse_toggle.setChecked(profile_opts.get('right_stick_mouse', False))
        self.mouse_toggle.blockSignals(False)
        from .studio_core import is_nikki_profile
        self.layout_btn.setEnabled(is_nikki_profile(config, selected))
        self.mouse_settings_btn.setEnabled(bool(selected))
        family = profile_family(config, self.owner.snapshot, selected)

        # 结构化抽取绑定信息，严格分离短按与长按
        badges = {}
        for trigger, entry in effective_mappings(config, self.owner.snapshot, selected).items():
            t_label = trigger_label(trigger, family)
            for gesture in ('short', 'long'):
                binding = entry.get(gesture, {})
                for token in output_tokens(binding):
                    badges.setdefault(token, []).append({
                        'trigger': t_label,
                        'input': trigger,
                        'family': family,
                        'gesture': gesture,
                    })

        for key, cap in self.keycaps.items():
            cap.set_mapping_info(badges.get(self.key_token(key), []), False)
        if hasattr(self, 'bindings'):
            self.bindings.refresh()

    def set_device_state(self, state):
        self.device_state = state
        self.refresh_curve_actions(state)
        self.refresh_touch_action(state)
        self.cloaking_action.setEnabled(bool((WINDOWS_FEATURES or sys.platform == 'darwin') and state and state.get('vendor') and state.get('product')))
        from .mapping_swap_ui import can_swap_bindings
        self.swap_bindings_action.setEnabled(can_swap_bindings(self.owner, self.current_scheme()))
        self.bindings._sync_controls()
        if hasattr(self, 'cloaking_dialog') and self.cloaking_dialog.isVisible():
            self.refresh_cloaking_status()

    def refresh_curve_actions(self, state):
        capabilities = curve_capabilities(state)
        supported = {'trigger': bool(capabilities['trigger_axes']),
                     'rumble': capabilities['rumble'],
                     'trigger_rumble': capabilities['trigger_rumble']}
        for kind, action in self.curve_actions.items():
            available = supported[kind] and callable(getattr(self.owner, 'open_curve_editor', None))
            action.setVisible(available)
            action.setEnabled(available)
        self.curves_btn.setVisible(any(action.isVisible() for action in self.curve_actions.values()))

    def open_curve(self, kind):
        # Recheck the current snapshot if a device changes while the menu is open.
        self.refresh_curve_actions(self.owner.snapshot)
        if self.curve_actions[kind].isEnabled():
            self.owner.open_curve_editor(kind)

    def refresh_touch_action(self, state):
        available = supports_touch(state) and callable(getattr(self.owner, 'open_touch_editor', None))
        self.touchpad_btn.setVisible(available)
        self.touchpad_btn.setEnabled(available)

    def open_touch(self):
        self.refresh_touch_action(self.owner.snapshot)
        profile = self.current_scheme()
        if self.touchpad_btn.isEnabled() and profile:
            self.owner.open_touch_editor(profile=profile, mode='kbm')

    def update_feedback(self, data, connected=False, suspended=False):
        from .mapping_engine import trigger_label
        pressed = set(data.get('outputs', [])) | set(data.get('recent_outputs', []))
        for key, cap in self.keycaps.items():
            active = self.key_token(key) in pressed
            if getattr(cap, 'is_pressed', False) != active:
                cap.is_pressed = active
                cap.update()
        events = data.get('events', [])
        state = self.device_state or {}
        family = state.get('family', 'generic')
        inputs = ' + '.join(button_text(k, family) for k in data.get('inputs', []))
        status = '编辑中 · 输出已暂停' if suspended else ('已连接' if connected else '未连接')
        if data.get('preview') and not suspended: status = '安全试按 · 不发送到游戏'
        last = events[-1] if events else None
        result = ''
        if last and last.get('trigger'):
            if last.get('gesture') == 'scroll':
                event_label = tr('滚动')
            elif last['trigger'].startswith('TP:'):
                event_label = button_text(last['trigger'], family)
            else:
                event_label = ' + '.join(button_text(k, family) for k in display_parts(last['trigger']))
                event_label += ' ' + tr('长按' if last.get('gesture') == 'long' else '短按')
            result = event_label + ' → ' + last.get('action', '')
        self.live.setText(' · '.join(x for x in (status, inputs, result) if x))
        if hasattr(self, 'bindings'):
            self.bindings.feedback(data)

    def cancel_capture(self):
        self.is_capturing = False

    def handle_device_input(self, state):
        self.set_device_state(state)

    def open_cloaking(self):
        if sys.platform == 'darwin' and not WINDOWS_FEATURES:
            if not (self.owner.snapshot or self.device_state):
                self.owner.notify(tr('请先连接并选择原生手柄'))
                return
            if not hasattr(self,'cloaking_dialog'):
                self.cloaking_dialog = QDialog(self); self.cloaking_dialog.setWindowTitle(tr('设备隐身'))
                layout = QVBoxLayout(self.cloaking_dialog)
                self.cloaking_toggle = Toggle(tr('向其他应用隐藏手柄原始输入'))
                self.cloaking_status_label = QLabel(); self.cloaking_status_label.setWordWrap(True)
                layout.addWidget(self.cloaking_status_label); layout.addWidget(self.cloaking_toggle)
                self.cloaking_toggle.toggled.connect(self.toggle_cloaking)
            self.refresh_cloaking_status(); self.cloaking_dialog.show()
            return
        if not WINDOWS_FEATURES:
            self.owner.notify(tr('HidHide 仅支持 Windows，macOS 无法隐藏手柄原始输入'))
            return
        opener = getattr(self.owner, 'open_hidhide_panel', None)
        if opener is None:
            self.owner.notify(tr('当前界面无法打开设备隐身配置'))
            return
        opener()

    def refresh_cloaking_status(self):
        if sys.platform != 'darwin' or WINDOWS_FEATURES:
            return
        from .controller_isolation_service import isolation_status
        info = isolation_status(self.owner.device)
        self.cloaking_toggle.blockSignals(True)
        self.cloaking_toggle.setChecked(bool(info.get('active') or info.get('restore_pending')))
        self.cloaking_toggle.setEnabled(bool(info.get('supported') or info.get('restore_pending')))
        self.cloaking_toggle.blockSignals(False)
        self.cloaking_status_label.setText(info.get('reason') or tr('已隔离当前手柄的原始输入；关闭或退出时恢复共享访问') if info.get('active') else info.get('reason') or tr('当前手柄原始输入可见；开启会阻止游戏原生手柄输入'))
        return

    def toggle_cloaking(self, enabled):
        if not (sys.platform == 'darwin' and not WINDOWS_FEATURES):
            return
        result = self.owner.set_controller_isolation(enabled)
        self.refresh_cloaking_status()
        self.cloaking_status_label.setText(result['message'])
        self.owner.refresh_controller_isolation()
