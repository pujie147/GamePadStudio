from __future__ import annotations

import argparse
import copy
from collections import deque
import re
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time

from PySide6.QtCore import Qt, QSize, QTimer, QThread, Signal, QUrl, QLockFile, QPointF, QRectF, QEvent
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap, QPainter, QKeySequence, QDesktopServices, QAction, QPen, QPainterPath
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget, QComboBox, QScrollArea, QLineEdit,
    QDialog, QFormLayout, QKeySequenceEdit, QFileDialog, QDialogButtonBox, QSlider, QCheckBox,
    QSystemTrayIcon, QMenu, QInputDialog, QListWidget, QListWidgetItem, QMessageBox, QSizePolicy, QSizeGrip, QFileDialog)
from shiboken6 import isValid

from .ipc import AgentClient, RemoteDevice, LocalServer, request, spawn, default_root, autostart_enabled, autostart_supported, set_autostart, cleanup_stale_agent, cleanup_stale_ui, read_lock_pid, is_process_alive
from .studio_core import ConfigStore, GestureEngine, BUTTONS, ACTION_NAMES
from .device import Device
from .actions import create_actions, input_permission_status, request_input_permission, parse_keys, launch_command
from .mapping_engine import MappingRuntime, effective_mappings, binding_label
from .mapping_ui import BindingDialog, TriggerLog
from .battery_monitor import BatteryMonitor, battery_reported, normalize_power
from .emergency_hotkey import EmergencyHotkey, emergency_hotkey_supported, normalize_hotkey_settings
from .screenshot_hotkey import ScreenshotHotkey, normalize_screenshot_hotkey_settings
from .application_profiles import application_profiles_supported
from .mapping_deck import MappingDeck
from .virtual_kbm_ui import VirtualKbmPage
from .curve_ui import CurveDialog, supports_curve
from .touch_ui import TouchGestureDialog, supports_touch
from .response_curves import curve_capabilities, normalize_curve
from .controller_photo import ControllerPhoto, ControllerInput, PHOTOS, photo_health
from .input_tester import InputTester
from .controller_catalog import CATALOG, button_labels, capture_button, button_order, controller_defaults
from .studio_core import device_config, profile_scope, DEVICE_SETTING_KEYS
from .mapping_engine import profile_family, input_sources, canonical_trigger, trigger_label
from .screenshot_service import take_screenshot, list_captures, set_favorite, delete_capture
from .manual_recording import ManualRecording

from .glass import (TOKENS, MONO_FONT_STACK, tag_style, token, token_color, STYLE, GlassWindow, GlassCanvas, GlassPanel, TitleBar,
                    IconButton, Indicator, Toggle, glyph, app_icon,
                    SquircleBadge, AppleRow, AppleGroup, LedSwatch)
from .te_widgets import DotMatrixDisplay, SpeakerGrille, RotaryKnob, RockerSwitch
from .i18n import (tr, tr_button, tr_profile, get_language, set_language,
                   init_language, get_language_preference)

WINDOWS_FEATURES = sys.platform == 'win32'
ACCESSIBILITY_SETTINGS_URL = 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'
SCREEN_CAPTURE_SETTINGS_URL = 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture'


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


def scroll(widget):
    area = QScrollArea(); area.setWidgetResizable(True); area.setWidget(widget)
    return area


def navigation_icon(index):
    return glyph({0:'controller',1:'mapping',2:'photos',3:'wave',4:'settings',5:'grid'}[index])


class CaptureWorker(QThread):
    ready = Signal(str)
    failed = Signal(str)
    def __init__(self, folder, mode):
        super().__init__(); self.folder, self.mode = folder, mode
    def run(self):
        try: self.ready.emit(take_screenshot(self.folder, mode=self.mode))
        except Exception as exc: self.failed.emit(str(exc))


MappingDialog = BindingDialog


class Studio(GlassWindow):
    device_sample = Signal(object)
    recording_finished = Signal(str,str)
    def __init__(self, root, standalone=False, lang=None, input_device=None):
        super().__init__()
        root=Path(root).resolve(); self.remote=not standalone; self.closed=False
        self.store=ConfigStore(root); self.config=self.store.data
        if not self.remote:
            self.store.activate_controller(None)
            self.store.save()
        self.lang_pref = lang
        init_language(lang or self.config.get('language', 'auto'))
        self.setWindowTitle('GamePad Studio'); self.setWindowIcon(app_icon()); self.resize(1440,900); self.setMinimumSize(960,640)
        self.client=AgentClient(root,self) if self.remote else None
        self.device=RemoteDevice(self.client) if self.remote else input_device if input_device is not None else Device()
        if not self.remote:self.device.preferred_key=self.config.get('preferred_controller','')
        self.actions=create_actions()
        self.engine=MappingRuntime(self.actions, self.dispatch, start_mouse=not self.remote)
        if not self.remote:
            from .application_profiles import ApplicationProfileResolver
            self.application_resolver = ApplicationProfileResolver(protected_pids={os.getpid()})
            self._application_profile_status = {'automatic': False, 'executable': '', 'profile': self.config['active_profile']}
            self._last_application_check = -float('inf')
        if self.remote:
            self.client.event.connect(self.agent_event)
            if request(root,'status',timeout=200) is None:
                cleanup_stale_agent(root)
                spawn(root,'--agent')
        self.snapshot=None; self.previous_connected=False; self.enabled=bool(self.config.get('mapping_enabled',True)); self.last_capture=0; self.worker=None
        self.manual_recording = None
        self.recording_finished.connect(self.on_recording_finished)
        if sys.platform == 'darwin' and not WINDOWS_FEATURES:
            from .macos_windows import configure_window_exclusions
            configure_window_exclusions({os.getpid(), read_lock_pid(root / 'agent.lock')})
        if not self.remote:
            from .haptic_engine import HapticEngine
            self.haptic_engine = HapticEngine(self.device)
        self.device_identity=object();self.button_names=button_labels('generic');self.mapping_boxes={}
        self.battery_monitor = BatteryMonitor() if not self.remote else None
        self._battery_alert_ids = set()
        self._battery_alert_order = deque()
        self.battery_warning = None
        self.emergency_hotkey = None if self.remote or not emergency_hotkey_supported() else EmergencyHotkey(self.emergency_pause)
        if self.emergency_hotkey:
            self.emergency_hotkey.configure(self.config.get('emergency_hotkey'))
        self.screenshot_hotkey = (ScreenshotHotkey(self.capture)
                                  if sys.platform == 'darwin' and not WINDOWS_FEATURES and not self.remote else None)
        if self.screenshot_hotkey:
            self.screenshot_hotkey.configure(self.config.get('screenshot_hotkey'))
        self.last_touch=None; self.last_buttons=set(); self.quitting=False; self.learn=False
        self.log_rows=[]; self.nav={}; self.mapping_labels={}; self.recent_labels=[]
        main = GlassCanvas()
        self.setCentralWidget(main)
        outer = QHBoxLayout(main)
        outer.setContentsMargins(16, 16, 24, 14)
        outer.setSpacing(20)

        rail = QWidget()
        rail.setObjectName('navRail')
        self.rail = rail
        rail.setFixedWidth(212)
        side = QVBoxLayout(rail)
        side.setContentsMargins(10, 12, 10, 12)
        side.setSpacing(6)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        brand = QPushButton()
        brand.setObjectName('brand')
        brand.setFixedSize(44, 44)
        brand.setIcon(app_icon())
        brand.setIconSize(QSize(40, 40))
        brand.setCursor(Qt.PointingHandCursor)
        brand.setToolTip(tr('GamePad Studio · 手柄控制中心 (返回概览)'))
        brand.setAccessibleName('GamePad Studio')
        brand.clicked.connect(lambda: self.navigate(0))
        brand_row.addWidget(brand)
        self.brand_copy = QWidget()
        brand_copy = QVBoxLayout(self.brand_copy)
        brand_copy.setContentsMargins(0, 0, 0, 0)
        brand_copy.setSpacing(2)
        brand_copy.addWidget(label('GamePad', 'brandTitle'))
        brand_copy.addWidget(label('STUDIO', 'brandCaption'))
        brand_row.addWidget(self.brand_copy, 1)
        side.addLayout(brand_row)
        side.addSpacing(14)
        self.nav_sections = []
        self.nav_titles = {}
        for group, items in [('工作空间', [(0, '设备概览'), (1, '按键配置'), (6, '虚拟键鼠')]),
                             ('设备与内容', [(5, '控制器库'), (2, '截图图库'), (3, '硬件遥测')])]:
            heading = label(tr(group), 'navSection')
            self.nav_sections.append(heading)
            side.addWidget(heading)
            for i, title in items:
                b = QPushButton(tr(title))
                b.setObjectName('sidebarNav')
                b.setIcon(glyph({5: 'grid', 0: 'controller', 1: 'mapping', 6: 'keyboard', 2: 'photos', 3: 'wave', 7: 'sparkle'}[i], TOKENS['ink_2'], TOKENS['accent']))
                b.setIconSize(QSize(20, 20))
                b.setCursor(Qt.PointingHandCursor)
                b.setFixedHeight(44)
                b.setCheckable(True)
                b.setAccessibleName(tr(title))
                b.setToolTip(tr(title))
                b.clicked.connect(lambda checked=False, j=i: self.navigate(j))
                side.addWidget(b)
                self.nav[i] = b
                self.nav_titles[i] = title
            side.addSpacing(10)

        side.addStretch()

        rail_sep = QFrame()
        rail_sep.setFixedHeight(1)
        rail_sep.setStyleSheet(f"background: {TOKENS['border']};")
        side.addWidget(rail_sep)
        side.addSpacing(8)

        for i, symbol, title, callback in [(4, 'settings', '系统设置', lambda: self.navigate(4)),
                                            (-1, 'help', '使用指南', self.show_help)]:
            b = QPushButton(tr(title))
            b.setObjectName('sidebarNav')
            b.setIcon(glyph(symbol, TOKENS['ink_2'], TOKENS['accent'] if i == 4 else None))
            b.setIconSize(QSize(22, 22))
            b.setFixedHeight(44)
            b.setCursor(Qt.PointingHandCursor)
            b.setAccessibleName(tr(title))
            b.setToolTip(tr(title))
            b.setCheckable(i == 4)
            b.clicked.connect(callback)
            side.addWidget(b)
            self.nav[i] = b
            self.nav_titles[i] = title

        self.side_status = QLabel(self)
        self.side_status.hide()
        outer.addWidget(rail)

        body = QWidget()
        content = QVBoxLayout(body)
        self.content_layout = content
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(18)

        chrome = TitleBar()
        header = QHBoxLayout(chrome)
        header.setContentsMargins(2, 0, 0, 2)
        header.setSpacing(10)

        window_label = label('GamePad Studio', 'caption')
        window_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        header.addWidget(window_label)
        header.addStretch()

        capsule = QWidget()
        capsule.setFixedHeight(38)
        capsule.setObjectName('statusCapsule')
        self.status_capsule = capsule
        capsule_layout = QHBoxLayout(capsule)
        capsule_layout.setContentsMargins(10, 2, 10, 2)
        capsule_layout.setSpacing(8)

        self.status_badge = Indicator()
        self.status_badge.setText(tr('未连接'))
        capsule_layout.addWidget(self.status_badge)

        self.status_label = label(tr('未连接'), 'section')
        self.status_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.status_label.setMinimumWidth(52)
        self.status_label.setMaximumWidth(200)
        self.status_label.setStyleSheet(f"font-size: 11.5px; font-weight: 700; color: {TOKENS['ink_2']}; padding-right: 4px;")
        capsule_layout.addWidget(self.status_label)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.VLine)
        _border_hi = TOKENS['border_hi']
        sep1.setStyleSheet(f'color: {_border_hi}; max-height: 18px;')
        capsule_layout.addWidget(sep1)

        self.pause_button = IconButton('pause', tr('暂停手柄映射'), self.toggle_pause, 28)
        capsule_layout.addWidget(self.pause_button)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.VLine)
        sep2.setStyleSheet(f'color: {_border_hi}; max-height: 18px;')
        capsule_layout.addWidget(sep2)

        self.capture_button = IconButton('camera', tr('即时截屏'), self.capture, 28)
        self.capture_button.setObjectName('primary')
        self.capture_button.set_symbol('camera', '#ffffff')
        capsule_layout.addWidget(self.capture_button)
        header.addWidget(capsule)

        header.addSpacing(6)
        for symbol, title, action in [('minimize', tr('最小化'), self.showMinimized),
                                      ('maximize', tr('最大化 / 还原'), chrome.toggle_maximized),
                                      ('close', tr('关闭窗口'), self.close)]:
            control = IconButton(symbol, title, action, 28)
            control.setObjectName('close' if symbol == 'close' else 'window')
            control.setIconSize(QSize(14, 14))
            header.addWidget(control)
        content.addWidget(chrome)
        intro = QVBoxLayout()
        intro.setSpacing(5)
        self.page_heading = label(tr('设备概览'), 'pageTitle')
        self.page_description = label('', 'pageSubtitle', True)
        intro.addWidget(self.page_heading)
        intro.addWidget(self.page_description)
        content.addLayout(intro)
        self.stack=QStackedWidget();content.addWidget(self.stack,1)
        self.build_home();self.build_mapping();self.build_gallery();self.build_test();self.build_settings()
        from .controller_gallery import ControllerGallery
        self.controllers=ControllerGallery(self.select_controller,lambda values:self.setting('controller_favorites',values),self.rescan_controllers,lambda:self.navigate(0),self.config['controller_favorites'])
        self.stack.addWidget(self.controllers)
        self.virtual_kbm_page = VirtualKbmPage(self, store=self.store)
        self.stack.addWidget(self.virtual_kbm_page)
        self.build_antigravity()
        foot=QHBoxLayout();self.notice=label(tr('映射运行中') if self.enabled else tr('映射已暂停'),'footerStatus');foot.addWidget(self.notice,1)
        self.fullscreen_hint=label(tr('F11 切换全屏 · Esc 返回窗口'), 'footerStatus')
        foot.addWidget(self.fullscreen_hint)
        self.size_grip = QSizeGrip(self);foot.addWidget(self.size_grip);content.addLayout(foot)
        for key, callback in [('F11', self.toggle_fullscreen), ('Escape', self.escape_workspace)]:
            action = QAction(self); action.setShortcut(QKeySequence(key));action.triggered.connect(callback);self.addAction(action)
        self.notice_timer=QTimer(self);self.notice_timer.setSingleShot(True);self.notice_timer.timeout.connect(lambda:self.notice.setText(tr('映射运行中') if self.enabled else tr('映射已暂停')))
        outer.addWidget(body,1)
        self.tray=QSystemTrayIcon(app_icon(),self); self.tray.setToolTip('GamePad Studio')
        self.tray_menu = QMenu(self)
        self.tray_open_action = self.tray_menu.addAction(tr('打开工作台'))
        self.tray_open_action.triggered.connect(self.show_home)
        self.tray_menu.addSeparator()
        self.tray_exit_action = self.tray_menu.addAction(tr('退出'))
        self.tray_exit_action.triggered.connect(self.quit_app)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(lambda reason: self.show_home()
                                    if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.messageClicked.connect(self.show_home)
        self.tray.show()
        self.navigate(0); self.refresh_gallery(); self.update_controller_ui(None)
        self.timer=QTimer(self); self.timer.timeout.connect(self.poll); self.timer.start(16)
        self.scan_timer=QTimer(self); self.scan_timer.timeout.connect(self.scan); self.scan_timer.start(1000)
        self.gallery_timer=QTimer(self); self.gallery_timer.timeout.connect(self.refresh_gallery); self.gallery_timer.start(5000)
        self.scan()
        self.reflow_workspace()
        if self.store.warning: self.notify(self.store.warning)

    def build_home(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        row = QHBoxLayout()
        row.setSpacing(14)

        # 1. Left: Hero Showcase Panel with vector blueprint & telemetry matrix
        hero, h = card('hero')
        h.setContentsMargins(22, 18, 22, 18)
        top_deck = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        self.controller_eyebrow = label('HARDWARE WORKSTATION', 'eyebrow')
        title_box.addWidget(self.controller_eyebrow)
        self.controller_heading = label(tr('通用 XInput'), 'productTitle')
        title_box.addWidget(self.controller_heading)

        # Clean pro workstation header (no redundant marketing text)
        self.controller_features = label('', 'caption')
        self.controller_features.setParent(hero)
        self.controller_features.hide()
        top_deck.addLayout(title_box)
        top_deck.addStretch()

        # Inline status readout (no nested pill box)
        pwr_layout = QHBoxLayout()
        pwr_layout.setContentsMargins(6, 4, 6, 4)
        pwr_layout.setSpacing(6)
        self.device_details = Indicator('power')
        pwr_layout.addWidget(self.device_details)
        self.power_label = label(tr('未连接'), 'muted')
        self.power_label.setObjectName('metric')
        pwr_layout.addWidget(self.power_label)
        top_deck.addLayout(pwr_layout)
        h.addLayout(top_deck)

        self.art = ControllerPhoto()
        self.art.setMinimumSize(280, 220)
        h.addWidget(self.art, 1)

        self.matrix = DotMatrixDisplay("FIELD TELEMETRY // HW-01", [
            ("LINK", "OFFLINE"), ("POWER", "--"), ("INPUTS", "--"), ("STATUS", "WAITING")
        ], color=TOKENS['green'])
        self.home_connection_hint = label(tr('连接手柄后，即可查看状态并配置按键。'), 'caption', True)
        h.addWidget(self.home_connection_hint)
        self.home_input_feedback = label(tr('等待输入'), 'muted', True)
        h.addWidget(self.home_input_feedback)

        self.photo_caption = label(tr('通用 XInput 键位示意'), 'muted')
        self.photo_caption.setParent(hero)
        self.photo_caption.hide()
        row.addWidget(hero, 7)

        # 2. Right: Single Unified Operations Rack
        right_panel = AppleGroup()
        right_panel.setMinimumWidth(320)
        right_panel.setMaximumWidth(420)

        # Unified monochrome badge icon color (Dieter Rams & Teenage Engineering hardware tone)
        BADGE_COLOR = (TOKENS['ink_2'], TOKENS['ink_2'])

        # Section 1: Tuning & Profiles
        hdr1 = QLabel(tr('配置与反馈'))
        hdr1.setObjectName('eyebrow')
        hdr1.setStyleSheet(f"color: {TOKENS['ink_3']}; font-size: 10px; font-weight: 700; letter-spacing: 0.8px; padding: 10px 16px 4px 16px;")
        right_panel.vbox.addWidget(hdr1)

        self.profile_combo = QComboBox()
        self.profile_combo.addItems(self.store.profiles_for(None))
        self.profile_combo.setCurrentText(self.config['active_profile'])
        self.profile_combo.currentTextChanged.connect(self.change_profile)
        self.profile_combo.activated.connect(lambda index: self.use_current_as_manual(self.profile_combo.itemText(index)))
        self.profile_combo.setMinimumWidth(138)
        self.profile_combo.setMaximumWidth(168)
        self.profile_combo.setMinimumHeight(36)
        self.profile_combo.setMaxVisibleItems(10)
        row_profile = AppleRow('controller', BADGE_COLOR, tr('配置预设'), '', self.profile_combo)
        row_profile.setToolTip(tr('按键映射方案与预设配置切换'))
        right_panel.add_row(row_profile)

        mode_box = QWidget()
        mb_l = QHBoxLayout(mode_box)
        mb_l.setContentsMargins(14, 2, 14, 4)
        self.mode_badge = QLabel(tr('🎮  手柄原生宏模式'))
        self.mode_badge.setStyleSheet(f"background: rgba(255, 87, 34, 0.12); color: {TOKENS['accent']}; border: 1px solid rgba(255, 87, 34, 0.3); border-radius: 6px; padding: 4px 8px; font-weight: 700; font-size: 10.5px;")
        mb_l.addWidget(self.mode_badge, 1)
        right_panel.vbox.addWidget(mode_box)

        self.rumble_button = button(tr('脉冲测试'), self.rumble, icon='wave', pill=True)
        self.rumble_button.setMinimumWidth(116)
        self.rumble_button.setMinimumHeight(36)
        row_rumble = AppleRow('wave', BADGE_COLOR, tr('触觉反馈'), '', self.rumble_button)
        self.home_rumble_row = row_rumble
        row_rumble.setToolTip(tr('双马达触觉脉冲与响应测试'))
        right_panel.add_row(row_rumble)

        self.nav_mapping_btn = button(tr('编辑按键 ›'), self._on_nav_mapping_clicked, pill=True)
        self.nav_mapping_btn.setMinimumWidth(116)
        self.nav_mapping_btn.setMinimumHeight(36)
        row_map = AppleRow('mapping', BADGE_COLOR, tr('按键映射'), '', self.nav_mapping_btn)
        row_map.setToolTip(tr('自定义按键键位与长按/短按宏映射'))
        right_panel.add_row(row_map)

        # Section 2: Shortcuts & Dispatch
        hdr2 = QLabel(tr('快捷操作'))
        hdr2.setObjectName('eyebrow')
        hdr2.setStyleSheet(f"color: {TOKENS['ink_3']}; font-size: 10px; font-weight: 700; letter-spacing: 0.8px; padding: 14px 16px 4px 16px;")
        right_panel.vbox.addWidget(hdr2)

        self.capture_action_btn = button(tr('查看图库 ›'), lambda: self.navigate(2), pill=True)
        self.capture_action_btn.setMinimumWidth(116)
        self.capture_action_btn.setMinimumHeight(36)
        self.row_capture = AppleRow('camera', BADGE_COLOR, tr('截图'), '', self.capture_action_btn)
        self.capture_heading = self.row_capture.title_label
        self.create_hint = QLabel(right_panel)
        self.create_hint.hide()
        right_panel.add_row(self.row_capture)

        self.guide_action_btn = button(tr('系统菜单 ›'), lambda: self.navigate(1), pill=True)
        self.guide_action_btn.setMinimumWidth(116)
        self.guide_action_btn.setMinimumHeight(36)
        self.row_guide = AppleRow('controller', BADGE_COLOR, tr('Xbox 导航'), '', self.guide_action_btn)
        self.guide_heading = self.row_guide.title_label
        self.guide_hint = QLabel(right_panel)
        self.guide_hint.hide()
        right_panel.add_row(self.row_guide)

        self.touch_action_btn = button(tr('手势映射 ›'), lambda checked=False: self.open_touch_editor(), pill=True)
        self.touch_action_btn.setMinimumWidth(116)
        self.touch_action_btn.setMinimumHeight(36)
        self.row_touchpad = AppleRow('touchpad', BADGE_COLOR, tr('触摸板'), tr('设置鼠标与手势动作'), self.touch_action_btn)
        right_panel.add_row(self.row_touchpad)
        right_panel.vbox.addStretch()
        telemetry = QVBoxLayout()
        telemetry.setContentsMargins(14, 12, 14, 12)
        telemetry.addWidget(self.matrix)
        right_panel.vbox.addLayout(telemetry)

        row.addWidget(right_panel, 3)
        layout.addLayout(row, 1)

        # 3. Bottom: Recent Captures Strip (Direct flat tray, no nested card-in-card)
        bottom = QWidget()
        b = QVBoxLayout(bottom)
        b.setContentsMargins(4, 4, 4, 0)
        b.setSpacing(8)
        heading = QHBoxLayout()
        head_v = QVBoxLayout()
        head_v.setSpacing(2)
        head_v.addWidget(label(tr('近期截图'), 'section'))
        head_v.addWidget(label(tr('快速回看最近保存的画面与精彩瞬间。'), 'caption'))
        heading.addLayout(head_v)
        heading.addStretch()
        all_btn = button(tr('查看完整图库 ›'), lambda: self.navigate(2))
        all_btn.setObjectName('nav')
        heading.addWidget(all_btn)
        b.addLayout(heading)

        self.recent_row = QHBoxLayout()
        self.recent_row.setSpacing(12)
        b.addLayout(self.recent_row)
        layout.addWidget(bottom)
        self.stack.addWidget(scroll(page))


    def build_mapping(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        tools = QHBoxLayout()
        tools.setSpacing(10)
        self.mapping_profile = label(tr_profile(self.config['active_profile']), 'section')
        self.mapping_profile.setParent(page)
        self.mapping_profile.hide()
        tools.addWidget(label(tr('手柄按键预设：'), 'muted'))
        self.mapping_combo = QComboBox()
        self.mapping_combo.setMinimumWidth(180)
        self.mapping_combo.currentTextChanged.connect(self.on_mapping_combo_changed)
        self.mapping_combo.activated.connect(lambda index: self.use_current_as_manual(self.mapping_combo.itemText(index)))
        tools.addWidget(self.mapping_combo)

        self.mapping_active_badge = QLabel(tr('已生效'))
        self.mapping_active_badge.setStyleSheet(f"background: rgba(16, 185, 129, 0.15); color: {TOKENS['green']}; border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 6px; padding: 3px 8px; font-weight: 700; font-size: 11px;")
        tools.addWidget(self.mapping_active_badge)

        self.mapping_activate_btn = button(tr('设为当前生效'), self.activate_current_gamepad_profile, primary=True, icon='play')
        tools.addWidget(self.mapping_activate_btn)

        tools.addStretch()
        self.learn_button = button(tr('识别按键'), self.start_learning, icon='controller')
        self.learn_button.setCheckable(True)
        tools.addWidget(self.learn_button)
        layout.addLayout(tools)
        profile_actions = QHBoxLayout()
        profile_actions.setSpacing(8)
        instruction = label(tr('选择按键，直接在动作卡中编辑映射。'), 'caption', True)
        instruction.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        profile_actions.addWidget(instruction, 1)
        profile_actions.addWidget(button(tr('新建手柄配置'), lambda: self.duplicate_profile(target_mode='gamepad'), icon='plus'))
        profile_actions.addWidget(button(tr('恢复默认'), self.reset_profile, icon='refresh'))
        self.mapping_more_button = button(tr('更多设置'), lambda: None)
        mapping_more = QMenu(self.mapping_more_button)
        self.application_profiles_action = mapping_more.addAction(tr('应用关联'), self.open_application_profiles)
        mapping_more.addAction(tr('设为手动预设'), lambda: self.change_profile(self.current_gamepad_profile()))
        mapping_more.addSeparator()
        self.import_profile_action = mapping_more.addAction(tr('导入预设'), self.import_profile)
        self.export_profile_action = mapping_more.addAction(tr('导出此预设'), lambda: self.export_profile(self.current_gamepad_profile()))
        self.mapping_more_button.setMenu(mapping_more)
        profile_actions.addWidget(self.mapping_more_button)
        self.delete_profile_btn = button(tr('删除'), self.delete_profile, icon='trash')
        self.delete_profile_btn.setObjectName('danger')
        profile_actions.addWidget(self.delete_profile_btn)
        layout.addLayout(profile_actions)

        workspace = QWidget()
        workspace.setMinimumHeight(350)
        columns = QHBoxLayout(workspace)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(16)

        # The controller is the input surface; action editing lives beside it.
        stage, body = card('hero')
        self.mapping_stage = stage
        stage.setFixedHeight(max(350, min(760, self.height() - 290)))
        body.setContentsMargins(22, 18, 22, 18)
        body.setSpacing(12)
        self.mapping_model = label(tr('通用 XInput'), 'productTitle')
        body.addWidget(self.mapping_model)
        body.addWidget(label(tr('点击手柄上的按键'), 'caption'))
        self.mapping_art = ControllerInput()
        self.mapping_art.setMinimumSize(240, 140)
        self.mapping_art.input_clicked.connect(self.select_mapping)
        body.addWidget(self.mapping_art, 1)

        self.selected_key = 0
        columns.addWidget(stage, 5, Qt.AlignTop)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(14)
        self.mapping_deck = MappingDeck(self)
        self.mapping_deck.trigger_selected.connect(self.select_mapping_trigger)
        editor_layout.addWidget(self.mapping_deck)
        input_card, input_layout = card()
        input_layout.setContentsMargins(14, 14, 14, 14)
        input_header = QHBoxLayout()
        input_header.addWidget(label(tr('选择输入'), 'section'))
        input_header.addStretch()
        input_header.addWidget(label(tr('圆点表示已配置'), 'caption'))
        input_layout.addLayout(input_header)
        rows = QWidget()
        grid = QGridLayout(rows)
        self.mapping_grid = grid
        grid.setContentsMargins(0, 0, 4, 0)
        grid.setSpacing(8)
        for i, key in enumerate([*range(64), 'LT', 'RT']):
            box = QPushButton()
            box.setObjectName('mappingTile')
            box.setCheckable(True)
            box.setFixedHeight(48)
            box.setCursor(Qt.PointingHandCursor)
            box.clicked.connect(lambda checked=False, k=key: self.select_mapping(k))
            b = QHBoxLayout(box)
            b.setContentsMargins(14, 8, 14, 8)
            b.setSpacing(10)

            name = label(self.button_names.get(key, str(key + 1)) if isinstance(key, int) else
                         trigger_label(key, 'generic'), 'section')
            name.setMinimumWidth(0)
            name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            name.setAttribute(Qt.WA_TransparentForMouseEvents)
            b.addWidget(name, 1)
            info = label('', 'muted')
            info.setObjectName('caption')
            info.setFixedWidth(12)
            info.setAlignment(Qt.AlignCenter)
            info.setStyleSheet(f"color: {TOKENS['cyan']}; font-size: 10px;")
            info.setAttribute(Qt.WA_TransparentForMouseEvents)
            b.addWidget(info)

            self.mapping_boxes[key] = (box, name)
            self.mapping_labels[key] = info
            grid.addWidget(box, i // 2, i % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.mapping_input_order = button_order('generic') + ['LT', 'RT']
        self.mapping_available = set(range(15)) | {'LT', 'RT'}
        self.mapping_inputs = scroll(rows)
        self.mapping_inputs.setMinimumWidth(0)
        self.mapping_inputs.setMinimumHeight(190)
        self.mapping_inputs.setMaximumHeight(328)
        self.mapping_inputs.setFrameShape(QFrame.NoFrame)
        self.mapping_inputs.viewport().installEventFilter(self)
        input_layout.addWidget(self.mapping_inputs)
        editor_layout.addWidget(input_card)
        editor_layout.addStretch()
        columns.addWidget(editor, 6)
        layout.addWidget(workspace, 3)
        self.mapping_feedback = label(tr('等待输入'), 'muted', True)
        feedback = QHBoxLayout()
        feedback.addWidget(self.mapping_feedback, 1)
        layout.addLayout(feedback)
        self.trigger_log = TriggerLog()
        layout.addWidget(self.trigger_log)
        self.stack.addWidget(scroll(page))

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Resize and not getattr(self, 'closed', False)
                and hasattr(self, 'mapping_inputs') and isValid(self.mapping_inputs)
                and watched is self.mapping_inputs.viewport()):
            self.reflow_mapping_inputs(event.size().width())
        return super().eventFilter(watched, event)

    def reflow_mapping_inputs(self, width=None):
        if not hasattr(self, 'mapping_inputs'):
            return
        width = self.mapping_inputs.viewport().width() if width is None else width
        columns = 3 if width >= 600 else 2
        for row in range(self.mapping_grid.rowCount()):
            self.mapping_grid.setRowStretch(row, 0)
        for column in range(3):
            self.mapping_grid.setColumnStretch(column, 1 if column < columns else 0)
        for box, _ in self.mapping_boxes.values():
            self.mapping_grid.removeWidget(box)
            box.hide()
        position = 0
        for key in self.mapping_input_order:
            box = self.mapping_boxes[key][0]
            self.mapping_grid.removeWidget(box)
            box.setVisible(key in self.mapping_available)
            if key in self.mapping_available:
                self.mapping_grid.addWidget(box, position // columns, position % columns)
                position += 1
        self.mapping_grid.setRowStretch((position + columns - 1) // columns, 1)

    def build_gallery(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.only_favorites = IconButton('heart', tr('仅看收藏'), size=32)
        self.only_favorites.setCheckable(True)
        self.only_favorites.toggled.connect(self.refresh_gallery)
        self.only_favorites.setStyleSheet(f'background: {TOKENS["elevated"]}; border: 1px solid {TOKENS["border_hi"]}; border-radius: {TOKENS["r_sm"]}px;')

        folder_btn = IconButton('folder', tr('打开截图文件夹'), self.open_capture_folder, 32)
        folder_btn.setStyleSheet(f'background: {TOKENS["elevated"]}; border: 1px solid {TOKENS["border_hi"]}; border-radius: {TOKENS["r_sm"]}px;')

        self.clean_captures_btn = IconButton('trash', tr('清理未收藏截图'), self.clean_unfavorited_captures, 32)
        self.clean_captures_btn.setObjectName('icon_danger')
        self.clean_captures_btn.setStyleSheet(f'background: {TOKENS["elevated"]}; border: 1px solid {TOKENS["border_hi"]}; border-radius: {TOKENS["r_sm"]}px;')

        bar.addWidget(self.only_favorites)
        bar.addWidget(folder_btn)
        bar.addWidget(self.clean_captures_btn)
        bar.addStretch()

        self.gallery_info = label('', 'muted')
        self.gallery_info.setStyleSheet(f'color: {TOKENS["ink_3"]}; font-size: 12px; font-weight: 600;')
        bar.addWidget(self.gallery_info)

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr('搜索截图文件...'))
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(240)
        self.search.setFixedHeight(32)
        self.search.setStyleSheet(
            f'background: {TOKENS["elevated"]}; border: 1px solid {TOKENS["border_hi"]}; '
            f'border-radius: {TOKENS["r_sm"]}px; padding: 0 12px; font-size: 12px;'
        )
        self.search.addAction(glyph('search', TOKENS['ink_3']), QLineEdit.LeadingPosition)
        self.search.textChanged.connect(self.refresh_gallery)
        bar.addWidget(self.search)
        layout.addLayout(bar)
        self.gallery_content = QWidget()
        self.gallery_grid = QGridLayout(self.gallery_content)
        self.gallery_grid.setContentsMargins(0, 10, 0, 10)
        self.gallery_grid.setSpacing(16)
        self.gallery_view = scroll(self.gallery_content)
        layout.addWidget(self.gallery_view, 1)
        self.stack.addWidget(page)

    def build_test(self):
        self.events = QListWidget()
        self.events.setWindowTitle(tr('活动记录'))
        self.events.resize(640, 400)
        self.events.setParent(self, Qt.Dialog)
        self.events.hide()
        self.tester = InputTester(self.events.show, self.test_rumble,
                                  self.open_stick_measurement, self.export_diagnostic)
        self.stack.addWidget(scroll(self.tester))

    def export_diagnostic(self):
        """Write a sanitized support report only after an explicit save choice."""
        filename = 'GamePadStudio-diagnostic-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.json'
        try:
            path, _ = QFileDialog.getSaveFileName(
                self, tr('导出诊断'), filename,
                tr('GamePad Studio 诊断报告 (*.json)'),
                options=QFileDialog.DontUseNativeDialog)
            if not path:
                return
            from .diagnostic_report import build_report, save_report
            status = self.client.status if self.remote and self.client.connected else None
            report = build_report(self.store.root, device=self.snapshot, status=status)
            save_report(path, report)
        except Exception as exc:
            self.notify(tr('诊断导出失败：') + str(exc))
            return
        self.notify(tr('诊断报告已保存：') + Path(path).name)

    def open_stick_measurement(self):
        from .stick_calibration import supports_right_stick
        from .stick_calibration_ui import StickMeasurementDialog
        if not supports_right_stick(self.snapshot):
            self.notify(tr('当前设备未提供可测量的右摇杆'))
            return
        if self.remote and not self.client.connected:
            self.notify(tr('后台未连接，无法测量'))
            return
        if self.remote:
            self.client.send('suspend', seconds=2)
        self.engine.reset(); self.actions.release_all()
        try:
            dialog = StickMeasurementDialog(self)
            dialog.exec()
            dialog.deleteLater()
        finally:
            if self.remote:
                self.client.send('suspend', seconds=2 if QApplication.activeModalWidget() is not None else 0)

    def open_mapping_swap(self, profile, first):
        from .mapping_swap_ui import MappingSwapDialog
        if not self.snapshot:
            self.notify(tr('请先连接手柄'))
            return
        if profile not in self.store.profiles_for(self.snapshot):
            self.notify(tr('输入设备已变化，请重新打开映射编辑。'))
            return
        if self.remote and not self.client.connected:
            self.notify(tr('后台未连接，修改尚未保存'))
            return
        self.end_learning()
        if self.remote:
            self.client.send('suspend', seconds=2)
        self.engine.reset(); self.actions.release_all()
        try:
            dialog = MappingSwapDialog(self, profile, str(first))
            dialog.exec()
            dialog.deleteLater()
        except ValueError as exc:
            self.notify(tr(str(exc)))
        finally:
            if self.remote:
                self.client.send('suspend', seconds=2 if QApplication.activeModalWidget() is not None else 0)

    def build_settings(self):
        page = QWidget()
        grid = QGridLayout(page)
        self.settings_grid = grid
        self.settings_page = page
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(16)

        # 1. 截图服务与存储
        cap = AppleGroup()
        hdr_cap = QLabel('  01 // CAPTURE ENGINE · ' + tr('截图服务与存储'))
        hdr_cap.setObjectName('eyebrow')
        hdr_cap.setStyleSheet(f"color: {TOKENS['accent']}; font-size: 10.5px; font-weight: 700; padding: 8px 16px 2px 16px;")
        cap.vbox.addWidget(hdr_cap)

        self.mode_combo = QComboBox()
        for name, value in [
            (tr('🎮 智能游戏屏幕 (自动探测锁定)'), 'game'),
            (tr('🖥️ 双屏全景全录制 (全部显示器 7680x2160)'), 'all'),
            (tr('🖥️ 显示器 1 (主屏幕)'), 'monitor_1'),
            (tr('🖥️ 显示器 2 (副屏幕)'), 'monitor_2'),
            (tr('🪟 智能目标窗口（前台优先）'), 'window'),
        ]:
            self.mode_combo.addItem(name, value)
        cur_mode = self.config.get('capture_mode', 'game')
        if cur_mode == 'monitor':
            cur_mode = 'game'
        idx = self.mode_combo.findData(cur_mode)
        self.mode_combo.setCurrentIndex(max(0, idx))
        self.mode_combo.currentIndexChanged.connect(lambda: self.setting('capture_mode', self.mode_combo.currentData()))
        self.mode_combo.setMinimumWidth(245)
        self.mode_combo.setMaximumWidth(320)
        self.mode_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        cap.add_row(AppleRow('camera', (TOKENS['accent'], TOKENS['accent_lo']), tr('捕获目标屏幕与范围'), tr('选择截图范围；录制屏幕在精彩回放中单独设置。'), self.mode_combo))

        if sys.platform == 'darwin' and not WINDOWS_FEATURES:
            self.screenshot_hotkey_button = button(tr('设置快捷键'), self.open_screenshot_hotkey_editor, pill=True)
            self.screenshot_hotkey_row = AppleRow(
                'camera', (TOKENS['purple'], TOKENS['accent_lo']), tr('物理键盘截图'),
                tr('在任何窗口按下快捷键，按上方范围保存截图。'), self.screenshot_hotkey_button)
            cap.add_row(self.screenshot_hotkey_row)
            self.refresh_screenshot_hotkey_status()

        folder_row = QWidget()
        f_layout = QHBoxLayout(folder_row)
        f_layout.setContentsMargins(14, 8, 14, 8)
        f_layout.setSpacing(12)
        f_layout.addWidget(SquircleBadge('folder', (TOKENS['purple'], TOKENS['accent_lo'])))
        f_v = QVBoxLayout()
        f_v.setContentsMargins(0, 0, 0, 0)
        f_v.setSpacing(2)
        f_title = label(tr('截图存储路径'), 'section')
        f_v.addWidget(f_title)
        self.folder_label = label(self.config['save_dir'], 'muted')
        self.folder_label.setObjectName('metric')
        self.folder_label.setWordWrap(True)
        self.folder_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.folder_label.setToolTip(self.config['save_dir'])
        f_v.addWidget(self.folder_label)
        f_layout.addLayout(f_v, 1)
        self.folder_button = button(tr('更改目录...'), self.choose_folder, pill=True)
        f_layout.addWidget(self.folder_button)
        cap.add_row(folder_row)

        cool_row = QWidget()
        c_layout = QHBoxLayout(cool_row)
        c_layout.setContentsMargins(14, 8, 14, 8)
        c_layout.setSpacing(12)
        c_layout.addWidget(SquircleBadge('timer', (TOKENS['amber'], TOKENS['amber'])))
        c_tv = QVBoxLayout()
        c_tv.setContentsMargins(0, 0, 0, 0)
        c_tv.setSpacing(3)
        c_top = QHBoxLayout()
        c_top.addWidget(label(tr('防抖冷却间隔'), 'section'))
        c_top.addStretch()
        cool_val = label(f"{self.config['cooldown']:.2f} " + tr('秒'), 'metric')
        cool_val.setStyleSheet(f"font: 12px {MONO_FONT_STACK}; font-weight: 700; color: {TOKENS['amber']};")
        c_top.addWidget(cool_val)
        c_tv.addLayout(c_top)
        c_desc = label(tr('连击防误触时间阈值'), 'caption')
        c_tv.addWidget(c_desc)

        cool_slider = QSlider(Qt.Horizontal)
        cool_slider.setRange(10, 200)
        cool_slider.setValue(round(self.config['cooldown'] * 100))
        c_tv.addWidget(cool_slider)
        c_layout.addLayout(c_tv, 1)

        def on_cool_change(v):
            sec = v / 100
            cool_val.setText(f"{sec:.2f} " + tr('秒'))
            self.setting('cooldown', sec)
        cool_slider.valueChanged.connect(on_cool_change)
        cap.add_row(cool_row)

        # 机械快门声音反馈开关
        self.shutter_sound_box = Toggle(tr('启用'))
        self.shutter_sound_box.setChecked(bool(self.config.get('capture_sound_enabled', True)))
        self.shutter_sound_box.toggled.connect(lambda v: self.setting('capture_sound_enabled', v))
        cap.add_row(AppleRow('wave', (TOKENS['amber'], TOKENS['accent_lo']), tr('机械快门音效反馈'), tr('截图成功时通过系统播放清脆的高保真相机机械快门声'), self.shutter_sound_box))

        # 截图手柄触觉微脉冲开关
        self.shutter_haptics_box = Toggle(tr('启用'))
        self.shutter_haptics_box.setChecked(bool(self.config.get('capture_haptics_enabled', True)))
        self.shutter_haptics_box.toggled.connect(lambda v: self.setting('capture_haptics_enabled', v))
        self.shutter_haptics_row = AppleRow('controller', (TOKENS['purple'], TOKENS['accent_lo']), tr('掌心触觉脉冲反馈'), tr('截图成功瞬间手柄给予 60ms 两段式物理快门轻触确认'), self.shutter_haptics_box)
        cap.add_row(self.shutter_haptics_row)

        # 屏蔽 Windows 截图与 Game Bar 弹窗开关
        self.gamebar_shield_box = Toggle(tr('启用'))
        init_shield = bool(WINDOWS_FEATURES and self.config.get('gamebar_shield_enabled', False))
        self.gamebar_shield_box.setChecked(init_shield)
        self.gamebar_shield_box.setEnabled(WINDOWS_FEATURES)
        self.gamebar_shield_box.setToolTip('' if WINDOWS_FEATURES else tr('请先连接并选择原生手柄；启用后游戏也无法直接读取该手柄'))
        self.gamebar_shield_box.toggled.connect(self.on_toggle_gamebar_shield)
        cap.add_row(AppleRow('shield', (TOKENS['purple'], TOKENS['accent_lo']), tr('屏蔽 Windows 截图与 Game Bar 弹窗') if WINDOWS_FEATURES else tr('屏蔽手柄触发系统弹窗'), tr('关闭 Windows 的手柄游戏栏与游戏录制响应') if WINDOWS_FEATURES else tr('隔离所选手柄的原始输入；系统菜单抑制效果需设备实测'), self.gamebar_shield_box))

        grid.addWidget(cap, 0, 0)

        # 2. 硬件交互与反馈
        hardware = AppleGroup()
        hdr_hw = QLabel('  02 // HAPTICS & ILLUMINATION · ' + tr('硬件交互与反馈'))
        hdr_hw.setObjectName('eyebrow')
        hdr_hw.setStyleSheet(f"color: {TOKENS['amber']}; font-size: 10.5px; font-weight: 700; padding: 8px 16px 2px 16px;")
        hardware.vbox.addWidget(hdr_hw)

        led_row = QWidget()
        l_layout = QHBoxLayout(led_row)
        l_layout.setContentsMargins(14, 8, 14, 8)
        l_layout.setSpacing(12)
        l_layout.addWidget(SquircleBadge('lightbulb', (TOKENS['cyan'], TOKENS['accent_lo'])))
        l_v = QVBoxLayout()
        l_v.setContentsMargins(0, 0, 0, 0)
        l_v.setSpacing(2)
        l_title = label(tr('LED 状态光条'), 'section')
        l_v.addWidget(l_title)
        l_desc = label(tr('手柄呼吸光条发光色调'), 'caption')
        l_v.addWidget(l_desc)
        l_layout.addLayout(l_v, 1)

        colors = QHBoxLayout()
        colors.setSpacing(8)
        self.led_buttons = []
        cur_led = self.config.get('led', '#5686ff')
        swatches = [
            ('#5686ff', tr('经典蓝 (默认)')),
            ('#ffffff', tr('纯白')),
            ('#000000', tr('关闭灯光')),
            (TOKENS['accent'], tr('活力橙')),
            (TOKENS['purple'], tr('霓虹紫')),
            (TOKENS['green'], tr('荧光绿')),
            (TOKENS['red'], tr('战意红')),
            (TOKENS['amber'], tr('暖阳金')),
        ]
        for color, name in swatches:
            b = LedSwatch(color, lambda checked=False, c=color: self.set_led(c))
            b.setToolTip(name)
            b.set_selected(str(color).lower() == str(cur_led).lower())
            colors.addWidget(b)
            self.led_buttons.append(b)

        self.led_reset_btn = QPushButton(tr('恢复默认'))
        self.led_reset_btn.setObjectName('pill')
        self.led_reset_btn.setCursor(Qt.PointingHandCursor)
        self.led_reset_btn.setToolTip(tr('恢复 PS5 DualSense 默认经典蓝色 (#5686ff)'))
        self.led_reset_btn.clicked.connect(lambda: self.set_led('#5686ff'))
        colors.addWidget(self.led_reset_btn)
        l_layout.addLayout(colors)
        self.led_settings_row = led_row
        hardware.add_row(led_row)

        self.battery_notifications_box = Toggle(tr('启用'))
        self.battery_notifications_box.setChecked(True)
        self.battery_notifications_box.toggled.connect(
            lambda value: self.setting('battery_notifications_enabled', value))
        self.battery_settings_row = AppleRow(
            'battery_low', (TOKENS['amber'], TOKENS['orange']), tr('低电量提醒'),
            tr('电量低或极低时提醒一次，充电恢复后重新判断'), self.battery_notifications_box)
        hardware.add_row(self.battery_settings_row)

        rumble_row = QWidget()
        r_layout = QHBoxLayout(rumble_row)
        r_layout.setContentsMargins(14, 8, 14, 8)
        r_layout.setSpacing(12)
        r_layout.addWidget(SquircleBadge('wave', (TOKENS['purple'], TOKENS['purple'])))
        r_tv = QVBoxLayout()
        r_tv.setContentsMargins(0, 0, 0, 0)
        r_tv.setSpacing(4)
        r_top = QHBoxLayout()
        r_top.addWidget(label(tr('双马达振动强度'), 'section'))
        r_top.addStretch()
        rumble_val = label(f"{round(device_config(self.config, self.snapshot)['rumble'] * 100)}%", 'metric')
        rumble_val.setStyleSheet(f"font: 12px {MONO_FONT_STACK}; font-weight: 700; color: {TOKENS['accent']};")
        r_top.addWidget(rumble_val)
        r_tv.addLayout(r_top)
        r_desc = label(tr('触觉反馈马达输出力度'), 'caption')
        r_tv.addWidget(r_desc)

        r_bottom = QHBoxLayout()
        r_bottom.setSpacing(10)
        rumble_slider = QSlider(Qt.Horizontal)
        rumble_slider.setRange(0, 100)
        rumble_slider.setValue(round(device_config(self.config, self.snapshot)['rumble'] * 100))
        r_bottom.addWidget(rumble_slider, 1)

        self.feedback_rumble = button(tr('脉冲测试'), self.rumble, primary=True, icon='wave', pill=True)
        r_bottom.addWidget(self.feedback_rumble)
        r_tv.addLayout(r_bottom)
        r_layout.addLayout(r_tv, 1)

        def on_rumble_change(v):
            pct = v / 100
            rumble_val.setText(f"{v}%")
            self.setting('rumble', pct)
        rumble_slider.valueChanged.connect(on_rumble_change)
        self.rumble_settings_row = rumble_row
        self.rumble_slider = rumble_slider
        self.rumble_value = rumble_val
        hardware.add_row(rumble_row)

        self.curve_rows = {}
        for kind, title, subtitle in (
            ('trigger', '扳机输入曲线', '左右扳机 · 输入灵敏度与死区'),
            ('rumble', '双马达振动曲线', '低频 / 高频 · 本应用的振动反馈'),
            ('trigger_rumble', '扳机振动曲线', '左右扳机马达 · 默认关闭')):
            edit = button(tr('编辑曲线'), lambda checked=False, kind=kind: self.open_curve_editor(kind), pill=True)
            row = AppleRow('wave', ['#8b5cf6', '#6d28d9'], tr(title), tr(subtitle), edit)
            self.curve_rows[kind] = row
            hardware.add_row(row)

        hint_row = QWidget()
        hint_layout = QHBoxLayout(hint_row)
        hint_layout.setContentsMargins(14, 8, 14, 8)
        hint_layout.setSpacing(12)
        hint_layout.addWidget(SquircleBadge('gamepad', (TOKENS['amber'], TOKENS['accent_lo'])))
        hint_v = QVBoxLayout()
        hint_v.setContentsMargins(0, 0, 0, 0)
        hint_v.setSpacing(3)
        hint_v.addWidget(label(tr('未检测到线性扳机（数字按键模式）'), 'section'))
        hint_v.addWidget(label(tr('当前手柄扳机识别为数字按键，不支持行程曲线。若支持 XInput，请长按 LOGO 键 3~5 秒或切换背部拨档至 PC/X 档。'), 'caption', True))
        hint_layout.addLayout(hint_v, 1)
        self.trigger_mode_hint = hint_row
        self.trigger_mode_hint.hide()
        hardware.add_row(self.trigger_mode_hint)

        self.touch_mouse_box = Toggle(tr('启用'))
        self.touch_mouse_box.setChecked(self.config['touch_mouse'])
        self.touch_mouse_box.toggled.connect(lambda value: self.setting('touch_mouse', value))
        self.touch_settings_row = AppleRow('touchpad', (TOKENS['green'], TOKENS['green']), tr('单指鼠标'), tr('单指滑动移动 Windows 鼠标指针'), self.touch_mouse_box)
        hardware.add_row(self.touch_settings_row)
        self.touch_gesture_settings_row = AppleRow(
            'touchpad', (TOKENS['green'], TOKENS['green']), tr('触摸板手势'),
            tr('单次动作映射与识别灵敏度'), button(tr('手势映射 ›'), lambda checked=False: self.open_touch_editor(), pill=True))
        hardware.add_row(self.touch_gesture_settings_row)

        # 触觉拟真引擎与波形调校
        haptic_row = QWidget()
        h_layout = QHBoxLayout(haptic_row)
        h_layout.setContentsMargins(14, 8, 14, 8)
        h_layout.setSpacing(12)
        h_layout.addWidget(SquircleBadge('wave', (TOKENS['accent'], TOKENS['accent_lo'])))
        h_tv = QVBoxLayout()
        h_tv.setContentsMargins(0, 0, 0, 0)
        h_tv.setSpacing(6)
        h_title = label(tr('应用触觉反馈'), 'section')
        h_tv.addWidget(h_title)
        h_desc = label(tr('毫秒级振动脉冲 · 快门、冲击与心跳反馈'), 'caption', True)
        h_tv.addWidget(h_desc)

        h_btns = QHBoxLayout()
        h_btns.setSpacing(8)
        self.test_haptic_shutter = button(tr('快门触觉'), lambda: self.test_haptic_pattern('shutter'), pill=True)
        self.test_haptic_impact = button(tr('冲击脉冲'), lambda: self.test_haptic_pattern('impact'), pill=True)
        self.test_haptic_heart = button(tr('心跳律动'), lambda: self.test_haptic_pattern('heartbeat'), pill=True)
        h_btns.addWidget(self.test_haptic_shutter)
        h_btns.addWidget(self.test_haptic_impact)
        h_btns.addWidget(self.test_haptic_heart)
        h_btns.addStretch()
        h_tv.addLayout(h_btns)
        h_layout.addLayout(h_tv, 1)
        self.haptic_settings_row = haptic_row
        hardware.add_row(haptic_row)
        self.hardware_empty = label(tr('连接手柄后显示它支持的硬件设置。'), 'caption', True)
        self.hardware_empty.setContentsMargins(16, 4, 16, 12)
        hardware.add_row(self.hardware_empty)

        grid.addWidget(hardware, 0, 1)

        # 3. 输入手势设定与文档
        general = AppleGroup()
        hdr_gen = QLabel('  03 // GESTURES & AUTOMATION · ' + tr('手势与高级设置'))
        hdr_gen.setObjectName('eyebrow')
        hdr_gen.setStyleSheet(f"color: {TOKENS['green']}; font-size: 10.5px; font-weight: 700; padding: 8px 16px 2px 16px;")
        general.vbox.addWidget(hdr_gen)

        lp_row = QWidget()
        lp_layout = QHBoxLayout(lp_row)
        lp_layout.setContentsMargins(14, 8, 14, 8)
        lp_layout.setSpacing(12)
        lp_layout.addWidget(SquircleBadge('timer', (TOKENS['amber'], TOKENS['amber'])))
        lp_tv = QVBoxLayout()
        lp_tv.setContentsMargins(0, 0, 0, 0)
        lp_tv.setSpacing(4)
        lp_top = QHBoxLayout()
        lp_top.addWidget(label(tr('长按手势识别阈值'), 'section'))
        lp_top.addStretch()
        lp_val = label(f"{self.config['long_press']:.2f} " + tr('秒'), 'metric')
        lp_val.setStyleSheet(f"font: 12px {MONO_FONT_STACK}; font-weight: 700; color: {TOKENS['chalk']};")
        lp_top.addWidget(lp_val)
        lp_tv.addLayout(lp_top)
        lp_desc = label(tr('按住按键达到设定时长触发二次宏动作'), 'caption')
        lp_tv.addWidget(lp_desc)

        lp_slider = QSlider(Qt.Horizontal)
        lp_slider.setRange(30, 150)
        lp_slider.setValue(round(self.config['long_press'] * 100))
        lp_tv.addWidget(lp_slider)
        lp_layout.addLayout(lp_tv, 1)

        def on_lp_change(v):
            sec = v / 100
            lp_val.setText(f"{sec:.2f} " + tr('秒'))
            self.setting('long_press', sec)
        self.long_press_slider = lp_slider
        self.long_press_value = lp_val
        lp_slider.valueChanged.connect(on_lp_change)
        general.add_row(lp_row)

        self.lang_combo = QComboBox()
        self.lang_combo.addItem('跟随系统 (System Default)', 'auto')
        self.lang_combo.addItem('English (US)', 'en')
        self.lang_combo.addItem('简体中文 (Simplified Chinese)', 'zh')
        cur_pref = self.lang_pref or self.config.get('language', 'auto')
        idx = self.lang_combo.findData(cur_pref)
        if idx >= 0:
            self.lang_combo.setCurrentIndex(idx)
        def on_lang_change(index):
            val = self.lang_combo.currentData()
            self.setting('language', val)
            set_language(val)
            self.notify(tr('界面语言已更新，部分设置重启后生效'))
        self.lang_combo.currentIndexChanged.connect(on_lang_change)
        self.lang_combo.setMinimumWidth(235)
        self.lang_combo.setMaximumWidth(290)
        self.lang_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        general.add_row(AppleRow('globe', (TOKENS['purple'], TOKENS['accent_lo']), tr('界面语言 / Language'), tr('界面语言与国际化设置'), self.lang_combo))

        help_btn = button(tr('查阅指南 ›'), self.show_help, pill=True)
        general.add_row(AppleRow('help', (TOKENS['accent'], TOKENS['accent_lo']), tr('使用指南与硬件支持'), tr('查阅全型号支持与高级特性说明'), help_btn))
        grid.addWidget(general, 1, 0)

        # 4. 常驻服务与自启
        background = AppleGroup()
        hdr_bg = QLabel('  04 // RUNTIME & DAEMON · ' + tr('系统服务与开机启动'))
        hdr_bg.setObjectName('eyebrow')
        hdr_bg.setStyleSheet(f"color: {TOKENS['cyan']}; font-size: 10.5px; font-weight: 700; padding: 8px 16px 2px 16px;")
        background.vbox.addWidget(hdr_bg)

        ag_row = QWidget()
        ag_layout = QHBoxLayout(ag_row)
        ag_layout.setContentsMargins(14, 8, 14, 8)
        ag_layout.setSpacing(12)
        ag_layout.addWidget(SquircleBadge('cpu', (TOKENS['cyan'], TOKENS['accent_lo'])))
        ag_tv = QVBoxLayout()
        ag_tv.setContentsMargins(0, 0, 0, 0)
        ag_tv.setSpacing(2)
        ag_title = label(tr('常驻映射监听进程 (Agent)'), 'section')
        ag_tv.addWidget(ag_title)
        ag_desc = label(tr('后台超低延迟按键拦截与手势守护服务'), 'caption')
        ag_tv.addWidget(ag_desc)
        ag_layout.addLayout(ag_tv, 1)

        self.agent_status = Indicator()
        self.agent_status.setText(tr('正在连接'))
        ag_layout.addWidget(self.agent_status)
        self.agent_toggle = button(tr('停止服务'), self.toggle_agent, pill=True)
        self.agent_toggle.setMinimumWidth(88)
        ag_layout.addWidget(self.agent_toggle)
        background.add_row(ag_row)

        self.emergency_hotkey_button = button(tr('设置快捷键'), self.open_emergency_hotkey_editor, pill=True)
        self.emergency_hotkey_button.setEnabled(emergency_hotkey_supported())
        self.emergency_hotkey_button.setToolTip('' if emergency_hotkey_supported() else tr('当前平台不支持全局紧急快捷键，请使用窗口中的暂停按钮'))
        self.emergency_hotkey_row = AppleRow(
            'shield', (TOKENS['amber'], TOKENS['orange']), tr('紧急暂停'),
            tr('使用物理键盘暂停当前映射') if emergency_hotkey_supported() else tr('当前平台不支持全局紧急快捷键，请使用窗口中的暂停按钮'), self.emergency_hotkey_button)
        background.add_row(self.emergency_hotkey_row)

        if sys.platform == 'darwin' and not WINDOWS_FEATURES:
            self.input_permission_button = button(tr('授权辅助功能'), self.authorize_input, pill=True)
            self.input_permission_row = AppleRow(
                'shield', (TOKENS['accent'], TOKENS['accent_lo']), tr('macOS 键鼠输出权限'),
                tr('请在系统设置 → 隐私与安全性 → 辅助功能中授权当前运行程序'), self.input_permission_button)
            background.add_row(self.input_permission_row)
            self.screen_permission_button = button(tr('授权屏幕录制'), self.authorize_screen_capture, pill=True)
            self.screen_permission_row = AppleRow(
                'camera', (TOKENS['purple'], TOKENS['accent_lo']), tr('macOS 截图与录制权限'),
                tr('请在系统设置 → 隐私与安全性 → 屏幕录制中授权当前运行程序'), self.screen_permission_button)
            background.add_row(self.screen_permission_row)
            self.refresh_input_permission_status()

        self.autostart = Toggle(tr('启用'))
        self.autostart.setChecked(autostart_enabled(self.store.root))
        self.autostart.setEnabled(autostart_supported())
        self.autostart.setToolTip('' if autostart_supported() else tr('当前平台不支持登录启动，请手动启动应用'))
        self.autostart.toggled.connect(self.toggle_autostart)
        background.add_row(AppleRow('autostart', (TOKENS['green'], TOKENS['green']), tr('系统开机自动启动'), tr('登录后在后台自动启动当前配置的映射服务') if autostart_supported() else tr('当前平台不支持登录启动，请手动启动应用'), self.autostart))
        grid.addWidget(background, 1, 1)

        # 5. 4K replay recording, using the encoder selected for this platform.
        replay_group = AppleGroup()
        replay_heading = ('HEVC / AV1 即时回放' if sys.platform == 'darwin' and not WINDOWS_FEATURES
                          else 'HEVC / AV1 标杆极清即时回放')
        hdr_replay = QLabel('  05 // 4K INSTANT REPLAY BUFFER · ' + tr(replay_heading))
        hdr_replay.setObjectName('eyebrow')
        hdr_replay.setStyleSheet(f"color: {TOKENS['purple']}; font-size: 10.5px; font-weight: 700; padding: 8px 16px 2px 16px;")
        replay_group.vbox.addWidget(hdr_replay)

        self.replay_toggle = Toggle(tr('启用'))
        self.replay_toggle.setChecked(bool(self.config.get('replay_buffer_enabled', False)))
        def on_replay_toggle(enabled):
            self.refresh_recording_displays()
            selected = self.replay_mode_combo.currentData()
            selected_row = next((row for row in self.recording_displays if selected == ('all' if row['index'] == 0 else f"monitor_{row['index']}")), None)
            if enabled and selected_row and not selected_row.get('recording_enabled', True):
                self.replay_toggle.blockSignals(True)
                self.replay_toggle.setChecked(False)
                self.replay_toggle.blockSignals(False)
                self.notify(selected_row['recording_reason'])
                return
            self.setting('replay_buffer_enabled', enabled)
            self._update_replay_hud()
        self.replay_toggle.toggled.connect(on_replay_toggle)
        replay_group.add_row(AppleRow('wave', (TOKENS['purple'], TOKENS['accent_lo']), tr('4K 极清回放缓存'),
                                      tr('先启用并等待画面积累，再用手柄映射或下方按钮保存过去的片段。'), self.replay_toggle))

        self.replay_mode_combo = QComboBox()
        self.replay_mode_combo.setMinimumWidth(180)
        self.replay_mode_combo.setMaximumWidth(360)
        self.replay_mode_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.replay_mode_combo.setMinimumContentsLength(18)
        self.replay_mode_combo.currentIndexChanged.connect(lambda: self.setting('replay_capture_mode', self.replay_mode_combo.currentData()))
        replay_group.add_row(AppleRow('camera', (TOKENS['accent'], TOKENS['accent_lo']), tr('录制屏幕'), tr('HDR 自动转换为标准颜色；刷新率不同的屏幕需分别录制。'), self.replay_mode_combo))
        self.recording_displays = []
        self.recording_display_hint = label('', 'caption', True)
        self.recording_display_hint.setContentsMargins(16, 0, 16, 8)
        replay_group.vbox.addWidget(self.recording_display_hint)
        self.refresh_recording_displays()

        rep_min_row = QWidget()
        rm_layout = QHBoxLayout(rep_min_row)
        rm_layout.setContentsMargins(14, 8, 14, 8)
        rm_layout.setSpacing(12)
        rm_layout.addWidget(SquircleBadge('timer', (TOKENS['accent'], TOKENS['accent_lo'])))
        rm_tv = QVBoxLayout()
        rm_tv.setContentsMargins(0, 0, 0, 0)
        rm_tv.setSpacing(2)
        rm_title = label(tr('最大回看时间 (纯内存滑动窗口)'), 'section')
        rm_tv.addWidget(rm_title)
        rm_desc = label(tr('100% 纯内存环形缓冲区保留的最长历史片段（零磁盘写入损耗）'), 'muted')
        rm_desc.setObjectName('caption')
        rm_tv.addWidget(rm_desc)

        cur_min = int(self.config.get('replay_buffer_minutes', 5))
        self.replay_min_label = label(f"{cur_min} " + tr('分钟'))
        self.replay_min_label.setStyleSheet(f"background: transparent; font: 12px {MONO_FONT_STACK}; font-weight: 700; color: {TOKENS['accent']}; padding: 0 2px;")
        
        self.replay_slider = QSlider(Qt.Horizontal)
        self.replay_slider.setRange(1, 10)
        self.replay_slider.setValue(cur_min)
        rm_tv.addWidget(self.replay_slider)
        rm_tv.addWidget(self.replay_min_label)
        rm_layout.addLayout(rm_tv, 1)

        def on_min_change(v):
            self.replay_min_label.setText(f"{v} " + tr('分钟'))
            self.setting('replay_buffer_minutes', v)
            self._update_replay_hud()
        self.replay_slider.valueChanged.connect(on_min_change)
        replay_group.add_row(rep_min_row)

        codec_row = QWidget()
        cd_layout = QHBoxLayout(codec_row)
        cd_layout.setContentsMargins(14, 8, 14, 8)
        cd_layout.setSpacing(12)
        cd_layout.addWidget(SquircleBadge('cpu', (TOKENS['green'], TOKENS['accent_lo'])))
        cd_tv = QVBoxLayout()
        cd_tv.setContentsMargins(0, 0, 0, 0)
        cd_tv.setSpacing(2)
        cd_title = label(tr('编码器与画质方案') if sys.platform == 'darwin' and not WINDOWS_FEATURES
                         else tr('硬件编码器与画质方案'), 'section')
        cd_tv.addWidget(cd_title)
        self.replay_codec_desc = label(tr('macOS 自动选择硬件或软件编码器与码率') if sys.platform == 'darwin' and not WINDOWS_FEATURES
                                       else tr('选择显卡硬件加速格式与码率'), 'muted')
        self.replay_codec_desc.setObjectName('caption')
        cd_tv.addWidget(self.replay_codec_desc)
        cd_layout.addLayout(cd_tv, 1)

        self.replay_codec_combo = QComboBox()
        self.replay_codec_combo.setMinimumWidth(180)
        self.replay_codec_combo.setMaximumWidth(300)
        self.replay_codec_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.replay_codec_combo.setMinimumContentsLength(18)
        self.replay_codec_combo.addItem(tr('HEVC 标杆极清 (推荐 · 50Mbps)'), 'hevc')
        self.replay_codec_combo.addItem(tr('AV1 软件编码 (45Mbps)') if sys.platform == 'darwin' and not WINDOWS_FEATURES
                                        else tr('AV1 次世代极清 (AMF/NVENC · 45Mbps)'), 'av1')
        self.replay_codec_combo.addItem(tr('H.264 兼容模式 (60Mbps)'), 'h264')
        cur_codec = self.config.get('replay_codec', 'hevc')
        self.replay_codec_combo.setCurrentIndex(max(0, self.replay_codec_combo.findData(cur_codec)))
        def on_codec_change():
            val = self.replay_codec_combo.currentData()
            self.setting('replay_codec', val)
            self._update_replay_hud()
        self.replay_codec_combo.currentIndexChanged.connect(on_codec_change)
        cd_layout.addWidget(self.replay_codec_combo)
        replay_group.add_row(codec_row)

        hud_row = QWidget()
        hud_layout = QHBoxLayout(hud_row)
        hud_layout.setContentsMargins(14, 8, 14, 8)
        hud_layout.setSpacing(12)
        hud_layout.addWidget(SquircleBadge('controller', (TOKENS['amber'], TOKENS['accent_lo'])))
        
        self.replay_hud_label = label(tr('正在检测编码器状态...') if sys.platform == 'darwin' and not WINDOWS_FEATURES
                                      else tr('正在探测硬件加速状态...'), 'muted')
        self.replay_hud_label.setStyleSheet(f"font: 11.5px {MONO_FONT_STACK}; color: {TOKENS['ink_2']}; font-weight: 600;")
        hud_layout.addWidget(self.replay_hud_label, 1)

        save_rep_btn = button(tr('立即保存当前回放'), self.trigger_manual_replay, pill=True)
        hud_layout.addWidget(save_rep_btn)
        replay_group.add_row(hud_row)

        grid.addWidget(replay_group, 2, 0, 1, 2)

        from .voice_entry_ui import VoiceEntryGroup
        self.voice_entry_group = VoiceEntryGroup(platform=sys.platform)
        self.voice_entry_group.hide()
        grid.addWidget(self.voice_entry_group, 3, 0, 1, 2)
        self.settings_groups = [cap, hardware, general, background, replay_group, self.voice_entry_group]
        for group in self.settings_groups:
            group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
            group.vbox.setAlignment(Qt.AlignTop)
            for text in group.findChildren(QLabel):
                if text.objectName() in ('caption', 'muted', 'section', 'eyebrow'):
                    text.setWordWrap(True)
                    text.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
                    text.setMinimumWidth(0)
        for header, title in [(hdr_cap, '截图服务与存储'), (hdr_hw, '硬件交互与反馈'),
                               (hdr_gen, '手势与高级设置'), (hdr_bg, '系统服务与开机启动'),
                               (hdr_replay, '精彩回放')]:
            header.setText(tr(title))
            header.setStyleSheet(f"color: {TOKENS['ink']}; font-size: 14px; font-weight: 700; padding: 14px 16px 10px;")
        self.settings_columns = 0
        self._update_replay_hud()
        self.stack.addWidget(scroll(page))

    def build_antigravity(self):
        from .antigravity_ui import AntigravityPage
        self.antigravity_page = AntigravityPage(self)
        self.stack.addWidget(scroll(self.antigravity_page))

    def _update_replay_hud(self):
        try:
            from .replay_service import calculate_estimated_ram_gb
            minutes = int(self.config.get('replay_buffer_minutes', 5))
            codec = self.config.get('replay_codec', 'hevc')
            bitrate = int(self.config.get('replay_bitrate_mbps', 50))
            ram_gb = calculate_estimated_ram_gb(minutes, bitrate)
            replay_status = self.client.status.get('replay', {}) if self.remote else {}
            enc = replay_status.get('encoder') or ('待启动' if get_language() == 'zh' else 'Not started')
            enabled = self.config.get('replay_buffer_enabled', False)
            if not enabled:
                status_text = '回放已关闭' if get_language() == 'zh' else 'Replay disabled'
            elif replay_status.get('last_error'):
                status_text = replay_status['last_error']
            elif replay_status.get('running'):
                status_text = '回放录制中' if get_language() == 'zh' else 'Replay recording'
            else:
                status_text = '等待录制启动' if get_language() == 'zh' else 'Waiting for recording'
            overhead_txt = f"{minutes} " + tr('分钟') + ("纯内存预估" if get_language() == 'zh' else " est. RAM")
            hw_core = ("编码器" if get_language() == 'zh' else "Encoder") if sys.platform == 'darwin' and not WINDOWS_FEATURES else (
                "硬件核心" if get_language() == 'zh' else "GPU Engine")
            self.replay_hud_label.setText(
                f"{status_text}  |  {codec.upper()} {bitrate}Mbps  |  {overhead_txt}: ~{ram_gb:.2f} GB (0 磁盘损耗)  |  {hw_core}: {enc}"
            )
            if replay_status.get('running') and replay_status.get('color_mode'):
                self.replay_hud_label.setText(self.replay_hud_label.text() + '  |  ' + replay_status['color_mode'])
        except Exception:
            pass

    def refresh_recording_displays(self):
        from .display_info import enumerate_displays, match_monitors, format_refresh
        from .replay_capture import create_replay_capture
        try:
            with create_replay_capture() as capture:
                rows = match_monitors(capture.monitors, enumerate_displays())
        except Exception:
            rows = [{'index': 0, 'recording_enabled': False,
                     'recording_reason': tr('无法确认所有显示器的刷新率，请选择单个屏幕录制')}]
        self.recording_displays = rows
        if WINDOWS_FEATURES and rows and rows[0].get('recording_enabled') and rows[0].get('display_count', 0) > 1 and rows[0].get('hdr_enabled'):
            rows[0]['recording_enabled'] = False
            rows[0]['recording_reason'] = tr('HDR 跨屏录制暂不支持，请选择单个屏幕录制')
        current = self.config.get('replay_capture_mode') or self.config.get('capture_mode', 'game')
        self.replay_mode_combo.blockSignals(True)
        self.replay_mode_combo.clear()
        self.replay_mode_combo.addItem(tr('跟随游戏所在屏幕'), 'game')
        for row in rows:
            index = row['index']
            if index == 0:
                title, mode = tr('0 号 · 全部屏幕'), 'all'
            else:
                hdr = 'HDR' if row.get('hdr_enabled') is True else 'SDR' if row.get('hdr_enabled') is False else tr('色彩待检测')
                title = f"{tr('屏幕')} {index} · {format_refresh(row.get('refresh_hz'))} · {hdr}"
                mode = f'monitor_{index}'
            self.replay_mode_combo.addItem(title, mode)
            item = self.replay_mode_combo.model().item(self.replay_mode_combo.count() - 1)
            item.setEnabled(row.get('recording_enabled', True))
            item.setToolTip(row.get('recording_reason') or row.get('friendly_name') or title)
        index = self.replay_mode_combo.findData(current)
        if index < 0:
            self.replay_mode_combo.addItem(tr('录制屏幕已断开'), current)
            index = self.replay_mode_combo.count() - 1
            self.replay_mode_combo.model().item(index).setEnabled(False)
            rows.append({'index': int(current.split('_')[-1]) if isinstance(current, str) and current.startswith('monitor_') and current.split('_')[-1].isdigit() else -1,
                         'recording_enabled': False, 'recording_reason': tr('录制屏幕已断开')})
        self.replay_mode_combo.setCurrentIndex(index)
        self.replay_mode_combo.blockSignals(False)
        reason = rows[0].get('recording_reason', '') if rows else ''
        self.recording_display_hint.setText(reason)
        self.recording_display_hint.setVisible(bool(reason))

    def trigger_manual_replay(self):
        if sys.platform == 'darwin' and not WINDOWS_FEATURES:
            if not self.config.get('replay_buffer_enabled', False):
                self.notify(tr('请先启用回放缓存，等待画面积累后保存'))
                return
            status = self.client.status.get('replay', {}) if self.remote and self.client.connected else {}
            if self.remote and not status.get('running'):
                self.notify(tr('回放录制尚未就绪，请检查状态并等待画面积累'))
                return
            if not self.remote and (not hasattr(self, 'replay_engine') or not self.replay_engine.is_running()):
                self.notify(tr('回放录制尚未就绪，请检查状态并等待画面积累'))
                return
        if self.remote:
            self.client.send('save_replay')
            self.notify(tr("正在生成 4K 极清精彩回放录像..."))
        else:
            if hasattr(self, 'replay_engine'):
                if sys.platform != 'darwin' and not self.replay_engine.is_running():
                    self.replay_engine.start()
                path = self.replay_engine.save_replay() if self.replay_engine.is_running() else None
                if path:
                    self.notify(f"🎬 {tr('精彩回放已保存')}: {Path(path).name}")
                    return
            if sys.platform == 'darwin' and not WINDOWS_FEATURES:
                self.notify(tr('请先启用回放缓存，等待画面积累后保存'))
            else:
                self.actions.shortcut('Win+Alt+G')
                self.notify(tr("已触发系统回放录制 (Win+Alt+G)"))

    def test_haptic_pattern(self, pattern: str):
        if not self.snapshot or not self.snapshot.get('rumble'):
            self.notify(tr('此设备暂时无法预览振动。'))
            return
        if self.remote:
            self.client.send('test_haptics', pattern=pattern, device_scope=profile_scope(self.snapshot),
                             instance_id=self.snapshot.get('instance_id'))
        else:
            if hasattr(self, 'haptic_engine'):
                self.haptic_engine.trigger_feedback(pattern)
        names = {'shutter': '快门触觉微脉冲', 'impact': '冲击振动脉冲', 'heartbeat': '心跳仿真律动'}
        self.notify(tr('已请求振动测试：') + tr(names.get(pattern, pattern)))

    def open_curve_editor(self, kind, channel=None):
        if supports_curve(self.snapshot, kind):
            dialog = CurveDialog(self, kind)
            if channel is not None:
                index = dialog.channel_combo.findData(channel)
                if index >= 0:
                    dialog.channel_combo.setCurrentIndex(index)
            dialog.exec()
            dialog.deleteLater()

    def open_touch_editor(self, profile=None, mode=None):
        if not supports_touch(self.snapshot):
            return False
        if profile is None:
            from .studio_core import profile_mode
            if mode is None:
                page = self.stack.currentIndex()
                mode = ('kbm' if page == 6 else 'gamepad' if page == 1 else
                        profile_mode(self.config, self.config.get('active_profile', '')))
            profile = self.virtual_kbm_page.current_scheme() if mode == 'kbm' else self.current_gamepad_profile()
        if profile not in self.store.profiles_for(self.snapshot):
            return False
        if self.remote:
            self.client.send('suspend', seconds=2)
        self.engine.reset()
        self.actions.release_all()
        dialog = TouchGestureDialog(self, profile)
        dialog.exec()
        dialog.deleteLater()
        if self.remote:
            self.client.send('suspend', seconds=2 if QApplication.activeModalWidget() is not None else 0)
        return True

    def preview_response_curve(self, kind, channel, curve, strength=.55):
        if not supports_curve(self.snapshot, kind):
            return False
        try:
            if self.remote:
                result = request(self.store.root, 'preview_curve', kind=kind, channel=channel,
                                 curve=normalize_curve(curve), strength=strength,
                                 device_scope=profile_scope(self.snapshot),
                                 instance_id=self.snapshot.get('instance_id'), timeout=1500)
                return bool(result and result.get('ok') and result.get('supported'))
            from .curve_preview import preview_response_curve
            return preview_response_curve(self.device, device_config(self.config, self.snapshot),
                                          self.snapshot, kind, channel, normalize_curve(curve),
                                          strength, self.haptic_engine)
        except (ValueError, TypeError, RuntimeError) as exc:
            self.notify(str(exc))
            return False

    def on_toggle_gamebar_shield(self, checked: bool):
        if sys.platform == 'darwin' and not WINDOWS_FEATURES:
            result = self.set_controller_isolation(checked)
            self.notify(result['message'])
            self.refresh_controller_isolation()
            return
        if not WINDOWS_FEATURES:
            self.notify(tr('此功能仅支持 Windows'))
            return
        from .gamebar_shield import set_gamebar_shield
        if self.remote:
            result = request(self.store.root, 'set_gamebar_shield', enabled=checked, timeout=4000)
            if result and result.get('ok') and 'applied' in result:
                msg = result['message']
                latest = ConfigStore(self.store.root)
                self.store.data.clear(); self.store.data.update(latest.data)
                self.store._baseline = copy.deepcopy(latest.data)
            else:
                msg = tr('后台未能应用设置，请重启后台后重试')
        else:
            _, msg = set_gamebar_shield(self.store, checked)
        self.notify(msg)
        enabled = self.config.get('gamebar_shield_enabled', False)
        self.gamebar_shield_box.blockSignals(True)
        self.gamebar_shield_box.setChecked(enabled)
        self.gamebar_shield_box.blockSignals(False)

        # 保持虚拟键鼠工作台内的快捷开关联动同步
        if hasattr(self, 'virtual_kbm_page') and hasattr(self.virtual_kbm_page, 'gamebar_shield_toggle'):
            self.virtual_kbm_page.gamebar_shield_toggle.blockSignals(True)
            self.virtual_kbm_page.gamebar_shield_toggle.setChecked(self.config.get('gamebar_shield_enabled', False))
            self.virtual_kbm_page.gamebar_shield_toggle.blockSignals(False)

    def set_controller_isolation(self, enabled):
        if self.remote:
            result = request(self.store.root, 'set_device_cloaking', enabled=enabled,
                             device_scope=profile_scope(self.snapshot), timeout=4000)
            if not result or not result.get('ok') or 'applied' not in result:
                return {'applied':False,'message':(result or {}).get('error') or tr('后台未能应用隔离设置')}
            latest = ConfigStore(self.store.root)
            self.store.data.clear(); self.store.data.update(latest.data)
            self.store._baseline = copy.deepcopy(latest.data)
            return result
        from .controller_isolation_service import set_controller_isolation
        def release():
            try:
                self.engine.reset()
            finally:
                self.actions.release_all()
        try:
            return set_controller_isolation(self.store,self.device,self.snapshot,enabled,release)
        except (ValueError,OSError,RuntimeError) as exc:
            return {'applied':False,'message':str(exc)}

    def refresh_controller_isolation(self):
        if sys.platform != 'darwin' or not hasattr(self,'gamebar_shield_box'):
            return
        from .controller_isolation_service import isolation_status
        status = isolation_status(self.device)
        self.gamebar_shield_box.blockSignals(True)
        self.gamebar_shield_box.setChecked(bool(status.get('active') or status.get('restore_pending')))
        self.gamebar_shield_box.setEnabled(bool(self.snapshot and (status.get('supported') or status.get('restore_pending'))))
        self.gamebar_shield_box.blockSignals(False)
        self.gamebar_shield_box.setToolTip(status.get('reason') or tr('隔离也会阻止游戏的原生手柄输入；关闭或退出软件时恢复'))

    def show_help(self):
        title = tr('使用指南')
        if get_language() == 'en':
            msg = (
                "Select a gamepad in the Controller Library. GamePad Studio maps one active controller at a time, "
                "releasing pressed keys upon switching or disconnecting.\n\n"
                "• PlayStation: Create / Share takes instant screenshots.\n"
                "• Xbox: Dedicated Share button captures screenshots; if absent, native input is preserved.\n"
                "• Switch: Capture button takes screenshots (or − minus if unmapped).\n\n"
                "Profiles are preserved per controller family. Click any controller button or key tile, then click Edit to configure.\n\n"
                "The background daemon runs on startup and mappings persist after closing this window. "
                "Virtual KBM and keyboard mappings do not block native input unless hardware cloaking (HidHide) is active."
            )
        else:
            msg = (
                "在手柄图库中选择设备。当前一次只为所选手柄执行映射，切换或断开时释放按键。\n\n"
                "PS：Create / Share 截图。Xbox：独立 Share 截图；未提供 Share 时保留原始按键，可自定义映射。Switch：优先 Capture，否则使用 −。\n\n"
                "预设按输入设备独立保存。点击映射页的手柄按键或按键列表，再点编辑。恢复默认只影响当前预设。\n\n"
                "后台随登录运行，关闭窗口不影响映射。键盘映射不屏蔽原始输入，Xbox 键的系统功能由 Windows 管理。游戏触觉和自适应扳机取决于游戏支持。\n\n"
                "产品图片来自品牌官网，通用手柄使用标注的示例机型。"
            )
        if sys.platform == 'darwin' and not WINDOWS_FEATURES:
            if get_language() == 'en':
                msg = (
                    "Select a gamepad in the Controller Library, then choose or edit a preset. "
                    "One selected controller is mapped at a time. Keys are released when switching or disconnecting.\n\n"
                    "Keyboard and mouse mapping requires Accessibility permission. Screenshots and replay require "
                    "Screen Recording permission. Authorize the running app from System Settings in the app's Settings page, "
                    "then restart the background service if macOS requires it.\n\n"
                    "Ctrl means the physical Control key. Cmd (Win / Meta in existing presets) means Command. "
                    "Controller isolation is opt-in and applies only to the selected supported native HID controller. "
                    "It also blocks games from reading that controller directly; turn it off to restore native input.\n\n"
                    "macOS supports game/window/display screenshots and same-refresh multi-display recording. "
                    "HDR-to-SDR capture requires macOS 15 or later on Apple Silicon. "
                    "Emergency Pause, login startup and .app preset associations are available in Settings. "
                    "Emergency shortcuts and login startup stay off until enabled. "
                    "Start/stop recording uses the Mac capture backend and saves a complete local MP4."
                )
            else:
                msg = (
                    "在控制器库中选择手柄，再选择或编辑预设。当前一次只为所选手柄执行映射，切换或断开时释放按键。\n\n"
                    "键鼠映射需要辅助功能权限；截图与回放需要屏幕录制权限。请在应用的系统设置页中授权当前运行程序，"
                    "系统要求时重新启动后台服务。\n\n"
                    "Ctrl 对应实际 Control 键，Cmd（兼容预设中的 Win / Meta）对应 Command 键。"
                    "设备隐身需明确开启，仅隔离所选支持的原生手柄；启用后游戏也不能直接读取该手柄，关闭时恢复原始输入。\n\n"
                    "macOS 支持游戏、窗口和显示器截图，以及同刷新率多屏录制。HDR 转标准颜色需要 macOS 15 或更新版本及 Apple Silicon。"
                    "录屏开关会保存从开始到停止的完整本地 MP4。系统设置中可配置紧急暂停、登录启动与 .app 应用关联；热键与登录启动默认关闭，需手动启用。"
                )
        QMessageBox.information(self, title, msg)

    def select_controller(self,instance):
        try:
            self.end_learning()
            if self.remote:self.client.send('select_device',instance_id=instance)
            else:
                self.engine.reset();self.actions.release_all();self.device.select(instance);self.poll()
                if self.snapshot:self.setting('preferred_controller',self.snapshot.get('device_key', self.snapshot['profile_key']))
        except (ValueError,RuntimeError) as exc:self.notify(str(exc))

    def rescan_controllers(self):
        if self.remote:self.client.send('scan')
        else:self.scan();self.poll()

    def update_controller_ui(self,state):
        self.device_capabilities_signature = self._device_ui_signature(state)
        family = profile_family(self.config, state)
        self.button_names = button_labels(family, state.get('controller_type', 0) if state else 0)
        if self.remote and state:
            latest = ConfigStore(self.store.root)
            self.store.data.clear(); self.store.data.update(latest.data)
            self.store._baseline = copy.deepcopy(latest.data)
        if not self.remote:
            identity = (profile_scope(state), state.get('instance_id')) if state else None
            if identity != getattr(self, '_activated_device_identity', object()):
                self.store.activate_controller(state)
                self._activated_device_identity = identity
                self.store.save()
        title = (state.get('name') or CATALOG[family]['name']) if state else tr('通用 XInput')
        description = state.get('name', title) if state else tr('未连接设备 · 通用 XInput 键位预览')
        self.controller_heading.setText(title)
        self.controller_heading.setToolTip(description)
        self.art.set_family(family); self.mapping_art.set_family(family)
        self.photo_caption.setText(PHOTOS.get(family, PHOTOS['generic'])['caption'] if state else tr('通用 XInput 键位示意'))
        self.mapping_model.setText(title); self.mapping_model.setToolTip(description)
        sources = set(input_sources(state))
        available = {int(key) for key in sources if key.isdigit()}
        self.mapping_sources = sources
        self.mapping_art.available = sources
        order = button_order(family)
        insert = order.index(10) + 1 if 10 in order else len(order)
        order[insert:insert] = ['LT', 'RT']
        self.mapping_input_order = order + [key for key in sorted(available) if key not in order]
        self.mapping_available = available | (sources & {'LT', 'RT'})
        for key in self.mapping_input_order:
            if key not in self.mapping_boxes:
                continue
            box, title_label = self.mapping_boxes[key]
            name = self.button_names.get(key, str(key + 1)) if isinstance(key, int) else trigger_label(key, family)
            title_label.setText(name)
            box.setAccessibleName(name); box.setToolTip(name)
        self.reflow_mapping_inputs()
        self.learn_button.setEnabled(True)
        previous_trigger = self.mapping_deck.selected_trigger
        if set(previous_trigger.split('+')).issubset(sources):
            self.select_mapping_trigger(previous_trigger)
        else:
            fallback = str(self.selected_key) if str(self.selected_key) in sources else next(iter(input_sources(state)), '0')
            if sources:
                self.select_mapping(fallback)
            else:
                self.mapping_art.select_buttons([])
                for box, _ in self.mapping_boxes.values():
                    box.setChecked(False)
        self.refresh_device_settings_ui(state)
        self.feedback_rumble.setEnabled(bool(state and state.get('rumble')))
        for control in (self.test_haptic_shutter, self.test_haptic_impact, self.test_haptic_heart):
            control.setEnabled(bool(state and state.get('rumble')))
            control.setToolTip(tr('双马达触觉脉冲与响应测试') if state and state.get('rumble') else tr('未连接手柄'))
        self.touch_mouse_box.setEnabled(supports_touch(state))
        self.touch_mouse_box.setToolTip(tr('触摸板鼠标') if supports_touch(state) else tr('设备未提供触摸板'))

        # Update Guide key and Touchpad row dynamically according to controller family
        if hasattr(self, 'guide_heading'):
            if family == 'xbox':
                self.guide_heading.setText(tr('Xbox 导航'))
                if hasattr(self, 'row_guide'):
                    self.row_guide.setToolTip(tr('Xbox 导航键：短按呼出 Game Bar，长按切换任务'))
            elif family == 'switch':
                self.guide_heading.setText(tr('Home 导航'))
                if hasattr(self, 'row_guide'):
                    self.row_guide.setToolTip(tr('Switch Home 键：短按返回主界面，长按快捷菜单'))
            elif family in ('dualsense', 'dualshock4'):
                self.guide_heading.setText(tr('PS 导航'))
                if hasattr(self, 'row_guide'):
                    self.row_guide.setToolTip(tr('PlayStation 系统键：呼出控制中心与多任务切换'))
            else:
                self.guide_heading.setText(tr('系统导航'))
                if hasattr(self, 'row_guide'):
                    self.row_guide.setToolTip(tr('通用手柄 Guide 键：呼出系统快捷主控'))

        # Update Eyebrow dynamically (concise serial header, zero marketing text)
        specs = {
            'dualsense': 'SONY · PLAYSTATION 5',
            'dualshock4': 'SONY · PLAYSTATION 4',
            'xbox': 'MICROSOFT · XBOX',
            'switch': 'NINTENDO · SWITCH',
            'generic': tr('通用手柄')
        }
        if hasattr(self, 'controller_eyebrow'):
            self.controller_eyebrow.setText(specs.get(family, specs['generic']))
        if hasattr(self, 'controller_features'):
            self.controller_features.hide()
        if hasattr(self, 'row_touchpad'):
            has_touchpad = supports_touch(state)
            self.row_touchpad.setVisible(has_touchpad)
            if hasattr(self.row_touchpad, '_associated_divider') and self.row_touchpad._associated_divider:
                self.row_touchpad._associated_divider.setVisible(has_touchpad)

        self.refresh_mappings()

    @staticmethod
    def _device_ui_signature(state):
        if not state:
            return None
        capabilities = curve_capabilities(state)
        return (state.get('family'), state.get('controller_type'), tuple(input_sources(state)),
                tuple(capabilities['trigger_axes']), capabilities['rumble'], capabilities['trigger_rumble'],
                bool(state.get('led')), bool(state.get('touchpad')))

    def toggle_autostart(self,enabled):
        if not autostart_supported():
            self.notify(tr('当前平台不支持登录启动，请手动启动应用'))
            return
        try:
            set_autostart(self.store.root,enabled)
            self._cancel_autostart_handoff()
            if enabled and self.remote and not self.client.connected:spawn(self.store.root,'--agent')
            if not enabled and self.remote and self.client.connected and sys.platform == 'darwin':
                # Unloading a LaunchAgent stops its process. Keep the open
                # workspace running with a manual backend after it releases
                # its input and lock, without changing the saved pause state.
                self._autostart_handoff_deadline = time.monotonic() + 30
                self._autostart_handoff_timer = QTimer(self)
                self._autostart_handoff_timer.setInterval(500)
                self._autostart_handoff_timer.timeout.connect(self._finish_autostart_handoff)
                self._autostart_handoff_timer.start()
            self.notify(tr('登录启动已开启') if enabled else tr('登录启动已关闭'))
        except OSError as exc:
            self.autostart.blockSignals(True); self.autostart.setChecked(not enabled); self.autostart.blockSignals(False); self.notify(tr('设置失败：')+str(exc))

    def _cancel_autostart_handoff(self):
        timer = getattr(self, '_autostart_handoff_timer', None)
        if timer:
            timer.stop()
            timer.deleteLater()
            self._autostart_handoff_timer = None

    def _finish_autostart_handoff(self):
        if self.closed or time.monotonic() > self._autostart_handoff_deadline:
            self._cancel_autostart_handoff()
            return
        status = request(self.store.root, 'status', timeout=150)
        if status and status.get('ok'):
            self._cancel_autostart_handoff()
            return
        pid = read_lock_pid(self.store.root / 'agent.lock')
        if pid and is_process_alive(pid):
            return
        self._cancel_autostart_handoff()
        cleanup_stale_agent(self.store.root)
        spawn(self.store.root, '--agent')

    def toggle_agent(self):
        if not self.remote:return
        self._cancel_autostart_handoff()
        if self.client.connected:self.client.send('stop')
        else:spawn(self.store.root,'--agent');self.notify(tr('后台启动中'))

    def agent_event(self,message):
        kind=message.get('type')
        if kind == 'battery':
            self.handle_battery_event(message)
            return
        if kind=='state' and hasattr(self,'replay_hud_label'):
            replay = message.get('replay', {})
            signature = (replay.get('running'), replay.get('encoder'), replay.get('last_error'), replay.get('color_mode'))
            if signature != getattr(self, '_replay_hud_signature', None):
                self._replay_hud_signature = signature
                self._update_replay_hud()
        if kind=='notice' and hasattr(self,'notice'):self.notify(tr(message['message']))
        elif kind=='capture':
            self.capture_button.setEnabled(True)
            if message.get('path'):self.captured(message['path'])
        elif kind=='replay_record':
            path = message.get('path')
            if path:
                self.notify(f"🎬 {tr('精彩回放已保存')}: {Path(path).name}")
            elif message.get("mode") == "system":
                self.notify(tr("已触发系统回放录制 (Win+Alt+G)"))
            else:
                self.notify(message.get("error") or "回放尚未就绪")
        elif kind=='recording':
            self.on_recording_finished(message.get('path',''), message.get('error',''))
        elif kind=='buttons' and self.learn:
            valid=[b for b in message['buttons'] if b in BUTTONS]
            if valid:self.end_learning();QTimer.singleShot(0,lambda:self.edit_mapping(valid[0]))
        elif kind=='reply' and not message.get('ok',True):self.notify(message.get('error', tr('操作失败') if get_language() == 'zh' else 'Operation failed'))
        elif kind=='state' and hasattr(self, 'virtual_kbm_page') and self.virtual_kbm_page.is_capturing:
            dev = message.get('device')
            if dev:
                self.virtual_kbm_page.handle_device_input(dev)

    def add_slider(self, layout, title, key, lo, hi, scale, unit=''):
        text=label('','muted'); slider=QSlider(Qt.Horizontal); slider.setRange(lo,hi); slider.setValue(round(self.config[key]*scale))
        def display(value):
            amount=f'{round(value/scale*100)}%' if key in ('rumble','deadzone') else f'{value/scale:.2f} {unit}'
            return f'{title}   {amount}'
        def change(value):
            text.setText(display(value)); self.setting(key,value/scale)
        text.setText(display(slider.value())); slider.valueChanged.connect(change); layout.addWidget(text); layout.addWidget(slider)

    def navigate(self,index):
        headings = ['设备概览','按键配置','截图图库','硬件遥测','系统设置','控制器库','虚拟键鼠','Antigravity 联动']
        descriptions = ['查看设备状态，调整配置并快速进入常用操作。',
                        '选择手柄按键，配置短按、长按与组合动作。',
                        '回看游戏截图与精彩回放，收藏值得保留的瞬间。',
                        '检查按键、摇杆和扳机的实时响应。',
                        '管理捕获、触觉反馈与后台服务，修改后自动保存。',
                        '连接设备，选择你的手柄并进入工作空间。',
                        '点击目标按键，选择或录入手柄按键、触摸手势。',
                        '通过手柄按键探测并唤起 Antigravity，激活对话框焦点并进行语音输入。']
        self.stack.setCurrentIndex(index)
        self.page_heading.setText(tr(headings[index]))
        self.page_description.setText(tr(descriptions[index]))
        for i,b in self.nav.items(): b.setChecked(i==index)
        if index==2: self.refresh_gallery()
        if index==4: self.refresh_recording_displays()
        if index==7 and hasattr(self, 'antigravity_page'): self.antigravity_page.refresh_status()
        if hasattr(self, 'virtual_kbm_page'):
            if index == 6:
                self.virtual_kbm_page.refresh_display()
            elif self.virtual_kbm_page.is_capturing:
                self.virtual_kbm_page.cancel_capture()

    def escape_workspace(self):
        if self.virtual_kbm_page.is_capturing:
            self.virtual_kbm_page.cancel_capture()
        else:
            self.leave_fullscreen()

    def reflow_workspace(self):
        if self.closed:
            return
        compact = self.width() <= 1200
        self.content_layout.setSpacing(12 if compact else 18)
        self.rail.setFixedWidth(64 if compact else 212)
        self.brand_copy.setVisible(not compact)
        for heading in self.nav_sections:
            heading.setVisible(not compact)
        for index, control in self.nav.items():
            control.setText('' if compact else tr(self.nav_titles[index]))
            if control.property('compact') != compact:
                control.setProperty('compact', compact)
                control.style().unpolish(control); control.style().polish(control)
        self.size_grip.setVisible(not (self.isFullScreen() or self.isMaximized()))
        if hasattr(self, 'mapping_stage'):
            self.mapping_stage.setFixedHeight(max(350, min(760, self.height() - 290)))
        if not hasattr(self, 'settings_groups'):
            return
        columns = 2 if self.stack.width() >= 1160 else 1
        if columns == self.settings_columns:
            return
        self.settings_columns = columns
        grid = self.settings_grid
        while grid.count():
            grid.takeAt(0)
        for row in range(grid.rowCount()): grid.setRowStretch(row, 0)
        grid.setColumnStretch(0, 1); grid.setColumnStretch(1, 1 if columns == 2 else 0)
        for index, group in enumerate(self.settings_groups):
            if columns == 2:
                if index >= 4:
                    grid.addWidget(group, index - 2, 0, 1, 2, Qt.AlignTop)
                else:
                    grid.addWidget(group, index // 2, index % 2, Qt.AlignTop)
            else:
                grid.addWidget(group, index, 0, Qt.AlignTop)
        grid.setRowStretch(len(self.settings_groups) - 2 if columns == 2 else len(self.settings_groups), 1)

    def show_home(self):
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
        self.show()
        self.raise_()
        self.activateWindow()

    def notify(self,text):
        if not text.startswith(('按下', 'Pressed', '已连接', 'Connected', '手柄已断开', 'Gamepad disconnected')):
            self.notice.setText(text);self.notice_timer.start(4500)
        self.events.insertItem(0, f'{datetime.now():%H:%M:%S}   {text}')
        while self.events.count()>100: self.events.takeItem(100)
        self.log_rows.append(dict(time=datetime.now().isoformat(),message=text))
        with (self.store.root/'events.jsonl').open('a',encoding='utf-8') as file: file.write(json.dumps(self.log_rows[-1],ensure_ascii=False)+'\n')

    def _valid_battery_warning(self, event):
        state = self.snapshot
        if not isinstance(event, dict) or not state:
            return False
        level = event.get('level')
        return (type(level) is int and level in (0, 1)
                and event.get('device_scope') == profile_scope(state)
                and event.get('instance_id') == state.get('instance_id')
                and normalize_power(state) == level
                and device_config(self.config, state).get('battery_notifications_enabled') is True)

    def handle_battery_event(self, event, notify=True):
        if not self._valid_battery_warning(event):
            return False
        if not notify:
            self.battery_warning = dict(event)
            return True
        event_id = event.get('id')
        if not isinstance(event_id, str) or not event_id or len(event_id) > 100:
            return False
        if event_id in self._battery_alert_ids:
            return False
        self.battery_warning = dict(event)
        self._battery_alert_ids.add(event_id)
        self._battery_alert_order.append(event_id)
        if len(self._battery_alert_order) > 128:
            self._battery_alert_ids.discard(self._battery_alert_order.popleft())
        title = tr('手柄电量极低') if event['level'] == 0 else tr('手柄电量低')
        description = tr('请尽快连接电源，避免游戏中断。') if event['level'] == 0 else tr('建议为手柄充电。')
        name = str(self.snapshot.get('name') or tr('当前手柄'))
        self.notify(f'{title} · {name}：{description}')
        if self.tray.isVisible() and QSystemTrayIcon.supportsMessages():
            self.tray.showMessage(title, name + '\n' + description, QSystemTrayIcon.Warning, 5000)
        return True

    def refresh_battery_status(self):
        state = self.snapshot
        enabled = device_config(self.config, state).get('battery_notifications_enabled') is True
        if not self.remote:
            event = self.battery_monitor.sample(state, time.monotonic(), enabled=enabled)
            if event:
                self.handle_battery_event(event)
            warning = self.battery_monitor.warning(state, enabled=enabled)
        else:
            warning = self.client.status.get('battery_warning') if self.client.connected else None
        self.battery_warning = dict(warning) if self._valid_battery_warning(warning) else None
        tip = (tr('请尽快连接电源，避免游戏中断。') if self.battery_warning['level'] == 0
               else tr('建议为手柄充电。')) if self.battery_warning else ''
        self.power_label.setToolTip(tip)
        reported = battery_reported(state)
        if reported != getattr(self, '_battery_reported_last', None):
            self._battery_reported_last = reported
            self.refresh_device_settings_ui(state)

    def setting(self,key,value):
        previous_hotkey = copy.deepcopy(self.config.get(key)) if key in ('emergency_hotkey', 'screenshot_hotkey') else None
        self.store.set_setting(key, value, self.snapshot)
        try:
            self.store.save()
        except Exception:
            if key in ('emergency_hotkey', 'screenshot_hotkey'):
                self.config[key] = previous_hotkey
            raise
        if self.remote:self.client.send('reload')
        if key == 'emergency_hotkey':
            if self.emergency_hotkey:
                self.emergency_hotkey.configure(self.config.get('emergency_hotkey'))
            self.refresh_emergency_hotkey_status()
            self.refresh_input_permission_status()
        elif key == 'screenshot_hotkey':
            if self.screenshot_hotkey:
                self.screenshot_hotkey.configure(self.config.get('screenshot_hotkey'))
            self.refresh_screenshot_hotkey_status()
        if key=='long_press': self.engine.threshold=value
        if key=='touch_mouse': self.last_touch=None
        if not self.remote and (key in DEVICE_SETTING_KEYS or key == 'capture_sound_enabled'):
            self.apply_device_feedback_settings(self.snapshot)

    def apply_device_feedback_settings(self, state):
        if self.remote:
            return
        settings = device_config(self.config, state)
        if hasattr(self.device, 'set_response_curves'):
            self.device.set_response_curves(settings)
        if hasattr(self, 'haptic_engine'):
            self.haptic_engine.set_config(
                sound_enabled=settings.get('capture_sound_enabled', True),
                haptics_enabled=bool(state and state.get('rumble')
                                     and settings.get('haptic_engine_enabled', True)
                                     and settings.get('capture_haptics_enabled', True)),
                intensity=settings.get('haptic_intensity', 1.),
                profile=settings.get('haptic_profile', 'crisp'),
                trigger_rumble_enabled=bool(state and state.get('trigger_rumble')
                                           and settings.get('trigger_rumble_enabled', False)))

    @staticmethod
    def _show_device_row(row, visible):
        row.setVisible(visible)
        divider = getattr(row, '_associated_divider', None)
        if divider is not None:
            divider.setVisible(visible)

    def refresh_device_settings_ui(self, state):
        settings = device_config(self.config, state)
        capabilities = curve_capabilities(state)
        has_led = bool(state and state.get('led'))
        has_rumble = bool(state and state.get('rumble'))
        has_touch = supports_touch(state)
        for row, visible in ((self.led_settings_row, has_led),
                             (self.battery_settings_row, battery_reported(state)),
                             (self.rumble_settings_row, has_rumble),
                             (self.haptic_settings_row, has_rumble),
                             (self.shutter_haptics_row, has_rumble),
                             (self.home_rumble_row, has_rumble),
                             (self.touch_settings_row, has_touch),
                             (self.touch_gesture_settings_row, has_touch)):
            self._show_device_row(row, visible)
        self.touch_action_btn.setEnabled(has_touch)
        self.touch_gesture_settings_row.control.setEnabled(has_touch)
        if hasattr(self, 'virtual_kbm_page'):
            self.virtual_kbm_page.refresh_touch_action(state)
        for kind, row in self.curve_rows.items():
            supported = bool(capabilities['trigger_axes']) if kind == 'trigger' else capabilities[kind]
            self._show_device_row(row, supported)
            row.control.setEnabled(supported)
        is_connected = bool(state and state.get('connected') is not False and state.get('name'))
        show_trigger_hint = is_connected and not bool(capabilities['trigger_axes'])
        if hasattr(self, 'trigger_mode_hint'):
            self._show_device_row(self.trigger_mode_hint, show_trigger_hint)
        self.curve_rows['trigger_rumble'].subtitle_label.setText(
            tr('左右扳机马达 · 应用反馈已启用') if settings.get('trigger_rumble_enabled') else
            tr('左右扳机马达 · 默认关闭'))
        self.hardware_empty.setVisible(not (has_led or has_rumble or has_touch or capabilities['trigger_axes']
                                            or capabilities['trigger_rumble'] or battery_reported(state)))
        for control, value in ((self.rumble_slider, round(settings['rumble'] * 100)),
                               (self.long_press_slider, round(settings['long_press'] * 100))):
            control.blockSignals(True)
            control.setValue(value)
            control.blockSignals(False)
        self.rumble_value.setText(f"{round(settings['rumble'] * 100)}%")
        self.long_press_value.setText(f"{settings['long_press']:.2f} " + tr('秒'))
        for control, value in ((self.touch_mouse_box, settings['touch_mouse']),
                               (self.battery_notifications_box, settings['battery_notifications_enabled']),
                               (self.shutter_haptics_box, settings['capture_haptics_enabled'])):
            control.blockSignals(True)
            control.setChecked(bool(value))
            control.blockSignals(False)
        self.rumble_slider.setEnabled(has_rumble)
        self.battery_notifications_box.setEnabled(battery_reported(state))
        self.shutter_haptics_box.setEnabled(has_rumble)
        for swatch in self.led_buttons:
            swatch.setEnabled(has_led)
            swatch.set_selected(str(swatch.color).lower() == str(settings.get('led', '#5686ff')).lower())
        if hasattr(self, 'led_reset_btn'):
            self.led_reset_btn.setEnabled(has_led)
        self.art.set_led(settings['led']); self.mapping_art.set_led(settings['led'])
        if has_led and not self.remote:
            self.device.led(settings['led'])
        self.apply_device_feedback_settings(state)
        self.refresh_controller_isolation()

    def scan(self):
        if self.closed:
            return
        try: self.device.scan()
        except Exception as exc: self.notify(tr('设备扫描失败：')+str(exc))

    def testing_protected(self):
        return self.stack.currentIndex()==3 and self.isVisible() and not self.isMinimized() and self.tester.protect.isChecked()

    def preview_requested(self):
        return self.stack.currentIndex() in (1,6) and self.isActiveWindow() and self.virtual_kbm_page.preview_toggle.isChecked()

    def poll(self):
        if self.closed:
            return
        try:
            state=self.device.read(); self.snapshot=state; connected=state is not None
            if not self.remote:
                self.device_sample.emit(state)
            identity=(state.get('instance_id'),profile_scope(state)) if state else None
            profile_changed=self.remote and (self.client.status.get('profile',self.config['active_profile'])!=self.config['active_profile'] or self.client.status.get('mapping_revision',0)!=self.config.get('mapping_revision',0))
            if profile_changed:
                latest=ConfigStore(self.store.root);self.store.data.clear();self.store.data.update(latest.data);self.store._baseline=copy.deepcopy(latest.data)
            capabilities_changed = self._device_ui_signature(state) != getattr(self, 'device_capabilities_signature', None)
            if identity!=self.device_identity or profile_changed or capabilities_changed:
                self.engine.reset();self.actions.release_all();self.last_buttons=set();self.device_identity=identity
                self.update_controller_ui(state)
                if sys.platform == 'darwin' and not self.remote:
                    from .controller_isolation_service import isolation_requested
                    self._isolation_apply_pending = bool(state and isolation_requested(self.config,state))
            if sys.platform == 'darwin' and not self.remote and getattr(self,'_isolation_apply_pending',False):
                from .controller_isolation_service import controller_neutral
                if controller_neutral(state):
                    self._isolation_apply_pending = False
                    self.notify(self.set_controller_isolation(True)['message'])
            if not self.remote:
                self.update_application_profile()
            self.refresh_battery_status()
            self.controllers.set_devices(self.device.available,state.get('instance_id') if state else None,state)
            if self.remote:
                online=self.client.connected; self.enabled=self.client.status.get('enabled',False)
                self.agent_status.setText(tr('运行中') if online else tr('已停止')); self.agent_toggle.setText(tr('停止后台') if online else tr('启动后台'))
                self.pause_button.setEnabled(online);self.pause_button.setText(tr('暂停映射') if self.enabled else tr('恢复映射'));self.pause_button.set_symbol('pause' if self.enabled else 'play')
                self.capture_button.setEnabled(online and not self.client.status.get('capturing',False))
                preview = self.preview_requested()
                now=time.monotonic()
                if online and (preview != getattr(self,'last_preview',False) or (preview and now-getattr(self,'preview_lease_time',0)>.25)):
                    self.client.send('preview',seconds=.75 if preview else 0)
                    self.preview_lease_time=now;self.last_preview=preview
                is_kbm_capturing = hasattr(self, 'virtual_kbm_page') and getattr(self.virtual_kbm_page, 'is_capturing', False)
                if online and (self.learn or is_kbm_capturing or QApplication.activeModalWidget() is not None or self.testing_protected()):
                    now=time.monotonic()
                    if now-getattr(self,'last_suspend',0)>.5:
                        self.client.send('suspend',seconds=2);self.last_suspend=now
            self.refresh_emergency_hotkey_status()
            self.refresh_screenshot_hotkey_status()
            self.refresh_controller_isolation()
            self.refresh_input_permission_status()
            if connected != self.previous_connected:
                self.engine.reset(); self.actions.release_all(); self.last_touch=None
                self.previous_connected=connected
                self.notify(tr('已连接 ') + state['name'] if connected else tr('手柄已断开，等待重新连接'))
                if connected and state.get('led') and not self.remote: self.device.led(device_config(self.config, state)['led'])
            self.home_connection_hint.setText(tr('按下手柄按键查看实时反馈，或进入按键配置调整动作。') if connected else tr('连接手柄后，即可查看状态并配置按键。'))
            if connected:
                dev_name = state.get('name', tr('手柄'))
                self.status_badge.setText(tr('●  已连接'))
                if hasattr(self, 'status_label'):
                    full_status = f"{dev_name} · {tr('已连接')}"
                    self.status_label.setMinimumWidth(64)
                    self.status_label.setMaximumWidth(200)
                    self.status_label.setText(self.status_label.fontMetrics().elidedText(full_status, Qt.ElideRight, 196))
                    self.status_label.setToolTip(full_status)
                    self.status_label.setStyleSheet(f"font-size: 11.5px; font-weight: 700; color: {TOKENS['ink']}; padding-right: 4px;")
                if hasattr(self, 'status_capsule'):
                    self.status_capsule.setStyleSheet(f"QWidget#statusCapsule {{ border: 1px solid rgba(16, 185, 129, 0.45); background: {TOKENS['surface']}; border-radius: 18px; }}")
            else:
                self.status_badge.setText(tr('○  等待连接'))
                if hasattr(self, 'status_label'):
                    self.status_label.setMinimumWidth(52)
                    self.status_label.setMaximumWidth(140)
                    self.status_label.setText(tr('未连接'))
                    self.status_label.setToolTip(tr('未连接手柄'))
                    self.status_label.setStyleSheet(f"font-size: 11.5px; font-weight: 700; color: {TOKENS['ink_2']}; padding-right: 4px;")
                if hasattr(self, 'status_capsule'):
                    self.status_capsule.setStyleSheet(f"QWidget#statusCapsule {{ border: 1px solid {TOKENS['border']}; background: {TOKENS['surface']}; border-radius: 18px; }}")
            self.side_status.setText(tr('●  已连接') if connected else tr('○  未连接'))
            self.rumble_button.setEnabled(bool(state and state.get('rumble')))
            self.tester.update_state(state,collect=self.stack.currentIndex()==3 and self.isVisible() and not self.isMinimized())
            self.mapping_art.update_state(state)
            if hasattr(self, 'matrix'):
                if state:
                    p_simple = {-1:'未报告',0:'极低',1:'低',2:'中等',3:'充足',4:'外接'}.get(state.get('power'), '未知') if get_language() == 'zh' else {-1:'N/A',0:'CRIT',1:'LOW',2:'MED',3:'FULL',4:'EXT'}.get(state.get('power'), 'UNK')
                    self.matrix.set_lines([
                        ('DEVICE', state['name'][:12].upper()),
                        ('POWER', p_simple),
                        ('STATUS', 'ONLINE'),
                        ('INPUTS', f"{len(state.get('buttons', []))} ACTIVE")
                    ])
                else:
                    self.matrix.set_lines([
                        ('LINK', 'OFFLINE'),
                        ('POWER', '--'),
                        ('INPUTS', '--'),
                        ('STATUS', 'WAITING')
                    ])
            if self.remote:
                feedback = self.client.status.get('mapping', {}) if self.client.connected else {}
            else:
                feedback = self.engine.feedback() if state else {}
            self.virtual_kbm_page.set_device_state(state)
            self.refresh_application_profile_status()
            self.virtual_kbm_page.update_feedback(feedback, bool(state), self.client.status.get('suspended',False) if self.remote else QApplication.activeModalWidget() is not None)
            self.mapping_deck.feedback(feedback)
            if not self.notice_timer.isActive():
                unsupported = feedback.get('unsupported_bindings') or []
                if self.enabled and unsupported:
                    self.notice.setText(tr('当前预设存在不支持的按键输出') + f' ({len(unsupported)})')
                    self.notice.setToolTip('；'.join(f"{row['value']}：{row['reason']}" for row in unsupported[:5]))
                else:
                    self.notice.setText(tr('映射运行中') if self.enabled else tr('映射已暂停'))
                    self.notice.setToolTip('')
            events = feedback.get('events',[])
            if events:
                from .mapping_engine import trigger_label
                last=events[-1]
                message=trigger_label(last['trigger'],(state or {}).get('family','generic'))+' '+('长按' if last.get('gesture')=='long' else '短按')+' → '+last.get('action','')
                message=('安全试按 · ' if feedback.get('preview') else '')+message
                self.mapping_feedback.setText(message); self.home_input_feedback.setText(message)
            elif not state:
                self.mapping_feedback.setText(tr('等待输入')); self.home_input_feedback.setText(tr('等待输入'))
            if not state:
                self.engine.update(None, self.config, enabled=False)
                self.device_details.setText(tr('未连接')); self.power_label.setText(tr('未连接')); self.engine.reset(); self.last_buttons=set()
                return
            power={-1:tr('电量未报告'),0:tr('电量极低'),1:tr('电量低'),2:tr('电量中等'),3:tr('电量充足'),4:tr('外接供电')}
            p_text = power.get(state['power'], tr('电量未知'))
            self.device_details.setText(p_text); self.power_label.setText(p_text)
            buttons=set(state['buttons']); new=buttons-self.last_buttons; self.last_buttons=buttons
            if hasattr(self, 'virtual_kbm_page'):
                self.virtual_kbm_page.set_device_state(state)
            if hasattr(self, 'virtual_kbm_page') and self.virtual_kbm_page.is_capturing:
                self.virtual_kbm_page.handle_device_input(state)
                return
            if self.learn and new and not self.remote:
                self.end_learning();number=min(new)
                if number in BUTTONS: QTimer.singleShot(0,lambda:self.edit_mapping(number))
                return
            if not self.remote and self.enabled and not self.learn and not self.testing_protected() and QApplication.activeModalWidget() is None:
                self.engine.update(state, self.config, preview=self.preview_requested())
            else:
                self.engine.reset()
                self.last_touch=None
            if hasattr(self, 'trigger_log'):
                live = bool(self.enabled) and not self.learn
                self.trigger_log.record(
                    events, feedback.get('sequence', 0), bool(feedback.get('preview')),
                    (state or {}).get('family', 'generic'),
                    new if live else (),
                    effective_mappings(self.config, state) if new else {},
                    feedback.get('macro_events', []), feedback.get('macro_sequence', 0))
            if new:
                self.notify(tr('按下 ') + ' / '.join(self.button_names.get(k,str(k)) for k in sorted(new)))
        except Exception as exc:
            self.enabled=False
            try: self.engine.reset(); self.actions.release_all()
            except Exception: pass
            self.pause_button.setText(tr('恢复映射'));self.pause_button.set_symbol('play'); self.notify(tr('映射已暂停：')+str(exc))

    def dispatch(self,binding,down=True):
        action=binding.get('action','none')
        if action=='gamepad_button':
            self.actions.gamepad_button(binding.get('value', '0'), down)
            return
        elif action=='gamepad_chord':
            self.actions.gamepad_chord(binding.get('value', '0'), down)
            return
        elif action=='gamepad_turbo':
            self.actions.gamepad_turbo(binding.get('value', '0'), down, binding.get('rate_hz', 15))
            return
        if action=='hold': self.actions.hold(binding.get('value',''),down); return
        if not down or action=='none': return
        if action=='capture': self.capture()
        elif action=='replay_record': self.trigger_manual_replay()
        elif action=='record_toggle': self.record_toggle()
        elif action in ('gallery','home'):
            self.navigate(2 if action=='gallery' else 0); self.show_home()
        elif action=='shortcut': self.actions.shortcut(binding['value'])
        elif action=='launch': launch_command(binding['executable'],binding.get('arguments',''))
        elif action=='antigravity': self.trigger_antigravity(binding)
        else: self.actions.media(action)

    def trigger_antigravity(self, binding=None):
        if hasattr(self, 'antigravity_page'):
            self.antigravity_page.trigger_from_gamepad(binding)
        elif sys.platform == 'darwin':
            try:
                from .voice_entry import VoiceEntryService
                service = VoiceEntryService()
                target = (binding or {}).get('target') or self.config.get('antigravity_link', {}).get('target_bundle_id', 'com.google.antigravity')
                service.activate(target)
                self.notify(tr('已唤起 Antigravity 并激活输入框'))
            except Exception as exc:
                self.notify(f"Antigravity: {exc}")

    def record_toggle(self):
        if self.remote:
            self.client.send('record_toggle')
            return
        if sys.platform != 'darwin':
            self.actions.shortcut('Win+Alt+R')
            return
        if self.manual_recording and self.manual_recording.status()['running']:
            self.manual_recording.request_stop()
            self.notify(tr('正在结束并保存录像'))
            return
        self.manual_recording = ManualRecording(
            self.config['save_dir'],
            capture_mode=self.config.get('replay_capture_mode') or self.config.get('capture_mode','game'),
            codec=self.config.get('replay_codec','hevc'), fps=self.config.get('replay_fps',30),
            bitrate_mbps=self.config.get('replay_bitrate_mbps',50),
            on_saved=self.recording_finished.emit)
        if self.manual_recording.start():
            self.notify(tr('正在启动录像；再次按键停止并保存'))
        else:
            self.notify(self.manual_recording.status()['last_error'])

    def on_recording_finished(self, path, error):
        if error:
            self.notify(tr('录像失败：')+error)
        elif path:
            self.notify(tr('录像已保存：')+Path(path).name)
            self.refresh_gallery()

    def toggle_pause(self):
        if self.remote:
            self.client.send('pause' if self.enabled else 'resume');return
        enable=not self.enabled
        self.enabled=False;self.config['mapping_enabled']=False;self.store.save()
        try:self.engine.reset()
        finally:self.actions.release_all()
        self.config['mapping_enabled']=enable;self.store.save();self.enabled=enable
        self.pause_button.setText(tr('暂停映射') if self.enabled else tr('恢复映射'));self.pause_button.set_symbol('pause' if self.enabled else 'play'); self.notify(tr('映射已恢复') if self.enabled else tr('映射已暂停 · 设备监测继续运行'))

    def emergency_pause(self):
        if self.closed or self.remote:
            return
        self.enabled = False
        self.config['mapping_enabled'] = False
        errors = []
        for release in (self.engine.reset, self.actions.release_all):
            try:
                release()
            except Exception as exc:
                errors.append(str(exc))
        try:
            self.store.save()
        except Exception as exc:
            errors.append(str(exc))
        self.pause_button.setText(tr('恢复映射'))
        self.pause_button.set_symbol('play')
        self.notify(tr('已紧急暂停映射，请在工作台手动恢复。')
                    + (' ' + tr('暂停状态保存或输入释放失败：') + '; '.join(errors) if errors else ''))

    def open_emergency_hotkey_editor(self):
        if not emergency_hotkey_supported():
            self.notify(tr('当前平台不支持全局紧急快捷键，请使用窗口中的暂停按钮'))
            return
        from .emergency_hotkey_ui import EmergencyHotkeyDialog
        self.engine.reset()
        self.actions.release_all()
        if self.remote:
            self.client.send('suspend', seconds=2)
        try:
            EmergencyHotkeyDialog(self).exec()
        finally:
            if self.remote:
                self.client.send('suspend', seconds=0)
                self.last_suspend = 0

    def open_screenshot_hotkey_editor(self):
        if not hasattr(self, 'screenshot_hotkey_row'):
            return
        from .screenshot_hotkey_ui import ScreenshotHotkeyDialog
        self.engine.reset()
        self.actions.release_all()
        if self.remote:
            self.client.send('suspend', seconds=2)
        try:
            ScreenshotHotkeyDialog(self).exec()
        finally:
            if self.remote:
                self.client.send('suspend', seconds=0)
                self.last_suspend = 0

    def refresh_screenshot_hotkey_status(self):
        if not hasattr(self, 'screenshot_hotkey_row'):
            return
        settings = normalize_screenshot_hotkey_settings(self.config.get('screenshot_hotkey'))
        if self.remote:
            online = self.client.connected
            status = self.client.status.get('screenshot_hotkey', {}) if online else {}
        else:
            online = True
            status = self.screenshot_hotkey.status() if self.screenshot_hotkey else {}
        registered = bool(online and settings['enabled'] and status.get('registered')
                          and status.get('shortcut') == settings['shortcut'])
        if not settings['enabled']:
            text = tr('已关闭 · 可用手柄映射或窗口按钮截图')
        elif not online:
            text = tr('后台离线，截图快捷键未生效')
        elif registered:
            text = settings['shortcut'] + ' · ' + tr('已生效')
        else:
            text = tr(status.get('error') or '截图快捷键未生效，请检查后台状态')
        self.screenshot_hotkey_row.subtitle_label.setText(text)

    def refresh_emergency_hotkey_status(self):
        if not emergency_hotkey_supported():
            self.emergency_hotkey_row.subtitle_label.setText(tr('当前平台不支持全局紧急快捷键，请使用窗口中的暂停按钮'))
            self.pause_button.setToolTip(tr('暂停手柄映射') if self.enabled else tr('恢复手柄映射'))
            return
        if self.remote:
            status = self.client.status.get('emergency_hotkey', {}) if self.client.connected else {}
            online = self.client.connected
        else:
            status = self.emergency_hotkey.status()
            online = True
        settings = normalize_hotkey_settings(self.config.get('emergency_hotkey'))
        registered = bool(online and status.get('registered') and settings['enabled']
                          and status.get('shortcut') == settings['shortcut'])
        if not settings['enabled']:
            text = tr('默认关闭 · 使用物理键盘暂停映射')
        elif not online:
            text = tr('后台离线，快捷键未生效')
        elif registered:
            text = settings['shortcut'] + ' · ' + tr('已生效')
        else:
            text = tr(status.get('error') or '快捷键未生效，请检查后台状态')
        self.emergency_hotkey_row.subtitle_label.setText(text)
        pause_hint = tr('暂停手柄映射') if self.enabled else tr('恢复手柄映射')
        self.pause_button.setToolTip(pause_hint + (' · ' + tr('紧急暂停') + ': ' + settings['shortcut']
                                                  if registered else ''))

    def refresh_input_permission_status(self, force=False):
        if not hasattr(self, 'input_permission_row'):
            return
        now = time.monotonic()
        if not force and now - getattr(self, '_last_permission_check', -float('inf')) < 1:
            return
        self._last_permission_check = now
        status = self.client.status.get('input_permission', {}) if self.remote and self.client.connected else {}
        if not status:
            status = input_permission_status()
        granted = bool(status.get('granted'))
        self.input_permission_row.subtitle_label.setText(
            tr('辅助功能已授权，键鼠映射可以输出') if granted
            else tr('请在系统设置 → 隐私与安全性 → 辅助功能中授权当前运行程序'))
        self.input_permission_row.subtitle_label.setToolTip(status.get('reason', ''))
        self.input_permission_button.setText(tr('打开系统设置') if granted else tr('授权辅助功能'))
        self.input_permission_button.setToolTip(tr('权限变更后可能需要重新启动后台服务'))
        from .macos_permissions import screen_capture_permission_status
        screen = screen_capture_permission_status()
        screen_granted = bool(screen.get('granted'))
        self.screen_permission_row.subtitle_label.setText(
            tr('屏幕录制已授权，截图与录制可以使用') if screen_granted
            else tr('请在系统设置 → 隐私与安全性 → 屏幕录制中授权当前运行程序'))
        self.screen_permission_row.subtitle_label.setToolTip(screen.get('reason', ''))
        self.screen_permission_button.setText(tr('打开系统设置') if screen_granted else tr('授权屏幕录制'))
        self.screen_permission_button.setToolTip(tr('权限变更后可能需要重新启动后台服务'))

    def authorize_input(self):
        self.request_macos_permission(request_input_permission, ACCESSIBILITY_SETTINGS_URL)

    def authorize_screen_capture(self):
        from .macos_permissions import request_screen_capture_permission
        self.request_macos_permission(request_screen_capture_permission, SCREEN_CAPTURE_SETTINGS_URL)

    def request_macos_permission(self, requester, settings_url):
        error = ''
        try:
            status = requester()
        except (OSError, AttributeError, RuntimeError) as exc:
            status = {'granted': False}
            error = tr('授权请求失败，请在系统设置中手动授权：') + str(exc)
        self.refresh_input_permission_status(force=True)
        QDesktopServices.openUrl(QUrl(settings_url))
        if not status.get('granted'):
            self.notify(error or tr('权限变更后可能需要重新启动后台服务'))

    def current_gamepad_profile(self):
        from .studio_core import profile_mode
        active = self.config.get('active_profile', '')
        gamepad_profiles = self.store.profiles_for(self.snapshot, mode='gamepad')
        if active in gamepad_profiles:
            return active
        combo_text = self.mapping_combo.currentText()
        if combo_text in gamepad_profiles:
            return combo_text
        return gamepad_profiles[0] if gamepad_profiles else ''

    def on_mapping_combo_changed(self, name):
        if not name:
            return
        self._last_gamepad_profile = name
        self.change_profile(name)

    def activate_current_gamepad_profile(self):
        profile = self.current_gamepad_profile()
        if profile:
            self.change_profile(profile)

    def change_profile(self,name):
        if not name or name not in self.config['profiles']: return
        if self.mapping_change({'op': 'select', 'profile': name}):
            if not self.snapshot: self.update_controller_ui(None)

    def use_current_as_manual(self, name):
        if self.application_profile_status().get('automatic'):
            self.change_profile(name)

    def open_application_profiles(self):
        if not application_profiles_supported():
            self.notify(tr('当前平台不支持应用关联'))
            return
        if not self.snapshot:
            self.notify(tr('请先连接手柄'))
            return
        from .application_profiles_ui import ApplicationProfilesDialog
        if self.remote:
            self.client.send('suspend', seconds=2)
        self.engine.reset(); self.actions.release_all()
        dialog = ApplicationProfilesDialog(self)
        dialog.exec()
        dialog.deleteLater()
        if self.remote:
            self.client.send('suspend', seconds=2 if QApplication.activeModalWidget() is not None else 0)

    def export_profile(self, profile):
        if not self.snapshot:
            self.notify(tr('请先连接手柄'))
            return
        from .profile_transfer import export_profile, save_profile_file
        if self.remote:
            self.client.send('suspend', seconds=2)
        self.engine.reset(); self.actions.release_all()
        try:
            package = export_profile(self.config, self.snapshot, profile)
            filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', profile) + '.gpsprofile.json'
            path, _ = QFileDialog.getSaveFileName(self, tr('导出预设'), filename,
                tr('GamePad Studio 预设 (*.gpsprofile.json *.json)'), options=QFileDialog.DontUseNativeDialog)
            if path:
                save_profile_file(path, package)
                self.notify(tr('预设已导出'))
        except (OSError, ValueError) as exc:
            self.notify(tr(str(exc)))
        finally:
            if self.remote:
                self.client.send('suspend', seconds=2 if QApplication.activeModalWidget() is not None else 0)

    def import_profile(self):
        if not self.snapshot:
            self.notify(tr('请先连接手柄'))
            return
        from .profile_transfer import load_profile_file
        from .profile_transfer_ui import ProfileImportDialog
        if self.remote:
            self.client.send('suspend', seconds=2)
        self.engine.reset(); self.actions.release_all()
        try:
            path, _ = QFileDialog.getOpenFileName(self, tr('导入预设'), '',
                tr('GamePad Studio 预设 (*.gpsprofile.json *.json)'), options=QFileDialog.DontUseNativeDialog)
            if not path:
                return
            package = load_profile_file(path)
            dialog = ProfileImportDialog(self, package)
            dialog.exec()
            dialog.deleteLater()
        except (OSError, ValueError) as exc:
            self.notify(tr(str(exc)))
        finally:
            if self.remote:
                self.client.send('suspend', seconds=2 if QApplication.activeModalWidget() is not None else 0)

    def application_profile_status(self):
        if not application_profiles_supported():
            return {'automatic': False, 'executable': '', 'profile': self.config.get('active_profile', '')}
        if self.remote:
            return self.client.status.get('application_profile', {})
        return getattr(self, '_application_profile_status', {})

    def update_application_profile(self, now=None, force=False):
        if not application_profiles_supported():
            return False
        from .application_profiles import foreground_application
        now = time.monotonic() if now is None else now
        if not force and now - self._last_application_check < .25:
            return False
        self._last_application_check = now
        resolved = self.application_resolver.resolve(
            self.config, self.snapshot, foreground_application(), now,
            editing=bool(self.learn or self.virtual_kbm_page.is_capturing or
                         QApplication.activeModalWidget() is not None or self.testing_protected()),
            preview=self.preview_requested())
        target = resolved.get('profile', '')
        changed = target in self.config['profiles'] and target != self.config['active_profile']
        if changed:
            self.engine.reset(); self.actions.release_all(); self.last_touch = None
            self.config['active_profile'] = target
            self.config['mapping_revision'] = self.config.get('mapping_revision', 0) + 1
            self.store.save()
        resolved['profile'] = self.config['active_profile']
        self._application_profile_status = resolved
        if changed:
            self.refresh_mappings()
        return changed

    def refresh_application_profile_status(self):
        status = self.application_profile_status()
        automatic = bool(status.get('automatic') and status.get('profile') == self.config.get('active_profile'))
        import ntpath
        tip = tr('应用自动切换：{app}', app=ntpath.basename(status.get('executable', ''))) if automatic else ''
        if hasattr(self, 'application_profiles_action'):
            self.application_profiles_action.setEnabled(bool(application_profiles_supported() and self.snapshot))
            self.application_profiles_action.setToolTip('' if application_profiles_supported() else tr('当前平台不支持应用关联'))
            self.import_profile_action.setEnabled(bool(self.snapshot))
            self.export_profile_action.setEnabled(bool(self.snapshot))
        if hasattr(self, 'virtual_kbm_page'):
            self.virtual_kbm_page.application_profiles_action.setEnabled(bool(application_profiles_supported() and self.snapshot))
            self.virtual_kbm_page.import_profile_action.setEnabled(bool(self.snapshot))
            self.virtual_kbm_page.export_profile_action.setEnabled(bool(self.snapshot))
            active = self.virtual_kbm_page.current_scheme() == self.config.get('active_profile')
            if active:
                self.virtual_kbm_page.scheme_active_badge.setText(tr('应用自动生效') if automatic else tr('✓ 已加载生效'))
            self.virtual_kbm_page.scheme_active_badge.setToolTip(tip if active else '')
        if hasattr(self, 'mapping_active_badge'):
            active = self.current_gamepad_profile() == self.config.get('active_profile')
            if active:
                self.mapping_active_badge.setText(tr('应用自动生效') if automatic else tr('已生效'))
            self.mapping_active_badge.setToolTip(tip if active else '')
        self.profile_combo.setToolTip(tip)

    def duplicate_profile(self, target_mode=None):
        if not target_mode:
            from .studio_core import profile_mode
            if self.stack.currentIndex() == 1:
                target_mode = 'gamepad'
            elif self.stack.currentIndex() == 6:
                target_mode = 'kbm'
            else:
                target_mode = profile_mode(self.config, self.config.get('active_profile', ''))
        title = tr('新建手柄配置') if target_mode == 'gamepad' else tr('新建键鼠预设')
        name, ok = QInputDialog.getText(self, title, tr('配置名称'))
        if ok and name.strip():
            name = name.strip()
            if name in self.config['profiles']:
                QMessageBox.warning(self, tr('名称重复'), tr('请使用不同的配置名称。'))
                return
            source = (self.virtual_kbm_page.current_scheme() if target_mode == 'kbm'
                      else self.current_gamepad_profile())
            self.mapping_change({'op': 'create', 'profile': name, 'mode': target_mode,
                                 'source': source})

    def reset_profile(self):
        from .studio_core import profile_mode
        if self.stack.currentIndex() == 6:
            name = self.virtual_kbm_page.current_scheme()
        elif self.stack.currentIndex() == 1:
            name = self.current_gamepad_profile()
        else:
            name = self.config['active_profile']
        if not name or name not in self.config['profiles']:
            return
        if profile_mode(self.config, name) == 'gamepad' and not self.snapshot:
            QMessageBox.information(self, tr('恢复默认'), tr('请先连接手柄'))
            return
        msg = f"恢复“{name}”的默认映射？" if get_language() == 'zh' else f"Reset default mappings for '{tr_profile(name)}'?"
        if QMessageBox.question(self,tr('恢复默认'),msg)!=QMessageBox.Yes:return
        self.engine.reset();self.actions.release_all()
        self.mapping_change({'op':'reset', 'profile':name})

    def delete_profile(self, *args):
        name = self.current_gamepad_profile() if self.stack.currentIndex() == 1 else self.config['active_profile']
        family_profiles = self.store.profiles_for(self.snapshot, mode='gamepad' if self.stack.currentIndex() == 1 else None)
        if len(family_profiles) <= 1:
            QMessageBox.information(
                self, tr('无法删除配置'),
                tr('当前设备至少需要保留一个配置预设（当前为“{name}”）。\n\n如需重置按键设定，请点击“恢复默认配置”；如需建立新配置，请点击“另存为新预设”。', name=tr_profile(name))
            )
            return
        reply = QMessageBox.question(
            self, tr('删除配置预设'),
            tr('确定要永久删除配置预设“{name}”吗？\n删除后不可恢复。', name=tr_profile(name)),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        self.mapping_change({'op':'delete', 'profile':name})

    @staticmethod
    def compact_binding(action, family='generic'):
        kind=action.get('action','none')
        if kind in ('gamepad_button','gamepad_chord','gamepad_turbo','gamepad_macro'):
            return binding_label(action, family)
        if kind in ('mouse_hold','mouse_click','wheel'):return binding_label(action)
        if kind in ('shortcut','hold'):return action.get('value','')
        base = {'none':'原始输入','capture':'截图','gallery':'图库','home':'控制中心',
                'replay_record':'回放录制','record_toggle':'录屏','suppress':'屏蔽按键'}.get(kind,ACTION_NAMES.get(kind,'原始输入'))
        return tr(base)

    def select_mapping(self,key):
        self.select_mapping_trigger(str(key))

    def select_mapping_trigger(self, trigger):
        try:
            trigger = canonical_trigger(trigger)
        except ValueError:
            return False
        members = set(trigger.split('+'))
        if not members.issubset(getattr(self, 'mapping_sources', set(input_sources(self.snapshot)))):
            return False
        if self.mapping_deck.selected_trigger != trigger:
            self.mapping_deck.set_trigger(trigger)
        for number, (box, _) in self.mapping_boxes.items():
            box.setChecked(str(number) in members)
        self.mapping_art.select_buttons(members)
        if len(members) == 1:
            member = next(iter(members))
            self.selected_key = int(member) if member.isdigit() else member
            selected_box = self.mapping_boxes.get(self.selected_key)
            if selected_box and not selected_box[0].isHidden():
                self.mapping_inputs.ensureWidgetVisible(selected_box[0], 0, 8)
        return True

    def _on_nav_mapping_clicked(self):
        from .studio_core import profile_mode
        mode = profile_mode(self.config, self.config.get('active_profile', ''))
        if mode == 'kbm':
            self.navigate(6)
        else:
            self.navigate(1)

    def refresh_mappings(self):
        from .studio_core import profile_mode
        active_prof = self.config.get('active_profile', '')
        current_mode = profile_mode(self.config, active_prof)
        self.mapping_profile.setText(tr_profile(active_prof))

        all_items = self.store.profiles_for(self.snapshot)
        gamepad_items = self.store.profiles_for(self.snapshot, mode='gamepad')

        # 1. Page 0 Master profile selector: holds ALL profiles
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for p_name in all_items:
            m = profile_mode(self.config, p_name)
            icon = glyph('controller' if m == 'gamepad' else 'keyboard', TOKENS['accent'] if m == 'gamepad' else TOKENS['cyan'])
            self.profile_combo.addItem(icon, p_name)
        self.profile_combo.setCurrentText(active_prof)
        self.profile_combo.blockSignals(False)

        # 2. Page 1 Mapping selector: holds ONLY gamepad profiles!
        self.mapping_combo.blockSignals(True)
        self.mapping_combo.clear()
        for p_name in gamepad_items:
            icon = glyph('controller', TOKENS['accent'])
            self.mapping_combo.addItem(icon, p_name)

        if current_mode == 'gamepad' and active_prof in gamepad_items:
            selected_gamepad = active_prof
        else:
            last = getattr(self, '_last_gamepad_profile', None)
            selected_gamepad = last if last in gamepad_items else (gamepad_items[0] if gamepad_items else '')
        self._last_gamepad_profile = selected_gamepad
        self.mapping_combo.setCurrentText(selected_gamepad)
        self.mapping_combo.blockSignals(False)

        if hasattr(self, 'mapping_active_badge'):
            is_active = (selected_gamepad == active_prof)
            if is_active:
                self.mapping_active_badge.setText(tr('已生效'))
                self.mapping_active_badge.setStyleSheet(f"background: rgba(16, 185, 129, 0.15); color: {TOKENS['green']}; border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 6px; padding: 3px 8px; font-weight: 700; font-size: 11px;")
                self.mapping_activate_btn.hide()
            else:
                self.mapping_active_badge.setText(tr('未激活'))
                self.mapping_active_badge.setStyleSheet(f"background: {TOKENS['surface']}; color: {TOKENS['ink_dim']}; border: 1px solid {TOKENS['border']}; border-radius: 6px; padding: 3px 8px; font-weight: 600; font-size: 11px;")
                self.mapping_activate_btn.show()

        if hasattr(self, 'mode_badge'):
            if current_mode == 'gamepad':
                self.mode_badge.setText(tr('手柄原生宏模式'))
                self.mode_badge.setStyleSheet(f"background: rgba(255, 87, 34, 0.12); color: {TOKENS['accent']}; border: 1px solid rgba(255, 87, 34, 0.3); border-radius: 6px; padding: 4px 8px; font-weight: 700; font-size: 10.5px;")
                if hasattr(self, 'nav_mapping_btn'):
                    self.nav_mapping_btn.setText(tr('编辑手柄宏 ›'))
            else:
                self.mode_badge.setText(tr('虚拟键鼠模拟模式'))
                self.mode_badge.setStyleSheet(f"background: rgba(119, 208, 212, 0.12); color: {TOKENS['cyan']}; border: 1px solid rgba(119, 208, 212, 0.3); border-radius: 6px; padding: 4px 8px; font-weight: 700; font-size: 10.5px;")
                if hasattr(self, 'nav_mapping_btn'):
                    self.nav_mapping_btn.setText(tr('配置虚拟键鼠 ›'))

        if hasattr(self, 'delete_profile_btn'):
            can_del = len(gamepad_items) > 1
            self.delete_profile_btn.setEnabled(True)
            del_tip = (tr('删除当前配置预设：') + tr_profile(selected_gamepad)) if can_del else (tr('当前模式仅剩此一个预设，点击查看说明') if get_language() == 'zh' else 'Only one profile remaining; click for details')
            self.delete_profile_btn.setToolTip(del_tip)

        if hasattr(self, 'virtual_kbm_page'):
            self.virtual_kbm_page.refresh_display()
        self.refresh_application_profile_status()
        self.mapping_deck.refresh()

        target_gamepad = self.current_gamepad_profile()
        compact = self.compact_binding
        family = (profile_family(self.config, self.snapshot, target_gamepad))
        gamepad_entries = effective_mappings(self.config, self.snapshot, target_gamepad)
        for key, info in self.mapping_labels.items():
            entry = gamepad_entries.get(str(key), {})
            short = compact(entry.get('short', {}), family)
            long = compact(entry.get('long', {}), family)
            original = short == long == tr('原始输入')
            info.setVisible(not original)
            info.setText('' if original else '●')
            info.setToolTip(f"{tr('短按')}：{short}\n{tr('长按')}：{long}")
            self.mapping_boxes[key][0].setToolTip(trigger_label(str(key), family) + '\n' + info.toolTip())
        self.select_mapping_trigger(self.mapping_deck.selected_trigger)
        if hasattr(self, 'row_guide'):
            guide = gamepad_entries.get('5', {})
            self.row_guide.setToolTip('短按：' + compact(guide.get('short', {}), family) + ' / 长按：' + compact(guide.get('long', {}), family))
        key = capture_button(family, self.snapshot.get('available_buttons') if self.snapshot else [k for k, (box, _) in self.mapping_boxes.items() if not box.isHidden()])
        self.capture_heading.setText(self.button_names[key] if key is not None else tr('截图'))
        active_entries = effective_mappings(self.config, self.snapshot)
        entry = active_entries.get(str(key), {}) if key is not None else {}
        tip = f"{self.button_names.get(key, tr('截图'))}：{tr('短按')} {compact(entry.get('short',{}))} / {tr('长按')} {compact(entry.get('long',{}))}" if key is not None else (tr('驱动未提供 Share，可在映射中自定义截图按键') if get_language() == 'zh' else 'Share button unavailable in driver; custom shortcut configurable in mapping')
        if hasattr(self, 'row_capture'):
            self.row_capture.setToolTip(tip)
        if hasattr(self, 'create_hint'):
            self.create_hint.setText(compact(entry.get('short', {})) + '  /  ' + compact(entry.get('long', {})) if key is not None else (tr('未设置') if get_language() == 'zh' else 'Not Configured'))
            self.create_hint.setToolTip(tip)
        if hasattr(self, 'capture_action_btn'):
            if key is not None:
                self.capture_action_btn.setText(tr('查看图库 ›'))
                try:
                    self.capture_action_btn.clicked.disconnect()
                except Exception:
                    pass
                self.capture_action_btn.clicked.connect(lambda: self.navigate(2))
            else:
                self.capture_action_btn.setText(tr('配置按键 ›'))
                try:
                    self.capture_action_btn.clicked.disconnect()
                except Exception:
                    pass
                self.capture_action_btn.clicked.connect(lambda: self.navigate(1))

    def start_learning(self):
        self.edit_mapping('0', new=True, capture=True)

    def end_learning(self):
        self.learn=False;self.learn_button.setChecked(False); self.learn_button.setText(tr('识别手柄按键'))

    def mapping_change(self, change):
        try:
            if self.remote:
                context = {'device_scope': profile_scope(self.snapshot)}
                if change.get('op') in ('import_profile', 'pointer_deadzone', 'swap_bindings'):
                    context['instance_id'] = (self.snapshot or {}).get('instance_id')
                result = request(self.store.root, 'mapping_change', change=change, **context)
                if not result or not result.get('ok', True) or 'config' not in result:
                    raise ValueError((result or {}).get('error', '后台未连接，修改尚未保存'))
                data = result['config']
                self.store.data.clear(); self.store.data.update(data)
                self.store._baseline = copy.deepcopy(data)
                if change.get('op') in ('select', 'create'):
                    self.client.status['application_profile'] = {
                        'automatic': False, 'executable': '', 'profile': data['active_profile']}
                if change.get('op') == 'import_profile':
                    self.last_imported_profile = result.get('imported_profile', '')
            else:
                self.engine.reset()
                self.store.apply_mapping_change(change, self.snapshot)
                if change.get('op') in ('select', 'create'):
                    self.application_resolver.manual_selection(self.config['active_profile'])
                    self._application_profile_status = {'automatic': False, 'executable': '', 'profile': self.config['active_profile']}
                if change.get('op') in ('application_profiles', 'select', 'create'):
                    self.update_application_profile(force=True)
                if change.get('op') == 'import_profile':
                    self.last_imported_profile = self.store.last_imported_profile
            self.refresh_mappings()
            if change.get('op') == 'import_profile':
                self.notify(tr('已导入预设：{name}', name=self.last_imported_profile))
            elif (change.get('op') == 'binding' and str(change.get('trigger', '')).startswith('TP:') and
                    change.get('mapping', {}).get('short', {}).get('action', 'none') not in ('none', 'suppress')):
                self.notify('手势已绑定并启用' if self.config.get('active_profile') == change.get('profile')
                            else '手势已绑定；此预设尚未生效，请设为当前生效。')
            else:
                self.notify('映射已保存')
            return True
        except (ValueError, OSError) as exc:
            self.notify(tr(str(exc))); return False

    def edit_mapping(self, key, new=False, output=None, capture=False, profile=None, mode=None):
        target_profile = profile or (self.virtual_kbm_page.current_scheme() if self.stack.currentIndex() == 6 else self.current_gamepad_profile())
        if target_profile not in self.store.profiles_for(self.snapshot):
            self.notify(tr('输入设备已变化，请重新打开映射编辑。'))
            return
        if self.remote:
            self.client.send('suspend', seconds=2)
        self.engine.reset()
        self.actions.release_all()
        from .studio_core import profile_mode
        target_mode = mode or profile_mode(self.config, target_profile)
        mapping = {} if new else effective_mappings(self.config, self.snapshot, target_profile).get(str(key), {})
        dialog = BindingDialog(self, key, mapping, output=output, new=new, profile=target_profile, mode=target_mode)
        if capture:
            dialog.start_capture()
        if dialog.exec() == QDialog.Accepted:
            self.mapping_change({
                'op': 'binding',
                'profile': dialog.profile,
                'trigger': dialog.trigger(),
                'previous_trigger': None if new else str(key),
                'mapping': dialog.value()
            })
        dialog.deleteLater()
        if self.remote:
            self.client.send('suspend', seconds=2 if QApplication.activeModalWidget() is not None else 0)

    def capture(self):
        if self.remote:
            if not self.client.send('capture'):self.notify(tr('请先启动后台映射'))
            return
        now=time.monotonic()
        if (self.worker and self.worker.isRunning()) or now-self.last_capture<self.config['cooldown']: return
        self.last_capture=now; self.capture_button.setEnabled(False)
        self.worker=CaptureWorker(self.config['save_dir'],self.config['capture_mode']); self.worker.ready.connect(self.captured); self.worker.failed.connect(lambda error:self.notify(tr('截图失败：')+error)); self.worker.finished.connect(lambda:self.capture_button.setEnabled(True)); self.worker.start()

    def captured(self,path):
        self.notify(tr('截图已保存')); self.refresh_gallery()
        if not self.isActiveWindow(): self.tray.showMessage(tr('精彩瞬间已保存'),Path(path).name,QSystemTrayIcon.Information,1800)

    @staticmethod
    def clear_layout(layout):
        while layout.count():
            item=layout.takeAt(0)
            if item.widget():
                widget=item.widget(); widget.hide(); widget.setParent(None); widget.deleteLater()

    def thumbnail(self,row,compact=False):
        thumb_file = row.get('thumb_path', row['path'])
        pix = QPixmap(thumb_file) if Path(thumb_file).suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp') else QPixmap()
        if pix.isNull():
            pix = QPixmap(280, 150)
            pix.fill(QColor(TOKENS.get('surface_lo', TOKENS['base'])))
            painter = QPainter(pix)
            painter.setPen(QColor(TOKENS['accent']))
            font = painter.font()
            font.setPointSize(14)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(pix.rect(), Qt.AlignCenter, tr('🎬 视频录像') if row.get('is_video') else tr('🖼️ 截图'))
            painter.end()

        if compact:
            box=GlassPanel(); layout=QHBoxLayout(box); layout.setContentsMargins(10,10,10,10); layout.setSpacing(12)
            image=QPushButton(); image.setFixedSize(112,64); image.setObjectName('icon')
            image.setIcon(QIcon(pix)); image.setIconSize(QSize(112,64)); image.clicked.connect(lambda:self.preview(row)); layout.addWidget(image)
            text=QVBoxLayout(); text.setSpacing(4); title=row['title']
            t_lbl = label(title, 'section'); t_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred);t_lbl.setToolTip(title);text.addWidget(t_lbl)
            time_lbl = label(tr('拍摄于 ') + f"{row.get('created','')[11:19]}", 'muted'); time_lbl.setObjectName('caption'); text.addWidget(time_lbl)
            layout.addLayout(text,1)
            return box
        box, b = card()
        b.setContentsMargins(14, 14, 14, 14)
        b.setSpacing(10)
        image = QPushButton()
        image.setMinimumHeight(150)
        image.setObjectName('icon')
        image.setIcon(QIcon(pix))
        image.setIconSize(QSize(280, 150))
        image.clicked.connect(lambda: self.preview(row))
        b.addWidget(image)
        caption = QHBoxLayout()
        title = label(row['title'], 'section')
        title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        title.setToolTip(row['title'])
        caption.addWidget(title, 1)
        tip = f"{row.get('created','')[:19].replace('T',' ')}"
        if row.get('is_video'):
            tip += f" · {row.get('size_mb', 0)} MB · 点击播放"
        else:
            tip += f" · {row.get('width','?')} × {row.get('height','?')}"
        image.setToolTip(tip)
        fav = IconButton('heart', tr('取消收藏') if row['favorite'] else tr('收藏'), lambda: self.favorite(row), 32)
        fav.setCheckable(True)
        fav.setChecked(row['favorite'])
        fav.setStyleSheet(f'background: {TOKENS["elevated"]}; border: 1px solid {TOKENS["border_hi"]}; border-radius: {TOKENS["r_sm"]}px;')
        caption.addWidget(fav)
        del_text = tr('删除视频') if row.get('is_video') else tr('删除截图')
        del_btn = IconButton('trash', del_text, lambda: self.delete_capture_confirm(row), 32)
        del_btn.setObjectName('icon_danger')
        del_btn.setStyleSheet(f'background: {TOKENS["elevated"]}; border: 1px solid {TOKENS["border_hi"]}; border-radius: {TOKENS["r_sm"]}px;')
        caption.addWidget(del_btn)
        b.addLayout(caption)
        return box

    def refresh_gallery(self,*args):
        if self.closed:
            return
        try: rows=list_captures(self.config['save_dir'])
        except OSError as exc: self.notify(tr('无法读取截图目录：')+str(exc)); return
        columns=max(2,min(5,self.gallery_view.viewport().width()//320))
        signature=(tuple((r['path'],r['favorite'],r.get('thumb_path'),r.get('title'),
                          r.get('created'),r.get('width'),r.get('height'),r.get('size_mb'))
                         for r in rows),self.search.text(),self.only_favorites.isChecked(),columns)
        if getattr(self,'gallery_signature',None)==signature: return
        self.gallery_signature=signature
        for i in range(self.gallery_grid.rowCount()):self.gallery_grid.setRowStretch(i,0)
        for i in range(self.gallery_grid.columnCount()):self.gallery_grid.setColumnStretch(i,0)
        self.clear_layout(self.gallery_grid); self.clear_layout(self.recent_row)
        filtered=[r for r in rows if (not self.only_favorites.isChecked() or r['favorite']) and self.search.text().lower() in (r['title']+r['path']).lower()]
        self.gallery_info.setText(f"{len(filtered)} / {len(rows)} " + tr('项内容'))
        for i,row in enumerate(filtered[:180]): self.gallery_grid.addWidget(self.thumbnail(row),i//columns,i%columns)
        if not filtered:
            empty = QWidget()
            empty_layout = QVBoxLayout(empty)
            empty_layout.setSpacing(12)
            icon = label('')
            icon.setPixmap(glyph('photos', TOKENS['ink_3']).pixmap(48, 48))
            empty_layout.addWidget(icon, 0, Qt.AlignHCenter)
            empty_layout.addWidget(label(tr('暂无截图') if not rows else tr('没有匹配的内容'), 'section'), 0, Qt.AlignHCenter)
            hint = label(tr('保存第一张游戏截图，精彩瞬间会出现在这里。') if not rows else tr('试试其他关键词，或关闭收藏筛选。'), 'caption', True)
            empty_layout.addWidget(hint, 0, Qt.AlignHCenter)
            if not rows:
                empty_layout.addWidget(button(tr('拍摄截图'), self.capture, primary=True, icon='camera'), 0, Qt.AlignHCenter)
            else:
                empty_layout.addWidget(button(tr('清除筛选'), self.clear_capture_filters, icon='refresh'), 0, Qt.AlignHCenter)
            self.gallery_grid.addWidget(empty, 0, 0, 1, columns, Qt.AlignCenter)
            for i in range(columns):
                self.gallery_grid.setColumnStretch(i, 1)
            self.gallery_grid.setRowStretch(0, 1)
        else:
            for i in range(columns):
                self.gallery_grid.setColumnStretch(i, 1)
            self.gallery_grid.setRowStretch((min(len(filtered), 180) + columns - 1) // columns, 1)
        for row in rows[:2]: self.recent_row.addWidget(self.thumbnail(row,True),1)
        if not rows:
            r_empty = GlassPanel()
            r_layout = QHBoxLayout(r_empty)
            r_layout.setContentsMargins(18, 18, 18, 18)
            r_layout.addWidget(label(tr('保存第一张游戏截图，精彩瞬间会出现在这里。'), 'caption', True), 1)
            r_layout.addWidget(button(tr('拍摄截图'), self.capture, primary=True, icon='camera'))
            self.recent_row.addWidget(r_empty, 1)

    def clear_capture_filters(self):
        self.search.clear()
        self.only_favorites.setChecked(False)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if getattr(self, 'closed', False):
            return
        if hasattr(self,'size_grip'):
            self.reflow_workspace()
        if hasattr(self,'recent_row'):QTimer.singleShot(0,self.refresh_gallery)

    def favorite(self,row):
        try: set_favorite(row['path'],not row['favorite']); self.refresh_gallery()
        except OSError as exc: self.notify(tr('收藏失败：')+str(exc))

    def preview(self, row):
        if row.get('is_video'):
            QDesktopServices.openUrl(QUrl.fromLocalFile(row['path']))
            self.notify(f"🎬 {tr('已调用系统播放器播放视频录像')}: {Path(row['path']).name}")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(tr('截图预览'))
        dialog.resize(960, 680)
        layout = QVBoxLayout(dialog)
        image = QLabel()
        image.setAlignment(Qt.AlignCenter)
        pix = QPixmap(row['path'])
        image.setPixmap(pix.scaled(920, 530, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(image, 1)
        layout.addWidget(label(row['title'], 'section'))
        layout.addWidget(label(Path(row['path']).name, 'muted'))
        bar = QHBoxLayout()
        bar.addWidget(button(tr('复制图像'), lambda: QApplication.clipboard().setPixmap(pix), pill=True))
        bar.addWidget(button(tr('打开原图'), lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(row['path'])), pill=True))
        del_btn = button(tr('删除截图'), lambda: self.delete_capture_confirm(row, on_deleted=dialog.accept), danger=True, icon='trash')
        bar.addWidget(del_btn)
        bar.addStretch()
        bar.addWidget(button(tr('关闭'), dialog.accept, pill=True))
        layout.addLayout(bar)
        dialog.exec()

    def delete_capture_confirm(self, row, on_deleted=None):
        if not isinstance(row, dict) or 'path' not in row:
            return
        name = Path(row['path']).name
        title = row.get('title', name)
        is_vid = bool(row.get('is_video'))
        item_type = tr('视频录像') if is_vid else tr('截图')
        reply = QMessageBox.question(
            self,
            tr('删除{type}', type=item_type),
            tr('确定要永久删除{type} "{title}" 吗？\n文件：{name}', type=item_type, title=title, name=name),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            try:
                if delete_capture(row['path']):
                    self.gallery_signature = None
                    self.refresh_gallery()
                    self.notify(tr('{type}已删除', type=item_type))
                    if on_deleted:
                        on_deleted()
                else:
                    self.notify(tr('截图文件不存在或已被删除'))
            except OSError as exc:
                self.notify(tr('删除失败：') + str(exc))

    def clean_unfavorited_captures(self, *args):
        try:
            rows = list_captures(self.config['save_dir'])
        except OSError as exc:
            self.notify(tr('无法读取截图目录：') + str(exc))
            return
        unfavorited = [r for r in rows if not r.get('favorite')]
        if not unfavorited:
            self.notify(tr('没有可清理的未收藏截图'))
            return
        reply = QMessageBox.question(
            self,
            tr('清理未收藏截图'),
            tr('确定要清理所有未收藏的截图吗？\n将永久删除 {count} 张截图，已收藏的 {retained} 张截图将被保留。', count=len(unfavorited), retained=len(rows) - len(unfavorited)),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            count = 0
            for r in unfavorited:
                try:
                    if delete_capture(r['path']):
                        count += 1
                except OSError:
                    pass
            self.gallery_signature = None
            self.refresh_gallery()
            self.notify(tr('已清理 {count} 张未收藏截图', count=count))

    def open_capture_folder(self):
        folder=Path(self.config['save_dir']); folder.mkdir(parents=True,exist_ok=True); QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def choose_folder(self):
        path=QFileDialog.getExistingDirectory(self,tr('截图保存位置'),self.config['save_dir'])
        if path: self.setting('save_dir',path); self.folder_label.setText(path);self.folder_button.setText(tr('截图位置：')+path); self.gallery_signature=None; self.refresh_gallery(); self.notify(tr('截图目录已更新'))

    def rumble(self):
        if self.remote:self.client.send('rumble',strength=device_config(self.config, self.snapshot)['rumble'], device_scope=profile_scope(self.snapshot));return
        self.notify(tr('已发送 350 ms 振动测试') if self.device.rumble(device_config(self.config, self.snapshot)['rumble']) else tr('当前设备暂不支持振动或尚未连接'))

    def test_rumble(self,strength):
        if self.remote:self.client.send('rumble',strength=strength, device_scope=profile_scope(self.snapshot))
        else:self.device.rumble(strength)

    def set_led(self, color):
        self.art.set_led(color); self.mapping_art.set_led(color)
        for b in getattr(self, 'led_buttons', []):
            if hasattr(b, 'set_selected'):
                b.set_selected(str(getattr(b, 'color', '')).lower() == str(color).lower())
        self.setting('led', color)
        if self.remote:
            self.client.send('led', color=color, device_scope=profile_scope(self.snapshot)); return
        if self.device.led(color):
            self.notify(tr('灯条颜色已更新'))
        else:
            self.notify(tr('灯条颜色已保存（设备连接后生效）'))

    def closeEvent(self,event):
        close_to_tray = bool(self.config.get('close_to_tray', False)) and not getattr(self, 'quitting', False)
        if close_to_tray and self.tray.isVisible():
            self.hide()
            self.notify(tr('GamePad Studio 已最小化至系统托盘'))
            event.ignore()
            return
        if hasattr(self, 'voice_entry_group') and not self.voice_entry_group.close_service():
            self.navigate(4)
            self.notify(self.voice_entry_group.status_label.text())
            event.ignore()
            return
        if hasattr(self, 'antigravity_page') and not self.antigravity_page.close_service():
            self.navigate(7)
            event.ignore()
            return
        if self.cleanup():
            event.accept()
        else:
            event.ignore()

    def cleanup(self):
        if self.closed:return True
        if hasattr(self,'voice_entry_group') and not self.voice_entry_group.close_service():
            return False
        if hasattr(self,'antigravity_page') and not self.antigravity_page.close_service():
            return False
        self.closed=True
        self.enabled=False
        errors=[]
        def finish(label, operation):
            try:
                operation()
            except Exception as exc:
                errors.append(label+'：'+str(exc))
        finish(tr('停止登录启动交接'), self._cancel_autostart_handoff)
        for timer in (self.timer,self.scan_timer,self.gallery_timer,self.notice_timer):
            finish(tr('停止界面计时器'), timer.stop)
        if self.emergency_hotkey:
            finish(tr('注销紧急暂停'), self.emergency_hotkey.close)
        if getattr(self, 'screenshot_hotkey', None):
            finish(tr('注销截图快捷键'), self.screenshot_hotkey.close)
        if self.remote:
            if self.client and self.client.connected:
                try: self.client.send('preview', seconds=0)
                except Exception: pass
                try: self.client.send('stop')
                except Exception: pass
            try:
                request(self.store.root, 'stop', timeout=600)
            except Exception:
                pass
        finish(tr('释放映射'), self.engine.reset)
        finish(tr('释放键鼠输入'), self.actions.release_all)
        finish(tr('关闭映射引擎'), self.engine.close)
        if self.manual_recording:
            finish(tr('结束录像'), self.manual_recording.stop)
        if hasattr(self, 'haptic_engine'):
            finish(tr('停止触觉反馈'), self.haptic_engine.close)
        finish(tr('恢复手柄共享访问'), getattr(self, '_restore_device_access', getattr(self.device, 'close', lambda: None)))
        if self.worker and self.worker.isRunning():
            finish(tr('结束截图'), self.worker.wait)
        self.cleanup_errors=errors
        if errors:
            self.closed=False
            self.notify(tr('退出尚未完成，请重试：')+'；'.join(errors))
            return False
        self.events.close(); self.tray.hide()
        return True

    def _restore_device_access(self):
        if sys.platform == 'win32' and getattr(self, '_cloaked_devices', None):
            try:
                from .hidhide import HidHideClient
                client = HidHideClient()
                if client.is_driver_installed():
                    if not self.remote or not (self.client and self.client.connected):
                        for vendor, product, dev_path in list(getattr(self, '_cloaked_devices', set())):
                            opts = {'device_path': dev_path} if dev_path else {}
                            client.uncloak_controller(vendor, product, **opts)
                        self._cloaked_devices.clear()
                        if hasattr(self, 'snapshot') and self.snapshot and self.snapshot.get('vendor') and self.snapshot.get('product'):
                            opts = {'device_path': self.snapshot.get('device_path')} if self.snapshot.get('device_path') else {}
                            client.uncloak_controller(self.snapshot['vendor'], self.snapshot['product'], **opts)
                        if not client.get_blacklist():
                            client.set_active(False)
            except Exception:
                pass
        self.device.close()

    def quit_app(self):
        self.quitting=True
        self.close()
        if self.closed:
            QApplication.quit()


class _SmokeDevice:
    """Startup/render checks must not open or exercise connected controllers."""
    available=[]
    def scan(self): pass
    def read(self): return None
    def close(self): pass
    def rumble(self,*_): return False
    def led(self,*_): return False


def run():
    pages={'home':0,'mappings':1,'gallery':2,'input':3,'settings':4,'controllers':5,'keyboard':6}
    parser=argparse.ArgumentParser(description=tr('GamePad Studio 手柄管理软件'))
    parser.add_argument('--data-dir',type=Path,default=None)
    parser.add_argument('--cli',action='store_true',help='使用原命令行截图模式')
    parser.add_argument('--smoke-test',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--page',choices=list(pages),default='home')
    parser.add_argument('--lang',choices=['auto','zh','en'],default=None,help=tr('UI 界面语言选择 (auto/zh/en)'))
    args,extra=parser.parse_known_args()
    if args.cli or extra:
        from .cli import run as cli
        sys.argv=[sys.argv[0],*extra]; cli(); return
    root=(args.data_dir or default_root()).resolve()
    root.mkdir(parents=True,exist_ok=True)
    if sys.platform=='win32':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('GamePadStudio.ControllerManager')
    app=QApplication(sys.argv[:1]); app.setApplicationName('GamePad Studio'); app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    lock=QLockFile(str(root/'studio.lock'))
    lock.setStaleLockTime(1500)
    if not lock.tryLock(100):
        # 尝试唤起已存在的前台界面
        response = request(root,'show',role='ui',page=args.page,timeout=400)
        if response and response.get('ok'):
            return
        # An unresponsive instance may still own inputs or be finalizing a
        # recording. Only the IPC backend may remove a verified stale owner.
        if not cleanup_stale_ui(root) or not lock.tryLock(400):
            QMessageBox.warning(None, tr('GamePad Studio'),
                                tr('已有工作台界面运行但暂时无法通信，请先退出原界面后重试。'))
            return 1
    window=Studio(root,standalone=args.smoke_test,lang=args.lang,
                  input_device=_SmokeDevice() if args.smoke_test else None)
    def handle_ui(message):
        if message.get('command')=='exit':QTimer.singleShot(50,window.quit_app)
        elif message.get('command')=='show':window.navigate(pages.get(message.get('page'),0));window.show_home()
        elif message.get('command')=='status':pass
        else:raise ValueError('Unknown UI command')
        return {'pid':os.getpid(),'pages':window.stack.count(),'page':window.stack.currentIndex(),'native_glass':window.native_glass}
    ui_server=LocalServer(root,handle_ui,role='ui')
    window.navigate(pages[args.page])
    app.aboutToQuit.connect(window.cleanup)
    if args.smoke_test:
        window.enabled=False; window.tray.hide()
        def smoke_finish():
            photos=photo_health()
            result={'ok':all(photos.values()),'photos':photos,'device':window.snapshot,'pages':window.stack.count(),'frozen':bool(getattr(sys,'frozen',False)),'native_glass':window.native_glass,'icon':not app_icon().isNull()}
            result['input_tester']=window.tester.snapshot()
            result['mapping_ui']={'labels':window.button_names,'axes':window.tester.axis_names,
                'profile':window.config['active_profile'],'bindings':window.store.mappings,
                'visible_buttons':[key for key,(box,_) in window.mapping_boxes.items() if not box.isHidden()],
                'profile_options':[window.mapping_combo.itemText(i) for i in range(window.mapping_combo.count())]}
            (root/'smoke-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            window.grab().save(str(root/'smoke-window.png'))
            window.quit_app()
        QTimer.singleShot(1200,smoke_finish)
    window.showFullScreen(); code=app.exec(); ui_server.close(); lock.unlock(); return code
