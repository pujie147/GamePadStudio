"""The input/gesture/output contract shared by the agent and every editor.

SDL button positions are canonical (never the printed A/B labels). Chords are
unordered sets. A consumed chord cannot fall through to its component buttons.
The clock is injectable so arbitration and releases can be tested without I/O.
"""
from __future__ import annotations

import copy
import math
import time
from collections import Counter
from .actions import parse_keys
from .response_curves import curve_capabilities, evaluate_curve, normalize_curve_channels
from .touch_gestures import (TOUCH_INPUTS, TouchGestureRecognizer, normalize_touch_settings,
                             touch_sources)

ALIASES = {'A': '0', 'B': '1', 'X': '2', 'Y': '3', 'BACK': '4',
           'VIEW': '4', 'CREATE': '4', 'GUIDE': '5', 'HOME': '5', 'PS': '5', 'XBOX': '5',
           'START': '6', 'OPTIONS': '6', 'L3': '7', 'R3': '8', 'LB': '9', 'L1': '9',
           'RB': '10', 'R1': '10', 'UP': '11', 'DOWN': '12', 'LEFT': '13', 'RIGHT': '14',
           'SHARE': '15', 'CAPTURE': '15', 'L2': 'LT', 'R2': 'RT',
           'LS_UP': 'LS:up', 'LS_DOWN': 'LS:down', 'LS_LEFT': 'LS:left', 'LS_RIGHT': 'LS:right'}
INPUTS = [str(i) for i in range(128)] + ['LT', 'RT'] + [f'{s}:{d}' for s in ('LS', 'RS') for d in ('up', 'down', 'left', 'right')] + ['LS:outer', 'LS:inner', 'LS:up:outer', 'LS:down:outer', 'LS:left:outer', 'LS:right:outer'] + list(TOUCH_INPUTS)
GESTURES = ('short', 'long')
GAMEPAD_TARGETS = ['0', '1', '2', '3', '9', '10', 'LT', 'RT', '11', '12', '13', '14', '7', '8', '4', '6', '5', '15']
GAMEPAD_ACTIONS = {'gamepad_button', 'gamepad_chord', 'gamepad_turbo', 'gamepad_macro'}
HOLD_ACTIONS = ('hold', 'mouse_hold', 'gamepad_button', 'gamepad_chord', 'gamepad_turbo')
ACTIONS = {'none', 'suppress', 'hold', 'shortcut', 'mouse_hold', 'mouse_click', 'wheel', 'capture',
           'replay_record', 'record_toggle', 'home', 'gallery', 'launch',
           'volume_mute', 'volume_up', 'volume_down', 'media',
           'gamepad_button', 'gamepad_chord', 'gamepad_turbo', 'gamepad_macro',
           'antigravity'}

# Mirrors the runner defaults below, so a validated binding always executes
# exactly as it was stored.
MACRO_STEP_LIMIT = 64
MACRO_HOLD_LIMITS = (10, 500)
MACRO_GAP_LIMITS = (0, 5000)
MACRO_HOLD_DEFAULT = 50
MACRO_GAP_DEFAULT = 30


def macro_loops(binding):
    """A looping macro is held by its trigger, so its key-up must stop the runner."""
    return binding.get('action') == 'gamepad_macro' and bool(binding.get('loop'))


def _macro_flag(value, label):
    if type(value) is not bool:
        raise ValueError(f'手柄动作宏的「{label}」开关应为布尔值')
    return value


def _macro_ms(value, field, limits, fallback):
    if value is None:
        return fallback
    low, high = limits
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'手柄动作宏的「{field}」应为 {low} 至 {high} 毫秒')
    number = int(round(value))
    if not low <= number <= high:
        raise ValueError(f'手柄动作宏的「{field}」应在 {low} 至 {high} 毫秒之间')
    return number


def _macro_step_buttons(step):
    buttons = step.get('buttons')
    if isinstance(buttons, (list, tuple)):
        parts = [str(b).strip() for b in buttons if str(b).strip()]
    else:
        parts = [p.strip() for p in str(step.get('value', '')).split('+') if p.strip()]
    parts = list(dict.fromkeys(parts))
    if not parts or len(parts) > 4 or not set(parts) <= set(GAMEPAD_TARGETS):
        raise ValueError('请选择支持的目标手柄按键')
    return sorted(parts, key=GAMEPAD_TARGETS.index)


def _macro_sequence(value):
    """Normalize a recorded combo into self-describing steps the runner can replay."""
    if not isinstance(value, (list, tuple)):
        raise ValueError('请录制手柄动作宏流程')
    if not value:
        raise ValueError('宏流程为空：请录制至少一个按键步骤')
    if len(value) > MACRO_STEP_LIMIT:
        raise ValueError(f'手柄动作宏最多 {MACRO_STEP_LIMIT} 步')
    steps = []
    for step in value:
        if not isinstance(step, dict):
            raise ValueError('手柄动作宏步骤格式错误')
        steps.append({'buttons': _macro_step_buttons(step),
                      'hold_ms': _macro_ms(step.get('hold_ms'), '每步按住时长',
                                            MACRO_HOLD_LIMITS, MACRO_HOLD_DEFAULT),
                      'gap_ms': _macro_ms(step.get('gap_ms'), '延迟时长',
                                          MACRO_GAP_LIMITS, MACRO_GAP_DEFAULT)})
    return steps


def canonical_trigger(value):
    parts = [ALIASES.get(p.strip().upper(), p.strip()) for p in str(value).split('+')]
    names = {key.upper(): key for key in INPUTS}
    parts = [names.get(p.upper(), p) for p in parts]
    if not parts or any(p not in INPUTS for p in parts):
        raise ValueError(f'未知手柄输入：{value}')
    if len(set(parts)) != len(parts) or len(parts) > 4:
        raise ValueError('组合键需要 1 至 4 个不同按键')
    if any(p.startswith('TP:') for p in parts) and len(parts) != 1:
        raise ValueError('触摸板手势作为独立动作使用，不能与按键组成组合键')
    return '+'.join(sorted(parts, key=INPUTS.index))


def validate_binding(binding):
    if not isinstance(binding, dict) or binding.get('action', 'none') not in ACTIONS:
        raise ValueError('未知映射动作')
    action = binding.get('action', 'none')
    if action in ('hold', 'mouse_hold'):
        if 'mode' in binding and (
                type(binding['mode']) is not str
                or binding['mode'] not in ('hold', 'toggle', 'turbo', 'double_tap_hold')):
            raise ValueError('键鼠按住模式应为按住、切换保持或连发（含双击并按住）')
        if binding.get('mode') == 'turbo':
            rate = binding.get('rate_hz', 15)
            if isinstance(rate, bool) or not isinstance(rate, int):
                raise ValueError('连发速率应为 5 至 30 Hz 之间的整数')
            if not 5 <= rate <= 30:
                raise ValueError('连发速率应在 5 至 30 Hz 之间')
            binding['rate_hz'] = int(rate)
    elif action != 'antigravity' and 'mode' in binding:
        raise ValueError('键鼠按住模式应为按住、切换保持或连发（含双击并按住）')
    if action == 'gamepad_button' and not str(binding.get('value', '')).strip():
        raise ValueError('请选择目标手柄按键')
    if action == 'gamepad_chord' and not str(binding.get('value', '')).strip():
        raise ValueError('请设置目标手柄组合键')
    if action == 'gamepad_turbo' and not str(binding.get('value', '')).strip():
        raise ValueError('请选择手柄连发按键')
    if action == 'gamepad_macro':
        binding['sequence'] = _macro_sequence(binding.get('sequence'))
        # Absent flags already read as False in the runner; do not materialize
        # them, or every untouched profile would gain keys on save.
        for field, label in (('interrupt', '可打断'), ('loop', '连续')):
            if field in binding:
                binding[field] = _macro_flag(binding[field], label)
    elif 'interrupt' in binding or 'loop' in binding:
        raise ValueError('可打断与连续仅用于手柄动作宏')
    if action in ('hold', 'shortcut') and not parse_keys(binding.get('value', '')):
        raise ValueError('请设置键盘按键或组合键')
    if action == 'hold' and binding.get('mode') == 'toggle' and any(
            key in (20, 144, 145) for key in parse_keys(binding['value'])):
        # Lock keys already toggle OS state on key-down. Merely releasing the
        # synthetic key on the second press cannot undo that state change.
        raise ValueError('CapsLock、NumLock 和 ScrollLock 不能使用切换保持')
    if action == 'hold' and binding.get('mode') == 'turbo' and any(
            key in (20, 144, 145) for key in parse_keys(binding['value'])):
        raise ValueError('CapsLock、NumLock 和 ScrollLock 不能使用连发')
    if action == 'hold' and binding.get('mode') == 'double_tap_hold' and any(
            key in (20, 144, 145) for key in parse_keys(binding['value'])):
        raise ValueError('CapsLock、NumLock 和 ScrollLock 不能使用双击并按住')
    if action in ('mouse_hold', 'mouse_click') and binding.get('value') not in ('left', 'right', 'middle'):
        raise ValueError('请选择鼠标按键')
    if action == 'wheel' and binding.get('value') not in ('up', 'down'):
        raise ValueError('请选择滚轮方向')
    if action == 'launch' and not binding.get('executable', '').strip():
        raise ValueError('请选择应用或命令')
    return copy.deepcopy(binding)


def validate_mappings(mappings):
    result = {}
    for trigger, entry in mappings.items():
        key = canonical_trigger(trigger)
        if not isinstance(entry, dict):
            raise ValueError('按键配置格式错误')
        if key in result:
            raise ValueError(f'重复的组合键：{trigger}')
        result[key] = {g: validate_binding(entry.get(g, {'action': 'none'})) for g in GESTURES}
        if key.startswith('TP:') and result[key]['long'].get('action') not in ('none', 'suppress'):
            raise ValueError('触摸板手势只设置一次触发动作')
        if key.startswith('TP:') and any(
                result[key][g].get('mode') in ('toggle', 'turbo', 'double_tap_hold') for g in GESTURES):
            raise ValueError('触摸板手势不能使用切换保持或连发（含双击并按住）')
        if 'long_press' in entry:
            threshold = float(entry['long_press'])
            if not .15 <= threshold <= 3:
                raise ValueError('长按时长应在 0.15 至 3 秒之间')
            result[key]['long_press'] = threshold
    return result


def legacy_action(value, chord=False):
    if not value or value == 'none':
        return {'action': 'none'}
    if isinstance(value, dict):
        return validate_binding(value)
    if value.startswith('action:'):
        return {'action': value.split(':', 1)[1]}
    if value.startswith('mouse:wheel_'):
        return {'action': 'wheel', 'value': value.rsplit('_', 1)[1]}
    if value.startswith('mouse:'):
        return {'action': 'mouse_click' if chord else 'mouse_hold', 'value': value.split(':', 1)[1]}
    return {'action': 'shortcut' if chord else 'hold', 'value': value}


def convert_scheme(scheme):
    mappings = {}
    chords_keys = [set(canonical_trigger(chord).split('+')) for chord in scheme.get('chords', {})]
    timing_window = max(.15, min(3., float(scheme.get('timing_window_s', .2))))
    for group in ('buttons', 'chords'):
        for key, value in scheme.get(group, {}).items():
            canon = canonical_trigger(key)
            entry = value if isinstance(value, dict) and 'action' not in value else {'short': value}
            target = {g: legacy_action(entry.get(g), group == 'chords') for g in GESTURES}
            if isinstance(value, dict) and 'short' in value:
                target['short'] = legacy_action(value['short'], chord=True)
                if value.get('long') == 'none':
                    target['long'] = {'action': 'suppress'}
                target['long_press'] = max(.15, min(3., float(scheme.get('timing_window_s', timing_window))))
            elif group == 'buttons' and any(canon in chord and len(chord) > 1 for chord in chords_keys):
                # Universal rule: any button acting as a chord prefix/modifier suppresses long hold
                if target['long'].get('action') == 'none':
                    target['long'] = {'action': 'suppress'}
                    target['long_press'] = timing_window
                if target['short'].get('action') == 'hold':
                    target['short']['action'] = 'shortcut'
            mappings[canon] = target
    # These used to be invisible, hardcoded side effects in the KBM engine.
    for direction, key in [('up', 'W'), ('down', 'S'), ('left', 'A'), ('right', 'D')]:
        mappings.setdefault('LS:' + direction, {'short': {'action': 'hold', 'value': key}, 'long': {'action': 'none'}})
    mappings.setdefault('LS:outer', {'short': {'action': 'hold', 'value': 'Shift'}, 'long': {'action': 'none'}})
    return mappings


def input_sources(state=None):
    """Expose reported hardware inputs, or standard XInput when disconnected."""
    if state is None:
        buttons, axes = set(range(15)), set(range(6))
    else:
        buttons = {int(value) for value in state.get('available_buttons', range(15))
                   if isinstance(value, (int, str)) and str(value).isdigit() and 0 <= int(value) < 128}
        if state.get('is_gamecontroller') is False and state.get('num_hats', 0):
            buttons.update(range(11, 15))
        if 'available_axes' in state:
            axes = set(state['available_axes'])
        elif state.get('is_gamecontroller') is False:
            axes = set(range(min(6, state.get('num_axes', len(state.get('axes', []))))))
        else:
            axes = set(range(min(6, len(state['axes'])))) if 'axes' in state else set(range(6))
    result = {str(key) for key in buttons}
    for axis, names in {0: ('LS:left', 'LS:right'), 1: ('LS:up', 'LS:down'),
                        2: ('RS:left', 'RS:right'), 3: ('RS:up', 'RS:down'),
                        4: ('LT',), 5: ('RT',)}.items():
        if axis in axes:
            result.update(names)
    if {0, 1} <= axes:
        result.update(('LS:inner', 'LS:outer', 'LS:up:outer', 'LS:down:outer', 'LS:left:outer', 'LS:right:outer'))
    result.update(touch_sources(state))
    return [key for key in INPUTS if key in result]


def effective_mappings(config, state=None, profile_name=None):
    target = profile_name or config.get('active_profile')
    if target not in config.get('profiles', {}):
        target = config.get('active_profile')
    if target not in config.get('profiles', {}):
        return {}
    from .studio_core import profile_scope
    owners = config.get('profile_devices', {})
    owners = owners if isinstance(owners, dict) else {}
    if target in owners and owners[target] != profile_scope(state):
        return {}
    available_inputs = set(input_sources(state))
    mappings = {trigger: copy.deepcopy(entry) for trigger, entry in config['profiles'][target].items()
                if set(trigger.split('+')) <= available_inputs}
    family = profile_family(config, state, target)
    if family == 'xbox' and config.get('gamebar_shield_enabled'):
        available = (state or {}).get('available_buttons', list(range(15)))
        if 5 in available:
            mappings.setdefault('5', {'short': {'action': 'home'}, 'long': {'action': 'none'}})
        if 15 in available:
            mappings.setdefault('15', {'short': {'action': 'capture'}, 'long': {'action': 'replay_record'}})
    ag_link = config.get('antigravity_link')
    if ag_link and ag_link.get('enabled'):
        trig = str(ag_link.get('trigger_key', '4'))
        gesture = ag_link.get('trigger_gesture', 'short')
        other_gesture = 'long' if gesture == 'short' else 'short'
        target_bundle = ag_link.get('target_bundle_id', 'com.google.antigravity')
        action_type = ag_link.get('action_type', 'focus')
        existing = mappings.get(trig, {})
        entry = copy.deepcopy(existing)
        entry[gesture] = {'action': 'antigravity', 'target': target_bundle, 'mode': action_type}
        if other_gesture not in entry:
            entry[other_gesture] = {'action': 'none'}
        mappings[trig] = entry
    return mappings


def profile_family(config, state=None, profile_name=None):
    from .controller_catalog import CATALOG
    family = (state or {}).get('family', 'generic')
    return family if family in CATALOG else 'generic'


def trigger_label(trigger, family='generic'):
    from .controller_catalog import button_labels
    names = button_labels(family)
    extra = {'LT': 'L2' if family in ('dualsense', 'dualshock4') else 'ZL' if family == 'switch' else 'LT',
             'RT': 'R2' if family in ('dualsense', 'dualshock4') else 'ZR' if family == 'switch' else 'RT',
             'LS:outer': '左摇杆推满', 'LS:inner': '左摇杆轻推',
             'LS:up:outer': '左摇杆上推满', 'LS:down:outer': '左摇杆下推满',
             'LS:left:outer': '左摇杆左推满', 'LS:right:outer': '左摇杆右推满'}
    from .i18n import tr
    extra.update({key: tr(title) for key, title in {
                  'TP:tap': '触摸轻点', 'TP:double_tap': '触摸双击', 'TP:hold': '触摸长按',
                  'TP:swipe_up': '触摸上滑', 'TP:swipe_down': '触摸下滑',
                  'TP:swipe_left': '触摸左滑', 'TP:swipe_right': '触摸右滑',
                  'TP:two_tap': '双指轻点', 'TP:scroll_up': '双指向上滚动',
                  'TP:scroll_down': '双指向下滚动'}.items()})
    for s, title in [('LS', '左摇杆'), ('RS', '右摇杆')]:
        extra.update({f'{s}:{d}': title + a for d, a in [('up', '↑'), ('down', '↓'), ('left', '←'), ('right', '→')]})
    return ' + '.join(names.get(int(p), p) if p.isdigit() else extra.get(p, p) for p in canonical_trigger(trigger).split('+'))


def binding_label(binding, family='generic'):
    from .studio_core import ACTION_NAMES
    from .i18n import tr
    action = binding.get('action', 'none')
    value = binding.get('value', '')
    if action == 'gamepad_button':
        return '手柄 ' + trigger_label(value, family)
    if action == 'gamepad_chord':
        return '手柄组合 [' + trigger_label(value, family) + ']'
    if action == 'gamepad_turbo':
        hz = binding.get('rate_hz', 15)
        return '手柄连发 [' + trigger_label(value, family) + f' {hz}Hz]'
    if action == 'gamepad_macro':
        flags = ([tr('可打断')] if binding.get('interrupt') else [])
        if binding.get('loop'):
            flags.append(tr('循环'))
        return ('手柄连招 (' + str(len(binding.get('sequence', []))) + ' 步)'
                + ('·' + '·'.join(flags) if flags else ''))
    if action in ('hold', 'shortcut'):
        if action == 'shortcut':
            return value
        if binding.get('mode') == 'turbo':
            hz = binding.get('rate_hz', 15)
            return f"{value}（连发 {hz}Hz）"
        if binding.get('mode') == 'double_tap_hold':
            return value + tr('（双击并按住）')
        return value + tr('（切换保持）' if binding.get('mode') == 'toggle' else '（按住）')
    if action in ('mouse_hold', 'mouse_click'):
        target = tr('鼠标' + {'left': '左键', 'right': '右键', 'middle': '中键'}.get(value, value))
        if action == 'mouse_click':
            return target
        if binding.get('mode') == 'turbo':
            hz = binding.get('rate_hz', 15)
            return f"{target}（连发 {hz}Hz）"
        if binding.get('mode') == 'double_tap_hold':
            return target + tr('（双击并按住）')
        return target + tr('（切换保持）' if binding.get('mode') == 'toggle' else '（按住）')
    if action == 'wheel':
        return tr('滚轮') + ('↑' if value == 'up' else '↓')
    return tr(ACTION_NAMES.get(action, action))


def output_tokens(binding):
    action = binding.get('action', 'none')
    if action == 'gamepad_button':
        return [f'pad:{binding.get("value", "")}']
    if action == 'gamepad_chord':
        return [f'pad:{p}' for p in binding.get('value', '').split('+')]
    if action == 'gamepad_turbo':
        return [f'pad_turbo:{binding.get("value", "")}']
    if action in ('hold', 'shortcut'):
        return [f'key:{key}' for key in parse_keys(binding.get('value', ''))]
    if action in ('mouse_hold', 'mouse_click'):
        return ['mouse:' + binding['value']]
    if action == 'wheel':
        return ['mouse:wheel_' + binding['value']]
    return [] if action in ('none', 'suppress') else ['action:' + action]


INPUT_THRESHOLD_DEFAULTS = {'trigger_press': .55, 'trigger_release': .35,
                            'stick_press': .50, 'stick_release': .35,
                            'outer_press': .85, 'outer_release': .75,
                            'outer_up_press': .85, 'outer_up_release': .75,
                            'outer_down_press': .85, 'outer_down_release': .75,
                            'outer_left_press': .85, 'outer_left_release': .75,
                            'outer_right_press': .85, 'outer_right_release': .75,
                            'chord_window': .08,
                            'suppress_ls_up': 'off'}


def input_thresholds(settings=None):
    """Invalid pairs fall back together; neutral input must always release."""
    settings = settings if isinstance(settings, dict) else {}
    result = dict(INPUT_THRESHOLD_DEFAULTS)
    for group in ('trigger', 'stick', 'outer'):
        press_key, release_key = group + '_press', group + '_release'
        try:
            raw_press = settings.get(press_key, result[press_key])
            raw_release = settings.get(release_key, result[release_key])
            if isinstance(raw_press, bool) or isinstance(raw_release, bool):
                continue
            press, release = float(raw_press), float(raw_release)
            if math.isfinite(press) and math.isfinite(release) and 0 < release < press <= 1:
                result.update({press_key: press, release_key: release})
        except (TypeError, ValueError, OverflowError):
            pass
    base_press = result['outer_press']
    base_release = result['outer_release']
    for d in ('up', 'down', 'left', 'right'):
        press_key = f'outer_{d}_press'
        release_key = f'outer_{d}_release'
        result[press_key] = base_press
        result[release_key] = base_release
        if press_key in settings and release_key in settings:
            try:
                raw_press = settings.get(press_key, base_press)
                raw_release = settings.get(release_key, base_release)
                if isinstance(raw_press, bool) or isinstance(raw_release, bool):
                    continue
                press, release = float(raw_press), float(raw_release)
                if math.isfinite(press) and math.isfinite(release) and 0 < release < press <= 1:
                    result[press_key] = press
                    result[release_key] = release
            except (TypeError, ValueError, OverflowError):
                pass
    # Walking is opt-in and uses reversed hysteresis: enter at a light push,
    # remain active through the transition band, leave at a stronger push.
    if 'walk_press' in settings and 'walk_release' in settings:
        try:
            raw_press, raw_release = settings['walk_press'], settings['walk_release']
            if not isinstance(raw_press, bool) and not isinstance(raw_release, bool):
                press, release = float(raw_press), float(raw_release)
                if math.isfinite(press) and math.isfinite(release) and 0 < press < release <= 1:
                    result.update({'walk_press': press, 'walk_release': release})
        except (TypeError, ValueError, OverflowError):
            pass
    suppress_up = settings.get('suppress_ls_up', 'off') if isinstance(settings, dict) else 'off'
    if suppress_up is True:
        suppress_up = 'filter'
    elif suppress_up not in ('filter', 'disable'):
        suppress_up = 'off'
    result['suppress_ls_up'] = suppress_up
    window = settings.get('chord_window', result['chord_window'])
    if isinstance(window, (int, float)) and not isinstance(window, bool):
        try:
            window = float(window)
            if math.isfinite(window) and .02 <= window <= .20:
                result['chord_window'] = window
        except (ValueError, OverflowError):
            pass
    return result


class InputNormalizer:
    """One hysteresis rule for execution, capture and input feedback."""
    def __init__(self):
        self.active = set()
        self.thresholds = input_thresholds()
        self.trigger_curves = normalize_curve_channels('trigger_curves')

    def update(self, state, settings=None):
        thresholds = input_thresholds(settings)
        self.thresholds = thresholds
        self.trigger_curves = normalize_curve_channels('trigger_curves', settings)
        supported_axes = curve_capabilities(state)['trigger_axes']
        for axis, channel in ((4, 'left'), (5, 'right')):
            if axis not in supported_axes:
                self.trigger_curves[channel] = normalize_curve_channels('trigger_curves')[channel]
        current = {str(b) for b in (state or {}).get('buttons', [])}
        axes = (state or {}).get('axes', [0.] * 6)
        axes = list(axes) + [0.] * (6 - len(axes))
        suppress_up = thresholds.get('suppress_ls_up', 'off')
        ls_up_val = -axes[1]
        if suppress_up == 'disable':
            ls_up_val = -1.0
        elif suppress_up == 'filter' and abs(axes[0]) > 0.25:
            # During horizontal roll, require a much deeper upward push (>= 0.82)
            # to avoid false jumps during 236/214 motions
            threshold_normal = thresholds['stick_release' if 'LS:up' in self.active else 'stick_press']
            if ls_up_val < max(threshold_normal * 1.5, 0.82):
                ls_up_val = -1.0

        values = {'LT': evaluate_curve(axes[4], self.trigger_curves['left']),
                  'RT': evaluate_curve(axes[5], self.trigger_curves['right']),
                  'LS:up': ls_up_val, 'LS:down': axes[1],
                  'LS:left': -axes[0], 'LS:right': axes[0], 'RS:up': -axes[3],
                  'RS:down': axes[3], 'RS:left': -axes[2], 'RS:right': axes[2]}
        for key, value in values.items():
            group = 'stick' if ':' in key else 'trigger'
            threshold = thresholds[group + ('_release' if key in self.active else '_press')]
            if value >= threshold:
                current.add(key)
        outer_threshold = thresholds['outer_release' if 'LS:outer' in self.active else 'outer_press']
        left_magnitude = math.hypot(axes[0], axes[1])
        if left_magnitude >= outer_threshold:
            current.add('LS:outer')
        for d in ('up', 'down', 'left', 'right'):
            outer_key = f'LS:{d}:outer'
            val = values[f'LS:{d}']
            thresh_key = f'outer_{d}_release' if outer_key in self.active else f'outer_{d}_press'
            if val >= thresholds[thresh_key]:
                current.add(outer_key)
        if 'walk_press' in thresholds and current.intersection({'LS:up', 'LS:down', 'LS:left', 'LS:right'}):
            walk_threshold = thresholds['walk_release' if 'LS:inner' in self.active else 'walk_press']
            if left_magnitude <= walk_threshold:
                current.add('LS:inner')
        current.intersection_update(input_sources(state))
        self.active = current
        return set(current)

    def reset(self):
        self.active.clear()


class GestureEngine:
    """Exclusive short/long gestures; holds release on break, cancel or reset."""
    def __init__(self, dispatch, threshold=.65, chord_window=.08):
        self.dispatch = dispatch
        self.threshold = threshold
        self.chord_window = chord_window
        self.pressed = {}
        self.consumed = set()
        self.chord_locks = []
        self.pending_releases = []
        self.current_trigger = ''
        self.current_gesture = ''

    def _emit(self, key, gesture, binding, down):
        self.current_trigger, self.current_gesture = key, gesture
        if binding.get('action', 'none') not in ('none', 'suppress'):
            self.dispatch(binding, down)

    def _finish(self, key, state, now, cancel=False):
        mapping, fired = state['mapping'], state['fired']
        if fired:
            action = mapping.get(fired, {})
            if action.get('action') in HOLD_ACTIONS or macro_loops(action):
                self._emit(key, fired, action, False)
        elif not cancel:
            action = mapping.get('short', {})
            self._emit(key, 'short', action, True)
            if action.get('action') in HOLD_ACTIONS:
                # A quick tap still needs a nonzero key-down interval for games (65ms ensures crossing 30/60Hz frame boundaries).
                self.pending_releases.append((now + .065, key, 'short', action))

    def pulse(self, trigger, binding, now, gesture='short'):
        """Dispatch a completed gesture once, with a bounded key/button pulse."""
        if binding.get('action', 'none') in ('none', 'suppress'):
            return
        self._emit(trigger, gesture, binding, True)
        if binding.get('action') in HOLD_ACTIONS:
            self.pending_releases.append((now + .065, trigger, gesture, binding))

    def update(self, buttons, mappings, now=None):
        now = time.monotonic() if now is None else now
        current = {str(b) for b in buttons}
        newly_pressed = current - getattr(self, '_prev_buttons', set())
        for item in list(self.pending_releases):
            when, key, gesture, action = item
            if now >= when:
                self._emit(key, gesture, action, False)
                self.pending_releases.remove(item)

        # 组合键两键依次松开锁定：只要组合键中任一按键仍被按下，该组按键全部处于锁定压制状态
        # 判定结束的时间是最后一个按键被释放的时间，无关松开顺序
        self.chord_locks = [lock for lock in self.chord_locks if lock & current]
        locked_buttons = set().union(*self.chord_locks) if self.chord_locks else set()

        self.consumed.intersection_update(current)
        # Inactive mappings do not introduce latency or reserve chord components.
        valid = {canonical_trigger(k): v for k, v in mappings.items()
                 if any(v.get(g, {}).get('action', 'none') != 'none' for g in GESTURES)}
        chords = [set(k.split('+')) for k in valid if '+' in k]
        # 动态优先级：按键包含新按下按键的组合优先；长度更长组合优先；彻底杜绝修饰键按住连续切技能时的按键吞吐与单键泄漏
        candidates = sorted((k for k in valid if set(k.split('+')) <= current),
                            key=lambda k: (-len(k.split('+')), -int(bool(set(k.split('+')) & newly_pressed)), k))
        chosen, occupied = [], set()
        for key in candidates:
            parts = set(key.split('+'))
            if parts & occupied or (parts <= self.consumed and key not in self.pressed):
                continue
            # 单键若属于当前仍生效的组合键锁定组，绝对严禁触发单键动作
            if len(parts) == 1 and parts <= locked_buttons:
                continue
            chosen.append(key)
            occupied.update(parts)
        for key, state in list(self.pressed.items()):
            if key not in chosen:
                parts = set(key.split('+'))
                # Losing a member normally completes a chord. Its surviving
                # modifier stays locked, but must not swallow an undecided tap.
                # Only a still-held trigger upgraded/occupied by another wins
                # without emitting its short gesture.
                cancel = parts <= current and (bool(parts & occupied) or bool(parts & locked_buttons))
                self._finish(key, state, now, cancel)
                if len(parts) > 1 or cancel:
                    self.consumed.update(parts & current)
                del self.pressed[key]
        for key in chosen:
            parts = set(key.split('+'))
            # Re-evaluate consumption after finishing a chord this frame.
            if parts <= self.consumed and key not in self.pressed:
                continue
            if key not in self.pressed:
                self.pressed[key] = {'start': now, 'fired': '', 'mapping': copy.deepcopy(valid[key])}
                if len(parts) > 1:
                    self.consumed.update(parts)
                    self.chord_locks.append(set(parts))
            state = self.pressed[key]
            if state['fired']:
                continue
            mapping = state['mapping']
            short, long = mapping.get('short', {}), mapping.get('long', {})
            age = now - state['start']
            threshold = mapping.get('long_press', self.threshold)
            is_prefix = any(parts < chord for chord in chords)
            is_chord = len(parts) > 1

            if long.get('action', 'none') not in ('none', 'suppress'):
                if age >= threshold:
                    self._emit(key, 'long', long, True)
                    state['fired'] = 'long'
            elif long.get('action') == 'suppress':
                if age >= threshold:
                    state['fired'] = 'long'
            elif ((is_chord and not is_prefix) or short.get('action') in HOLD_ACTIONS
                  or short.get('action') == 'gamepad_macro'):
                # A macro self-terminates, so it is deliberately absent from
                # HOLD_ACTIONS: it must fire instantly without a synthetic release.
                wait = (max(self.chord_window, 0.15) if is_prefix and short.get('action') == 'mouse_hold' else (self.chord_window if is_prefix else 0.))
                if age >= wait:
                    self._emit(key, 'short', short, True)
                    state['fired'] = 'short'
            elif is_prefix and age >= threshold:
                state['fired'] = 'long'
        self._prev_buttons = set(current)

    def reset(self):
        errors = []
        for key, state in list(self.pressed.items()):
            try:
                self._finish(key, state, 0, cancel=True)
            except Exception as exc:
                errors.append(exc)
        for item in list(self.pending_releases):
            _, key, gesture, action = item
            try:
                self._emit(key, gesture, action, False)
            except Exception as exc:
                errors.append(exc)
        self.pressed.clear()
        self.pending_releases.clear()
        self.consumed.clear()
        self.chord_locks.clear()
        self._prev_buttons = set()
        if errors:
            raise errors[0]


class MappingRuntime:
    """Only this object sends virtual input. Remote UIs merely display its state."""
    def __init__(self, actions, dispatch, start_mouse=True):
        self.actions, self.dispatch = actions, dispatch
        self.normalizer = InputNormalizer()
        self.engine = GestureEngine(self._dispatch)
        self.output_counts = Counter()
        self.toggle_latches = {}
        self.turbo_bindings = {}
        self.double_tap_bindings = {}
        self.macro_runners = []
        self.macro_events = []
        self.macro_seq = 0
        self.pulses = []
        self.recent = {}
        self.events = []
        self.seq = 0
        self.now = 0.
        self.inputs = set()
        self.last_touch = None
        self.touch_recognizer = TouchGestureRecognizer()
        self.touch_activity = {'contacts': 0, 'mode': 'idle'}
        self.preview = False
        self.blocked = set()
        self.curve_blocked = set()
        self.mouse_thread = None
        self.config_signature = None
        self.unsupported_bindings = []
        from .virtual_xinput import compose_xusb_report
        self.virtual_xinput_state = compose_xusb_report()[1]
        if start_mouse:
            from .virtual_kbm import VirtualMouseThread
            self.mouse_thread = VirtualMouseThread(actions)
            self.mouse_thread.start()

    @property
    def threshold(self):
        return self.engine.threshold

    @threshold.setter
    def threshold(self, value):
        self.engine.threshold = value

    def _hold(self, binding, down):
        tokens = output_tokens(binding)
        if self.preview:
            pass
        elif binding['action'] == 'hold':
            self.actions.hold(binding['value'], down)
        elif binding['action'] in GAMEPAD_ACTIONS:
            self.dispatch(binding, down)
        else:
            token = tokens[0]
            if (down and self.output_counts[token] == 0) or (not down and self.output_counts[token] == 1):
                self.actions.mouse_button(binding['value'], down)
        for token in tokens:
            self.output_counts[token] = max(0, self.output_counts[token] + (1 if down else -1))

    def _unsupported_key_reason(self, binding):
        """Identify a platform-unsupported key without swallowing output errors."""
        if binding.get('action') not in ('hold', 'shortcut'):
            return ''
        supported = getattr(self.actions, 'supports_key', None)
        if not callable(supported) or supported(binding.get('value', '')):
            return ''
        capability = getattr(self.actions, 'key_capability', None)
        result = capability(binding.get('value', '')) if callable(capability) else None
        return (result.get('reason') if isinstance(result, dict) else None) or '此平台不支持该键盘输出'

    def _macro_note(self, runner, reason, held):
        # A separate ring from `events`: mapping_deck reads the tail of `events`
        # to decide which gesture card to light, so step noise cannot live there.
        steps = runner['steps']
        self.macro_seq += 1
        self.macro_events.append({
            'seq': self.macro_seq,
            'trigger': runner['owner'][0], 'gesture': runner['owner'][1],
            'reason': reason, 'steps': len(steps), 'reached': runner['index'] + 1,
            'cycles': runner['cycles'], 'loop': bool(runner.get('loop')),
            'elapsed': max(0.0, self.now - runner['started']), 'held': held or [],
        })
        self.macro_events = self.macro_events[-60:]

    def _stop_runner(self, runner, reason):
        held = None
        if runner.get('is_down') and runner.get('active_binding'):
            held = str(runner['active_binding'].get('value', '')).split('+')
            self._hold(runner['active_binding'], False)
            runner['is_down'] = False
        self._macro_note(runner, reason, held)
        if runner in self.macro_runners:
            self.macro_runners.remove(runner)

    def _interrupt_macros(self):
        for runner in list(self.macro_runners):
            if runner.get('interrupt'):
                self._stop_runner(runner, 'interrupted')

    def _stop_macro(self, owner):
        for runner in list(self.macro_runners):
            if runner.get('owner') == owner:
                self._stop_runner(runner, 'released')

    def _advance_macro(self, runner, wrap=False):
        steps = runner['steps']
        index = 0 if wrap else runner['index'] + 1
        if wrap:
            runner['cycles'] += 1
        runner['index'] = index
        runner['phase'] = 'hold'
        step = steps[index]
        runner['active_binding'] = step['binding']
        self._hold(step['binding'], True)
        runner['is_down'] = True
        runner['next_time'] += step['hold_s']

    def _dispatch(self, binding, down=True):
        action = binding.get('action', 'none')
        # Old or imported Windows profiles can contain keys that the current
        # platform cannot synthesize. Keep their gesture/chord arbitration but
        # leave this one output inert; native posting failures still propagate.
        if self._unsupported_key_reason(binding):
            return
        if down:
            # Runs before anything is pressed so an interrupted macro releases
            # its buttons rather than racing the new action for the same bit.
            self._interrupt_macros()
        if action in ('hold', 'mouse_hold', 'gamepad_button', 'gamepad_chord') and binding.get('mode') == 'toggle':
            if down:
                owner = (self.engine.current_trigger, self.engine.current_gesture)
                held = self.toggle_latches.get(owner)
                if held is None:
                    self._hold(binding, True)
                    self.toggle_latches[owner] = copy.deepcopy(binding)
                else:
                    self._hold(held, False)
                    del self.toggle_latches[owner]
        elif action in ('hold', 'mouse_hold', 'gamepad_button', 'gamepad_chord') and binding.get('mode') == 'turbo':
            owner = (self.engine.current_trigger, self.engine.current_gesture)
            if down:
                rate = binding.get('rate_hz', 15)
                half_period = 0.5 / rate
                self._hold(binding, True)
                self.turbo_bindings[owner] = {
                    'binding': copy.deepcopy(binding),
                    'rate_hz': rate,
                    'half_period': half_period,
                    'next_toggle': self.now + half_period,
                    'is_down': True,
                }
            else:
                info = self.turbo_bindings.get(owner)
                if not info:
                    for k, v in list(self.turbo_bindings.items()):
                        if v['binding']['action'] == binding['action'] and v['binding'].get('value') == binding.get('value'):
                            owner = k
                            info = v
                            break
                if info:
                    if info.get('is_down'):
                        self._hold(info['binding'], False)
                        info['is_down'] = False
                    self.turbo_bindings.pop(owner, None)
        elif action in ('hold', 'mouse_hold', 'gamepad_button', 'gamepad_chord') and binding.get('mode') == 'double_tap_hold':
            owner = (self.engine.current_trigger, self.engine.current_gesture)
            if down:
                self._hold(binding, True)
                self.double_tap_bindings[owner] = {
                    'binding': copy.deepcopy(binding),
                    'step': 0,
                    't_up': self.now + 0.045,
                    't_hold': self.now + 0.090,
                    'is_down': True,
                }
            else:
                info = self.double_tap_bindings.get(owner)
                if not info:
                    for k, v in list(self.double_tap_bindings.items()):
                        if v['binding']['action'] == binding['action'] and v['binding'].get('value') == binding.get('value'):
                            owner = k
                            info = v
                            break
                if info:
                    if info.get('is_down'):
                        self._hold(info['binding'], False)
                        info['is_down'] = False
                    self.double_tap_bindings.pop(owner, None)
        elif down and action == 'gamepad_macro':
            raw_steps = binding.get('sequence', [])
            norm_steps = []
            for st in (raw_steps if isinstance(raw_steps, list) else ()):
                if not isinstance(st, dict):
                    continue
                if 'buttons' in st and isinstance(st['buttons'], (list, tuple)):
                    val = '+'.join(str(b).strip() for b in st['buttons'] if str(b).strip())
                else:
                    val = str(st.get('value', '')).strip()
                if not val:
                    continue
                hold_s = max(0.01, float(st.get('hold_ms', MACRO_HOLD_DEFAULT)) / 1000.0)
                gap_s = max(0.0, float(st.get('gap_ms', MACRO_GAP_DEFAULT)) / 1000.0)
                sub = {'action': 'gamepad_chord' if '+' in val else 'gamepad_button', 'value': val}
                norm_steps.append({'binding': sub, 'hold_s': hold_s, 'gap_s': gap_s})
            if norm_steps:
                first = norm_steps[0]
                self._hold(first['binding'], True)
                self.macro_runners.append({
                    'steps': norm_steps,
                    'index': 0,
                    'phase': 'hold',
                    'next_time': self.now + first['hold_s'],
                    'active_binding': first['binding'],
                    'is_down': True,
                    'owner': (self.engine.current_trigger, self.engine.current_gesture),
                    'interrupt': bool(binding.get('interrupt')),
                    'loop': bool(binding.get('loop')),
                    'started': self.now,
                    'cycles': 1,
                })
        elif action == 'gamepad_macro':
            self._stop_macro((self.engine.current_trigger, self.engine.current_gesture))
        elif action in HOLD_ACTIONS:
            self._hold(binding, down)
        elif down and action in ('shortcut', 'mouse_click'):
            held = dict(binding, action='hold' if action == 'shortcut' else 'mouse_hold')
            # 连续高频敲击同一快捷键/点击时，立刻完结前序未到期脉冲释放，确保 Windows 发出独立两次按压
            for item in list(self.pulses):
                when, old_held = item
                if old_held == held:
                    self._hold(old_held, False)
                    self.pulses.remove(item)
            self._hold(held, True)
            self.pulses.append((self.now + .05, held))
        elif down and action == 'wheel':
            if not self.preview:self.actions.scroll(1 if binding['value'] == 'up' else -1)
        elif down and not self.preview:
            self.dispatch(binding, True)
        if down:
            self.seq += 1
            for token in output_tokens(binding):
                self.recent[token] = self.now + .18
            self.events.append({'seq': self.seq, 'trigger': self.engine.current_trigger,
                                'gesture': self.engine.current_gesture, 'action': binding_label(binding)})
            self.events = self.events[-60:]

    def update(self, state, config, enabled=True, now=None, preview=False):
        consume_error = getattr(self.mouse_thread, 'consume_output_error', None)
        if callable(consume_error):
            error = consume_error()
            if error is not None:
                # Propagate on the owner thread, where Agent/Studio pause
                # mapping and release all other held keyboard/mouse output.
                raise error
        from .studio_core import device_config, profile_scope, profile_mode
        config = device_config(config, state)
        self.now = time.monotonic() if now is None else now
        previous_inputs = set(self.inputs)
        if self.preview != preview:
            self.reset(blocked=previous_inputs)
            self.preview = preview
        profile_options = config.get('profile_options')
        profile_options = profile_options if isinstance(profile_options, dict) else {}
        owners = config.get('profile_devices', {})
        owners = owners if isinstance(owners, dict) else {}
        owns_profile = owners.get(config['active_profile'], profile_scope(state)) == profile_scope(state)
        options = profile_options.get(config['active_profile']) if owns_profile else {}
        options = options if isinstance(options, dict) else {}
        input_settings = options.get('input')
        input_settings = copy.deepcopy(input_settings) if isinstance(input_settings, dict) else {}
        supported_axes = curve_capabilities(state)['trigger_axes']
        input_settings['trigger_curves'] = {
            channel: config.get('trigger_curves', {}).get(channel)
            for axis, channel in ((4, 'left'), (5, 'right')) if axis in supported_axes}
        previous_thresholds = self.normalizer.thresholds
        previous_curves = self.normalizer.trigger_curves
        self.inputs = self.normalizer.update(state, input_settings)
        raw_axes = list((state or {}).get('axes', [])) + [0.] * 6
        raw_triggers = {name for axis, name in ((4, 'LT'), (5, 'RT'))
                        if isinstance(raw_axes[axis], (int, float)) and raw_axes[axis] > .02}
        self.curve_blocked.intersection_update(raw_triggers)
        self.blocked.intersection_update(self.inputs)
        mappings = effective_mappings(config, state)
        signature = (profile_scope(state), (state or {}).get('instance_id'), config['active_profile'], repr(mappings), repr(options),
                     repr(self.normalizer.trigger_curves), config.get('long_press', .65),
                     config.get('touch_mouse', False), config.get('touch_gestures_enabled', False),
                     config.get('touch_scroll', False), repr(normalize_touch_settings(config)))
        if signature != self.config_signature:
            if self.config_signature is not None:
                # Lowering an input threshold must not turn existing deflection
                # into a new press. A binding-only edit still permits a genuinely
                # new button pressed this frame, while releasing older holds.
                blocked = previous_inputs | self.inputs if previous_thresholds != self.normalizer.thresholds else previous_inputs
                if previous_curves != self.normalizer.trigger_curves:
                    self.curve_blocked.update(raw_triggers)
                    blocked.update(self.inputs)
                self.reset(blocked=blocked)
            self.config_signature = signature
            self.unsupported_bindings = [
                {'trigger': trigger, 'gesture': gesture,
                 'value': binding.get('value', ''),
                 'reason': reason}
                for trigger, entry in mappings.items()
                for gesture in GESTURES
                for binding in (entry.get(gesture, {}),)
                if (reason := self._unsupported_key_reason(binding))
            ]
            self.touch_recognizer.reset(block_until_release=True)
            if self.mouse_thread:
                self.mouse_thread.configure(options.get('mouse', {}))
        self.engine.threshold = config.get('long_press', .65)
        self.engine.chord_window = self.normalizer.thresholds['chord_window']
        if not enabled or not state:
            self.reset()
            return
        for when, binding in list(self.pulses):
            if self.now >= when:
                self._hold(binding, False)
                self.pulses.remove((when, binding))
        regular_mappings = {key: entry for key, entry in mappings.items() if not key.startswith('TP:')}
        self.engine.update(self.inputs - self.blocked - self.curve_blocked, regular_mappings, self.now)
        for owner, info in list(self.turbo_bindings.items()):
            toggles = 0
            while self.now >= info['next_toggle'] - 1e-9 and toggles < 20:
                info['is_down'] = not info['is_down']
                self._hold(info['binding'], info['is_down'])
                if info['is_down']:
                    for token in output_tokens(info['binding']):
                        self.recent[token] = self.now + .18
                info['next_toggle'] += info['half_period']
                toggles += 1
            if toggles >= 20 and self.now >= info['next_toggle'] - 1e-9:
                info['next_toggle'] = self.now + info['half_period']
        for owner, info in list(self.double_tap_bindings.items()):
            if info['step'] == 0 and self.now >= info['t_up'] - 1e-9:
                self._hold(info['binding'], False)
                info['is_down'] = False
                info['step'] = 1
            if info['step'] == 1 and self.now >= info['t_hold'] - 1e-9:
                self._hold(info['binding'], True)
                info['is_down'] = True
                info['step'] = 2
                for token in output_tokens(info['binding']):
                    self.recent[token] = self.now + .18
        for runner in list(self.macro_runners):
            steps = runner['steps']
            loops = 0
            while self.now >= runner['next_time'] - 1e-9 and loops < 20:
                loops += 1
                idx = runner['index']
                cur_step = steps[idx]
                if runner['phase'] == 'hold':
                    if runner.get('is_down'):
                        self._hold(runner['active_binding'], False)
                        runner['is_down'] = False
                    wrap = idx + 1 >= len(steps)
                    if wrap and not runner.get('loop'):
                        self._stop_runner(runner, 'complete')
                        break
                    if cur_step['gap_s'] > 0:
                        # A looping macro spends its final gap between cycles.
                        runner['phase'] = 'gap'
                        runner['next_time'] += cur_step['gap_s']
                        runner['wrap'] = wrap
                    else:
                        self._advance_macro(runner, wrap=wrap)
                else:
                    self._advance_macro(runner, wrap=bool(runner.pop('wrap', False)))
        self._update_virtual_xinput_state(state, regular_mappings)
        touch = self.touch_recognizer.update(state, self.now, config)
        self.touch_activity = {'contacts': touch['contacts'], 'mode': touch['mode']}
        if config.get('touch_gestures_enabled'):
            for event in touch['events']:
                if event in touch_sources(state):
                    self.inputs.add(event)
                    self.engine.pulse(event, mappings.get(event, {}).get('short', {}), self.now)
        if touch['scroll_steps']:
            steps = touch['scroll_steps']
            event = 'TP:scroll_up' if steps > 0 else 'TP:scroll_down'
            binding = mappings.get(event, {}).get('short', {})
            custom = (config.get('touch_gestures_enabled')
                      and binding.get('action', 'none') != 'none')
            if not custom:
                binding = ({'action': 'wheel', 'value': 'up' if steps > 0 else 'down'}
                           if config.get('touch_scroll') else None)
            if binding is not None and event in touch_sources(state):
                self.inputs.add(event)
                for _ in range(abs(steps)):
                    self.engine.pulse(event, binding, self.now, gesture='scroll')
        if config.get('touch_mouse') and not self.preview:
            dx, dy = touch['pointer_delta']
            dx, dy = dx * 1600, dy * 900
            if (dx or dy) and abs(dx) < 250 and abs(dy) < 250:
                self.actions.move_mouse(dx, dy)
        self.last_touch = state.get('touch')
        if self.mouse_thread:
            axes = list(state.get('axes', [])) + [0.] * 6
            mouse_opts = options.get('mouse', {})
            stick_source = mouse_opts.get('stick_source', 'right')
            if stick_source == 'left':
                stick_axes = (axes[0], axes[1])
                required_sources = {'LS:left', 'LS:up'}
                blocked_sources = {'LS:up', 'LS:down', 'LS:left', 'LS:right'}
            else:
                stick_axes = (axes[2], axes[3])
                required_sources = {'RS:left', 'RS:up'}
                blocked_sources = {'RS:up', 'RS:down', 'RS:left', 'RS:right'}
            move = (owns_profile and options.get('right_stick_mouse', profile_mode(config, config['active_profile']) == 'kbm')
                    and required_sources <= set(input_sources(state))
                    and not self.blocked.intersection(blocked_sources))
            desktop = mouse_opts.get('mode', 'game') == 'desktop' or (hasattr(self.actions, 'is_nikki_game_focused') and not self.actions.is_nikki_game_focused())
            self.mouse_thread.update_stick(stick_axes[0] if move else 0., stick_axes[1] if move else 0., is_desktop=desktop)
            self.mouse_thread.set_click_lock(any(n for k, n in self.output_counts.items() if k.startswith('mouse:')))

            if hasattr(self.mouse_thread, 'update_gyro'):
                gyro_enabled = bool(mouse_opts.get('gyro_enabled', False))
                gyro_trigger = mouse_opts.get('gyro_trigger', 'LT')
                trigger_active = (gyro_trigger in ('', 'always') or
                                  gyro_trigger in self.inputs or
                                  ALIASES.get(gyro_trigger.upper()) in self.inputs)
                gyro_active = owns_profile and gyro_enabled and not self.preview and trigger_active
                gyro_data = state.get('gyro', [])
                if gyro_active and len(gyro_data) >= 3:
                    self.mouse_thread.update_gyro(*gyro_data[:3])
                else:
                    self.mouse_thread.update_gyro(0.0, 0.0, 0.0)

    def _update_virtual_xinput_state(self, state, regular_mappings):
        from .virtual_xinput import XUSB_BUTTON_MASKS, compose_xusb_report
        intercepted = set()
        for trig_key, entry in (regular_mappings or {}).items():
            if not isinstance(entry, dict):
                continue
            if any(entry.get(g, {}).get('action', 'none') != 'none' for g in GESTURES):
                parts = trig_key.split('+')
                if len(parts) == 1:
                    intercepted.add(parts[0])
                elif set(parts) <= self.inputs:
                    intercepted.update(parts)
        intercepted.update(self.engine.consumed)
        passthrough = {
            b for b in self.inputs
            if (b in XUSB_BUTTON_MASKS or b in ('LT', 'RT')) and b not in intercepted
        }
        emitted = {
            tok.split(':', 1)[1]
            for tok, cnt in self.output_counts.items()
            if cnt > 0 and tok.startswith('pad:')
        }
        combined = passthrough | emitted
        _, snap = compose_xusb_report(combined, (state or {}).get('axes', ()))
        snap['passthrough_buttons'] = sorted(passthrough)
        snap['emitted_buttons'] = sorted(emitted)
        snap['intercepted_buttons'] = sorted(intercepted & self.inputs)
        self.virtual_xinput_state = snap

    def feedback(self):
        return {'preview': self.preview, 'inputs': sorted(self.inputs), 'active': list(self.engine.pressed),
                'outputs': sorted(k for k, n in self.output_counts.items() if n),
                'recent_outputs': sorted(k for k, until in self.recent.items() if until > self.now),
                'events': list(self.events), 'sequence': self.seq, 'touch': dict(self.touch_activity),
                'macro_events': list(self.macro_events), 'macro_sequence': self.macro_seq,
                'unsupported_bindings': [dict(item) for item in self.unsupported_bindings]}

    def reset(self, blocked=None):
        # Stop continuous motion even if a following key-up is rejected.
        if self.mouse_thread:
            if hasattr(self.mouse_thread, 'update_gyro'):
                self.mouse_thread.update_gyro(0.0, 0.0, 0.0)
            self.mouse_thread.update_stick(0., 0.)
            if hasattr(self.mouse_thread, '_reset_motion'):
                self.mouse_thread._reset_motion()
            clear_error = getattr(self.mouse_thread, 'clear_output_error', None)
            if callable(clear_error):
                clear_error()
        errors = []
        try:
            self.engine.reset()
        except Exception as exc:
            errors.append(exc)
        for owner, info in list(self.turbo_bindings.items()):
            try:
                if info.get('is_down'):
                    self._hold(info['binding'], False)
                    info['is_down'] = False
            except Exception as exc:
                errors.append(exc)
            else:
                del self.turbo_bindings[owner]
        for owner, info in list(self.double_tap_bindings.items()):
            try:
                if info.get('is_down'):
                    self._hold(info['binding'], False)
                    info['is_down'] = False
            except Exception as exc:
                errors.append(exc)
            else:
                del self.double_tap_bindings[owner]
        for runner in list(self.macro_runners):
            try:
                if runner.get('is_down') and runner.get('active_binding'):
                    self._hold(runner['active_binding'], False)
                    runner['is_down'] = False
            except Exception as exc:
                errors.append(exc)
            else:
                self.macro_runners.remove(runner)
        for owner, binding in list(self.toggle_latches.items()):
            try:
                self._hold(binding, False)
            except Exception as exc:
                errors.append(exc)
            else:
                del self.toggle_latches[owner]
        for item in list(self.pulses):
            try:
                self._hold(item[1], False)
            except Exception as exc:
                errors.append(exc)
            else:
                self.pulses.remove(item)
        if not errors:
            self.output_counts.clear()
        self.recent.clear()
        self.last_touch = None
        self.touch_recognizer.reset(block_until_release=True)
        self.touch_activity = {'contacts': 0, 'mode': 'idle'}
        from .virtual_xinput import compose_xusb_report
        self.virtual_xinput_state = compose_xusb_report()[1]
        self.blocked.update(self.inputs if blocked is None else blocked)
        if self.mouse_thread:
            self.mouse_thread.update_stick(0., 0.)
        if errors:
            raise OSError('部分键鼠按键未能释放；映射保持暂停，可再次尝试释放') from errors[0]

    def close(self):
        try:
            self.reset()
        finally:
            if self.mouse_thread:
                self.mouse_thread.stop()
                self.mouse_thread.join(timeout=1.)
