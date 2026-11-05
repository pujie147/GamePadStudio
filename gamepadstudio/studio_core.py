"""Persistent profiles and deterministic button gestures (independent of the UI)."""
from __future__ import annotations

import copy
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from .controller_catalog import CATALOG, controller_defaults, desktop_defaults
from .response_curves import CURVE_CHANNELS, normalize_curve_channels
from .touch_gestures import normalize_touch_sensitivity, touch_sources

KEYBOARD_PROFILE_CLEANUP_VERSION = 1
DEVICE_PROFILE_VERSION = 1
OFFLINE_PROFILE_SCOPE = 'offline:xinput'
DEVICE_SETTING_KEYS = ('led', 'rumble', 'touch_mouse', 'long_press', 'deadzone',
                       'capture_haptics_enabled', 'haptic_intensity', 'haptic_profile',
                       'haptic_engine_enabled', 'device_cloaking_enabled', 'mac_controller_isolation_requested',
                       'trigger_curves', 'rumble_curves', 'trigger_rumble_curves',
                       'trigger_rumble_enabled', 'touch_gestures_enabled', 'touch_scroll',
                       'touch_gesture_sensitivity', 'battery_notifications_enabled')
DEVICE_SETTING_DEFAULTS = {'led': '#5686ff', 'rumble': .35, 'touch_mouse': False,
                           'long_press': .65, 'deadzone': .10,
                           'capture_haptics_enabled': True, 'haptic_intensity': 1.0,
                           'haptic_profile': 'crisp', 'haptic_engine_enabled': True,
                           'device_cloaking_enabled': True, 'mac_controller_isolation_requested':False,
                           'battery_notifications_enabled': True}
DEVICE_SETTING_DEFAULTS.update({key: normalize_curve_channels(key) for key in CURVE_CHANNELS})
DEVICE_SETTING_DEFAULTS['trigger_rumble_enabled'] = False
DEVICE_SETTING_DEFAULTS.update(touch_gestures_enabled=False, touch_scroll=False,
                               touch_gesture_sensitivity=.5)


def profile_scope(state):
    """Use physical identity when available; the disconnected preview is public."""
    if not state:
        return OFFLINE_PROFILE_SCOPE
    return str(state.get('device_key') or state.get('profile_key') or state.get('family') or 'generic')


def pointer_input_signature(state):
    """Describe reported inputs only after validating actual RS axes and data."""
    from .stick_calibration import supports_right_stick
    from .mapping_engine import input_sources
    from .profile_transfer import _device_source
    if not supports_right_stick(state):
        raise ValueError('当前设备未提供有效的右摇杆双轴，请重新连接或测量')
    source = _device_source(state)
    return dict(source, model=source.get('model', ''), inputs=sorted(input_sources(state)))


def mapping_input_signature(state):
    """Describe a connected controller's reported inputs without requiring sticks."""
    from .mapping_engine import input_sources
    from .profile_transfer import _device_source
    if (not isinstance(state, dict) or not state or state.get('connected') is False
            or type(state.get('instance_id')) is not int or state['instance_id'] < 0):
        raise ValueError('请先连接手柄，再交换绑定')
    source = _device_source(state)
    return dict(source, model=source.get('model', ''), inputs=sorted(input_sources(state)))


def swap_binding_entry(config, state, profile, trigger):
    """Snapshot one full effective binding, including its device timing default."""
    from .mapping_engine import canonical_trigger, effective_mappings, input_sources, validate_mappings
    mapping_input_signature(state)
    key = canonical_trigger(trigger)
    if not set(key.split('+')) <= set(input_sources(state)):
        raise ValueError('请选择当前手柄支持的输入按键')
    profiles, owners = config.get('profiles'), config.get('profile_devices')
    if not isinstance(profiles, dict) or profile not in profiles:
        raise ValueError('预设已删除，请重新选择')
    if not isinstance(owners, dict) or owners.get(profile) != profile_scope(state):
        raise ValueError('该预设不属于当前输入设备')
    if not isinstance(profiles[profile], dict):
        raise ValueError('按键配置格式错误')
    resolved = device_config(config, state)
    entry = effective_mappings(resolved, state, profile).get(key, {})
    if not isinstance(entry, dict) or set(entry) - {'short', 'long', 'long_press'}:
        raise ValueError('按键配置包含不支持的内容，请先编辑该绑定')
    threshold = entry.get('long_press', resolved.get('long_press', .65))
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError('长按时长应在 0.15 至 3 秒之间')
    try:
        valid = math.isfinite(threshold) and .15 <= threshold <= 3.
    except (ValueError, OverflowError):
        valid = False
    if not valid:
        raise ValueError('长按时长应在 0.15 至 3 秒之间')
    try:
        result = validate_mappings({key: entry})[key]
    except (TypeError, OverflowError) as exc:
        raise ValueError('按键配置格式错误') from exc
    for gesture in ('short', 'long'):
        result[gesture].setdefault('action', 'none')
    if (key in ('TP:scroll_up', 'TP:scroll_down') and resolved.get('touch_scroll')
            and result['short']['action'] == 'none'):
        result['short'] = dict(result['short'], action='wheel',
                               value='up' if key == 'TP:scroll_up' else 'down')
    result['long_press'] = float(threshold)
    return result


def device_config(config, state):
    """Resolve hardware preferences without borrowing another controller's values."""
    resolved = dict(config)
    resolved.update(DEVICE_SETTING_DEFAULTS)
    settings = config.get('device_settings', {})
    settings = settings.get(profile_scope(state), {}) if isinstance(settings, dict) else {}
    if isinstance(settings, dict):
        resolved.update({key: copy.deepcopy(value) for key, value in settings.items()
                         if key in DEVICE_SETTING_KEYS})
    for key in CURVE_CHANNELS:
        resolved[key] = normalize_curve_channels(key, resolved)
    resolved['trigger_rumble_enabled'] = resolved.get('trigger_rumble_enabled') is True
    for key in ('touch_gestures_enabled', 'touch_scroll', 'mac_controller_isolation_requested'):
        resolved[key] = resolved.get(key) is True
    if type(resolved.get('battery_notifications_enabled')) is not bool:
        resolved['battery_notifications_enabled'] = True
    resolved['touch_gesture_sensitivity'] = normalize_touch_sensitivity(resolved.get('touch_gesture_sensitivity'))
    return resolved


def is_nikki_profile(config, name):
    from .kbm_mapper import NIKKI_PROFILE_NAME
    sources = config.get('profile_sources', {})
    return name == NIKKI_PROFILE_NAME or (isinstance(sources, dict)
                                         and sources.get(name) == NIKKI_PROFILE_NAME)

NIKKI_PROFILE_OPTIONS = {'right_stick_mouse': True,
                         'input': {'stick_press': .24, 'stick_release': .17,
                                   'trigger_press': .30, 'trigger_release': .20,
                                   'walk_press': .62, 'walk_release': .72,
                                   'chord_window': .055},
                         'mouse': {'mode': 'game', 'sensitivity': 24.0, 'deadzone': .09,
                                   'y_ratio': .70, 'edge_boost': 1.45, 'invert_y': False}}

BUTTONS = {0: '×  交叉', 1: '○  圆圈', 2: '□  方块', 3: '△  三角',
           4: 'Create', 5: 'PS', 6: 'Options', 7: 'L3', 8: 'R3',
           9: 'L1', 10: 'R1', 11: '方向键 ↑', 12: '方向键 ↓',
           13: '方向键 ←', 14: '方向键 →', 15: '麦克风', 16:'背键 P1',17:'背键 P3',18:'背键 P2',19:'背键 P4',20: '触摸板'}
ACTION_NAMES = {'none': '保留原始输入', 'capture': '保存截图', 'gallery': '打开截图资料库',
                'home': '打开控制中心', 'shortcut': '键盘快捷键', 'hold': '按住键盘按键',
                'launch': '启动应用 / 命令', 'volume_mute': '系统声音静音',
                'volume_up': '音量 +', 'volume_down': '音量 −', 'media': '播放 / 暂停',
                'replay_record': '保存精彩瞬间 (回放录制)',
                'record_toggle': '开始/停止录屏',
                'gamepad_button': '映射为手柄按键',
                'gamepad_axis': '映射为手柄摇杆方向',
                'mouse_move': '模拟鼠标移动',
                'gamepad_chord': '手柄多键组合宏',
                'gamepad_turbo': '手柄高频连发 (Turbo)',
                'gamepad_macro': '手柄连招序列宏'}
ACTION_NAMES.update({'mouse_hold': '按住鼠标按键', 'mouse_click': '点击鼠标按键', 'wheel': '鼠标滚轮', 'antigravity': '唤起 Antigravity 对话框'})
ACTION_NAMES['suppress'] = '不触发（组合键前缀）'


def get_action_names(lang=None):
    from .i18n import get_language
    is_en = (lang == 'en') or (lang is None and get_language() == 'en')
    if is_en:
        return {'none': 'Pass-through (Native Input)', 'capture': 'Take Screenshot', 'gallery': 'Open Captures Gallery',
                'home': 'Open Control Center', 'shortcut': 'Keyboard Shortcut', 'hold': 'Hold Keyboard Key',
                'launch': 'Launch App / Command', 'volume_mute': 'Mute System Audio',
                'volume_up': 'Volume +', 'volume_down': 'Volume −', 'media': 'Play / Pause Media',
                'replay_record': 'Instant Replay (Save Clip)',
                'record_toggle': 'Toggle Screen Recording',
                'gamepad_button': 'Remap to Gamepad Button',
                'gamepad_axis': 'Remap to Gamepad Stick Direction',
                'mouse_move': 'Emulated Mouse Move',
                'gamepad_chord': 'Gamepad Multi-button Macro',
                'gamepad_turbo': 'Gamepad Turbo (Rapid Fire)',
                'gamepad_macro': 'Gamepad Sequence Macro',
                'mouse_hold': 'Hold Mouse Button', 'mouse_click': 'Click Mouse Button', 'wheel': 'Mouse Wheel',
                'antigravity': 'Activate Antigravity Composer',
                'suppress': 'Suppress (Chord Prefix)'}
    return ACTION_NAMES


def profile_mode(config, name):
    if not name:
        return 'gamepad'
    modes = config.get('profile_modes')
    modes = modes if isinstance(modes, dict) else {}
    if modes.get(name) in ('gamepad', 'kbm'):
        return modes[name]
    from .kbm_mapper import NIKKI_PROFILE_NAME
    options = config.get('profile_options')
    options = options if isinstance(options, dict) else {}
    profile_options = options.get(name)
    profile_options = profile_options if isinstance(profile_options, dict) else {}
    if name in (NIKKI_PROFILE_NAME, '桌面导航') or '键鼠' in name or profile_options.get('right_stick_mouse'):
        return 'kbm'
    profile = config.get('profiles', {}).get(name, {})
    for entry in profile.values():
        if isinstance(entry, dict):
            for g in ('short', 'long'):
                act = entry.get(g, {}).get('action')
                if act in ('hold', 'shortcut', 'mouse_hold', 'mouse_click', 'wheel'):
                    return 'kbm'
    return 'gamepad'


def get_buttons(lang=None):
    from .i18n import get_language
    is_en = (lang == 'en') or (lang is None and get_language() == 'en')
    if is_en:
        return {0: '×  Cross', 1: '○  Circle', 2: '□  Square', 3: '△  Triangle',
                4: 'Create', 5: 'PS', 6: 'Options', 7: 'L3', 8: 'R3',
                9: 'L1', 10: 'R1', 11: 'D-Pad ↑', 12: 'D-Pad ↓',
                13: 'D-Pad ←', 14: 'D-Pad →', 15: 'Mic / Mute',
                16: 'Paddle P1', 17: 'Paddle P3', 18: 'Paddle P2', 19: 'Paddle P4', 20: 'Touchpad'}
    return BUTTONS


def default_config(root: Path):
    from .emergency_hotkey import DEFAULT_SHORTCUT
    from .screenshot_hotkey import DEFAULT_SHORTCUT as SCREENSHOT_SHORTCUT
    from .kbm_mapper import NIKKI_PROFILE_NAME, NIKKI_LAYOUT_VERSION, infinity_nikki_defaults
    base = {'4': {'short': {'action': 'capture'}, 'long': {'action': 'replay_record'}},
            '5': {'short': {'action': 'home'}, 'long': {'action': 'none'}}}
    action_macro = copy.deepcopy(base)
    action_macro['0'] = {'short': {'action': 'none'}, 'long': {'action': 'gamepad_turbo', 'value': '0', 'rate_hz': 15}}
    action_macro['2'] = {'short': {'action': 'none'}, 'long': {'action': 'gamepad_chord', 'value': '2+3'}}
    action_macro['9+10'] = {'short': {'action': 'gamepad_chord', 'value': '9+10+3'}, 'long': {'action': 'none'}}

    nikki = infinity_nikki_defaults('dualsense', touch_inputs=[])
    options = {NIKKI_PROFILE_NAME: copy.deepcopy(NIKKI_PROFILE_OPTIONS)}
    return {'version': 1, 'mapping_version': 2, 'mapping_revision': 0,
            'keyboard_profile_cleanup_version': KEYBOARD_PROFILE_CLEANUP_VERSION,
            'nikki_layout_version': NIKKI_LAYOUT_VERSION,
            'nikki_profile_layouts': {NIKKI_PROFILE_NAME: {'version': NIKKI_LAYOUT_VERSION,
                                                         'touch_inputs': []}},
            'nikki_touch_settings_initialized': [],
            'device_profile_version': DEVICE_PROFILE_VERSION,
            'active_profile': '主机体验',
            'profiles': {'主机体验': base, '动作与格斗宏': action_macro, NIKKI_PROFILE_NAME: nikki},
            'profile_modes': {'主机体验': 'gamepad', '动作与格斗宏': 'gamepad', NIKKI_PROFILE_NAME: 'kbm'},
            'profile_options': options,
            'profile_devices': {NIKKI_PROFILE_NAME: OFFLINE_PROFILE_SCOPE},
            'profile_sources': {NIKKI_PROFILE_NAME: NIKKI_PROFILE_NAME},
            'device_settings': {},
            'application_profiles': {},
            'emergency_hotkey': {'enabled': False, 'shortcut': DEFAULT_SHORTCUT},
            'screenshot_hotkey': {'enabled': sys.platform == 'darwin', 'shortcut': SCREENSHOT_SHORTCUT},
            'save_dir': str(root / 'Captures'), 'capture_mode': 'game', 'cooldown': .5,
            'long_press': .65, 'deadzone': .10, 'rumble': .35, 'led': '#5686ff',
            'touch_mouse': False, 'close_to_tray': False, 'mapping_enabled': True, 'controller_profiles':{}, 'controller_favorites':[], 'preferred_controller':'',
            'profile_families':{'主机体验':'dualsense','动作与格斗宏':'dualsense', NIKKI_PROFILE_NAME: 'all'},
            'replay_buffer_enabled': False, 'replay_buffer_minutes': 5, 'replay_codec': 'hevc',
            'replay_bitrate_mbps': 50, 'replay_fps': 30, 'replay_capture_mode': 'game',
            'capture_sound_enabled': True, 'capture_haptics_enabled': True,
            'haptic_engine_enabled': True, 'haptic_intensity': 1.0, 'haptic_profile': 'crisp',
            'device_cloaking_enabled': True,
            'antigravity_link': {'enabled': False, 'target_bundle_id': 'com.google.antigravity',
                                 'trigger_key': '4', 'trigger_gesture': 'short', 'action_type': 'focus'},
            **{key: copy.deepcopy(DEVICE_SETTING_DEFAULTS[key])
               for key in (*CURVE_CHANNELS, 'trigger_rumble_enabled')},
            'gamebar_shield_enabled': False, 'gamebar_shield_backup': {}}


class ConfigStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / 'studio.json'
        self.data = default_config(root)
        self.warning = ''
        needs_recording_mode = False
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(saved, dict) or not isinstance(saved.get('profiles'), dict):
                    raise ValueError('配置格式错误')
                if not saved['profiles'] or saved.get('active_profile') not in saved['profiles']:
                    raise ValueError('配置预设不存在')
                for profile in saved['profiles'].values():
                    if not isinstance(profile, dict):
                        raise ValueError('按键配置格式错误')
                    from .mapping_engine import validate_mappings
                    validate_mappings(profile)
                self.data.update(saved)
                if not isinstance(self.data.get('profile_modes'), dict):
                    self.data['profile_modes'] = {}
                options = self.data.get('profile_options')
                self.data['profile_options'] = {name: value if isinstance(value, dict) else {}
                                                for name, value in options.items()} if isinstance(options, dict) else {}
                # Defaults describe fresh installs; absent saved markers still
                # identify old configurations that need a one-time migration.
                self.data['mapping_version'] = saved.get('mapping_version', 0)
                self.data['keyboard_profile_cleanup_version'] = saved.get('keyboard_profile_cleanup_version', 0)
                self.data['device_profile_version'] = saved.get('device_profile_version', 0)
                for field in ('profile_devices', 'profile_sources', 'device_settings', 'nikki_profile_layouts'):
                    value = saved.get(field, {})
                    self.data[field] = value if isinstance(value, dict) else {}
                self.data['profile_devices'] = {name: owner for name, owner in self.data['profile_devices'].items()
                                                 if name in self.data['profiles'] and isinstance(owner, str)}
                self.data['profile_sources'] = {name: source for name, source in self.data['profile_sources'].items()
                                                 if name in self.data['profiles'] and isinstance(source, str)}
                self.data['device_settings'] = {scope: options for scope, options in self.data['device_settings'].items()
                                               if isinstance(scope, str) and isinstance(options, dict)}
                application_profiles = self.data.get('application_profiles', {})
                self.data['application_profiles'] = {
                    scope: options for scope, options in application_profiles.items()
                    if isinstance(scope, str) and isinstance(options, dict)
                } if isinstance(application_profiles, dict) else {}
                self.data['nikki_profile_layouts'] = {
                    name: value for name, value in self.data['nikki_profile_layouts'].items()
                    if name in self.data['profiles'] and isinstance(value, dict)}
                initialized = saved.get('nikki_touch_settings_initialized', [])
                self.data['nikki_touch_settings_initialized'] = [scope for scope in initialized
                    if isinstance(scope, str)] if isinstance(initialized, list) else []
                try:
                    self.data['nikki_layout_version'] = int(saved.get('nikki_layout_version', 0))
                except (TypeError, ValueError, OverflowError):
                    self.data['nikki_layout_version'] = 0
                if not isinstance(self.data['controller_profiles'],dict):self.data['controller_profiles']={}
                self.data['controller_profiles']={key:value for key,value in self.data['controller_profiles'].items() if isinstance(value,str) and value in self.data['profiles']}
                if not isinstance(self.data['profile_families'],dict):self.data['profile_families']={}
                for device_key,profile in self.data['controller_profiles'].items():
                    family=device_key.split(':')[0]
                    if family in CATALOG:self.data['profile_families'].setdefault(profile,family)
                self.data.setdefault('legacy_dualsense_profile',self.data['active_profile'])
                favorites=self.data['controller_favorites']
                self.data['controller_favorites']=[x for x in favorites if isinstance(x,str) and x in CATALOG] if isinstance(favorites,list) else []
                for key, lo, hi in [('deadzone', 0, .5), ('rumble', 0, 1), ('long_press', .2, 3), ('cooldown', .1, 10)]:
                    self.data[key] = max(lo, min(hi, float(self.data[key])))
                if self.data.get('capture_mode') == 'monitor':
                    self.data['capture_mode'] = 'game'
                legacy_replay_mode = self.data.get('capture_mode', 'game')
                # A screenshot's window scope is not a replay-buffer scope.
                if legacy_replay_mode == 'window':
                    legacy_replay_mode = 'game'
                self.data['replay_capture_mode'] = saved.get('replay_capture_mode') or legacy_replay_mode
                needs_recording_mode = not saved.get('replay_capture_mode')
                if self.data['replay_capture_mode'] == 'monitor':
                    self.data['replay_capture_mode'] = 'game'
                self.data.setdefault('replay_buffer_enabled', False)
                self.data.setdefault('replay_buffer_minutes', 5)
                self.data.setdefault('replay_codec', 'hevc')
                self.data.setdefault('replay_bitrate_mbps', 50)
                self.data.setdefault('replay_fps', 30)
                self.data['replay_buffer_minutes'] = max(1, min(10, int(self.data.get('replay_buffer_minutes', 5))))
                if self.data.get('replay_codec') not in ('hevc', 'av1', 'h264'):
                    self.data['replay_codec'] = 'hevc'
                self.data.setdefault('capture_sound_enabled', True)
                self.data.setdefault('capture_haptics_enabled', True)
                self.data.setdefault('haptic_engine_enabled', True)
                self.data.setdefault('haptic_intensity', 1.0)
                self.data.setdefault('haptic_profile', 'crisp')
                from .kbm_mapper import NIKKI_PROFILE_NAME, LEGACY_NIKKI_PROFILE_NAME, infinity_nikki_defaults
                if NIKKI_PROFILE_NAME not in self.data['profiles']:
                    self.data['profiles'][NIKKI_PROFILE_NAME] = copy.deepcopy(
                        self.data['profiles'].get(LEGACY_NIKKI_PROFILE_NAME, infinity_nikki_defaults('dualsense', touch_inputs=[])))
                    old_options = self.data.get('profile_options', {}).get(LEGACY_NIKKI_PROFILE_NAME)
                    if old_options is not None:
                        self.data['profile_options'][NIKKI_PROFILE_NAME] = copy.deepcopy(old_options)
                self.data['profile_families'][NIKKI_PROFILE_NAME] = 'all'
            except (ValueError, TypeError, AttributeError, OSError) as exc:
                backup = self.path.with_name(f'studio.invalid-{time.time_ns()}.json')
                backup.write_bytes(self.path.read_bytes())
                self.warning = f'配置无法读取，已备份并恢复默认值：{exc}'
                self.data = default_config(root)

        from .emergency_hotkey import normalize_hotkey_settings
        self.data['emergency_hotkey'] = normalize_hotkey_settings(self.data.get('emergency_hotkey'))
        from .screenshot_hotkey import normalize_screenshot_hotkey_settings
        self.data['screenshot_hotkey'] = normalize_screenshot_hotkey_settings(self.data.get('screenshot_hotkey'))
        needs_cleanup = self.data.get('keyboard_profile_cleanup_version', 0) < KEYBOARD_PROFILE_CLEANUP_VERSION
        if needs_cleanup and self.path.exists():
            backup = self.path.with_name('studio.before-keyboard-profile-cleanup-v1.json')
            if not backup.exists():
                backup.write_bytes(self.path.read_bytes())
        needs_mapping_migration = self.data.get('mapping_version', 0) < 2
        if needs_mapping_migration:
            self._migrate_mappings()
        if needs_cleanup:
            self._cleanup_keyboard_profiles()
        from .kbm_mapper import NIKKI_LAYOUT_VERSION
        needs_nikki_layout = self.data.get('nikki_layout_version', 0) < NIKKI_LAYOUT_VERSION
        if needs_nikki_layout:
            self._update_nikki_layout()
        needs_device_profiles = self.data.get('device_profile_version', 0) < DEVICE_PROFILE_VERSION
        if needs_device_profiles:
            self._migrate_device_profiles()
        if needs_mapping_migration or needs_cleanup or needs_nikki_layout or needs_device_profiles:
            self.data['mapping_revision'] = self.data.get('mapping_revision', 0) + 1
            self.save(merge=False)
        elif needs_recording_mode:
            self.save(merge=False)
        self._baseline = copy.deepcopy(self.data)

    def _migrate_mappings(self):
        if self.path.exists():
            backup = self.path.with_name('studio.before-unified-mapping-v2.json')
            if not backup.exists():
                backup.write_bytes(self.path.read_bytes())
        # Existing unified bindings are authoritative. Legacy standalone
        # schemes are retired by the keyboard-profile cleanup below.
        self.data.pop('virtual_kbm_schemes', None)
        self.data.pop('active_virtual_kbm_scheme', None)
        self.data['mapping_version'] = 2
        self.data.setdefault('mapping_revision', 0)

    def _cleanup_keyboard_profiles(self):
        from .kbm_mapper import NIKKI_PROFILE_NAME, LEGACY_NIKKI_PROFILE_NAME
        removed = {name for name in self.data['profiles']
                   if name != NIKKI_PROFILE_NAME
                   and (name == LEGACY_NIKKI_PROFILE_NAME or profile_mode(self.data, name) == 'kbm')}
        remove_profiles(self.data, removed)
        self.data.setdefault('profile_modes', {})[NIKKI_PROFILE_NAME] = 'kbm'
        self.data['profile_families'][NIKKI_PROFILE_NAME] = 'all'
        self.data.pop('virtual_kbm_schemes', None)
        self.data.pop('active_virtual_kbm_scheme', None)
        self.data['keyboard_profile_cleanup_version'] = KEYBOARD_PROFILE_CLEANUP_VERSION
        return removed

    def _update_nikki_layout(self):
        """Upgrade the offline template; real device capabilities complete clones."""
        from .kbm_mapper import NIKKI_PROFILE_NAME, NIKKI_LAYOUT_VERSION, infinity_nikki_defaults
        if self.path.exists():
            backup = self.path.with_name(f'studio.before-nikki-layout-v{NIKKI_LAYOUT_VERSION}.json')
            if not backup.exists():
                backup.write_bytes(self.path.read_bytes())
        old_version = self.data.get('nikki_layout_version', 0)
        markers = self.data.setdefault('nikki_profile_layouts', {})
        for name in self.data['profiles']:
            if is_nikki_profile(self.data, name):
                markers.setdefault(name, {'version': 2, 'touch_inputs': []})
        fresh = infinity_nikki_defaults('dualsense', touch_inputs=[])
        if old_version < 2:
            # The historical, pre-2.0 preset used different game controls.
            self.data['profiles'][NIKKI_PROFILE_NAME] = fresh
            self.data['profile_options'][NIKKI_PROFILE_NAME] = copy.deepcopy(NIKKI_PROFILE_OPTIONS)
        else:
            previous = infinity_nikki_defaults('dualsense', touch_inputs=[], layout_version=2)
            self._merge_nikki_defaults(NIKKI_PROFILE_NAME, previous, fresh)
        markers[NIKKI_PROFILE_NAME] = {'version': NIKKI_LAYOUT_VERSION, 'touch_inputs': []}
        self.data.setdefault('profile_modes', {})[NIKKI_PROFILE_NAME] = 'kbm'
        self.data['profile_families'][NIKKI_PROFILE_NAME] = 'all'
        self.data['nikki_layout_version'] = NIKKI_LAYOUT_VERSION

    def _merge_nikki_defaults(self, name, previous, current, new_touch_inputs=()):
        """Replace only fields still equal to their earlier shipped defaults."""
        mappings = self.data['profiles'][name]
        self._preserve_nikki_menu_fallbacks(mappings, previous, current, new_touch_inputs)
        installed = False
        for trigger in set(previous) | set(current):
            old, new = previous.get(trigger, {}), current.get(trigger, {})
            if trigger not in mappings:
                if trigger in new_touch_inputs and trigger in current:
                    mappings[trigger] = copy.deepcopy(new)
                    installed = True
                continue
            existing = mappings[trigger]
            # Previously configured gestures, including explicit "none", are
            # authoritative; new default gestures only fill absent sources.
            if trigger.startswith('TP:') and trigger in new_touch_inputs:
                continue
            for field in ('short', 'long', 'long_press'):
                if field in old and existing.get(field) == old[field]:
                    if field in new:
                        existing[field] = copy.deepcopy(new[field])
                    else:
                        existing.pop(field, None)
        return installed

    @staticmethod
    def _preserve_nikki_menu_fallbacks(mappings, previous, current, new_touch_inputs):
        # A custom or disabled gesture must not silently consume the only
        # physical shortcut to that game menu during an upgrade.
        for gesture, chord in (('TP:swipe_left', '0+4'), ('TP:swipe_right', '2+4'),
                               ('TP:swipe_up', '3+4')):
            old = previous.get(chord, {}).get('short', {})
            new = current.get(chord, {}).get('short', {})
            if old.get('action') != 'shortcut' or new.get('action') != 'none':
                continue
            final = mappings.get(gesture)
            if final is None and gesture in new_touch_inputs:
                final = current.get(gesture)
            expected = {'action': 'shortcut', 'value': old.get('value')}
            if not final or final.get('short', {}) != expected:
                current[chord]['short'] = copy.deepcopy(old)

    def _upgrade_nikki_profile(self, name, state):
        from .kbm_mapper import NIKKI_LAYOUT_VERSION, infinity_nikki_defaults
        if not state or not is_nikki_profile(self.data, name):
            return
        markers = self.data.setdefault('nikki_profile_layouts', {})
        marker = markers.get(name, {})
        version = marker.get('version', min(2, self.data.get('nikki_layout_version', 2)))
        version = version if type(version) is int else 2
        installed_sources = marker.get('touch_inputs', [])
        installed_sources = set(installed_sources) if isinstance(installed_sources, list) else set()
        available_sources = set(touch_sources(state))
        new_sources = available_sources - installed_sources
        if version >= NIKKI_LAYOUT_VERSION and not new_sources:
            return
        family = state.get('family', 'generic')
        buttons = state.get('available_buttons')
        previous = infinity_nikki_defaults(family, buttons, touch_inputs=installed_sources,
                                           layout_version=2 if version < 3 else NIKKI_LAYOUT_VERSION)
        current = infinity_nikki_defaults(family, buttons,
                                          touch_inputs=installed_sources | available_sources)
        installed = self._merge_nikki_defaults(name, previous, current, new_sources)
        markers[name] = {'version': NIKKI_LAYOUT_VERSION,
                         'touch_inputs': sorted(installed_sources | available_sources),
                         'touch_defaults': bool(marker.get('touch_defaults') or installed)}

    def _initialize_nikki_touch_settings(self, state, force=False):
        from .kbm_mapper import NIKKI_TOUCH_SETTINGS
        if not touch_sources(state):
            return
        scope = profile_scope(state)
        initialized = self.data.setdefault('nikki_touch_settings_initialized', [])
        if scope in initialized and not force:
            return
        settings = self.data.setdefault('device_settings', {}).setdefault(scope, {})
        for key, value in NIKKI_TOUCH_SETTINGS.items():
            if force or settings.get(key, DEVICE_SETTING_DEFAULTS[key]) == DEVICE_SETTING_DEFAULTS[key]:
                settings[key] = value
        if scope not in initialized:
            initialized.append(scope)

    def _migrate_device_profiles(self):
        """Keep old names and edits, then assign each profile one physical owner."""
        from .kbm_mapper import NIKKI_PROFILE_NAME
        if self.path.exists():
            backup = self.path.with_name('studio.before-device-profiles-v1.json')
            if not backup.exists():
                backup.write_bytes(self.path.read_bytes())
        owners = self.data.setdefault('profile_devices', {})
        self.data.setdefault('profile_sources', {})[NIKKI_PROFILE_NAME] = NIKKI_PROFILE_NAME
        owners[NIKKI_PROFILE_NAME] = OFFLINE_PROFILE_SCOPE
        for scope, name in self.data.get('controller_profiles', {}).items():
            if name != NIKKI_PROFILE_NAME:
                owners.setdefault(name, scope)
        active = self.data['active_profile']
        legacy = {key: copy.deepcopy(self.data.get(key, DEVICE_SETTING_DEFAULTS[key]))
                  for key in DEVICE_SETTING_KEYS}
        owner = owners.get(active)
        if owner and owner != OFFLINE_PROFILE_SCOPE:
            self.data.setdefault('device_settings', {}).setdefault(owner, copy.deepcopy(legacy))
        self.data['legacy_device_settings'] = {'profile': active,
                                                'family': self.data['profile_families'].get(active, 'dualsense'),
                                                'values': legacy}
        self.data['device_profile_version'] = DEVICE_PROFILE_VERSION

    @property
    def mappings(self):
        return self.data['profiles'][self.data['active_profile']]

    def _unique_profile_name(self, base):
        name, number = base, 2
        while name in self.data['profiles']:
            name, number = f'{base} {number}', number + 1
        return name

    def _clone_profile(self, source, scope, family, available=None, state=None):
        from .kbm_mapper import NIKKI_PROFILE_NAME, NIKKI_LAYOUT_VERSION, infinity_nikki_defaults
        base = f'{source} · {CATALOG.get(family, CATALOG["generic"])["name"]}'
        name = self._unique_profile_name(base)
        mappings = copy.deepcopy(self.data['profiles'][source])
        if is_nikki_profile(self.data, source):
            # Re-target untouched hardware-only defaults, preserving every
            # edited game binding and the template itself.
            source_marker = self.data.get('nikki_profile_layouts', {}).get(source, {})
            previous_defaults = infinity_nikki_defaults(
                'dualsense' if source == NIKKI_PROFILE_NAME else self.data.get('profile_families', {}).get(source, 'dualsense'),
                touch_inputs=[] if source == NIKKI_PROFILE_NAME else source_marker.get('touch_inputs', []),
                layout_version=source_marker.get('version', 2))
            sources = touch_sources(state)
            hardware_defaults = infinity_nikki_defaults(family, available, touch_inputs=sources)
            self._preserve_nikki_menu_fallbacks(mappings, previous_defaults, hardware_defaults, sources)
            for trigger in set(previous_defaults) | set(hardware_defaults):
                if trigger not in mappings or mappings.get(trigger) == previous_defaults.get(trigger):
                    if trigger in hardware_defaults:
                        mappings[trigger] = copy.deepcopy(hardware_defaults[trigger])
                    else:
                        mappings.pop(trigger, None)
            self.data['profile_sources'][name] = NIKKI_PROFILE_NAME
            self.data.setdefault('nikki_profile_layouts', {})[name] = {
                'version': NIKKI_LAYOUT_VERSION, 'touch_inputs': list(sources),
                'touch_defaults': any(trigger.startswith('TP:') and mappings.get(trigger) == entry
                                      for trigger, entry in hardware_defaults.items())}
        elif source in self.data.get('profile_sources', {}):
            self.data['profile_sources'][name] = self.data['profile_sources'][source]
        self.data['profiles'][name] = mappings
        self.data['profile_options'][name] = copy.deepcopy(self.data['profile_options'].get(source, {}))
        self.data.setdefault('profile_modes', {})[name] = profile_mode(self.data, source)
        self.data['profile_families'][name] = family
        self.data['profile_devices'][name] = scope
        return name

    def _own_legacy_profile(self, name, scope, family, legacy_scope=None, available=None, state=None):
        owner = self.data['profile_devices'].get(name)
        if owner == scope:
            return name
        if owner and owner != legacy_scope:
            return self._clone_profile(name, scope, family, available, state)
        # A pre-identity model key can be adopted once. Once ownership has
        # moved to a physical key, another controller receives its own copy.
        if owner and legacy_scope and owner == legacy_scope:
            for profile, existing in list(self.data['profile_devices'].items()):
                if existing == legacy_scope:
                    self.data['profile_devices'][profile] = scope
            old_settings = self.data['device_settings'].get(legacy_scope)
            if isinstance(old_settings, dict):
                self.data['device_settings'].setdefault(scope, copy.deepcopy(old_settings))
        self.data['profile_devices'][name] = scope
        self.data['profile_families'][name] = family
        return name

    def _ensure_profile_scope(self, state):
        from .kbm_mapper import NIKKI_PROFILE_NAME, infinity_nikki_defaults
        scope = profile_scope(state)
        family = (state or {}).get('family', 'generic')
        available = (state or {}).get('available_buttons', list(range(15)) if not state else None)
        owners = self.data.setdefault('profile_devices', {})
        initialized = self.data.setdefault('device_profile_initialized', [])
        if not isinstance(initialized, list):
            initialized = self.data['device_profile_initialized'] = []
        first_activation = scope not in initialized
        if state and first_activation and NIKKI_PROFILE_NAME not in self.data['profiles']:
            self.data['profiles'][NIKKI_PROFILE_NAME] = infinity_nikki_defaults('xbox', range(15), touch_inputs=[])
            self.data['profile_options'][NIKKI_PROFILE_NAME] = copy.deepcopy(NIKKI_PROFILE_OPTIONS)
            self.data.setdefault('profile_modes', {})[NIKKI_PROFILE_NAME] = 'kbm'
        if NIKKI_PROFILE_NAME in self.data['profiles']:
            self.data.setdefault('profile_sources', {})[NIKKI_PROFILE_NAME] = NIKKI_PROFILE_NAME
            owners[NIKKI_PROFILE_NAME] = OFFLINE_PROFILE_SCOPE
        self.data.setdefault('device_settings', {})
        remembered = self.data['controller_profiles'].get(scope)
        legacy_scope = None
        if state:
            model = str(state.get('model_key') or state.get('profile_key') or scope)
            if remembered not in self.data['profiles']:
                legacy_scope = model if model != scope else None
                remembered = self.data['controller_profiles'].get(model)
                if legacy_scope and self.data['profile_devices'].get(remembered) not in (None, legacy_scope):
                    # This model alias was already claimed by another physical
                    # controller. It is not a selection for the new device.
                    remembered = None
            prefix = ':'.join(model.split(':')[:3])
            migrated = self.data.setdefault('xbox_rawinput_profiles', [])
            if family == 'xbox' and model.endswith('7200') and prefix not in migrated:
                active = self.data['active_profile']
                previous = [key for key, profile in self.data['controller_profiles'].items()
                            if key.startswith(prefix + ':') and key.endswith('7801') and profile == active]
                if previous:
                    remembered, legacy_scope = active, previous[0]
                    if self.data.get('preferred_controller') in previous:
                        self.data['preferred_controller'] = scope
                migrated.append(prefix)
        if remembered in self.data['profiles'] and owners.get(remembered) != scope:
            if remembered == NIKKI_PROFILE_NAME:
                candidates = [name for name, owner in owners.items() if owner == scope
                              and is_nikki_profile(self.data, name)]
                remembered = candidates[0] if candidates else self._clone_profile(
                    remembered, scope, family, available, state)
            else:
                remembered = self._own_legacy_profile(remembered, scope, family, legacy_scope, available, state)
        # Unclaimed presets from the old family model belong to the first
        # matching device; later devices never see or edit those objects.
        for name in list(self.data['profiles']):
            profile_family = self.data['profile_families'].get(name)
            if name not in owners and (profile_family == family or (not state and profile_family == 'generic')):
                owners[name] = scope
        if remembered not in self.data['profiles'] and owners.get(self.data['active_profile']) == scope:
            remembered = self.data['active_profile']
        if not remembered or remembered not in self.data['profiles']:
            legacy = self.data.get('legacy_dualsense_profile', '主机体验') if family == 'dualsense' and state else None
            if legacy in self.data['profiles'] and owners.get(legacy) in (None, scope):
                remembered = self._own_legacy_profile(legacy, scope, family, available=available, state=state)
            else:
                own_native = [name for name in self.data['profiles'] if owners.get(name) == scope
                              and profile_mode(self.data, name) == 'gamepad']
                if own_native:
                    remembered = own_native[0]
                else:
                    base = 'XInput · 默认' if not state else CATALOG.get(family, CATALOG['generic'])['name'] + ' · 默认'
                    remembered = self._unique_profile_name(base)
                    self.data['profiles'][remembered] = controller_defaults('xbox' if not state else family, available)
                    self.data['profile_options'][remembered] = {}
                    self.data.setdefault('profile_modes', {})[remembered] = 'gamepad'
                    self.data['profile_families'][remembered] = family
                    owners[remembered] = scope
        if state and first_activation and not any(owner == scope and is_nikki_profile(self.data, name)
                             for name, owner in owners.items()):
            self._clone_profile(NIKKI_PROFILE_NAME, scope, family, available, state)
        if state and family == 'xbox':
            old_defaults = [{capture: {'short': {'action': 'capture'}, 'long': {'action': action}},
                             '5': {'short': {'action': 'home'}, 'long': {'action': 'none'}}}
                            for capture in ('4', '15') for action in ('gallery', 'replay_record')]
            if re.fullmatch(r'Xbox · 默认(?: \d+)?', remembered) and self.data['profiles'][remembered] in old_defaults:
                self.data['profiles'][remembered] = controller_defaults('xbox', available)
        self.data['controller_profiles'][scope] = remembered
        legacy_settings = self.data.get('legacy_device_settings', {})
        if scope in self.data['device_settings'] and owners.get(legacy_settings.get('profile')) == scope:
            # The original active controller has consumed its one-time values,
            # including when a model identity was upgraded to a physical key.
            self.data.pop('legacy_device_settings', None)
        if scope not in self.data['device_settings']:
            legacy = self.data.get('legacy_device_settings', {})
            if state and (legacy.get('profile') == remembered or
                          (legacy.get('profile') == NIKKI_PROFILE_NAME and is_nikki_profile(self.data, remembered)) or
                          (legacy.get('profile') == self.data.get('legacy_dualsense_profile')
                           and legacy.get('family') == family)):
                self.data['device_settings'][scope] = copy.deepcopy(legacy.get('values', DEVICE_SETTING_DEFAULTS))
                self.data.pop('legacy_device_settings', None)
            else:
                self.data['device_settings'][scope] = copy.deepcopy(DEVICE_SETTING_DEFAULTS)
        if state:
            for name, owner in list(owners.items()):
                if owner == scope and is_nikki_profile(self.data, name):
                    self._upgrade_nikki_profile(name, state)
            if any(owner == scope and self.data.get('nikki_profile_layouts', {}).get(name, {}).get('touch_defaults')
                   for name, owner in owners.items()):
                self._initialize_nikki_touch_settings(state)
        if first_activation:
            initialized.append(scope)
        return remembered

    def activate_controller(self, state):
        saved = self._ensure_profile_scope(state)
        changed = self.data['active_profile'] != saved
        self.data['active_profile'] = saved
        return changed

    def remember_profile(self, state, name):
        if name not in self.data['profiles']:
            raise ValueError('预设已删除，请重新选择')
        scope = profile_scope(state)
        owner = self.data['profile_devices'].get(name)
        if owner is None:
            family = (state or {}).get('family', 'generic')
            if self.data['profile_families'].get(name, family) not in (family, 'all'):
                raise ValueError('该预设不属于当前输入设备')
            self.data['profile_devices'][name] = scope
            self.data['profile_families'][name] = family
        elif owner != scope:
            raise ValueError('该预设不属于当前输入设备')
        self.data['active_profile'] = name
        self.data['controller_profiles'][scope] = name

    def profiles_for(self, state, mode=None):
        self._ensure_profile_scope(state)
        scope = profile_scope(state)
        profiles = [name for name in self.data['profiles'] if self.data['profile_devices'].get(name) == scope]
        if mode:
            profiles = [name for name in profiles if profile_mode(self.data, name) == mode]
        return profiles

    def settings_for(self, state):
        resolved = device_config(self.data, state)
        return {key: copy.deepcopy(resolved[key]) for key in DEVICE_SETTING_KEYS}

    def application_settings(self, state):
        """Return this device's usable application rules, tolerating stale files."""
        from .application_profiles import normalize_application_profiles
        if not state:
            return {'enabled': False, 'rules': []}
        scope = profile_scope(state)
        all_settings = self.data.get('application_profiles', {})
        settings = all_settings.get(scope, {}) if isinstance(all_settings, dict) else {}
        if not isinstance(settings, dict):
            return {'enabled': False, 'rules': []}
        rules = settings.get('rules', [])
        rules = rules if isinstance(rules, list) else []
        usable = []
        for rule in rules:
            try:
                normalized = normalize_application_profiles(
                    {'enabled': False, 'rules': [rule]}, self.data, state)
                usable.extend(normalized['rules'])
            except (TypeError, ValueError):
                continue
        # A stale file can contain duplicate paths. The first valid rule wins.
        normalized = []
        seen = set()
        from .application_profiles import canonical_executable
        for rule in usable:
            path = canonical_executable(rule['executable'])
            if path not in seen:
                seen.add(path); normalized.append(rule)
        return {'enabled': settings.get('enabled') is True, 'rules': normalized}

    def set_setting(self, key, value, state=None):
        if key == 'emergency_hotkey':
            from .emergency_hotkey import normalize_hotkey_settings
            value = normalize_hotkey_settings(value, strict=True)
        elif key == 'screenshot_hotkey':
            from .screenshot_hotkey import normalize_screenshot_hotkey_settings
            value = normalize_screenshot_hotkey_settings(value, strict=True)
        if key in ('emergency_hotkey', 'screenshot_hotkey') and sys.platform == 'darwin' and value['enabled']:
            other = 'screenshot_hotkey' if key == 'emergency_hotkey' else 'emergency_hotkey'
            if self.data.get(other, {}).get('enabled') and self.data[other].get('shortcut') == value['shortcut']:
                raise ValueError('该快捷键已用于' + ('截图' if other == 'screenshot_hotkey' else '紧急暂停') + '，请更换快捷键')
        if key == 'battery_notifications_enabled' and type(value) is not bool:
            raise ValueError('低电量提醒设置应为开启或关闭')
        if key in CURVE_CHANNELS:
            value = normalize_curve_channels(key, {key: value})
        elif key in ('trigger_rumble_enabled', 'touch_gestures_enabled', 'touch_scroll', 'mac_controller_isolation_requested'):
            value = value is True
        elif key == 'touch_gesture_sensitivity':
            value = normalize_touch_sensitivity(value)
        if key in DEVICE_SETTING_KEYS:
            scope = profile_scope(state)
            self.data.setdefault('device_settings', {}).setdefault(scope, {}).update({key: copy.deepcopy(value)})
        else:
            self.data[key] = copy.deepcopy(value)

    def delete_profile(self, name, state=None):
        if name not in self.data['profiles']:
            return None
        family_profiles = self.profiles_for(state)
        if name not in family_profiles:
            raise ValueError('该预设不属于当前输入设备')
        if len(family_profiles) <= 1 and name in family_profiles:
            return None
        self.data['profiles'].pop(name, None)
        self.data['profile_families'].pop(name, None)
        self.data.get('profile_modes', {}).pop(name, None)
        self.data.get('profile_options', {}).pop(name, None)
        self.data.get('profile_devices', {}).pop(name, None)
        self.data.get('profile_sources', {}).pop(name, None)
        for settings in self.data.get('application_profiles', {}).values():
            if isinstance(settings, dict) and isinstance(settings.get('rules'), list):
                settings['rules'] = [rule for rule in settings['rules']
                                     if not isinstance(rule, dict) or rule.get('profile') != name]
        remaining = self.profiles_for(state)
        fallback = remaining[0] if remaining else (next(iter(self.data['profiles'])) if self.data['profiles'] else '主机体验')
        for k, p in list(self.data['controller_profiles'].items()):
            if p == name:
                if k == profile_scope(state):
                    self.data['controller_profiles'][k] = fallback
                else:
                    # Another pre-migration reference receives its own default
                    # on activation; it must not inherit this device's fallback.
                    self.data['controller_profiles'].pop(k, None)
        if self.data['active_profile'] == name:
            self.data['active_profile'] = fallback
        self.save()
        return fallback

    def save(self, merge=True):
        self.save_data(merge)

    def save_data(self, merge=True):
        # Three-way merge prevents a stale UI settings panel from overwriting
        # a profile selected or edited by the independent mapping process.
        if merge and self.path.exists() and hasattr(self, '_baseline'):
            try:
                latest = json.loads(self.path.read_text(encoding='utf-8'))
                if latest.get('device_profile_version', 0) > self._baseline.get('device_profile_version', 0):
                    # An old panel cannot overwrite an ambiguously shared
                    # preset after it has acquired a physical owner.
                    for name, owner in latest.get('profile_devices', {}).items():
                        if owner != OFFLINE_PROFILE_SCOPE:
                            for field in ('profiles', 'profile_options', 'profile_modes', 'profile_families'):
                                if name in latest.get(field, {}):
                                    value = latest[field][name]
                                    self.data.setdefault(field, {})[name] = copy.deepcopy(value)
                                    self._baseline.setdefault(field, {})[name] = copy.deepcopy(value)
                    for field in ('profile_devices', 'profile_sources', 'device_settings',
                                  'device_profile_initialized', 'device_profile_version'):
                        if field in latest:
                            self.data[field] = copy.deepcopy(latest[field])
                            self._baseline[field] = copy.deepcopy(latest[field])
                self._preserve_newer_nikki_layouts(latest)
                if latest.get('keyboard_profile_cleanup_version', 0) > self._baseline.get('keyboard_profile_cleanup_version', 0):
                    # A settings window opened before cleanup may still have
                    # edited a removed preset. Do not resurrect it on save.
                    from .kbm_mapper import NIKKI_PROFILE_NAME, LEGACY_NIKKI_PROFILE_NAME
                    removed = {name for name in self._baseline.get('profiles', {})
                               if name != NIKKI_PROFILE_NAME and name not in latest.get('profiles', {})
                               and (name == LEGACY_NIKKI_PROFILE_NAME or profile_mode(self._baseline, name) == 'kbm')}
                    old_active = self.data.get('active_profile')
                    old_legacy = self.data.get('legacy_dualsense_profile')
                    old_devices = {key for key, name in self.data.get('controller_profiles', {}).items() if name in removed}
                    remove_profiles(self.data, removed)
                    if old_active in removed:
                        self.data['active_profile'] = latest['active_profile']
                    if old_legacy in removed:
                        self.data['legacy_dualsense_profile'] = latest.get('legacy_dualsense_profile', NIKKI_PROFILE_NAME)
                    for key in old_devices:
                        self.data['controller_profiles'][key] = latest.get('controller_profiles', {}).get(key, NIKKI_PROFILE_NAME)
                    self.data.pop('virtual_kbm_schemes', None)
                    self.data.pop('active_virtual_kbm_scheme', None)
                    self.data['keyboard_profile_cleanup_version'] = latest['keyboard_profile_cleanup_version']
                merged = merge_changes(self._baseline, self.data, latest)
                merged['mapping_revision'] = max(latest.get('mapping_revision', 0), self.data.get('mapping_revision', 0))
                self.data.clear(); self.data.update(merged)
            except (OSError, ValueError):
                pass
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, self.path)
        self._baseline = copy.deepcopy(self.data)

    def _preserve_newer_nikki_layouts(self, latest):
        """A stale panel cannot undo a template or device capability upgrade."""
        from .kbm_mapper import NIKKI_TOUCH_SETTINGS
        global_upgrade = latest.get('nikki_layout_version', 0) > self._baseline.get('nikki_layout_version', 0)
        protected = set()
        for name in latest.get('profiles', {}):
            if not is_nikki_profile(latest, name):
                continue
            current = latest.get('nikki_profile_layouts', {}).get(name, {})
            previous = self._baseline.get('nikki_profile_layouts', {}).get(name, {})
            if (global_upgrade or current.get('version', 0) > previous.get('version', 0)
                    or set(current.get('touch_inputs', [])) - set(previous.get('touch_inputs', []))):
                protected.add(name)
        for name in protected:
            for field in ('profiles', 'profile_options', 'profile_modes', 'profile_families',
                          'profile_sources', 'profile_devices', 'nikki_profile_layouts'):
                if name in latest.get(field, {}):
                    value = latest[field][name]
                    self.data.setdefault(field, {})[name] = copy.deepcopy(value)
                    self._baseline.setdefault(field, {})[name] = copy.deepcopy(value)
            scope = latest.get('profile_devices', {}).get(name)
            if scope in latest.get('device_settings', {}):
                for key in NIKKI_TOUCH_SETTINGS:
                    if key in latest['device_settings'][scope]:
                        value = latest['device_settings'][scope][key]
                        self.data.setdefault('device_settings', {}).setdefault(scope, {})[key] = copy.deepcopy(value)
                        self._baseline.setdefault('device_settings', {}).setdefault(scope, {})[key] = copy.deepcopy(value)
        if protected:
            initialized = latest.get('nikki_touch_settings_initialized', [])
            self.data['nikki_touch_settings_initialized'] = sorted(set(
                self.data.get('nikki_touch_settings_initialized', [])) | set(initialized))
            self._baseline['nikki_touch_settings_initialized'] = copy.deepcopy(initialized)
        if global_upgrade:
            self.data['nikki_layout_version'] = latest['nikki_layout_version']
            self._baseline['nikki_layout_version'] = latest['nikki_layout_version']

    def _import_profile(self, change, state):
        """Create one verified, inactive profile without borrowing device data."""
        if not state or state.get('instance_id') is None:
            raise ValueError('请先连接手柄，再导入预设')
        from .profile_transfer import preview_profile_import

        preview = preview_profile_import(change.get('package'), state)
        expected = change.get('expected_inputs')
        if not isinstance(expected, dict) or expected != preview['input_signature']:
            raise ValueError('输入设备能力已变化，请重新预览导入内容')
        profile = preview['profile']
        requested = change.get('name', profile['name'])
        if (not isinstance(requested, str) or not requested.strip() or len(requested.strip()) > 80
                or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                       for char in requested)):
            raise ValueError('请输入 1 至 80 个字符的预设名称')
        requested = requested.strip()
        occupied = set(self.data['profiles'])
        revision = self.data.get('mapping_revision', 0)
        if self.path.exists():
            # A settings panel can hold an older snapshot while the backend has
            # already added a preset. The three-way save must not overwrite it.
            latest = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(latest, dict) or not isinstance(latest.get('profiles'), dict):
                raise ValueError('当前配置无法读取，请重新打开软件')
            occupied.update(latest['profiles'])
            revision = max(revision, latest.get('mapping_revision', 0))
        name, index = requested, 2
        while name in occupied:
            suffix = f' · 导入 {index}'
            name = requested[:80 - len(suffix)].rstrip() + suffix
            index += 1
        before = copy.deepcopy(self.data)
        baseline = copy.deepcopy(getattr(self, '_baseline', None))
        staged = copy.deepcopy(self.data)
        staged['profiles'][name] = {row['trigger']: copy.deepcopy(row['mapping'])
                                   for row in preview['accepted']}
        staged.setdefault('profile_modes', {})[name] = profile['mode']
        staged.setdefault('profile_options', {})[name] = copy.deepcopy(profile['options'])
        staged.setdefault('profile_devices', {})[name] = profile_scope(state)
        staged.setdefault('profile_families', {})[name] = state.get('family', 'generic')
        staged['mapping_revision'] = revision + 1
        self.data.clear(); self.data.update(staged)
        try:
            self.save()
        except Exception:
            self.data.clear(); self.data.update(before)
            if baseline is not None:
                self._baseline = baseline
            raise
        # This is a reply detail, not persisted source/migration metadata.
        self.last_imported_profile = name
        return copy.deepcopy(self.data)

    def _apply_pointer_deadzone(self, change, state):
        """Apply one measured preference to the latest preset as one transaction."""
        signature = pointer_input_signature(state)
        expected = change.get('expected_input_signature')
        if not isinstance(expected, dict) or expected != signature:
            raise ValueError('输入设备能力已变化，请重新测量右摇杆')
        value = change.get('deadzone')
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('居中容错建议应为 0.01 至 0.50 之间的有限数值')
        try:
            valid = math.isfinite(value) and .01 <= value <= .50
        except (ValueError, OverflowError):
            valid = False
        if not valid:
            raise ValueError('居中容错建议应为 0.01 至 0.50 之间的有限数值')
        before = copy.deepcopy(self.data)
        baseline = copy.deepcopy(getattr(self, '_baseline', None))
        staged = copy.deepcopy(self.data)
        if self.path.exists():
            staged = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(staged, dict) or not isinstance(staged.get('profiles'), dict):
                raise ValueError('当前配置无法读取，请重新打开软件')
        name = change.get('profile')
        if not isinstance(name, str) or name not in staged['profiles']:
            raise ValueError('预设已删除，请重新选择')
        owners = staged.get('profile_devices')
        if not isinstance(owners, dict) or owners.get(name) != profile_scope(state):
            raise ValueError('该预设不属于当前输入设备')
        if profile_mode(staged, name) != 'kbm':
            raise ValueError('请选择当前手柄的键鼠预设')
        options = staged.setdefault('profile_options', {})
        if not isinstance(options, dict):
            raise ValueError('预设操作手感格式错误')
        options = options.setdefault(name, {})
        if not isinstance(options, dict):
            raise ValueError('预设操作手感格式错误')
        mouse = options.setdefault('mouse', {})
        if not isinstance(mouse, dict):
            raise ValueError('预设视角与指针设置格式错误')
        mouse['deadzone'] = float(value)
        staged['mapping_revision'] = staged.get('mapping_revision', 0) + 1
        self.data.clear(); self.data.update(staged)
        try:
            self.save(merge=False)
        except Exception:
            self.data.clear(); self.data.update(before)
            if baseline is not None:
                self._baseline = baseline
            raise
        return copy.deepcopy(self.data)

    def _swap_bindings(self, change, state):
        """Exchange two current entries in the latest file as one transaction."""
        from .mapping_engine import canonical_trigger, validate_mappings
        signature = mapping_input_signature(state)
        expected_inputs = change.get('expected_inputs')
        if not isinstance(expected_inputs, dict) or expected_inputs != signature:
            raise ValueError('输入设备能力已变化，请重新打开交换绑定')
        if not isinstance(change.get('first'), str) or not isinstance(change.get('second'), str):
            raise ValueError('请选择两个不同的输入来源')
        first, second = canonical_trigger(change['first']), canonical_trigger(change['second'])
        if first == second:
            raise ValueError('请选择两个不同的输入来源')
        if first.startswith('TP:') != second.startswith('TP:'):
            raise ValueError('触摸手势只能与触摸手势交换绑定')
        staged = copy.deepcopy(self.data)
        if self.path.exists():
            staged = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(staged, dict) or not isinstance(staged.get('profiles'), dict):
                raise ValueError('当前配置无法读取，请重新打开软件')
        name = change.get('profile')
        if not isinstance(name, str):
            raise ValueError('请选择当前手柄的预设')
        resolved = device_config(staged, state)
        if (any(key in ('TP:scroll_up', 'TP:scroll_down') for key in (first, second))
                and resolved.get('touch_scroll') and not resolved.get('touch_gestures_enabled')):
            raise ValueError('请先在触摸板设置中开启手势绑定，再交换滚动手势')
        before_first = swap_binding_entry(staged, state, name, first)
        before_second = swap_binding_entry(staged, state, name, second)
        if (not isinstance(change.get('expected_first'), dict)
                or not isinstance(change.get('expected_second'), dict)
                or change['expected_first'] != before_first or change['expected_second'] != before_second):
            raise ValueError('绑定或长按设置已变化，请重新打开交换绑定')
        # Explicitly save both sides, including empty entries, so a device's
        # implicit default cannot reappear after it has been moved elsewhere.
        swapped = validate_mappings({first: before_second, second: before_first})
        for key, entry in swapped.items():
            if key in ('TP:scroll_up', 'TP:scroll_down') and entry['short'].get('action') == 'none':
                # Scroll's runtime default deliberately falls back for "none";
                # an explicit empty side of this exchange must suppress it.
                entry['short']['action'] = 'suppress'
        staged['profiles'][name].update(swapped)
        staged['mapping_revision'] = staged.get('mapping_revision', 0) + 1
        before, baseline = copy.deepcopy(self.data), copy.deepcopy(getattr(self, '_baseline', None))
        self.data.clear(); self.data.update(staged)
        try:
            self.save(merge=False)
        except Exception:
            self.data.clear(); self.data.update(before)
            if baseline is not None:
                self._baseline = baseline
            raise
        return copy.deepcopy(self.data)

    def apply_mapping_change(self, change, state=None):
        from .mapping_engine import canonical_trigger, input_sources, validate_mappings
        op = change['op']
        if op == 'import_profile':
            return self._import_profile(change, state)
        if op == 'pointer_deadzone':
            return self._apply_pointer_deadzone(change, state)
        if op == 'swap_bindings':
            return self._swap_bindings(change, state)
        if op == 'application_profiles':
            if not state:
                raise ValueError('请先连接手柄，再设置应用关联')
            from .application_profiles import normalize_application_profiles
            settings = normalize_application_profiles(change.get('settings'), self.data, state)
            self.data.setdefault('application_profiles', {})[profile_scope(state)] = settings
            self.data['mapping_revision'] = self.data.get('mapping_revision', 0) + 1
            self.save()
            return copy.deepcopy(self.data)
        name = change.get('profile', self.data['active_profile'])
        if op != 'create' and name not in self.data['profiles']:
            raise ValueError('预设已删除，请重新选择')
        scope = profile_scope(state)
        if op != 'create' and self.data.get('profile_devices', {}).get(name) != scope:
            raise ValueError('该预设不属于当前输入设备')
        if op == 'binding':
            key = canonical_trigger(change['trigger'])
            if not set(key.split('+')) <= set(input_sources(state)):
                raise ValueError('请选择当前手柄支持的输入按键')
            mapping = validate_mappings({key: change['mapping']})[key]
            previous = change.get('previous_trigger')
            if previous and canonical_trigger(previous) != key:
                self.data['profiles'][name][canonical_trigger(previous)] = {g: {'action': 'none'} for g in ('short','long')}
            self.data['profiles'][name][key] = mapping
            if key.startswith('TP:') and mapping['short'].get('action', 'none') != 'none':
                self.set_setting('touch_gestures_enabled', True, state)
        elif op == 'unbind':
            self.data['profiles'][name][canonical_trigger(change['trigger'])] = {g: {'action': 'none'} for g in ('short', 'long')}
        elif op == 'select':
            self.remember_profile(state, name)
        elif op == 'create':
            if not name.strip() or name in self.data['profiles']:
                raise ValueError('请使用不重复的预设名称')
            source = change.get('source') or self.data['active_profile']
            mode = change.get('mode') or profile_mode(self.data, source)
            family = (state or {}).get('family', 'generic')
            if source not in self.data['profiles'] or self.data.get('profile_devices', {}).get(source) != scope:
                raise ValueError('该预设不属于当前输入设备')
            source_mode = profile_mode(self.data, source)
            if mode == source_mode and source in self.data['profiles']:
                self.data['profiles'][name] = copy.deepcopy(self.data['profiles'][source])
                self.data['profile_options'][name] = copy.deepcopy(self.data['profile_options'].get(source, {}))
            elif mode == 'gamepad':
                self.data['profiles'][name] = controller_defaults(family, (state or {}).get('available_buttons'))
                self.data['profile_options'][name] = {}
            else:
                self.data['profiles'][name] = desktop_defaults(family, (state or {}).get('available_buttons'))
                self.data['profile_options'][name] = {'right_stick_mouse': True}
            self.data['profile_families'][name] = family
            self.data['profile_devices'][name] = scope
            if mode == source_mode and is_nikki_profile(self.data, source):
                from .kbm_mapper import NIKKI_PROFILE_NAME
                self.data['profile_sources'][name] = NIKKI_PROFILE_NAME
                self.data.setdefault('nikki_profile_layouts', {})[name] = copy.deepcopy(
                    self.data.get('nikki_profile_layouts', {}).get(source, {'version': 2, 'touch_inputs': []}))
                self._upgrade_nikki_profile(name, state)
            self.data.setdefault('profile_modes', {})[name] = mode
            self.remember_profile(state, name)
        elif op == 'options':
            allowed = {'right_stick_mouse', 'mouse', 'input'}
            if set(change['options']) - allowed:
                raise ValueError('未知预设选项')
            if 'input' in change['options'] and not isinstance(change['options']['input'], dict):
                raise ValueError('输入手感设置格式错误')
            if 'mouse' in change['options']:
                mouse = change['options']['mouse']
                if not isinstance(mouse, dict):
                    raise ValueError('预设视角与指针设置格式错误')
                if 'invert_y' in mouse and not isinstance(mouse['invert_y'], bool):
                    raise ValueError('Y 轴反转设置应为开启或关闭')
                if 'gyro_enabled' in mouse and not isinstance(mouse['gyro_enabled'], bool):
                    raise ValueError('陀螺仪设置应为开启或关闭')
                if 'gyro_invert_y' in mouse and not isinstance(mouse['gyro_invert_y'], bool):
                    raise ValueError('陀螺仪 Y 轴反转设置应为开启或关闭')
                if 'stick_source' in mouse and (
                        type(mouse['stick_source']) is not str or mouse['stick_source'] not in ('right', 'left')):
                    raise ValueError('鼠标控制摇杆应为右摇杆或左摇杆')
            self.data['profile_options'].setdefault(name, {}).update(copy.deepcopy(change['options']))
        elif op == 'delete':
            if self.delete_profile(name, state) is None:
                raise ValueError('至少保留一个预设')
            self.data['profile_options'].pop(name, None)
            self.data.get('profile_modes', {}).pop(name, None)
        elif op == 'reset':
            from .kbm_mapper import NIKKI_LAYOUT_VERSION, infinity_nikki_defaults
            if profile_mode(self.data, name) == 'kbm':
                family = (state or {}).get('family', 'xbox')
                available = (state or {}).get('available_buttons', list(range(15)) if not state else None)
                if is_nikki_profile(self.data, name):
                    sources = touch_sources(state)
                    self.data['profiles'][name] = infinity_nikki_defaults(family, available, touch_inputs=sources)
                    self.data['profile_options'][name] = copy.deepcopy(NIKKI_PROFILE_OPTIONS)
                    self.data.setdefault('nikki_profile_layouts', {})[name] = {
                        'version': NIKKI_LAYOUT_VERSION, 'touch_inputs': list(sources),
                        'touch_defaults': bool(sources)}
                    self._initialize_nikki_touch_settings(state, force=True)
                else:
                    self.data['profiles'][name] = desktop_defaults(family, available)
                    self.data['profile_options'][name] = {'right_stick_mouse': True}
            else:
                self.data['profiles'][name] = controller_defaults((state or {}).get('family', 'xbox'),
                                                                 (state or {}).get('available_buttons', list(range(15))))
                self.data['profile_options'][name] = {}
        elif op == 'allocate_unused_xinput':
            from .device_quirks import partition_dinput_buttons_by_sdl_db
            avail = list((state or {}).get('available_buttons') or range(15))
            total = max(avail, default=14) + 1
            sdl_map = str((state or {}).get('sdl_mapping') or '')
            part = (state or {}).get('button_partition') or partition_dinput_buttons_by_sdl_db(total, sdl_map)
            use_canonical = bool(change.get('use_canonical_slots', False))
            seq_map = (
                part.get('canonical_allocated_sequences', {})
                if use_canonical
                else {str(k): v for k, v in part.get('allocated_sequences', {}).items()}
            )
            valid_sources = set(input_sources(state))
            overwrite = bool(change.get('overwrite', True))
            current_prof = self.data['profiles'][name]
            for trig, seq in seq_map.items():
                if trig not in valid_sources:
                    continue
                existing = current_prof.get(trig, {})
                if overwrite or existing.get('short', {}).get('action', 'none') == 'none':
                    current_prof[trig] = {
                        'short': copy.deepcopy(seq),
                        'long': copy.deepcopy(existing.get('long', {'action': 'none'})),
                    }
        else:
            raise ValueError('未知映射修改')
        self.data['mapping_revision'] = self.data.get('mapping_revision', 0) + 1
        self.save()
        return copy.deepcopy(self.data)


def remove_profiles(config, removed):
    """Remove presets and stale metadata, keeping device references usable."""
    from .kbm_mapper import NIKKI_PROFILE_NAME
    profiles = config['profiles']
    for name in removed:
        profiles.pop(name, None)
    for field in ('profile_modes', 'profile_families', 'profile_options', 'profile_devices', 'profile_sources', 'nikki_profile_layouts'):
        metadata = config.get(field)
        if isinstance(metadata, dict):
            config[field] = {name: value for name, value in metadata.items() if name in profiles}
        else:
            config[field] = {}
    if config.get('active_profile') in removed:
        config['active_profile'] = NIKKI_PROFILE_NAME
    if 'legacy_dualsense_profile' in config and config['legacy_dualsense_profile'] not in profiles:
        config['legacy_dualsense_profile'] = NIKKI_PROFILE_NAME
    controller_profiles = config.get('controller_profiles', {})
    config['controller_profiles'] = {key: NIKKI_PROFILE_NAME if name in removed else name
                                     for key, name in controller_profiles.items()
                                     if name in removed or name in profiles}
    for settings in config.get('application_profiles', {}).values():
        if isinstance(settings, dict) and isinstance(settings.get('rules'), list):
            settings['rules'] = [rule for rule in settings['rules']
                                 if isinstance(rule, dict) and rule.get('profile') in profiles]


def merge_changes(base, edited, latest):
    """Apply only this writer's changes; keep unrelated concurrent edits."""
    result = copy.deepcopy(latest)
    for key in base.keys() - edited.keys():
        result.pop(key, None)
    for key, value in edited.items():
        if key not in base or value != base[key]:
            if isinstance(value, dict) and isinstance(base.get(key), dict) and isinstance(result.get(key), dict):
                result[key] = merge_changes(base[key], value, result[key])
            else:
                result[key] = copy.deepcopy(value)
    return result


# Compatibility import: there is only one gesture implementation.
from .mapping_engine import GestureEngine


def radial_deadzone(x, y, deadzone):
    magnitude = (x*x + y*y) ** .5
    if magnitude <= deadzone:
        return 0., 0.
    gain = min(1., (magnitude - deadzone) / (1 - deadzone)) / magnitude
    return x * gain, y * gain
