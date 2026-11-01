"""Unified Virtual XInput (Xbox 360 / XUSB_REPORT) emitter via ViGEmBus (ViGEmClient.dll).

Provides:
- `XUSB_REPORT` 12-byte structure and `XUSB_BUTTON_MASKS` mapping canonical GamePad Studio
  slots ('0'..'14', 'LT', 'RT', 'LS:*', 'RS:*') to standard XInput bitmasks and axes.
- `compose_xusb_report(buttons, axes, trigger_overrides)` for deterministic frame synthesis
  across L1 (DInput unused-button sequences) and L2 (gesture rewrite streams).
- `VirtualXInputPad` wrapping `ViGEmClient.dll` with bidirectional rumble notification
  pass-through (`vigem_target_x360_register_notification`) and Share-button ('15') bridging.
"""
from __future__ import annotations

import ctypes as C
import os
import socket
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

VIGEM_ERROR_NONE = 0x20000000

# Canonical GamePad Studio button slot -> XUSB_GAMEPAD_* bitmask (16-bit wButtons)
XUSB_BUTTON_MASKS: dict[str, int] = {
    '0': 0x1000,   # A (South)
    '1': 0x2000,   # B (East)
    '2': 0x4000,   # X (West)
    '3': 0x8000,   # Y (North)
    '4': 0x0020,   # Back / View
    '5': 0x0400,   # Guide / Xbox
    '6': 0x0010,   # Start / Menu
    '7': 0x0040,   # L3 / Left Thumb
    '8': 0x0080,   # R3 / Right Thumb
    '9': 0x0100,   # LB / Left Shoulder
    '10': 0x0200,  # RB / Right Shoulder
    '11': 0x0001,  # D-Pad Up
    '12': 0x0002,  # D-Pad Down
    '13': 0x0004,  # D-Pad Left
    '14': 0x0008,  # D-Pad Right
}

BUTTON_ALIASES: dict[str, str] = {
    'A': '0', 'B': '1', 'X': '2', 'Y': '3',
    'BACK': '4', 'VIEW': '4', 'SELECT': '4',
    'GUIDE': '5', 'HOME': '5', 'XBOX': '5',
    'START': '6', 'MENU': '6',
    'L3': '7', 'LS': '7', 'R3': '8', 'RS': '8',
    'LB': '9', 'L1': '9', 'RB': '10', 'R1': '10',
    'UP': '11', 'DOWN': '12', 'LEFT': '13', 'RIGHT': '14',
    'SHARE': '15', 'CAPTURE': '15',
    'L2': 'LT', 'R2': 'RT',
}


class XUSB_REPORT(C.Structure):
    _fields_ = [
        ('wButtons', C.c_ushort),
        ('bLeftTrigger', C.c_ubyte),
        ('bRightTrigger', C.c_ubyte),
        ('sThumbLX', C.c_short),
        ('sThumbLY', C.c_short),
        ('sThumbRX', C.c_short),
        ('sThumbRY', C.c_short),
    ]


class HID_GAMEPAD_REPORT(C.Structure):
    """Standard 11-byte USB/DriverKit HID Gamepad Report for macOS (`IOHIDUserDevice` / VirtualHID)."""
    _fields_ = [
        ('reportId', C.c_ubyte),
        ('buttonsLo', C.c_ubyte),
        ('buttonsHi', C.c_ubyte),
        ('hat', C.c_ubyte),
        ('lx', C.c_ubyte),
        ('ly', C.c_ubyte),
        ('rx', C.c_ubyte),
        ('ry', C.c_ubyte),
        ('lt', C.c_ubyte),
        ('rt', C.c_ubyte),
        ('aux', C.c_ubyte),
    ]


# D-Pad (11=Up, 12=Down, 13=Left, 14=Right) -> standard 8-direction HID Hat value (0..7, 8=neutral)
_DPAD_TO_HAT: dict[tuple[bool, bool, bool, bool], int] = {
    (True, False, False, False): 0,   # N
    (True, False, False, True): 1,    # NE
    (False, False, False, True): 2,   # E
    (False, True, False, True): 3,    # SE
    (False, True, False, False): 4,   # S
    (False, True, True, False): 5,    # SW
    (False, False, True, False): 6,   # W
    (True, False, True, False): 7,    # NW
}


def _clamp_short(val: float) -> int:
    v = int(round(max(-1.0, min(1.0, float(val))) * 32767.0))
    return max(-32768, min(32767, v))


def _clamp_byte(val: float) -> int:
    v = int(round(max(0.0, min(1.0, float(val))) * 255.0))
    return max(0, min(255, v))


def normalize_xinput_token(token: Any) -> str:
    raw = str(token).strip()
    return BUTTON_ALIASES.get(raw.upper(), raw)


def compose_xusb_report(
    buttons: Iterable[Any] = (),
    axes: Iterable[float] = (),
    trigger_overrides: Optional[dict[str, float]] = None,
) -> tuple[XUSB_REPORT, dict[str, Any]]:
    """Synthesize a 12-byte `XUSB_REPORT` and state snapshot from canonical buttons and axes.

    Args:
        buttons: Iterable of active canonical button tokens ('0'..'15', 'LT', 'RT', 'LS:up', etc.).
        axes: Up to 6 normalized floats `[lx, ly, rx, ry, lt, rt]` where sticks are `[-1.0..1.0]`
              (SDL convention: Y < 0 is up) and triggers are `[0.0..1.0]`.
        trigger_overrides: Optional `{'LT': float, 'RT': float}` overrides in `[0.0..1.0]`.
    """
    axis_list = [float(a) for a in (axes or ())]
    while len(axis_list) < 6:
        axis_list.append(0.0)

    lx, ly, rx, ry, lt, rt = axis_list[:6]
    if trigger_overrides:
        if 'LT' in trigger_overrides:
            lt = max(lt, float(trigger_overrides['LT']))
        if 'RT' in trigger_overrides:
            rt = max(rt, float(trigger_overrides['RT']))

    mask = 0
    share_pressed = False
    for raw_tok in (buttons or ()):
        tok = normalize_xinput_token(raw_tok)
        if tok in XUSB_BUTTON_MASKS:
            mask |= XUSB_BUTTON_MASKS[tok]
        elif tok == 'LT':
            lt = 1.0
        elif tok == 'RT':
            rt = 1.0
        elif tok == '15':
            share_pressed = True
        elif tok == 'LS:up':
            ly = -1.0
        elif tok == 'LS:down':
            ly = 1.0
        elif tok == 'LS:left':
            lx = -1.0
        elif tok == 'LS:right':
            lx = 1.0
        elif tok == 'RS:up':
            ry = -1.0
        elif tok == 'RS:down':
            ry = 1.0
        elif tok == 'RS:left':
            rx = -1.0
        elif tok == 'RS:right':
            rx = 1.0

    # XUSB_REPORT thumb Y axis uses +32767 for UP (opposite of SDL's -1.0 for UP)
    thumb_lx = _clamp_short(lx)
    thumb_ly = _clamp_short(-ly)
    thumb_rx = _clamp_short(rx)
    thumb_ry = _clamp_short(-ry)
    left_trig = _clamp_byte(lt)
    right_trig = _clamp_byte(rt)

    report = XUSB_REPORT(
        wButtons=mask,
        bLeftTrigger=left_trig,
        bRightTrigger=right_trig,
        sThumbLX=thumb_lx,
        sThumbLY=thumb_ly,
        sThumbRX=thumb_rx,
        sThumbRY=thumb_ry,
    )
    snapshot = {
        'wButtons': mask,
        'bLeftTrigger': left_trig,
        'bRightTrigger': right_trig,
        'sThumbLX': thumb_lx,
        'sThumbLY': thumb_ly,
        'sThumbRX': thumb_rx,
        'sThumbRY': thumb_ry,
        'share_pressed': share_pressed,
    }
    return report, snapshot


def compose_hid_gamepad_report(
    buttons: Iterable[Any] = (),
    axes: Iterable[float] = (),
    trigger_overrides: Optional[dict[str, float]] = None,
) -> tuple[HID_GAMEPAD_REPORT, dict[str, Any]]:
    """Synthesize an 11-byte standard USB/DriverKit `HID_GAMEPAD_REPORT` for macOS parity."""
    _, xusb_snap = compose_xusb_report(buttons, axes, trigger_overrides)
    wmask = xusb_snap['wButtons']
    up = bool(wmask & XUSB_BUTTON_MASKS['11'])
    down = bool(wmask & XUSB_BUTTON_MASKS['12'])
    left = bool(wmask & XUSB_BUTTON_MASKS['13'])
    right = bool(wmask & XUSB_BUTTON_MASKS['14'])
    hat = _DPAD_TO_HAT.get((up, down, left, right), 8)

    # Map canonical slots 0..10 to 16-bit HID button bits
    btn_bits = 0
    norm_tokens = {normalize_xinput_token(b) for b in (buttons or ())}
    for slot in range(11):
        if str(slot) in norm_tokens:
            btn_bits |= (1 << slot)

    # Convert signed [-32768..32767] XUSB thumb axes to unsigned [0..255] HID axes (128 = center, Y down = 255)
    lx_u8 = max(0, min(255, int(round((xusb_snap['sThumbLX'] / 32767.0 + 1.0) * 127.5))))
    ly_u8 = max(0, min(255, int(round((-xusb_snap['sThumbLY'] / 32767.0 + 1.0) * 127.5))))
    rx_u8 = max(0, min(255, int(round((xusb_snap['sThumbRX'] / 32767.0 + 1.0) * 127.5))))
    ry_u8 = max(0, min(255, int(round((-xusb_snap['sThumbRY'] / 32767.0 + 1.0) * 127.5))))

    hid_report = HID_GAMEPAD_REPORT(
        reportId=1,
        buttonsLo=btn_bits & 0xFF,
        buttonsHi=(btn_bits >> 8) & 0xFF,
        hat=hat,
        lx=lx_u8,
        ly=ly_u8,
        rx=rx_u8,
        ry=ry_u8,
        lt=xusb_snap['bLeftTrigger'],
        rt=xusb_snap['bRightTrigger'],
        aux=1 if xusb_snap['share_pressed'] else 0,
    )
    hid_snap = {
        'reportId': 1,
        'buttons': btn_bits,
        'hat': hat,
        'lx': lx_u8,
        'ly': ly_u8,
        'rx': rx_u8,
        'ry': ry_u8,
        'lt': xusb_snap['bLeftTrigger'],
        'rt': xusb_snap['bRightTrigger'],
        'share_pressed': xusb_snap['share_pressed'],
    }
    return hid_report, hid_snap


_SLOT_TO_SDL_NAME: dict[str, str] = {
    '0': 'a', '1': 'b', '2': 'x', '3': 'y',
    '4': 'back', '5': 'guide', '6': 'start',
    '7': 'leftstick', '8': 'rightstick',
    '9': 'leftshoulder', '10': 'rightshoulder',
    '11': 'dpup', '12': 'dpdown', '13': 'dpleft', '14': 'dpright',
    '15': 'misc1',
}


def export_sdl_gamecontroller_config(
    guid: str,
    name: str,
    sdl_mapping: Optional[str] = None,
    mappings: Optional[dict[str, Any]] = None,
    platform: Optional[str] = None,
) -> str:
    """Export an `SDL_GAMECONTROLLERCONFIG` line combining L1 `SDL_GameControllerDB` and L2 button remaps.

    Provides zero-latency native controller parity for SDL2 / Steam / CrossOver / Wine games on macOS and Windows.
    """
    plat = platform or ('Mac OS X' if sys.platform == 'darwin' else 'Windows')
    entries: dict[str, str] = {
        'a': 'b0', 'b': 'b1', 'x': 'b2', 'y': 'b3',
        'back': 'b4', 'guide': 'b5', 'start': 'b6',
        'leftstick': 'b7', 'rightstick': 'b8',
        'leftshoulder': 'b9', 'rightshoulder': 'b10',
        'dpup': 'b11', 'dpdown': 'b12', 'dpleft': 'b13', 'dpright': 'b14',
        'leftx': 'a0', 'lefty': 'a1', 'rightx': 'a2', 'righty': 'a3',
        'lefttrigger': 'a4', 'righttrigger': 'a5',
    }
    if sdl_mapping:
        for token in sdl_mapping.split(','):
            if ':' in token:
                k, v = token.split(':', 1)
                k, v = k.strip(), v.strip()
                if k and k != 'platform' and v:
                    entries[k] = v

    # Apply single-button L2 `gamepad_button` remaps: if physical slot `src` is remapped to target `dst`,
    # bind SDL logical button `dst` to the physical source of `src`!
    if mappings:
        base_entries = dict(entries)
        for src_trigger, spec in mappings.items():
            if not isinstance(spec, dict) or '+' in str(src_trigger):
                continue
            short_act = spec.get('short') if isinstance(spec.get('short'), dict) else {}
            if short_act.get('action') == 'gamepad_button':
                dst_slot = normalize_xinput_token(short_act.get('value', ''))
                dst_sdl = _SLOT_TO_SDL_NAME.get(dst_slot)
                src_sdl = _SLOT_TO_SDL_NAME.get(str(src_trigger))
                if dst_sdl:
                    phys_token = base_entries.get(src_sdl, f'b{src_trigger}' if str(src_trigger).isdigit() else '')
                    if phys_token:
                        entries[dst_sdl] = phys_token

    safe_name = (name or 'GamePad Studio Virtual XInput').replace(',', ' ')
    safe_guid = (guid or '030000005e0400008e02000000000000').strip()
    body = ','.join(f'{k}:{v}' for k, v in entries.items())
    return f'{safe_guid},{safe_name},{body},platform:{plat},'


VIGEM_ERROR_NONE = 0x20000000

# VIGEM_ERRORS from the upstream include/ViGEm/Client.h, kept for diagnostics so a
# failed connect reports what the bus actually said instead of a bare hex number.
VIGEM_ERROR_NAMES: dict[int, str] = {
    0x20000000: 'NONE',
    0xE0000001: 'BUS_NOT_FOUND',
    0xE0000002: 'NO_FREE_SLOT',
    0xE0000003: 'INVALID_TARGET',
    0xE0000004: 'REMOVAL_FAILED',
    0xE0000005: 'ALREADY_CONNECTED',
    0xE0000006: 'TARGET_UNINITIALIZED',
    0xE0000007: 'TARGET_NOT_PLUGGED_IN',
    0xE0000008: 'BUS_VERSION_MISMATCH',
    0xE0000009: 'BUS_ACCESS_FAILED',
    0xE0000010: 'CALLBACK_ALREADY_REGISTERED',
    0xE0000011: 'CALLBACK_NOT_FOUND',
    0xE0000012: 'BUS_ALREADY_CONNECTED',
    0xE0000013: 'BUS_INVALID_HANDLE',
    0xE0000014: 'XUSB_USERINDEX_OUT_OF_RANGE',
    0xE0000015: 'INVALID_PARAMETER',
    0xE0000016: 'NOT_SUPPORTED',
    0xE0000017: 'WINAPI',
}

VIGEM_DLL_NAME = 'ViGEmClient.dll'

# Every candidate DLL is rejected unless it exposes all of these, so a 32-bit or
# partial build can never half-load and produce silent no-op frames later.
VIGEM_REQUIRED_EXPORTS: tuple[str, ...] = (
    'vigem_alloc', 'vigem_free', 'vigem_connect', 'vigem_disconnect',
    'vigem_target_x360_alloc', 'vigem_target_free',
    'vigem_target_add', 'vigem_target_remove', 'vigem_target_x360_update',
)

# Process-wide caches: a CDLL handle cannot be unloaded, and mapping-UI repaints ask
# the availability question many times per second.
_BACKEND_STATUS: Optional[dict[str, Any]] = None
_CLIENT_CACHE: dict[str, Any] = {}


def vigem_error_name(code: Any) -> str:
    try:
        raw = int(code) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return f'UNKNOWN({code!r})'
    return f'{VIGEM_ERROR_NAMES.get(raw, "UNKNOWN")}(0x{raw:08X})'


def _driver_bus_present() -> bool:
    """True when the ViGEmBus kernel driver is installed (its binary, not the client DLL)."""
    root = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    return (root / 'System32' / 'drivers' / 'ViGEmBus.sys').is_file()


def _client_dll_dirs() -> list[Path]:
    """Directories that may carry ViGEmClient.dll, best match first.

    `native/` is the copy vendored with the package (also where the PyInstaller spec
    lands it inside `_internal/gamepadstudio/native/`); the rest are user drop-ins.
    """
    here = Path(__file__).resolve().parent
    dirs = [here / 'native', here / 'bin', here]
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            base = Path(meipass)
            dirs += [base / 'gamepadstudio' / 'native', base / 'native', base]
        exe_dir = Path(sys.executable).resolve().parent
        dirs += [exe_dir / '_internal' / 'gamepadstudio' / 'native',
                 exe_dir / '_internal' / 'native', exe_dir]
    for env_key, fallback in (('ProgramFiles', r'C:\Program Files'),
                              ('ProgramFiles(x86)', r'C:\Program Files (x86)')):
        prog = os.environ.get(env_key, fallback)
        dirs.append(Path(prog) / 'Nefarius Software Solutions' / 'ViGEm Bus Driver')
    return dirs


def _site_package_dlls() -> list[Path]:
    """ViGEmClient.dll copies shipped by an installed `vgamepad`, preferring this bitness."""
    try:
        import vgamepad  # type: ignore
        root = Path(vgamepad.__file__).resolve().parent
    except Exception:
        return []
    hint = 'x64' if sys.maxsize > 2 ** 32 else 'x86'
    found = list(root.rglob(VIGEM_DLL_NAME))
    found.sort(key=lambda p: (0 if hint in str(p.parent).lower() else 1, len(p.parts), str(p)))
    return found


def client_dll_candidates() -> list[Path]:
    """Ordered, de-duplicated ViGEmClient.dll locations, honouring VIGEM_CLIENT_DLL."""
    candidates: list[Path] = []
    env_path = os.environ.get('VIGEM_CLIENT_DLL', '').strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates += [d / VIGEM_DLL_NAME for d in _client_dll_dirs()]
    candidates += _site_package_dlls()
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        try:
            key = str(path.resolve()).lower()
        except OSError:
            key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _find_vigem_dll() -> Optional[str]:
    if sys.platform != 'win32':
        return None
    for candidate in client_dll_candidates():
        if candidate.is_file():
            return str(candidate)
    return VIGEM_DLL_NAME


def _mac_socket_path() -> Optional[str]:
    sock_path = os.environ.get('GPS_MAC_VIRTUAL_PAD_SOCKET', '').strip()
    if sock_path and Path(sock_path).exists():
        return sock_path
    return None


def _bind_client_functions(lib: Any) -> None:
    """Declare argtypes/restypes for the entry points this module calls."""
    lib.vigem_alloc.argtypes = []
    lib.vigem_alloc.restype = C.c_void_p
    lib.vigem_free.argtypes = [C.c_void_p]
    lib.vigem_free.restype = None
    lib.vigem_connect.argtypes = [C.c_void_p]
    lib.vigem_connect.restype = C.c_uint32
    lib.vigem_disconnect.argtypes = [C.c_void_p]
    lib.vigem_disconnect.restype = None
    lib.vigem_target_x360_alloc.argtypes = []
    lib.vigem_target_x360_alloc.restype = C.c_void_p
    lib.vigem_target_free.argtypes = [C.c_void_p]
    lib.vigem_target_free.restype = None
    lib.vigem_target_add.argtypes = [C.c_void_p, C.c_void_p]
    lib.vigem_target_add.restype = C.c_uint32
    lib.vigem_target_remove.argtypes = [C.c_void_p, C.c_void_p]
    lib.vigem_target_remove.restype = C.c_uint32
    lib.vigem_target_x360_update.argtypes = [C.c_void_p, C.c_void_p, XUSB_REPORT]
    lib.vigem_target_x360_update.restype = C.c_uint32
    if hasattr(lib, 'vigem_target_x360_register_notification'):
        lib.vigem_target_x360_register_notification.argtypes = [
            C.c_void_p, C.c_void_p, _NOTIFICATION_CB_TYPE, C.c_void_p
        ]
        lib.vigem_target_x360_register_notification.restype = C.c_uint32


def _try_load_client(path: Path) -> tuple[Any, Optional[str]]:
    """Load one candidate DLL, verifying the required exports are callable bindings."""
    try:
        lib = C.CDLL(str(path))
    except OSError as exc:
        return None, f'{path}: {exc}'
    missing = [name for name in VIGEM_REQUIRED_EXPORTS if not hasattr(lib, name)]
    if missing:
        return None, f'{path}: missing exports {", ".join(missing)}'
    try:
        _bind_client_functions(lib)
    except Exception as exc:  # a stub/proxy lib (tests) may not allow argtypes assignment
        return None, f'{path}: binding failed: {exc}'
    return lib, None


def vigem_backend_status(force: bool = False) -> dict[str, Any]:
    """Probe the real virtual-gamepad backend, cached for the lifetime of the process.

    Presence of `ViGEmBus.sys` alone is not enough: the client library is a separate
    user-mode DLL that the driver package does not ship, so an honest answer needs a
    loadable DLL and (on Windows) a successful `vigem_connect` handshake. The connect
    probe only opens and closes a bus handle - it creates no virtual device.
    """
    global _BACKEND_STATUS

    sock_path = _mac_socket_path()
    if force or _BACKEND_STATUS is None:
        status: dict[str, Any] = {
            'platform': sys.platform,
            'available': False,
            'transport': None,
            'dll_path': None,
            'driver_installed': _driver_bus_present() if sys.platform == 'win32' else None,
            'connect_error': None,
            'reason': None,
            'candidates': [str(p) for p in client_dll_candidates() if p.is_file()],
        }
        if sock_path:
            status.update(available=True, transport='virtualhid-socket', reason=None)
        elif sys.platform == 'win32':
            lib: Any = None
            load_error = None
            for candidate in client_dll_candidates():
                if not candidate.is_file():
                    continue
                lib, load_error = _try_load_client(candidate)
                if lib is not None:
                    status['dll_path'] = str(candidate)
                    break
            if lib is None:
                status['transport'] = 'vigem'
                status['reason'] = ('client_dll_missing' if status['driver_installed']
                                    else 'driver_and_client_missing')
                status['load_error'] = load_error
            else:
                _CLIENT_CACHE['lib'] = lib
                err = None
                client = None
                try:
                    client = lib.vigem_alloc()
                    if not client:
                        err = 0xE0000013  # BUS_INVALID_HANDLE: alloc failed
                    else:
                        err = lib.vigem_connect(client)
                    if err == VIGEM_ERROR_NONE:
                        status.update(available=True, transport='vigem', connect_error=None)
                    else:
                        status['transport'] = 'vigem'
                        status['connect_error'] = vigem_error_name(err)
                        status['reason'] = 'connect_failed'
                except Exception as exc:
                    status['transport'] = 'vigem'
                    status['reason'] = 'connect_raised'
                    status['connect_error'] = f'{type(exc).__name__}: {exc}'
                finally:
                    try:
                        if client and err == VIGEM_ERROR_NONE:
                            lib.vigem_disconnect(client)
                        if client:
                            lib.vigem_free(client)
                    except Exception:
                        pass
        _BACKEND_STATUS = status
    return dict(_BACKEND_STATUS or {})


def virtual_gamepad_backend_available() -> bool:
    """Return True only if a virtual gamepad backend is actually usable right now.

    Windows needs both the ViGEmBus driver and a loadable ViGEmClient.dll that can
    connect; macOS needs the VirtualHID socket. Cached - pass force=True to re-probe.
    """
    return bool(vigem_backend_status()['available'])


def reset_vigem_backend_probe() -> None:
    """Forget the cached probe so the next check re-reads driver, DLL and bus state."""
    global _BACKEND_STATUS
    _BACKEND_STATUS = None
    _CLIENT_CACHE.clear()


_NOTIFICATION_CB_TYPE = C.CFUNCTYPE(
    None,
    C.c_void_p,  # client
    C.c_void_p,  # target
    C.c_ubyte,   # large_motor
    C.c_ubyte,   # small_motor
    C.c_ubyte,   # led_number
    C.c_void_p,  # user_data
)


class VirtualXInputPad:
    """Manages a unified Virtual XInput / HID controller across Windows (`ViGEmClient`) and macOS (`Darwin` IPC/Bridge)."""

    def __init__(
        self,
        lib: Any = None,
        on_share: Optional[Callable[[], None]] = None,
        socket_path: Optional[str] = None,
    ):
        self._lib = lib
        self.on_share = on_share
        self._socket_path = socket_path or os.environ.get('GPS_MAC_VIRTUAL_PAD_SOCKET', '').strip() or None
        self._sock: Optional[socket.socket] = None
        self._client = None
        self._target = None
        self._connected = False
        self._last_share_down = False
        self._rumble_callback: Optional[Callable[[float, float], None]] = None
        self._frame_callbacks: list[Callable[[dict[str, Any], dict[str, Any]], None]] = []
        self._c_notification_cb = None
        self.last_error: Optional[str] = None
        self.last_report: dict[str, Any] = compose_xusb_report()[1]
        self.last_hid_report: dict[str, Any] = compose_hid_gamepad_report()[1]

    def _load_library(self) -> Any:
        if self._lib is not None:
            return self._lib
        cached = _CLIENT_CACHE.get('lib')
        if cached is not None:
            self._lib = cached
            return cached
        if _CLIENT_CACHE.get('load_failed'):
            # Negative cache: without it every emitted frame (the poll loop runs at
            # ~125 Hz) would re-run the whole candidate list through LoadLibrary and
            # fail again for the rest of the session. reset_vigem_backend_probe() clears
            # this, so installing the DLL without restarting the app still works on the
            # next probe.
            self.last_error = _CLIENT_CACHE['load_failed']
            return None
        for candidate in client_dll_candidates():
            if not candidate.is_file():
                continue
            lib, error = _try_load_client(candidate)
            if lib is not None:
                _CLIENT_CACHE['lib'] = lib
                self._lib = lib
                self.last_error = None
                return lib
            self.last_error = error
        if self.last_error is None:
            self.last_error = ('ViGEmClient.dll not found in: ' +
                               ', '.join(str(p) for p in client_dll_candidates()[:3]))
        _CLIENT_CACHE['load_failed'] = self.last_error
        return None

    @property
    def is_connected(self) -> bool:
        return bool(self._connected and ((self._client and self._target) or self._sock is not None))

    def connect(self) -> bool:
        if self.is_connected:
            return True
        if self._lib is None and self._socket_path and Path(self._socket_path).exists():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                sock.connect(self._socket_path)
                self._sock = sock
                self._connected = True
                return True
            except Exception:
                self._sock = None
        lib = self._load_library()
        if lib is None:
            return False
        try:
            client = lib.vigem_alloc()
            if not client:
                self.last_error = 'vigem_alloc returned NULL (' + vigem_error_name(0xE0000013) + ')'
                return False
            err = lib.vigem_connect(client)
            if err != VIGEM_ERROR_NONE:
                self.last_error = 'vigem_connect: ' + vigem_error_name(err)
                lib.vigem_free(client)
                return False
            target = lib.vigem_target_x360_alloc()
            if not target:
                self.last_error = 'vigem_target_x360_alloc returned NULL'
                lib.vigem_disconnect(client)
                lib.vigem_free(client)
                return False
            err = lib.vigem_target_add(client, target)
            if err != VIGEM_ERROR_NONE:
                self.last_error = 'vigem_target_add: ' + vigem_error_name(err)
                lib.vigem_target_free(target)
                lib.vigem_disconnect(client)
                lib.vigem_free(client)
                return False
            self.last_error = None
            self._client = client
            self._target = target
            self._connected = True
            self._prime_neutral_report()
            if self._rumble_callback is not None:
                self._bind_notification()
            return True
        except Exception:
            self._connected = False
            return False

    def _prime_neutral_report(self) -> None:
        """Clear the phantom stick offset a freshly added ViGEm target exposes.

        A brand-new X360 target reports `LS=(-3356,-1869) RS=(-3255,-848)` to
        `XInputGetState` (~10% of full scale, identical on every machine and every
        connect), and ViGEmBus drops an update that equals the zero report it keeps
        internally, so the first all-neutral frame never lands. The offset therefore
        stays visible to the game until something *different* arrives - which reads
        as stick drift for as long as the app has no real input to send. Nudge one
        axis by 1 LSB (0.003% of range, below any deadzone) and then send the true
        neutral report, which takes the reported state to exactly zero.

        Both writes go straight to the driver rather than through `emit_frame`,
        because this is a transport quirk: a frame the engine never asked for must
        not show up in `last_report`, in the registered frame callbacks, or in the
        trigger log the player reads.
        """
        if self._lib is None or not self._client or not self._target:
            return
        neutral = XUSB_REPORT(wButtons=0, bLeftTrigger=0, bRightTrigger=0,
                              sThumbLX=0, sThumbLY=0, sThumbRX=0, sThumbRY=0)
        nudge = XUSB_REPORT(wButtons=0, bLeftTrigger=0, bRightTrigger=0,
                            sThumbLX=1, sThumbLY=0, sThumbRX=0, sThumbRY=0)
        try:
            if self._lib.vigem_target_x360_update(self._client, self._target, nudge) != VIGEM_ERROR_NONE:
                return
            self._lib.vigem_target_x360_update(self._client, self._target, neutral)
        except Exception:
            return

    def _bind_notification(self) -> bool:
        if not self.is_connected or not hasattr(self._lib, 'vigem_target_x360_register_notification'):
            return False

        def _on_notify(_client, _target, large_motor, small_motor, _led, _userdata):
            cb = self._rumble_callback
            if callable(cb):
                try:
                    cb(float(large_motor) / 255.0, float(small_motor) / 255.0)
                except Exception:
                    pass

        try:
            self._c_notification_cb = _NOTIFICATION_CB_TYPE(_on_notify)
            res = self._lib.vigem_target_x360_register_notification(
                self._client, self._target, self._c_notification_cb, None
            )
            return res == VIGEM_ERROR_NONE
        except Exception:
            return False

    def register_rumble_callback(self, callback: Optional[Callable[[float, float], None]]) -> bool:
        """Register a callback `callback(large_motor_0_to_1, small_motor_0_to_1)` for in-game rumble pass-through."""
        self._rumble_callback = callback
        if callback is not None and self.is_connected:
            return self._bind_notification()
        return True

    def register_frame_callback(self, callback: Callable[[dict[str, Any], dict[str, Any]], None]) -> None:
        """Register a cross-platform listener `callback(xusb_snapshot, hid_snapshot)` invoked on every emitted frame."""
        if callable(callback) and callback not in self._frame_callbacks:
            self._frame_callbacks.append(callback)

    def emit_frame(
        self,
        buttons: Iterable[Any] = (),
        axes: Iterable[float] = (),
        trigger_overrides: Optional[dict[str, float]] = None,
    ) -> dict[str, Any]:
        """Compose and transmit an `XUSB_REPORT` + `HID_GAMEPAD_REPORT` frame to the virtual controller."""
        report, snapshot = compose_xusb_report(buttons, axes, trigger_overrides)
        hid_report, hid_snapshot = compose_hid_gamepad_report(buttons, axes, trigger_overrides)
        self.last_report = snapshot
        self.last_hid_report = hid_snapshot

        # Bridge Share ('15') rising edge to capture callback since XUSB_REPORT has no bit 15
        share_now = bool(snapshot.get('share_pressed'))
        if share_now and not self._last_share_down and callable(self.on_share):
            try:
                self.on_share()
            except Exception:
                pass
        self._last_share_down = share_now

        for cb in list(self._frame_callbacks):
            try:
                cb(snapshot, hid_snapshot)
            except Exception:
                pass

        if not self.is_connected:
            self.connect()
        if self._sock is not None:
            try:
                self._sock.send(bytes(hid_report))
            except Exception:
                pass
        if self.is_connected and self._lib is not None:
            try:
                self._lib.vigem_target_x360_update(self._client, self._target, report)
            except Exception:
                pass
        return snapshot

    def reset(self) -> dict[str, Any]:
        self._last_share_down = False
        return self.emit_frame(buttons=(), axes=())

    def close(self) -> None:
        try:
            if self.is_connected:
                self.reset()
        except Exception:
            pass
        if self._lib is not None and self._client and self._target:
            try:
                if hasattr(self._lib, 'vigem_target_x360_unregister_notification') and self._c_notification_cb:
                    self._lib.vigem_target_x360_unregister_notification(self._target)
            except Exception:
                pass
            try:
                self._lib.vigem_target_remove(self._client, self._target)
            except Exception:
                pass
            try:
                self._lib.vigem_target_free(self._target)
            except Exception:
                pass
            try:
                self._lib.vigem_disconnect(self._client)
            except Exception:
                pass
            try:
                self._lib.vigem_free(self._client)
            except Exception:
                pass
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._target = None
        self._client = None
        self._connected = False
        self._c_notification_cb = None
