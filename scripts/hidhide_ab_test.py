"""HidHide 开关前后的控制器可见性 A/B 对比。

目的：证明「开启隐身之后，普通游戏进程看不到物理手柄，而白名单里的程序仍然能看到」，
并且关闭后完全恢复。探针必须运行在**未被白名单登记**的进程里，否则测的是我们自己。

用法（用项目虚拟环境的解释器，SDL 通道依赖 pygame 自带的 SDL2.dll）：
    .venv\\Scripts\\python.exe scripts/hidhide_ab_test.py --list   # 只看当前可见性与设备组
    .venv\\Scripts\\python.exe scripts/hidhide_ab_test.py          # 只读对比，不改驱动配置
    .venv\\Scripts\\python.exe scripts/hidhide_ab_test.py --apply  # 真正开启隐身并对比，结束必定恢复
    .venv\\Scripts\\python.exe scripts/hidhide_ab_test.py --apply --group KEY  # 指定要隐藏的设备组

安全：脚本结束时把白名单/黑名单/总开关恢复成开始时的样子；Ctrl+C 也会走恢复。
      探针临时登记进白名单的解释器路径在同一个子进程里撤销，不落残留。
前提：接上要测试的手柄；HidHide 的控制设备同一时刻只允许一个客户端，跑之前先关掉设备隐身配置面板。
"""

from __future__ import annotations

import argparse
import atexit
import ctypes as C
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes as W
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')

CHANNELS = ('sdl_dinput', 'sdl_raw', 'xinput')
XINPUT_DLLS = ('xinput1_4.dll', 'xinput1_3.dll', 'xinput9_1_0.dll')
XINPUT_CONTAINER_CLASSES = ('xnacomposite', 'xboxcomposite')


# ──────────────────────────────────────────────────────────────────
# 探针：在独立进程里枚举，输出 JSON
# ──────────────────────────────────────────────────────────────────
def probe_channel(channel: str) -> dict:
    result = {'channel': channel, 'devices': [], 'error': ''}
    if channel == 'xinput':
        return probe_xinput(result)
    return probe_sdl(result, rawinput=(channel == 'sdl_raw'))


def sdl_library():
    """pygame 自带的那颗 SDL2.dll —— 和 device.load_sdl_library() 同一颗。

    未登记的那一路用的是 base 解释器，它没有 pygame，所以退化成按路径加载同一颗 dll。
    """
    try:
        import pygame
        dll = Path(pygame.__file__).resolve().parent / 'SDL2.dll'
    except Exception:
        candidates = [Path(sys.prefix) / 'Lib' / 'site-packages' / 'pygame' / 'SDL2.dll',
                      ROOT / '.venv' / 'Lib' / 'site-packages' / 'pygame' / 'SDL2.dll']
        if sys.platform != 'win32' or not any(item.exists() for item in candidates):
            raise
        dll = next(item for item in candidates if item.exists())
    if sys.platform == 'win32':
        return C.CDLL(str(dll))
    library = C.CDLL(str(Path(pygame.base.__file__).resolve()))
    if not hasattr(library, 'SDL_Init'):
        raise RuntimeError('pygame 的 SDL2 库未公开控制器接口')
    return library


def probe_sdl(result: dict, rawinput: bool) -> dict:
    try:
        sdl = sdl_library()
    except Exception as exc:
        result['error'] = (f'SDL 加载失败：{exc} —— 请用项目虚拟环境的解释器运行本脚本'
                           f'（当前：{sys.executable}）')
        return result
    sdl.SDL_SetHint(b'SDL_JOYSTICK_RAWINPUT', b'1' if rawinput else b'0')
    sdl.SDL_SetHint(b'SDL_JOYSTICK_HIDAPI', b'1')
    if sdl.SDL_Init(0x2200) != 0:
        result['error'] = f'SDL_Init 失败：{sdl.SDL_GetError().decode(errors="replace")}'
        return result
    try:
        count = sdl.SDL_NumJoysticks()
        sdl.SDL_JoystickNameForIndex.restype = C.c_char_p
        sdl.SDL_JoystickOpen.restype = C.c_void_p
        sdl.SDL_JoystickOpen.argtypes = [C.c_int]
        sdl.SDL_JoystickClose.argtypes = [C.c_void_p]
        sdl.SDL_JoystickGetVendor.restype = C.c_uint16
        sdl.SDL_JoystickGetProduct.restype = C.c_uint16
        # 句柄是 64 位指针，不声明 argtypes 会被 ctypes 当 32 位 int 转换而溢出
        sdl.SDL_JoystickGetVendor.argtypes = [C.c_void_p]
        sdl.SDL_JoystickGetProduct.argtypes = [C.c_void_p]
        getter = getattr(sdl, 'SDL_JoystickPathForIndex', None)
        if getter is not None:
            getter.restype = C.c_char_p
        for index in range(count):
            name = sdl.SDL_JoystickNameForIndex(index) or b''
            handle = sdl.SDL_JoystickOpen(index)
            entry = {
                'index': index,
                'name': name.decode(errors='replace'),
                'game_controller': bool(sdl.SDL_IsGameController(index)),
                'open_ok': handle is not None,
            }
            if handle:
                entry['vendor'] = sdl.SDL_JoystickGetVendor(handle)
                entry['product'] = sdl.SDL_JoystickGetProduct(handle)
                sdl.SDL_JoystickClose(handle)
            if getter is not None and entry['open_ok']:
                path = getter(index)
                entry['path'] = (path or b'').decode(errors='replace')
            result['devices'].append(entry)
    finally:
        sdl.SDL_Quit()
    return result


def probe_xinput(result: dict) -> dict:
    library = None
    for name in XINPUT_DLLS:
        try:
            library = C.WinDLL(name)
            result['channel'] = f'xinput ({name})'
            break
        except OSError:
            continue
    if library is None:
        result['error'] = '未找到 XInput DLL'
        return result

    class State(C.Structure):
        _fields_ = [('wPacketNumber', W.WORD),
                    ('dwButtons', C.c_uint32),
                    ('bLeftTrigger', C.c_uint8), ('bRightTrigger', C.c_uint8),
                    ('sThumbLX', C.c_int16), ('sThumbLY', C.c_int16),
                    ('sThumbRX', C.c_int16), ('sThumbRY', C.c_int16)]

    library.XInputGetState.restype = C.c_uint
    library.XInputGetState.argtypes = [C.c_uint, C.POINTER(State)]
    for user in range(4):
        state = State()
        code = library.XInputGetState(user, C.byref(state))
        result['devices'].append({'index': user, 'connected': code == 0, 'code': code})
    return result


def run_probe(register_self: bool) -> dict:
    """子进程入口：可选地先把自身登记进白名单，模拟「被授权的程序」，然后撤销。

    登记必须在本进程内闭环撤销，否则一次 --list 就会把解释器永久留在白名单里。
    """
    from gamepadstudio import hidhide

    nt_path = hidhide.dos_to_nt_path(sys.executable) or sys.executable
    registered = False
    state = hidhide.read_state()
    if register_self and not state.inverse:
        if not any(item.upper() == nt_path.upper() for item in state.whitelist):
            registered = hidhide.transact(
                lambda current: hidhide.Change(whitelist=tuple(list(current.whitelist) + [nt_path]))).ok

    def measure():
        current = hidhide.read_state()
        return {
            'probe_exe': sys.executable,
            'probe_nt_path': nt_path,
            'whitelisted': any(item.upper() == nt_path.upper() for item in current.whitelist),
            'register_requested': register_self,
            'register_ok': registered,
            'driver': {'loaded': current.loaded, 'active': current.active, 'inverse': current.inverse,
                       'blacklist': list(current.blacklist), 'whitelist_count': len(current.whitelist)},
            'channels': [probe_channel(channel) for channel in CHANNELS],
        }

    try:
        payload = measure()
    finally:
        if registered:
            hidhide.transact(lambda current: hidhide.Change(
                whitelist=tuple(item for item in current.whitelist if item.upper() != nt_path.upper())))

    print('PROBE_JSON' + json.dumps(payload, ensure_ascii=False))
    return payload


# ──────────────────────────────────────────────────────────────────
# 编排：基线 → 隐藏 → 对比 → 恢复
# ──────────────────────────────────────────────────────────────────
def ordinary_exe(override: str = '') -> tuple:
    """造一个「永远不会被自我登记覆盖」的探针程序，返回 (程序路径, 需清理的临时文件)。

    self_app_paths() 在开发态会把 sys.executable、_base_executable 以及同目录的
    python.exe/pythonw.exe 全登记进白名单，所以换解释器逃不出登记范围 ——
    把 base 解释器复制成另一个文件名，才是真正未授权的游戏进程镜像。
    """
    if override:
        return override, ''
    base = str(getattr(sys, '_base_executable', '') or sys.executable or '')
    source = Path(base)
    if not base or not source.is_file():
        return '', ''
    clone = source.with_name('hidhide_padprobe.exe')
    try:
        if not clone.exists():
            shutil.copy2(source, clone)
    except OSError as exc:
        print(f'无法在 {source.parent} 复制出探针程序（{exc}）—— 请用 --ordinary-exe 指定一个未登记的程序。')
        return '', ''
    return str(clone), str(clone)


def _drop_clone(path: str) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def xinput_backed(group) -> bool:
    """目标的 XInput 通道是否只能按在线槽位判断。

    XnaComposite/XboxComposite 容器（360 接收器、Xbox 手柄）本身就是 XInput 节点；
    蓝牙/纯 HID 目标则根本没有槽位，别的设备占着 slot0 也不该计入它的结论。
    """
    if group.base_class.lower() in XINPUT_CONTAINER_CLASSES:
        return True
    return any(item.xinput_instance_id for item in group.interfaces)


def online_slots(obs: dict) -> list:
    for item in obs.values():
        if item.get('xinput'):
            return sorted(item['items'])
    return []


def attributable_by_slot_parity(before: dict, before_allowed: dict) -> bool:
    """槽位归因：基线里两路探针看到的在线槽位一致，说明槽位没因白名单差异预先分裂，
    于是隐藏后「未授权少、已授权不少」只能是被隐藏的目标造成的。

    光看设备类做不到这点 —— 蓝牙 LE 手柄经微软 XInput 兼容驱动同时提供 HID 面和槽位面，
    按类判定会把真实生效的 XInput 隐藏漏报成「不计入结论」。
    """
    slots = online_slots(before_allowed)
    return bool(slots) and online_slots(before) == slots


def child_probe(register_self: bool, exe: str = '') -> dict:
    command = [exe or sys.executable, str(Path(__file__).resolve()), '--probe']
    if register_self:
        command.append('--register-self')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if sys.platform == 'win32' else 0
    done = subprocess.run(command, capture_output=True, text=True, errors='replace', creationflags=flags)
    for line in (done.stdout or '').splitlines():
        if line.startswith('PROBE_JSON'):
            return json.loads(line[len('PROBE_JSON'):])
    tail = '\n'.join((done.stderr or done.stdout or '探针无输出').strip().splitlines()[-8:])
    return {'error': tail[:900]}


def channel_devices(channel: dict) -> list:
    if channel['channel'].startswith('xinput'):
        return [f'slot{item["index"]}' for item in channel['devices'] if item['connected']]
    return [f'{item["name"]} [{item["vendor"]:04X}:{item["product"]:04X}]' if item.get('vendor')
            else (item['name'] or f'index{item["index"]}')
            for item in channel['devices']]


def visible_devices(probe: dict) -> dict:
    """每个通道返回「看得见的东西」，用于前后对比。"""
    return {channel['channel']: (f'ERROR {channel["error"]}' if channel.get('error')
                                 else channel_devices(channel))
            for channel in probe.get('channels', [])}


def target_matches(device: str, group: dict) -> bool:
    vid_pid = group.get('vid_pid') or ''
    name = (group.get('name') or '').lower()
    lowered = device.lower()
    return (vid_pid and vid_pid.lower() in lowered) or (name and name[:12] in lowered)


def observations(probe: dict, group: dict) -> dict:
    """逐通道判断目标设备是否可见。

    XInput 拿不到槽位到物理设备的映射：只有当目标本身就是 XnaComposite/XboxComposite
    （槽位的祖先节点）时，在线槽位才能算作「看得见目标」；否则槽位属于别的设备，
    把它计入结论会误判 —— 本机就同时插着 360 接收器和蓝牙 Series 手柄。
    """
    result = {}
    backed = bool(group.get('xinput_backed')) if group else False
    for channel in probe.get('channels', []):
        name = channel['channel']
        if channel.get('error'):
            result[name] = {'see_target': False, 'items': [], 'matches': [], 'error': channel['error']}
            continue
        items = channel_devices(channel)
        if name.startswith('xinput'):
            result[name] = {'see_target': backed and bool(items), 'items': items,
                            'matches': items if backed else [], 'xinput': True,
                            'note': '' if backed else '在线槽位不属于本目标（槽位无法映射到设备）'}
        else:
            hits = [item for item in items if target_matches(item, group)]
            result[name] = {'see_target': bool(hits), 'items': items, 'matches': hits}
    return result


def compare(before: dict, after: dict) -> dict:
    """隐藏前后的逐通道结论：目标从哪些通道消失、哪些通道还看得见。"""
    gone = [channel for channel, item in before.items()
            if item['see_target'] and not after.get(channel, {'see_target': False})['see_target']]
    still = [channel for channel, item in after.items() if item['see_target']]
    return {'gone': gone, 'still': still}


def show(label: str, probe: dict, group: dict = None, identity: str = '') -> dict:
    print(f'\n{label}')
    if probe.get('error') and not probe.get('channels'):
        print(f'  探针失败：{probe["error"]}')
        return {}
    summary = visible_devices(probe)
    obs = observations(probe, group) if group else {}
    print(f'  探针进程: {probe.get("probe_exe") or identity or "?"}   '
          f'已登记白名单: {probe.get("whitelisted")}   '
          f'驱动: loaded={probe.get("driver", {}).get("loaded")} '
          f'active={probe.get("driver", {}).get("active")} '
          f'黑名单={len(probe.get("driver", {}).get("blacklist", []))}')
    for channel, items in summary.items():
        if isinstance(items, str):
            print(f'  {channel:22} {items}')
            continue
        seen = ''
        if channel in obs:
            if obs[channel].get('note'):
                seen = f'  ← {obs[channel]["note"]}'
            elif obs[channel]['see_target']:
                seen = f'  ← 目标 {"+".join(obs[channel]["matches"])}'
            else:
                seen = '  ← 目标不可见'
        print(f'  {channel:22} {len(items)} 个: ' + (' | '.join(items) if items else '(无)') + seen)
    return obs




def main() -> int:
    parser = argparse.ArgumentParser(description='HidHide 开关前后的控制器可见性对比')
    parser.add_argument('--probe', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--register-self', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--list', action='store_true', help='只列出设备组与当前可见性')
    parser.add_argument('--apply', action='store_true', help='真正开启隐身做对比（结束会恢复）')
    parser.add_argument('--group', default='', help='要隐藏的设备组 key，缺省取唯一的游戏设备组')
    parser.add_argument('--wait', type=int, default=0,
                        help='等待游戏设备组出现的秒数（无线手柄会休眠，按 Xbox 键唤醒后再跑）')
    parser.add_argument('--ordinary-exe', default='',
                        help='充当「未授权游戏进程」的解释器，缺省用 venv 的 base python')
    args = parser.parse_args()

    if args.probe:
        run_probe(args.register_self)
        return 0

    if sys.platform != 'win32':
        print('本脚本只在 Windows 上有意义。')
        return 2

    from gamepadstudio import hidhide, hidhide_devices
    from gamepadstudio.hidhide_control import HidHideController

    info = hidhide.driver_info()
    if not info.loaded:
        print(f'驱动不可用：installed={info.installed} {info.error}')
        return 2

    controller = HidHideController()
    ordinary, clone = ordinary_exe(args.ordinary_exe)
    if not ordinary:
        print('没有「未被自我登记覆盖」的探针程序可用：用项目 venv 的解释器运行，'
              '或用 --ordinary-exe 指定一个不在白名单里的程序。')
        return 2
    atexit.register(_drop_clone, clone)
    print(f'未授权视角探针 = {ordinary}\n已授权视角探针 = {sys.executable}')
    baseline_state = hidhide.read_state()
    if baseline_state.occupied:
        print(f'配置被占用：{baseline_state.error}')
        return 2
    baseline = {'active': baseline_state.active, 'inverse': baseline_state.inverse,
                'whitelist': list(baseline_state.whitelist), 'blacklist': list(baseline_state.blacklist)}
    print(f'基线：active={baseline["active"]} inverse={baseline["inverse"]} '
          f'白名单={len(baseline["whitelist"])} 黑名单={len(baseline["blacklist"])}')

    groups = hidhide_devices.enumerate_groups()
    if args.list:
        print('设备组（勾选整行即隐藏这一组的全部节点）：')
        for group in groups:
            if not group.connected:
                continue
            print(f'  {group.key}')
            print(f'      {group.name}  {group.vid_pid_text()}  游戏设备={group.gaming}  '
                  f'接口={len(group.interfaces)}  容器={group.base_container or "(无)"}  '
                  f'XInput={group.interfaces[0].xinput_instance_id or "(无)"}')
        show('当前可见性（未做任何修改）', child_probe(False, ordinary))
        show('当前可见性（探针先登记自己）', child_probe(True))
        return 0

    deadline = time.monotonic() + max(0, args.wait)
    while True:
        if args.group:
            match = next((group for group in groups if group.key == args.group), None)
            if match is not None and match.connected:
                chosen = match
                break
            missing = f'{args.group} 当前未连接' if match is not None else f'找不到设备组 {args.group}'
        else:
            gaming = [group for group in groups if group.connected and group.gaming]
            if len(gaming) == 1:
                chosen = gaming[0]
                break
            if len(gaming) > 1:
                print(f'需要明确指定 --group：当前已连接的游戏设备组有 {len(gaming)} 个。')
                for group in gaming:
                    print(f'  {group.key}  {group.name}  容器类={group.base_class}')
                return 2
            missing = '没有已连接的游戏设备组'
        if time.monotonic() >= deadline:
            print(f'{missing} —— 用 --list 查看设备组，或加 --wait 给出手柄唤醒时间。')
            return 2
        print(f'等待目标（{missing}），还剩约 {int(deadline - time.monotonic())} 秒 —— 按手柄的 Xbox 键唤醒…')
        time.sleep(2)
        groups = hidhide_devices.enumerate_groups()

    group_payload = {'key': chosen.key, 'name': chosen.name, 'vid_pid': chosen.vid_pid_text(),
                     'xinput_backed': xinput_backed(chosen)}
    print(f'\n目标设备：{chosen.name}  {chosen.vid_pid_text()}  '
          f'XInput 通道适用={"是" if group_payload["xinput_backed"] else "否（槽位属于别的设备）"}')
    if baseline['inverse']:
        print('注意：白名单处于「反选」模式，脚本按白名单语义做的对比不成立。')

    # 无线手柄会在「枚举到」和「探针真的读得到」之间掉线，所以基线要等到看得见为止
    wait_until = time.monotonic() + max(0, args.wait)
    while True:
        raw_before = child_probe(False, ordinary)
        raw_allowed = child_probe(True)
        before = observations(raw_before, group_payload)
        if not before or not observations(raw_allowed, group_payload):
            show('① 隐身开启前（普通进程视角）', raw_before, group_payload)
            print('\n探针未能枚举任何通道，先解决上面的错误再重跑。')
            return 7
        if any(item['see_target'] for item in before.values() if not item.get('xinput')):
            break
        if time.monotonic() >= wait_until:
            show('① 隐身开启前（普通进程视角）', raw_before, group_payload)
            print('\n警告：普通进程在基线状态下就没看到目标设备（HID/DirectInput 通道），后面的对比无意义。'
                  f'\n  当前基线已是 active={baseline["active"]} 黑名单={len(baseline["blacklist"])} 个 —— '
                  '若手柄已被别人隐藏，请先恢复；否则确认手柄已连接并保持唤醒，用 --wait 给足唤醒时间。')
            return 3
        print(f'  普通进程还看不到目标（手柄掉线？）—— 继续等待，'
              f'还剩约 {int(wait_until - time.monotonic())} 秒，可按手柄的 Xbox 键保持唤醒…')
        time.sleep(2)

    before = observations(raw_before, group_payload)
    before_allowed = observations(raw_allowed, group_payload)
    if not group_payload['xinput_backed'] and attributable_by_slot_parity(before, before_allowed):
        group_payload['xinput_backed'] = True
        before = observations(raw_before, group_payload)
        before_allowed = observations(raw_allowed, group_payload)
        print('\n  槽位归因生效：基线两路看到的在线槽位一致，隐藏后的槽位差计入结论。')
    show('① 隐身开启前（普通进程视角）', raw_before, group_payload)
    show('② 隐身开启前（已登记进程视角）', raw_allowed, group_payload)

    restore_needed = False
    exit_code = 0
    try:
        if args.apply:
            paths = hidhide_devices.hide_paths_for(
                chosen, tuple(item.instance_id for item in chosen.interfaces))
            print(f'\n将写入黑名单的实例路径：\n' + '\n'.join(f'  {p}' for p in paths))
            result = controller.set_paths_hidden(paths, True)
            if not result['ok']:
                print(f'隐藏失败：{result["message"]}')
                return 4
            restore_needed = True
            enabled = controller.set_enabled(True)
            if not enabled['ok']:
                print(f'总开关失败：{enabled["message"]}')
                return 4

            # 只有「已登记探针确实看得到手柄」的那一轮才作数：它同时证明手柄当时是醒着的，
            # 而同一轮的普通探针看不到 —— 才是隐藏生效，而不是手柄睡着了。
            wait_until = time.monotonic() + max(0, args.wait)
            while True:
                raw_after = child_probe(False, ordinary)
                raw_after_allowed = child_probe(True)
                after = observations(raw_after, group_payload)
                after_allowed = observations(raw_after_allowed, group_payload)
                if not after or not after_allowed:
                    show('③ 隐身开启后（普通进程视角）', raw_after, group_payload)
                    print('  隐藏后的探针没能枚举任何通道，结论不成立。')
                    return 7
                if any(item['see_target'] for item in after_allowed.values()):
                    break
                if time.monotonic() >= wait_until:
                    print('  已登记探针始终看不到手柄（掉线或白名单失效），无法判定。')
                    break
                print(f'  已登记探针还没看到手柄，重测一轮，还剩约 '
                      f'{int(wait_until - time.monotonic())} 秒…')
                time.sleep(2)

            show('③ 隐身开启后（普通进程视角）', raw_after, group_payload)
            show('④ 隐身开启后（已登记进程视角）', raw_after_allowed, group_payload)

            gone_and_still = compare(before, after)
            print('\n结论（隐藏后）：')
            print(f'  普通进程已看不到的通道：{gone_and_still["gone"] or "无"}')
            print(f'  普通进程仍能看到的通道：{gone_and_still["still"] or "无（目标已彻底隐藏）"}')
            for channel in ('sdl_dinput', 'sdl_raw'):
                if channel in before and channel in after:
                    print(f'  {channel:22} {len(before[channel]["items"])} 个 → {len(after[channel]["items"])} 个')
            x_before, x_after = online_slots(before), online_slots(after)
            x_allowed = online_slots(after_allowed)
            if x_before or x_after or x_allowed:
                role = '计入结论' if group_payload['xinput_backed'] else '仅供参考，不计入结论'
                print(f'  XInput 在线槽位          基线 {x_before or "(无)"} → 未授权 {x_after or "(无)"}'
                      f' / 同轮已授权 {x_allowed or "(无)"}   （{role}）')
            allowed_visible = any(item['see_target'] for item in after_allowed.values())
            print(f'  已登记进程仍能看到目标：{"是" if allowed_visible else "否（白名单没起作用）"}')
            if gone_and_still['still'] or not allowed_visible:
                exit_code = 5
        else:
            print('\n未加 --apply：只做基线测量，不改动驱动配置。')
    finally:
        if restore_needed:
            print('\n恢复基线配置…')
            hidhide.transact(lambda state: hidhide.Change(whitelist=tuple(baseline['whitelist']),
                                                          blacklist=tuple(baseline['blacklist']),
                                                          active=baseline['active']))
            wait_until = time.monotonic() + max(0, args.wait)
            while True:
                raw_restored = child_probe(False, ordinary)
                restored = observations(raw_restored, group_payload)
                if restored and any(item['see_target'] for item in restored.values()):
                    break
                if time.monotonic() >= wait_until:
                    break
                print('  普通进程还没重新看到手柄，重测一轮…')
                time.sleep(2)
            restored = show('⑤ 恢复后（普通进程视角）', raw_restored, group_payload)
            final = hidhide.read_state()
            same = (final.active == baseline['active'] and
                    sorted(item.upper() for item in final.whitelist) == sorted(item.upper() for item in baseline['whitelist']) and
                    sorted(item.upper() for item in final.blacklist) == sorted(item.upper() for item in baseline['blacklist']))
            print(f'  配置回到基线：{"是" if same else "否"}\n    白名单={len(final.whitelist)}/'
                  f'{len(baseline["whitelist"])} 黑名单={len(final.blacklist)}/{len(baseline["blacklist"])} '
                  f'active={final.active}/{baseline["active"]}')
            back = any(item['see_target'] for item in restored.values())
            print(f'  目标设备重新可见：{"是" if back else "否（手柄可能又睡了，看配置那一行判断）"}')
            if not same:
                exit_code = 6

    print(f'\n退出码 {exit_code}（0=通过）')
    return exit_code




if __name__ == '__main__':
    raise SystemExit(main())
