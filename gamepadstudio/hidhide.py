"""
HidHide Windows 内核过滤驱动接入层（硬件独占屏蔽与设备隐身）

1. 驱动是微软 WHQL 认证的 HidHide.sys 上层过滤，挂在 HIDClass / XnaComposite / XboxComposite 上
2. 通过 IOCTL 与 \\\\.\\HidHide 通信；该控制设备是独占的，同一时刻只允许一个客户端
3. IOCTL 只要求 FILE_READ_DATA，因此普通权限即可读写配置，不需要管理员，也不需要 HidHide 界面
4. 配置持久保存在受保护的注册表里，所以一律「一次会话内读→算差异→写→读回校验」，
   并且只增删本程序登记的条目，不动其他工具写进去的内容
"""

import sys
import os
if sys.platform == 'win32':
    import winreg
else:
    winreg = None
import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, List, Tuple, Optional

# HidHide 官方 IOCTL 控制码
IOCTL_GET_WHITELIST = 0x80016000
IOCTL_SET_WHITELIST = 0x80016004
IOCTL_GET_BLACKLIST = 0x80016008
IOCTL_SET_BLACKLIST = 0x8001600C
IOCTL_GET_ACTIVE    = 0x80016010
IOCTL_SET_ACTIVE    = 0x80016014

# 官方发布与下载页
HIDHIDE_RELEASE_URL = "https://github.com/nefarius/HidHide/releases/latest"

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = -1


def encode_multi_sz(strings: List[str]) -> bytes:
    """将字符串列表编码为 Windows MULTI_SZ 双空结尾 Unicode 宽字节流"""
    if not strings:
        return b'\x00\x00\x00\x00'
    return b''.join(s.encode('utf-16le') + b'\x00\x00' for s in strings) + b'\x00\x00'




def dos_to_nt_path(dos_path: str) -> str:
    """将 Win32 DOS 路径 (例 D:\\...) 转换为 Windows 内核驱动所需的 NT 路径 (例 \\Device\\HarddiskVolume4\\...)"""
    if sys.platform != 'win32' or not dos_path:
        return dos_path
    try:
        drive = dos_path[:2]
        if len(drive) == 2 and drive[1] == ':':
            buf = (C.c_wchar * 1024)()
            res = C.windll.kernel32.QueryDosDeviceW(drive, buf, 1024)
            if res > 0:
                return buf.value + dos_path[2:]
    except Exception:
        pass
    return dos_path








def is_apple_bootcamp_environment() -> bool:
    """Return True on Windows when running on Apple Mac Bootcamp hardware."""
    if sys.platform != 'win32' or winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as bios:
            for field in ("SystemManufacturer", "BaseBoardManufacturer"):
                try:
                    value, _ = winreg.QueryValueEx(bios, field)
                    if "apple" in str(value or "").lower():
                        return True
                except Exception:
                    pass
    except Exception:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\KeyMagic"):
            return True
    except Exception:
        return False








# ═════════════════════════════════════════════════════════════════════════
# 独占会话层
#
# HidHide 的控制设备用 WdfDeviceInitSetExclusive(TRUE) 创建，同一时刻只允许
# 一个客户端打开；第二个 open 返回 ERROR_ACCESS_DENIED(5)，与是否管理员无关
# (设备 SDDL 给 World 留了 RWX，IOCTL 也只要求 FILE_READ_DATA)。因此按 IOCTL
# 逐条 open/close 的写法会被 HidHide 客户端、Handheld Companion 或本程序的
# 另一个进程抢占，读回来的是“失败”，而不是“空列表”。
# 这里一次 open 内完成整批读写；被占用时不做等待重试，直接返回占用状态，
# 由界面提示原因并让用户手动刷新。
# ═════════════════════════════════════════════════════════════════════════

IOCTL_GET_WLINVERSE = 0x80016018
IOCTL_SET_WLINVERSE = 0x8001601C
IOCTL_ADD_SESSION_BLACKLIST = 0x80016020
IOCTL_CLR_SESSION_BLACKLIST = 0x80016024

CONTROL_DEVICE = r'\\.\HidHide'
FILE_SHARE_DELETE = 0x00000004
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
_OCCUPIED_ERRORS = (ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION)
_INVALID_HANDLE = C.c_void_p(-1).value

# 已知会管理 HidHide 配置的程序；占用时用来给出疑似来源。
HIDHIDE_CLIENTS = ('HidHideClient.exe', 'HidHideCLI.exe', 'HandheldCompanion.exe',
                   'DS4Windows.exe', 'MotionAssistant.exe', 'MotionAssistantPlus.exe',
                   'x360ce.exe', 'JoyToKey.exe', 'AntiMicroX.exe', 'Steam.exe')

_kernel32 = None


def win32_kernel32():
    """kernel32 with correct prototypes; the legacy client above leaves them unset."""
    global _kernel32
    if _kernel32 is None and sys.platform == 'win32':
        try:
            k = C.WinDLL('kernel32', use_last_error=True)
        except OSError:
            return None
        k.CreateFileW.restype = W.HANDLE
        k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
        k.DeviceIoControl.restype = W.BOOL
        k.DeviceIoControl.argtypes = [W.HANDLE, W.DWORD, C.c_void_p, W.DWORD, C.c_void_p, W.DWORD,
                                      C.POINTER(W.DWORD), C.c_void_p]
        k.CloseHandle.argtypes = [W.HANDLE]
        k.FormatMessageW.restype = W.DWORD
        k.FormatMessageW.argtypes = [W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.LPWSTR, W.DWORD, C.c_void_p]
        k.QueryDosDeviceW.restype = W.DWORD
        k.QueryDosDeviceW.argtypes = [W.LPCWSTR, W.LPWSTR, W.DWORD]
        k.CreateToolhelp32Snapshot.restype = W.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
        k.Process32FirstW.restype = W.BOOL
        k.Process32FirstW.argtypes = [W.HANDLE, C.c_void_p]
        k.Process32NextW.restype = W.BOOL
        k.Process32NextW.argtypes = [W.HANDLE, C.c_void_p]
        _kernel32 = k
    return _kernel32


def win_error_text(code: int) -> str:
    if not code:
        return ''
    k = win32_kernel32()
    message = ''
    if k:
        try:
            buffer = C.create_unicode_buffer(512)
            if k.FormatMessageW(0x1000, None, code, 0x0409, buffer, 512, None):
                message = buffer.value.strip()
        except Exception:
            message = ''
    return f'{message or "未知错误"} (win32={code})'


def running_hidhide_clients() -> List[str]:
    """Which known HidHide frontends are running; used only to name a likely holder.

    We do not chase the real handle owner (that needs SeDebugPrivilege and can hang
    on NtQueryObject), so the UI words this as a suspect, not a fact.
    """
    if sys.platform != 'win32':
        return []
    found = []
    try:
        k = win32_kernel32()
        TH32CS_SNAPPROCESS = 0x00000002

        class Entry(C.Structure):
            _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('th32ProcessID', C.c_uint),
                        ('th32DefaultHeapID', C.POINTER(C.c_ulong)), ('th32ModuleID', C.c_uint),
                        ('cntThreads', W.DWORD), ('th32ParentProcessID', C.c_uint),
                        ('pcPriClassBase', C.c_long), ('dwFlags', W.DWORD),
                        ('szExeFile', W.WCHAR * 260)]

        snapshot = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == _INVALID_HANDLE:
            return []
        try:
            entry = Entry()
            entry.dwSize = C.sizeof(Entry)
            first = k.Process32FirstW(snapshot, C.byref(entry))
            current = entry if first else None
            while current is not None:
                name = str(current.szExeFile or '')
                if name.lower() in [item.lower() for item in HIDHIDE_CLIENTS] and name not in found:
                    found.append(name)
                nxt = Entry()
                nxt.dwSize = C.sizeof(nxt)
                current = nxt if k.Process32NextW(snapshot, C.byref(nxt)) else None
        finally:
            k.CloseHandle(snapshot)
    except Exception:
        return found
    return found


def occupied_reason(holders: Optional[List[str]] = None) -> str:
    names = holders if holders is not None else running_hidhide_clients()
    if names:
        return f'设备隐身配置正被 {"、".join(names)} 使用，无法读取或修改。关闭它后点「刷新」。'
    return '设备隐身配置正被其他程序占用，无法读取或修改。关闭 HidHide 配置界面等程序后点「刷新」。'


def decode_list(data: Optional[bytes]) -> Optional[List[str]]:
    """None means the read was unusable; an empty list means the driver really is empty.

    An empty list comes back as a single UTF-16 null (2 bytes), not as the 4-byte
    double terminator this module sends, so length checks must not assume symmetry.
    """
    if data is None or len(data) < 2 or len(data) % 2:
        return None
    try:
        text = data.decode('utf-16le', errors='strict')
    except UnicodeError:
        return None
    return [item for item in text.split('\x00') if item]


def same_paths(left, right) -> bool:
    return {os.path.normcase(item) for item in (left or ())} == {os.path.normcase(item) for item in (right or ())}


@dataclass(frozen=True)
class DriverInfo:
    installed: bool = False
    loaded: bool = False
    occupied: bool = False
    package_version: str = ''
    driver_version: str = ''
    install_path: str = ''
    cli_path: str = ''
    client_path: str = ''
    watchdog: str = ''
    service_state: str = ''
    error: str = ''

    @property
    def ready(self) -> bool:
        return bool(self.installed and self.loaded)


@dataclass(frozen=True)
class HidHideState:
    loaded: bool = False
    active: bool = False
    inverse: bool = False
    whitelist: Tuple[str, ...] = ()
    blacklist: Tuple[str, ...] = ()
    occupied: bool = False
    holders: Tuple[str, ...] = ()
    error: str = ''


@dataclass(frozen=True)
class Change:
    whitelist: Optional[Tuple[str, ...]] = None
    blacklist: Optional[Tuple[str, ...]] = None
    active: Optional[bool] = None


@dataclass(frozen=True)
class Operation:
    ok: bool
    message: str = ''
    occupied: bool = False
    holders: Tuple[str, ...] = ()
    state: Optional[HidHideState] = None

    @property
    def failed(self) -> bool:
        return not self.ok


class HidHideSession:
    """One exclusive handle for a whole batch of HidHide reads and writes."""

    def __init__(self):
        self._handle = None
        self.occupied = False
        self.holders: Tuple[str, ...] = ()
        self.error = ''

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    def open(self) -> bool:
        if self._handle:
            return True
        self.occupied = False
        self.holders = ()
        self.error = ''
        k = win32_kernel32()
        if not k:
            self.error = 'HidHide 仅支持 Windows'
            return False
        handle = k.CreateFileW(CONTROL_DEVICE, GENERIC_READ,
                               FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                               None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        if handle and handle != _INVALID_HANDLE:
            self._handle = handle
            return True
        code = C.get_last_error()
        if code in _OCCUPIED_ERRORS:
            self.occupied = True
            self.holders = tuple(running_hidhide_clients())
            self.error = occupied_reason(list(self.holders))
        elif code in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
            self.error = '未检测到运行中的设备隐身驱动。'
        else:
            self.error = win_error_text(code) or '无法打开设备隐身驱动。'
        return False

    def close(self):
        k = win32_kernel32()
        handle, self._handle = self._handle, None
        if k and handle:
            try:
                k.CloseHandle(handle)
            except Exception:
                pass

    def _ioctl(self, code: int, in_bytes: bytes = b'', out_size: Optional[int] = None):
        k = win32_kernel32()
        if not (k and self._handle):
            return None
        in_buffer = C.create_string_buffer(in_bytes, len(in_bytes)) if in_bytes else None
        if out_size is None:
            needed = W.DWORD(0)
            k.DeviceIoControl(self._handle, code, in_buffer, 0, None, 0, C.byref(needed), None)
            out_size = max(needed.value, 2)
        out_buffer = C.create_string_buffer(out_size) if out_size else None
        returned = W.DWORD(0)
        ok = k.DeviceIoControl(self._handle, code, in_buffer, len(in_bytes),
                               out_buffer, out_size, C.byref(returned), None)
        if not ok:
            self.error = win_error_text(C.get_last_error())
            return None
        return out_buffer.raw[:returned.value] if out_buffer else b''

    def _read_flag(self, code: int):
        data = self._ioctl(code, out_size=1)
        return None if data is None or len(data) < 1 else bool(data[0])

    def _read_list(self, code: int):
        return decode_list(self._ioctl(code))

    def read(self) -> HidHideState:
        if not self._handle and not self.open():
            return HidHideState(occupied=self.occupied, holders=self.holders, error=self.error)
        active = self._read_flag(IOCTL_GET_ACTIVE)
        inverse = self._read_flag(IOCTL_GET_WLINVERSE)
        whitelist = self._read_list(IOCTL_GET_WHITELIST)
        blacklist = self._read_list(IOCTL_GET_BLACKLIST)
        if active is None or whitelist is None or blacklist is None:
            return HidHideState(occupied=self.occupied, holders=self.holders,
                                error=self.error or '无法读取设备隐身配置')
        return HidHideState(loaded=True, active=active, inverse=bool(inverse),
                            whitelist=tuple(whitelist), blacklist=tuple(blacklist))

    def apply(self, change: Change) -> Operation:
        if not self._handle and not self.open():
            return Operation(False, self.error, self.occupied, self.holders)
        before = self.read()
        if not before.loaded:
            return Operation(False, before.error, before.occupied, before.holders, before)
        writes = []
        if change.whitelist is not None and not same_paths(change.whitelist, before.whitelist):
            writes.append((IOCTL_SET_WHITELIST, encode_multi_sz(list(change.whitelist))))
        if change.blacklist is not None and not same_paths(change.blacklist, before.blacklist):
            writes.append((IOCTL_SET_BLACKLIST, encode_multi_sz(list(change.blacklist))))
        if change.active is not None and change.active != before.active:
            writes.append((IOCTL_SET_ACTIVE, b'\x01' if change.active else b'\x00'))
        for code, payload in writes:
            if self._ioctl(code, payload, 0) is None:
                return Operation(False, f'写入失败：{self.error}', self.occupied, self.holders, before)
        after = self.read()
        if not after.loaded:
            return Operation(False, f'写入后无法确认结果：{self.error}', after.occupied, after.holders, before)
        if change.whitelist is not None and not same_paths(change.whitelist, after.whitelist):
            return Operation(False, '写入后校验不一致：应用列表未生效', False, (), after)
        if change.blacklist is not None and not same_paths(change.blacklist, after.blacklist):
            return Operation(False, '写入后校验不一致：隐藏设备列表未生效', False, (), after)
        if change.active is not None and change.active != after.active:
            return Operation(False, '写入后校验不一致：总开关未生效', False, (), after)
        return Operation(True, '', False, (), after)


def read_state(session: Optional[HidHideSession] = None) -> HidHideState:
    if session is not None:
        return session.read()
    with HidHideSession() as own:
        return own.read()


def transact(mutator: Callable[[HidHideState], Optional[Change]]) -> Operation:
    """Read, mutate, write and verify inside a single exclusive session."""
    with HidHideSession() as session:
        state = session.read()
        if not state.loaded:
            return Operation(False, state.error, state.occupied, state.holders, state)
        change = mutator(state)
        if change is None:
            return Operation(True, '', False, (), state)
        return session.apply(change)


def _reg_value(root, key, value):
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(root, key) as handle:
            return winreg.QueryValueEx(handle, value)[0]
    except OSError:
        return None


def _file_version(path: str) -> str:
    if sys.platform != 'win32' or not path or not os.path.isfile(path):
        return ''
    try:
        version_dll = C.WinDLL('version', use_last_error=True)
        size = version_dll.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ''
        block = C.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(path, 0, size, block):
            return ''
        pointer = C.POINTER(C.c_void_p)()
        length = W.UINT(0)
        if not version_dll.VerQueryValueW(block, '\\VarFileInfo\\Translation', C.byref(pointer), C.byref(length)):
            return ''
        pair = C.cast(pointer, C.POINTER(W.WORD * 2)).contents
        sub = f'\\StringFileInfo\\{pair[0]:04x}{pair[1]:04x}\\FileVersion'
        if not version_dll.VerQueryValueW(block, sub, C.byref(pointer), C.byref(length)):
            return ''
        return (C.cast(pointer, W.LPWSTR).value or '').strip()
    except Exception:
        return ''


def _service_state(name: str) -> str:
    if sys.platform != 'win32':
        return ''
    states = {1: 'stopped', 2: 'start pending', 3: 'stop pending', 4: 'running', 5: 'continue pending', 6: 'pause pending', 7: 'paused'}

    class _Status(C.Structure):
        _fields_ = [('service_type', W.DWORD), ('current_state', W.DWORD),
                    ('controls_accepted', W.DWORD), ('win32_exit_code', W.DWORD),
                    ('service_exit_code', W.DWORD), ('check_point', W.DWORD),
                    ('wait_hint', W.DWORD)]

    try:
        adv = C.WinDLL('advapi32', use_last_error=True)
        adv.OpenSCManagerW.restype = W.HANDLE
        adv.OpenSCManagerW.argtypes = [W.LPCWSTR, W.LPCWSTR, W.DWORD]
        adv.OpenServiceW.restype = W.HANDLE
        adv.OpenServiceW.argtypes = [W.HANDLE, W.LPCWSTR, W.DWORD]
        adv.QueryServiceStatus.argtypes = [W.HANDLE, C.POINTER(_Status)]
        adv.CloseServiceHandle.argtypes = [W.HANDLE]
        manager = adv.OpenSCManagerW(None, None, 0x0001)
        if not manager:
            return ''
        try:
            service = adv.OpenServiceW(manager, name, 0x0004)
            if not service:
                return ''
            try:
                status = _Status()
                if not adv.QueryServiceStatus(service, C.byref(status)):
                    return ''
                return states.get(status.current_state, 'unknown')
            finally:
                adv.CloseServiceHandle(service)
        finally:
            adv.CloseServiceHandle(manager)
    except Exception:
        return ''


UNINSTALL_ROOTS = (r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                   r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')


def _package_registration() -> dict:
    if winreg is None:
        return {}
    for root in UNINSTALL_ROOTS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as parent:
                for index in range(winreg.QueryInfoKey(parent)[0]):
                    name = winreg.EnumKey(parent, index)
                    try:
                        with winreg.OpenKey(parent, name) as entry:
                            display = str(winreg.QueryValueEx(entry, 'DisplayName')[0])
                            if 'HidHide' not in display:
                                continue
                            result = {}
                            for key, value in (('package_version', 'DisplayVersion'),
                                               ('install_path', 'InstallLocation')):
                                try:
                                    result[key] = str(winreg.QueryValueEx(entry, value)[0] or '')
                                except OSError:
                                    result[key] = ''
                            return result
                    except OSError:
                        continue
        except OSError:
            continue
    return {}


def driver_info() -> DriverInfo:
    """Layered detection: package registration, service, driver binary, live control device."""
    if sys.platform != 'win32':
        return DriverInfo(error='HidHide 仅支持 Windows')
    package = _package_registration()
    install_path = package.get('install_path', '')
    if not install_path:
        guess = Path(r'C:\Program Files\Nefarius Software Solutions\HidHide')
        install_path = str(guess) if guess.is_dir() else ''
    cli_path = ''
    client_path = ''
    if install_path:
        for name in ('HidHideCLI.exe', 'HidHideClient.exe'):
            for folder in (Path(install_path) / 'x64', Path(install_path)):
                candidate = folder / name
                if candidate.is_file():
                    if name == 'HidHideCLI.exe':
                        cli_path = str(candidate)
                    else:
                        client_path = str(candidate)
                    break
    service = _reg_value(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Services\HidHide', 'ImagePath') \
        if winreg is not None else None
    driver_path = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'drivers', 'HidHide.sys')
    installed = bool(service or package or os.path.isfile(driver_path))
    info = DriverInfo(installed=installed,
                      package_version=package.get('package_version', ''),
                      driver_version=_file_version(driver_path),
                      install_path=install_path,
                      cli_path=cli_path,
                      client_path=client_path,
                      watchdog=_service_state('HidHideWatchdog.exe') or _service_state('HidHideWatchdog'),
                      service_state=_service_state('HidHide'))
    with HidHideSession() as session:
        info = replace(info, loaded=bool(session._handle),
                       occupied=bool(session.occupied),
                       error='' if session._handle else session.error)
    return info


def self_app_paths() -> List[str]:
    """NT image paths this program can run as; HidHide matches the process image, not a DOS path."""
    if sys.platform != 'win32':
        return []
    candidates = [sys.executable, getattr(sys, '_base_executable', '') or '']
    executable = Path(sys.executable)
    if getattr(sys, 'frozen', False):
        candidates.append(str(executable.with_name('DualSenseStudio.exe')))
    elif 'python' in executable.name.lower():
        candidates += [str(executable.with_name('python.exe')), str(executable.with_name('pythonw.exe'))]
    paths = []
    for candidate in candidates:
        if not candidate or not os.path.isfile(candidate):
            continue
        nt = dos_to_nt_path(candidate)
        if nt and nt not in paths:
            paths.append(nt)
    return paths


def ensure_self_access(session: Optional[HidHideSession] = None) -> Tuple[bool, str]:
    """登记本程序的手柄访问权限，供 SDL 枚举之前调用。

    只在过滤真正生效、且我们还没在列表里时才写；反向模式下列表含义反转，
    登记自身反而会把自己禁掉，所以跳过。
    """
    if sys.platform != 'win32':
        return True, ''
    state = session.read() if session is not None else read_state()
    if not state.loaded:
        return (False, state.error) if state.occupied else (True, '')
    if state.inverse or not state.active:
        return True, ''
    missing = [path for path in self_app_paths()
               if not any(existing.upper() == path.upper() for existing in state.whitelist)]
    if not missing:
        return True, ''

    def mutate(current: HidHideState) -> Optional[Change]:
        if current.inverse:
            return None
        whitelist = list(current.whitelist)
        for path in missing:
            if not any(existing.upper() == path.upper() for existing in whitelist):
                whitelist.append(path)
        return Change(whitelist=tuple(whitelist)) if whitelist != list(current.whitelist) else None

    result = transact(mutate)
    if not result.ok:
        return False, result.message or '无法登记本程序的手柄访问权限，已隐身的手柄可能不可见。'
    return True, ''


# ── 驱动安装与修复 ─────────────────────────────────────────────────
# 内核驱动安装必须提权、通常要重启，这是唯一无法避免的一次 UAC；装好之后
# 的所有配置读写都不再需要管理员。安装包是上游未修改的官方包，先校验哈希。

INSTALLER_SHA256 = 'f4bbbcb82e6258641b887c74bc81c4c5f66e4aa811808dfc304347687b7605f6'
INSTALLER_NAME = 'HidHide_1.5.230_x64.exe'
REBOOT_CODES = (1641, 3010)
CANCELLED_CODES = (1223, 1602)


@dataclass(frozen=True)
class InstallResult:
    ok: bool
    message: str = ''
    reboot_required: bool = False
    cancelled: bool = False
    code: int = 0


def bundled_installer() -> str:
    here = Path(__file__).resolve().parent / 'native' / 'hidhide' / INSTALLER_NAME
    if here.is_file():
        return str(here)
    return ''


def _sha256(path: str) -> str:
    import hashlib
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
    except OSError:
        return ''
    return digest.hexdigest()


def _run_elevated_wait(program: str, arguments: str) -> int:
    """ShellExecuteExW with runas; returns the exit code, or -1 when it could not start."""
    SW_SHOW = 5
    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    INFINITE = 0xFFFFFFFF

    class Info(C.Structure):
        _fields_ = [('cbSize', W.DWORD), ('fMask', W.ULONG), ('hwnd', W.HWND),
                    ('lpVerb', W.LPCWSTR), ('lpFile', W.LPCWSTR), ('lpParameters', W.LPCWSTR),
                    ('lpDirectory', W.LPCWSTR), ('nShow', W.INT), ('hInstApp', W.HINSTANCE),
                    ('lpIDList', C.c_void_p), ('lpClass', W.LPCWSTR), ('hkeyClass', W.HANDLE),
                    ('dwHotKey', W.DWORD), ('hIcon', W.HANDLE), ('hProcess', W.HANDLE)]

    try:
        shell32 = C.WinDLL('shell32', use_last_error=True)
        kernel = win32_kernel32()
        info = Info()
        info.cbSize = C.sizeof(Info)
        info.fMask = SEE_MASK_NOCLOSEPROCESS
        info.lpVerb = 'runas'
        info.lpFile = program
        info.lpParameters = arguments
        info.nShow = SW_SHOW
        if not shell32.ShellExecuteExW(C.byref(info)):
            return -1
        if not info.hProcess:
            return 0
        try:
            kernel32 = kernel or C.WinDLL('kernel32', use_last_error=True)
            kernel32.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
            kernel32.WaitForSingleObject(info.hProcess, INFINITE)
            exit_code = W.DWORD(0)
            kernel32.GetExitCodeProcess.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
            kernel32.GetExitCodeProcess(info.hProcess, C.byref(exit_code))
            return int(exit_code.value)
        finally:
            kernel32.CloseHandle(info.hProcess)
    except Exception:
        return -1


def install_driver() -> InstallResult:
    if sys.platform != 'win32':
        return InstallResult(False, '设备隐身驱动仅支持 Windows。')
    if is_apple_bootcamp_environment():
        return InstallResult(False,
                             '检测到 Apple Boot Camp 环境：HidHide 会与苹果内置键盘/触控板的复合驱动冲突，'
                             '可能导致内置输入设备失灵，因此拒绝安装。')
    installer = bundled_installer()
    if not installer:
        return InstallResult(False, '安装包未随本程序发布，请到官方发布页下载安装。')
    if _sha256(installer) != INSTALLER_SHA256:
        return InstallResult(False, '安装包校验失败（SHA-256 不匹配），已取消安装。')
    code = _run_elevated_wait(installer, '/quiet')
    if code in CANCELLED_CODES:
        return InstallResult(False, '已取消安装。', cancelled=True, code=code)
    if code in REBOOT_CODES:
        return InstallResult(True, '驱动已安装，需要重启系统后生效。', reboot_required=True, code=code)
    if code == 0:
        return InstallResult(True, '驱动已安装。若手柄仍未生效，请重启系统。', code=code)
    if code < 0:
        return InstallResult(False, '无法启动安装程序。', code=code)
    return InstallResult(False, f'安装未完成（退出码 {code}）。', code=code)


def start_driver_service() -> InstallResult:
    """驱动已安装但没在跑时的修复入口（例如被手动停止）。"""
    if sys.platform != 'win32':
        return InstallResult(False, '设备隐身驱动仅支持 Windows。')
    code = _run_elevated_wait(os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'sc.exe'),
                              'start HidHide')
    if code in CANCELLED_CODES:
        return InstallResult(False, '已取消。', cancelled=True, code=code)
    if code == 0:
        return InstallResult(True, '已请求启动设备隐身驱动。', code=code)
    if code == 1056:
        return InstallResult(True, '驱动已在运行。', code=code)
    if code < 0:
        return InstallResult(False, '无法启动服务控制程序。', code=code)
    return InstallResult(False, f'启动驱动服务失败（退出码 {code}），请重启系统后重试。', code=code)

