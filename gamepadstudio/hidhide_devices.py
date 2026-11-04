"""HidHide 设备目录：按物理设备（base container）分组枚举 HID 接口。

隐藏一个手柄要写的不止一个节点，这里复刻 HidHide 客户端的规则：
  * 组内每个 HID 子接口的 deviceInstancePath；
  * 每个子接口最近的 GUID_DEVINTERFACE_XUSB 祖先（XInput 通道）；
  * 当容器内 HID 子接口全选、且容器类属于 HID/Xbox 复合类时，再加容器节点本身。
只读枚举：除打开 HID 接口读取厂商/产品串外，不读写驱动配置。
"""

from __future__ import annotations

import ctypes as C
import re
import sys
from ctypes import wintypes as W
from dataclasses import dataclass, replace
from typing import List, Optional, Tuple

if sys.platform == 'win32':
    import winreg
else:
    winreg = None

GENERIC_READ = 0x80000000
FILE_SHARE_ALL = 0x00000007
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
CR_SUCCESS = 0
CM_LOCATE_DEVNODE_NORMAL = 0x00000000
CM_GET_DEVICE_INTERFACE_LIST_PRESENT = 0x00000000
CM_GET_DEVICE_INTERFACE_LIST_ALL_DEVICES = 0x00000001
HIDP_STATUS_SUCCESS = 0x00110000

# XInput 由这两个复合驱动类承载；HidHide 的 upper filter 也挂在它们上面。
XINPUT_CLASSES = ('xnacomposite', 'xboxcomposite')
BLOCKABLE_CONTAINER_CLASSES = ('hidclass',) + XINPUT_CLASSES
_HID_STRING_CHARS = 127
_INVALID_HANDLE = C.c_void_p(-1).value

_libraries: dict = {}


def _dll(name: str):
    if sys.platform != 'win32':
        return None
    if name not in _libraries:
        try:
            _libraries[name] = C.WinDLL(name, use_last_error=True)
        except OSError:
            _libraries[name] = None
    return _libraries[name]


def _prepare():
    cm = _dll('cfgmgr32')
    if cm is not None and not hasattr(cm, '_gps_ready'):
        cm.CM_Get_Device_Interface_List_SizeW.argtypes = [C.POINTER(W.DWORD), C.POINTER(GUID),
                                                          W.LPCWSTR, W.DWORD]
        cm.CM_Get_Device_Interface_ListW.argtypes = [C.POINTER(GUID), W.LPCWSTR, W.LPWSTR,
                                                     W.DWORD, W.DWORD]
        cm.CM_Locate_DevNodeW.restype = W.LONG
        cm.CM_Locate_DevNodeW.argtypes = [C.POINTER(W.DWORD), W.LPWSTR, W.DWORD]
        cm.CM_Get_Device_IDW.restype = W.LONG
        cm.CM_Get_Device_IDW.argtypes = [W.DWORD, W.LPWSTR, W.DWORD, W.DWORD]
        cm.CM_Get_Parent.restype = W.LONG
        cm.CM_Get_Parent.argtypes = [C.POINTER(W.DWORD), W.DWORD, W.DWORD]
        cm.CM_Get_Child.restype = W.LONG
        cm.CM_Get_Child.argtypes = [C.POINTER(W.DWORD), W.DWORD, W.DWORD]
        cm.CM_Get_Sibling.restype = W.LONG
        cm.CM_Get_Sibling.argtypes = [C.POINTER(W.DWORD), W.DWORD, W.DWORD]
        cm._gps_ready = True
    hid = _dll('hid')
    if hid is not None and not hasattr(hid, '_gps_ready'):
        hid.HidD_GetHidGuid.argtypes = [C.POINTER(GUID)]
        hid.HidD_GetAttributes.restype = W.BOOL
        hid.HidD_GetAttributes.argtypes = [W.HANDLE, C.POINTER(HidAttributes)]
        for name in ('HidD_GetManufacturerString', 'HidD_GetProductString', 'HidD_GetSerialNumberString'):
            function = getattr(hid, name)
            function.restype = W.BOOL
            function.argtypes = [W.HANDLE, C.c_void_p, W.ULONG]
        hid.HidD_GetPreparsedData.restype = W.BOOL
        hid.HidD_GetPreparsedData.argtypes = [W.HANDLE, C.POINTER(C.c_void_p)]
        hid.HidD_FreePreparsedData.restype = W.BOOL
        hid.HidD_FreePreparsedData.argtypes = [C.c_void_p]
        hid.HidP_GetCaps.restype = W.LONG
        # The parsed-data handle is passed by value, and the caps are read out of an
        # oversized scratch buffer: guessing sizeof(HIDP_CAPS) wrong corrupts the heap.
        hid.HidP_GetCaps.argtypes = [C.c_void_p, C.c_void_p]
        hid._gps_ready = True
    kernel = _dll('kernel32')
    if kernel is not None and not hasattr(kernel, '_gps_hidhide_ready'):
        kernel.CreateFileW.restype = W.HANDLE
        kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p,
                                       W.DWORD, W.DWORD, W.HANDLE]
        kernel.CloseHandle.argtypes = [W.HANDLE]
        kernel._gps_hidhide_ready = True
    return kernel, cm, hid


class GUID(C.Structure):
    _fields_ = [('Data1', W.DWORD), ('Data2', W.WORD), ('Data3', W.WORD),
                ('Data4', W.BYTE * 8)]

    @classmethod
    def from_string(cls, text: str) -> 'GUID':
        raw = text.strip('{}').replace('-', '')
        guid = cls()
        guid.Data1 = int(raw[0:8], 16)
        guid.Data2 = int(raw[8:12], 16)
        guid.Data3 = int(raw[12:16], 16)
        guid.Data4 = (W.BYTE * 8).from_buffer_copy(bytes.fromhex(raw[16:32]))
        return guid

    def as_string(self) -> str:
        tail = ''.join(f'{byte:02x}' for byte in self.Data4)
        return '{%08x-%04x-%04x-%s-%s}' % (self.Data1, self.Data2, self.Data3, tail[:4], tail[4:])


class HidAttributes(C.Structure):
    _fields_ = [('Size', W.DWORD), ('VendorID', W.WORD), ('ProductID', W.WORD),
                ('VersionNumber', W.WORD)]


USAGE_NAMES = {
    (0x01, 0x02): '鼠标', (0x01, 0x04): '摇杆', (0x01, 0x05): '游戏手柄', (0x01, 0x06): '键盘',
    (0x01, 0x0C): '多媒体控制', (0x05, 0x00): '游戏控制器', (0x05, 0x01): '游戏手柄',
    (0x0B, 0x1F): '条形码扫描', (0x0C, 0x01): '音频控制',
}


@dataclass(frozen=True)
class HidInterface:
    instance_id: str
    symbolic_link: str = ''
    description: str = ''
    manufacturer: str = ''
    product: str = ''
    serial: str = ''
    vendor_id: int = 0
    product_id: int = 0
    usage_page: int = 0
    usage: int = 0
    present: bool = False
    readable: bool = False
    denied: bool = False
    xinput_instance_id: str = ''
    base_container: str = ''
    base_class: str = ''
    base_class_guid: str = ''
    container_id: str = ''
    child_count: int = 0

    @property
    def gaming(self) -> bool:
        return self.usage_page == 0x05 or (self.usage_page == 0x01 and self.usage in (0x04, 0x05))

    @property
    def collection(self) -> str:
        """子行标题：优先说清这是哪个集合，而不是重复父级的产品名。"""
        from .i18n import tr
        name = USAGE_NAMES.get((self.usage_page, self.usage))
        if name:
            return tr(name)
        if self.usage_page or self.usage:
            return f'{tr("集合")} 0x{self.usage_page:02X} / 0x{self.usage:02X}'
        return ''

    @property
    def label(self) -> str:
        return self.collection or ' '.join(
            part for part in (self.manufacturer, self.product) if part) or self.description or self.instance_id


@dataclass(frozen=True)
class DeviceGroup:
    key: str
    name: str
    interfaces: Tuple[HidInterface, ...]
    base_container: str = ''
    base_class: str = ''
    base_class_guid: str = ''
    child_count: int = 0

    @property
    def vendor_id(self) -> int:
        return next((item.vendor_id for item in self.interfaces if item.vendor_id), 0)

    @property
    def product_id(self) -> int:
        return next((item.product_id for item in self.interfaces if item.product_id), 0)

    @property
    def connected(self) -> bool:
        return any(item.present for item in self.interfaces)

    @property
    def gaming(self) -> bool:
        return any(item.gaming for item in self.interfaces)

    @property
    def denied(self) -> bool:
        return all(item.denied for item in self.interfaces)

    def vid_pid_text(self) -> str:
        return f'{self.vendor_id:04X}:{self.product_id:04X}' if self.vendor_id else ''


def _enum_value(instance_id: str, value: str) -> str:
    if winreg is None or not instance_id:
        return ''
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            f'SYSTEM\\CurrentControlSet\\Enum\\{instance_id}') as key:
            return str(winreg.QueryValueEx(key, value)[0] or '')
    except OSError:
        return ''


def resolve_indirect_string(text: str) -> str:
    """`@input.inf,%hid_device_system_game%;HID-compliant game controller` -> readable text.

    DeviceDesc is stored as an MUI indirect string. SHLoadIndirectString resolves
    it; the literal after the last ';' is the embedded fallback, so the UI never
    has to show the raw '@...' form.
    """
    raw = str(text or '')
    if not raw.startswith('@'):
        return raw
    shlwapi = _dll('shlwapi')
    if shlwapi is not None:
        try:
            shlwapi.SHLoadIndirectString.restype = C.HRESULT
            shlwapi.SHLoadIndirectString.argtypes = [W.LPCWSTR, W.LPWSTR, W.UINT, C.c_void_p]
            buffer = C.create_unicode_buffer(512)
            if not C.GetLastError() and shlwapi.SHLoadIndirectString(raw, buffer, 512, None) == 0:
                return buffer.value.strip() or raw
        except Exception:
            pass
    return raw.rsplit(';', 1)[-1].strip() or raw


def class_name(class_guid: str) -> str:
    """ClassGUID has no friendly name on the device key; the class key carries it."""
    guid = str(class_guid or '')
    if not guid:
        return ''
    return _reg_class_value(guid, 'Class').lower()


def _reg_class_value(class_guid: str, value: str) -> str:
    if winreg is None:
        return ''
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            f'SYSTEM\\CurrentControlSet\\Control\\Class\\{class_guid}') as key:
            return str(winreg.QueryValueEx(key, value)[0] or '')
    except OSError:
        return ''


def _locate(cm, instance_id: str):
    node = W.DWORD(0)
    buffer = C.create_unicode_buffer(instance_id)
    if cm is None or cm.CM_Locate_DevNodeW(C.byref(node), buffer, CM_LOCATE_DEVNODE_NORMAL) != CR_SUCCESS:
        return None
    return node.value


def _instance_of(cm, node) -> str:
    buffer = C.create_unicode_buffer(512)
    if cm is None or node is None or cm.CM_Get_Device_IDW(node, buffer, 512, 0) != CR_SUCCESS:
        return ''
    return buffer.value


def parent_of(cm, instance_id: str) -> str:
    node = _locate(cm, instance_id)
    if node is None:
        return ''
    parent = W.DWORD(0)
    if cm.CM_Get_Parent(C.byref(parent), node, 0) != CR_SUCCESS:
        return ''
    return _instance_of(cm, parent.value)


def child_count(cm, instance_id: str) -> int:
    node = _locate(cm, instance_id)
    if node is None:
        return 0
    current = W.DWORD(0)
    if cm.CM_Get_Child(C.byref(current), node, 0) != CR_SUCCESS:
        return 0
    count = 1
    sibling = W.DWORD(0)
    while cm.CM_Get_Sibling(C.byref(sibling), current.value, 0) == CR_SUCCESS:
        current = W.DWORD(sibling.value)
        count += 1
    return count


def interface_list(guid: GUID, target_instance: str = '',
                   flags: int = CM_GET_DEVICE_INTERFACE_LIST_ALL_DEVICES) -> List[str]:
    """Device interface paths of one class, optionally limited to one device instance."""
    _, cm, _ = _prepare()
    if cm is None:
        return []
    needed = W.DWORD(0)
    if cm.CM_Get_Device_Interface_List_SizeW(C.byref(needed), C.byref(guid),
                                             target_instance or None, flags) != CR_SUCCESS:
        return []
    if needed.value < 2:
        return []
    buffer = C.create_unicode_buffer(needed.value)
    if cm.CM_Get_Device_Interface_ListW(C.byref(guid), target_instance or None, buffer,
                                        needed.value, flags) != CR_SUCCESS:
        return []
    raw = C.string_at(C.byref(buffer), needed.value * 2)
    return [item for item in raw.decode('utf-16le', 'replace').split('\x00') if item]


def xinput_ancestor_of(cm, instance_id: str) -> str:
    """Nearest node (self included) that carries the XInput driver class.

    HidHide installs its upper filter on HIDClass, XnaComposite and XboxComposite,
    so the XnaComposite/XboxComposite devnode is what XInput reaches. Its class is
    read from the registry instead of matching a device-interface GUID: the GUID
    differs per driver package and guessing it silently misses devices.
    """
    current = instance_id
    for _ in range(12):
        if not current:
            break
        if class_name(_enum_value(current, 'ClassGUID')) in XINPUT_CLASSES:
            return current
        current = parent_of(cm, current)
    return ''


def base_container_of(cm, instance_id: str, container_id: str) -> Tuple[str, str, str, int]:
    """Climb while the parent shares the container id; return (path, class, class guid, children)."""
    if not container_id or not instance_id:
        return '', '', '', 0
    current = instance_id
    for _ in range(12):
        parent = parent_of(cm, current)
        if not parent or _enum_value(parent, 'ContainerId') != container_id:
            break
        current = parent
    guid = _enum_value(current, 'ClassGUID')
    return current, class_name(guid), guid, child_count(cm, current)


def interface_instance_id(link: str) -> str:
    r"""`\\?\HID#VID_054C&PID_0CE6&MI_03#7&1a2b&0&0000#{guid}` -> `HID\VID_054C&PID_0CE6&MI_03\7&1a2b&0&0000`.

    Only the trailing interface GUID is stripped: some paths (Bluetooth HID, for
    example) carry a `{...}` in the middle, so split() would truncate too early.
    """
    body = str(link or '').rsplit('#{', 1)[0]
    if body.upper().startswith('\\\\?\\'):
        body = body[4:]
    if '#' not in body:
        return ''
    return body.replace('#', '\\')


def canonical_instance(instance_id: str) -> str:
    """Registry spelling wins so what we display and write matches HidHide's own text."""
    if winreg is None or '\\' not in instance_id:
        return instance_id
    hardware, _, leaf = instance_id.partition('\\')
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            f'SYSTEM\\CurrentControlSet\\Enum\\{hardware}') as parent:
            for index in range(winreg.QueryInfoKey(parent)[0]):
                name = winreg.EnumKey(parent, index)
                if name.upper() == leaf.upper():
                    return f'{hardware}\\{name}'
    except OSError:
        return instance_id
    return instance_id


def read_interface(link: str, instance_id: str, present: bool) -> HidInterface:
    """Open one HID collection and copy the strings HidHide's own list shows."""
    kernel, _, hid = _prepare()
    result = HidInterface(instance_id=instance_id, symbolic_link=link, present=present)
    if not (kernel and hid) or not (link and present):
        return result
    handle = kernel.CreateFileW(link, GENERIC_READ, FILE_SHARE_ALL, None,
                                OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if not handle or handle == _INVALID_HANDLE:
        code = C.get_last_error()
        return replace(result, denied=code in (ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION))
    try:
        attributes = HidAttributes()
        attributes.Size = C.sizeof(HidAttributes)
        if hid.HidD_GetAttributes(handle, C.byref(attributes)):
            result = replace(result, vendor_id=attributes.VendorID,
                             product_id=attributes.ProductID, readable=True)

        def text(reader):
            buffer = C.create_unicode_buffer(_HID_STRING_CHARS)
            if not reader(handle, C.addressof(buffer), _HID_STRING_CHARS * 2):
                return ''
            return buffer.value.strip()

        result = replace(result,
                         manufacturer=text(hid.HidD_GetManufacturerString),
                         product=text(hid.HidD_GetProductString),
                         serial=text(hid.HidD_GetSerialNumberString))
        preparsed = C.c_void_p()
        if hid.HidD_GetPreparsedData(handle, C.byref(preparsed)):
            try:
                caps = C.create_string_buffer(128)
                if hid.HidP_GetCaps(preparsed.value, C.addressof(caps)) == HIDP_STATUS_SUCCESS:
                    result = replace(result,
                                     usage_page=int.from_bytes(caps.raw[0:2], 'little'),
                                     usage=int.from_bytes(caps.raw[2:4], 'little'))
            finally:
                hid.HidD_FreePreparsedData(preparsed.value)
    finally:
        kernel.CloseHandle(handle)
    return result


def enumerate_groups() -> List[DeviceGroup]:
    _, cm, hid = _prepare()
    if not (cm and hid):
        return []
    hid_guid = GUID()
    hid.HidD_GetHidGuid(C.byref(hid_guid))
    buckets: dict = {}
    for link in interface_list(hid_guid):
        instance = canonical_instance(interface_instance_id(link))
        if not instance:
            continue
        present = _locate(cm, instance) is not None
        info = read_interface(link, instance, present)
        info = replace(info, description=resolve_indirect_string(_enum_value(instance, 'DeviceDesc')))
        container = _enum_value(instance, 'ContainerId')
        if container:
            base_path, base_class, base_guid, count = base_container_of(cm, instance, container)
            info = replace(info, container_id=container, base_container=base_path,
                           base_class=base_class, base_class_guid=base_guid, child_count=count)
        info = replace(info, xinput_instance_id=xinput_ancestor_of(cm, instance))
        buckets.setdefault(info.base_container or instance, []).append(info)

    groups: List[DeviceGroup] = []
    for key, members in buckets.items():
        members.sort(key=lambda item: item.instance_id.upper())
        head = members[0]
        words: List[str] = []
        for item in members:
            for token in re.split(r'\s+', ' '.join((item.manufacturer, item.product)).strip()):
                if token and token not in words:
                    words.append(token)
        groups.append(DeviceGroup(key=key, name=' '.join(words) or head.description or key,
                                  interfaces=tuple(members), base_container=head.base_container,
                                  base_class=head.base_class, base_class_guid=head.base_class_guid,
                                  child_count=head.child_count))
    groups.sort(key=lambda group: (not group.connected, not group.gaming, group.name.lower()))
    return groups


def hide_paths_for(group: DeviceGroup, selected: Tuple[str, ...]) -> List[str]:
    """The instance paths HidHide needs for this selection (mirrors its own client)."""
    chosen = {item.upper() for item in selected}
    members = [item for item in group.interfaces if item.instance_id.upper() in chosen]
    if not members:
        return []
    paths: List[str] = []
    whole = len(members) == len(group.interfaces)
    children_match = not group.child_count or group.child_count == len(group.interfaces)
    if whole and group.base_container and group.base_class in BLOCKABLE_CONTAINER_CLASSES and children_match:
        paths.append(group.base_container)
    for item in members:
        for candidate in (item.instance_id, item.xinput_instance_id):
            if candidate and candidate not in paths:
                paths.append(candidate)
    return paths


def group_for_device(vendor: Optional[int], product: Optional[int], device_path: str = '') -> Optional[DeviceGroup]:
    """Locate the group holding the connected controller described by a device snapshot."""
    groups = enumerate_groups()
    if not groups or not vendor:
        return None
    if device_path:
        instance = interface_instance_id(device_path).upper()
        exact = [group for group in groups
                 if any(item.instance_id.upper() == instance for item in group.interfaces)]
        if len(exact) == 1:
            return exact[0]
    matched = [group for group in groups if group.connected and group.vendor_id == vendor
               and (product is None or group.product_id == product)]
    return matched[0] if len(matched) == 1 else None
