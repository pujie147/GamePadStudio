"""Portable presets: pure validation and hardware compatibility, with no output."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import tempfile

from .controller_catalog import CATALOG
from .mapping_engine import (GAMEPAD_TARGETS, MOUSE_DIRECTIONS, STICK_DIRECTIONS,
                             canonical_trigger, input_sources, validate_mappings)
from .schema_registry import (
    GYRO_TRIGGERS,
    HOLD_MODES,
    INPUT_OPTION_FIELDS,
    MOUSE_BOOL_FIELDS,
    MOUSE_MODES,
    MOUSE_NUMERIC_RANGES,
    MOUSE_OPTION_FIELDS,
    STICK_SOURCES,
    SUPPRESS_LS_UP_MODES,
    THRESHOLD_GROUPS,
)


PROFILE_FORMAT = 'gamepadstudio-profile'
PROFILE_VERSION = 1
MAX_PROFILE_BYTES = 256 * 1024
MAX_PROFILE_MAPPINGS = 512
_INPUT_KINDS = {'sdl_gamecontroller', 'raw_joystick'}
_SAFE_ACTIONS = {'none', 'suppress', 'hold', 'shortcut', 'mouse_hold', 'mouse_click',
                 'wheel', 'capture', 'replay_record', 'record_toggle', 'home', 'gallery',
                 'volume_mute', 'volume_up', 'volume_down', 'media',
                 'gamepad_button', 'gamepad_chord', 'gamepad_turbo',
                 'gamepad_axis', 'mouse_move'}
_INPUT_FIELDS = INPUT_OPTION_FIELDS


def _fields(value, allowed, required=(), error='便携预设格式错误'):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError(error)


def _text(value, max_length, allow_empty=False, error='便携预设文本格式错误'):
    if (not isinstance(value, str) or len(value) > max_length
            or any(ord(char) < 32 for char in value) or (not allow_empty and not value.strip())):
        raise ValueError(error)
    return value.strip()


def _number(value, low, high, error='预设参数应为允许范围内的有限数值'):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(error)
    try:
        valid = math.isfinite(value) and low <= value <= high
    except (ValueError, OverflowError):
        valid = False
    if not valid:
        raise ValueError(error)
    return value


def _model(value):
    value = _text(value, 256, error='原始输入设备缺少可用的型号信息')
    if any(char in value for char in '/\\') or any(
            marker in value.casefold() for marker in (':serial:', ':path:', ':session:')):
        raise ValueError('便携预设只保存设备型号，不保存物理设备身份')
    return value


def _device_source(state):
    state = state or {}
    family = state.get('family', 'xbox')
    family = family if isinstance(family, str) and family in CATALOG else 'generic'
    kind = 'raw_joystick' if state.get('is_gamecontroller') is False else 'sdl_gamecontroller'
    result = {'family': family, 'input_kind': kind}
    if kind == 'raw_joystick':
        result['model'] = _model(state.get('model_key'))
    return result


def _source(value):
    _fields(value, {'family', 'input_kind', 'model'}, ('family', 'input_kind'))
    if (not isinstance(value['family'], str) or value['family'] not in CATALOG
            or not isinstance(value['input_kind'], str) or value['input_kind'] not in _INPUT_KINDS):
        raise ValueError('便携预设的输入设备类型不受支持')
    result = {'family': value['family'], 'input_kind': value['input_kind']}
    if value['input_kind'] == 'raw_joystick':
        result['model'] = _model(value.get('model'))
    elif 'model' in value:
        raise ValueError('标准手柄预设不需要物理设备型号信息')
    return result


def _options(value):
    _fields(value, {'right_stick_mouse', 'input', 'mouse'}, error='预设操作手感格式错误')
    result = {}
    if 'right_stick_mouse' in value:
        if not isinstance(value['right_stick_mouse'], bool):
            raise ValueError('右摇杆鼠标设置应为开启或关闭')
        result['right_stick_mouse'] = value['right_stick_mouse']
    if 'input' in value:
        settings = value['input']
        _fields(settings, _INPUT_FIELDS, error='预设按键响应设置格式错误')
        normalized = {}
        for key, item in settings.items():
            if key == 'suppress_ls_up':
                if type(item) is not str or item not in SUPPRESS_LS_UP_MODES:
                    raise ValueError('格斗防串跳设置不受支持')
                normalized[key] = item
            else:
                normalized[key] = _number(item, .02, .20) if key == 'chord_window' else _number(item, .01, 1.)
        for group in THRESHOLD_GROUPS:
            press, release = group + '_press', group + '_release'
            if (press in normalized) != (release in normalized):
                raise ValueError('开始响应与松开位置需要成对设置')
            if press in normalized:
                valid = (normalized[press] < normalized[release] if group == 'walk'
                         else normalized[release] < normalized[press])
                if not valid:
                    raise ValueError('按键响应与松开位置的顺序不正确')
        result['input'] = normalized
    if 'mouse' in value:
        mouse = value['mouse']
        _fields(mouse, MOUSE_OPTION_FIELDS,
                error='预设视角与指针设置格式错误')
        normalized = {}
        for bool_field in MOUSE_BOOL_FIELDS:
            if bool_field in mouse:
                if not isinstance(mouse[bool_field], bool):
                    if 'invert_y' in bool_field:
                        raise ValueError('Y 轴反转设置应为开启或关闭')
                    raise ValueError('陀螺仪开关设置应为开启或关闭')
                normalized[bool_field] = mouse[bool_field]
        if 'mode' in mouse:
            if type(mouse['mode']) is not str or mouse['mode'] not in MOUSE_MODES:
                raise ValueError('请选择游戏视角或桌面指针')
            normalized['mode'] = mouse['mode']
        if 'stick_source' in mouse:
            if type(mouse['stick_source']) is not str or mouse['stick_source'] not in STICK_SOURCES:
                raise ValueError('请选择右摇杆或左摇杆作为鼠标控制源')
            normalized['stick_source'] = mouse['stick_source']
        if 'gyro_trigger' in mouse:
            if type(mouse['gyro_trigger']) is not str or mouse['gyro_trigger'] not in GYRO_TRIGGERS:
                raise ValueError('陀螺仪激活条件不受支持')
            normalized['gyro_trigger'] = mouse['gyro_trigger']
        for key, (low, high) in MOUSE_NUMERIC_RANGES.items():
            if key in mouse:
                normalized[key] = _number(mouse[key], low, high)
        result['mouse'] = normalized
    return result


def _binding(binding):
    if not isinstance(binding, dict) or not isinstance(binding.get('action'), str):
        raise ValueError('预设映射动作格式错误')
    action = binding['action']
    if action == 'launch':
        raise ValueError('便携预设不能包含启动应用或命令的动作')
    if action == 'gamepad_macro':
        raise ValueError('便携预设暂不支持手柄连招序列')
    if action not in _SAFE_ACTIONS:
        raise ValueError('便携预设包含不受支持的映射动作')
    has_value = action in {'hold', 'shortcut', 'mouse_hold', 'mouse_click', 'wheel',
                           'gamepad_button', 'gamepad_chord', 'gamepad_turbo',
                           'gamepad_axis', 'mouse_move'}
    # Older local entries keep an empty value even for "none". It carries no
    # behavior and is discarded; every other extra field is rejected.
    allowed = {'action', 'value'} | ({'rate_hz'} if action == 'gamepad_turbo' else set())
    if action in ('hold', 'mouse_hold'):
        allowed.add('mode')
        allowed.add('rate_hz')
    _fields(binding, allowed, ('value',) if has_value else ())
    result = {'action': action}
    if 'mode' in binding:
        if type(binding['mode']) is not str or binding['mode'] not in HOLD_MODES:
            raise ValueError('键鼠按住模式应为按住、切换保持或连发（含双击并按住）')
        result['mode'] = binding['mode']
    if action in ('hold', 'mouse_hold') and 'rate_hz' in binding:
        if binding.get('mode') != 'turbo':
            raise ValueError('仅连发模式允许设置连发速率')
        rate = binding['rate_hz']
        if isinstance(rate, bool) or not isinstance(rate, int) or not 5 <= rate <= 30:
            raise ValueError('连发速率应为 5 至 30 Hz 之间的整数')
        result['rate_hz'] = rate
    elif action in ('hold', 'mouse_hold') and binding.get('mode') == 'turbo':
        result['rate_hz'] = 15
    if 'value' in binding:
        value = _text(binding['value'], 256, allow_empty=not has_value)
        if has_value:
            result['value'] = value
        elif value:
            raise ValueError('该映射动作不需要额外按键参数')
    if action in ('gamepad_button', 'gamepad_chord', 'gamepad_turbo'):
        target = canonical_trigger(result['value'])
        parts = target.split('+')
        if not set(parts) <= set(GAMEPAD_TARGETS) or (action != 'gamepad_chord' and len(parts) != 1):
            raise ValueError('请选择支持的目标手柄按键')
        result['value'] = target
    if action == 'gamepad_axis':
        if result['value'] not in STICK_DIRECTIONS:
            raise ValueError('请选择支持的目标摇杆方向')
    if action == 'mouse_move':
        if result['value'] not in MOUSE_DIRECTIONS:
            raise ValueError('请选择鼠标移动方向')
    if action == 'gamepad_turbo':
        rate = binding.get('rate_hz', 15)
        if isinstance(rate, bool) or not isinstance(rate, int) or not 5 <= rate <= 30:
            raise ValueError('手柄连发速度应为 5 至 30 Hz')
        result['rate_hz'] = rate
    return result


def _profile(value):
    _fields(value, {'name', 'mode', 'mappings', 'options'}, ('name', 'mode', 'mappings', 'options'))
    name = _text(value['name'], 80, error='请输入不超过 80 个字符的预设名称')
    if value['mode'] not in ('kbm', 'gamepad'):
        raise ValueError('便携预设的映射模式不受支持')
    mappings = value['mappings']
    if not isinstance(mappings, dict) or len(mappings) > MAX_PROFILE_MAPPINGS:
        raise ValueError('便携预设最多包含 512 条映射')
    normalized = {}
    for trigger, entry in mappings.items():
        if not isinstance(trigger, str):
            raise ValueError('映射来源应为按键或手势名称')
        key = canonical_trigger(trigger)
        if key in normalized:
            raise ValueError('便携预设包含重复的映射来源')
        _fields(entry, {'short', 'long', 'long_press'})
        mapping = {gesture: _binding(entry[gesture]) if gesture in entry else {'action': 'none'}
                   for gesture in ('short', 'long')}
        if 'long_press' in entry:
            mapping['long_press'] = _number(entry['long_press'], .15, 3.)
        normalized[key] = mapping
    # All actions are checked before any unsupported source can be skipped.
    normalized = validate_mappings(normalized)
    return {'name': name, 'mode': value['mode'], 'mappings': normalized,
            'options': _options(value['options'])}


def _encoded(value):
    try:
        data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError('便携预设应为有效的 JSON 内容') from exc
    if len(data) > MAX_PROFILE_BYTES:
        raise ValueError('便携预设文件不能超过 256 KiB')
    return data


def _package(package):
    _encoded(package)
    _fields(package, {'format', 'version', 'source', 'profile'}, ('format', 'version', 'source', 'profile'))
    if package['format'] != PROFILE_FORMAT:
        raise ValueError('请选择 GamePad Studio 便携预设文件')
    if type(package['version']) is not int or package['version'] != PROFILE_VERSION:
        raise ValueError('便携预设文件版本不受支持')
    return {'format': PROFILE_FORMAT, 'version': PROFILE_VERSION,
            'source': _source(package['source']), 'profile': _profile(package['profile'])}


def export_profile(config, state, name):
    """Copy one owned preset, never settings or references belonging to a device."""
    from .studio_core import profile_mode, profile_scope
    if name not in config.get('profiles', {}) or config.get('profile_devices', {}).get(name) != profile_scope(state):
        raise ValueError('该预设不属于当前输入设备')
    package = {'format': PROFILE_FORMAT, 'version': PROFILE_VERSION, 'source': _device_source(state),
               'profile': {'name': name, 'mode': profile_mode(config, name),
                           'mappings': copy.deepcopy(config['profiles'][name]),
                           'options': copy.deepcopy(config.get('profile_options', {}).get(name, {}))}}
    return _package(package)


def preview_profile_import(package, state):
    """Plan a fresh preset; neither configuration nor operating-system state changes."""
    package = _package(package)
    source, target = package['source'], _device_source(state)
    if 'raw_joystick' in (source['input_kind'], target['input_kind']):
        if (source['input_kind'] != target['input_kind']
                or source.get('model', '').casefold() != target.get('model', '').casefold()
                or source['family'] != target['family']):
            raise ValueError('原始输入外设的按键编号只适用于相同型号，请连接匹配的设备')
    available = set(input_sources(state))
    cross_family = source['family'] != target['family']
    accepted, skipped = [], []
    profile = copy.deepcopy(package['profile'])
    profile['mappings'] = {}
    for trigger, mapping in package['profile']['mappings'].items():
        parts = trigger.split('+')
        missing = set(parts) - available
        special = cross_family and any(part.isdigit() and int(part) > 14 for part in parts)
        if missing or special:
            reason = '型号专属按键不能跨手柄类型导入' if special else '当前设备不支持此输入'
            skipped.append({'trigger': trigger, 'reason': reason})
        else:
            profile['mappings'][trigger] = copy.deepcopy(mapping)
            accepted.append({'trigger': trigger, 'mapping': copy.deepcopy(mapping)})
    signature = dict(target, model=target.get('model', ''), inputs=sorted(available))
    return {'profile': profile, 'accepted': accepted, 'skipped': skipped,
            'input_signature': signature}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('便携预设 JSON 包含重复字段')
        result[key] = value
    return result


def _constant(_value):
    raise ValueError('便携预设不能包含非有限数值')


def load_profile_file(path):
    """Read bounded UTF-8 JSON and validate its complete portable schema."""
    with Path(path).open('rb') as file:
        data = file.read(MAX_PROFILE_BYTES + 1)
    if len(data) > MAX_PROFILE_BYTES:
        raise ValueError('便携预设文件不能超过 256 KiB')
    try:
        value = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_object, parse_constant=_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('便携预设应为有效的 UTF-8 JSON 文件') from exc
    return _package(value)


def save_profile_file(path, package):
    """Write one validated package atomically without modifying its source preset."""
    data = _encoded(_package(package))
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.', suffix='.tmp', delete=False) as file:
            temporary = Path(file.name)
            file.write(data)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path
