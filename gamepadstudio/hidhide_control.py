"""HidHide 操作层：把界面意图翻译成一次性的驱动事务。

三条约定：
  * 快照只读，绝不为了“顺便修一下”去写驱动；写操作只能来自用户动作。
  * 只增删本程序登记的条目，其他工具写进 HidHide 的白名单/黑名单原样保留。
  * HidHide 自己的模型是「设备勾选」与「总开关」相互独立，这里也保持一致，
    避免同一个开关在两个程序里含义不同。
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

from . import hidhide
from . import hidhide_devices

MANAGED_PATHS_KEY = 'hidhide_managed_paths'
MANAGED_APPS_KEY = 'hidhide_managed_apps'
ENABLED_KEY = 'hidhide_enabled'
LEGACY_ENABLED_KEY = 'device_cloaking_enabled'


def _dos_devices() -> List[Tuple[str, str]]:
    """[(drive letter path, NT device path)] for the current mount table."""
    k = hidhide.win32_kernel32()
    if not (sys.platform == 'win32' and k):
        return []
    import ctypes as _C
    found = []
    for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
        dos = f'{letter}:'
        try:
            buffer = _C.create_unicode_buffer(1024)
            if k.QueryDosDeviceW(dos, buffer, 1024):
                found.append((dos, buffer.value))
        except Exception:
            continue
    return found


def nt_to_dos(path: str, devices: Optional[List[Tuple[str, str]]] = None) -> str:
    r"""`\Device\HarddiskVolume3\Users\...` -> `C:\Users\...` for display only."""
    if not path:
        return path
    best_dos, best_length = '', 0
    for dos, device in (devices if devices is not None else _dos_devices()):
        if device and path.upper().startswith(device.upper()) and len(device) > best_length:
            best_dos, best_length = dos, len(device)
    return path if not best_dos else best_dos + path[best_length:]


@dataclass(frozen=True)
class AppEntry:
    nt_path: str
    display: str
    name: str
    source: str          # self | managed | external
    exists: bool
    editable: bool


class HidHideController:
    def __init__(self, store=None, own_keys=None):
        self.store = store
        # 谁创建了当前在场的虚拟手柄，只有持有 pad 的那一侧知道（Agent/UI 主进程）。
        self._own_keys_provider = own_keys

    # ── 配置中的“本程序管理”集合 ────────────────────────────────────
    def _setting(self, key: str, default):
        if self.store is None:
            return default
        value = self.store.data.get(key, default)
        return default if value is None else value

    def _save_setting(self, key: str, value) -> None:
        if self.store is None:
            return
        self.store.data[key] = value
        try:
            self.store.save()
        except Exception:
            pass

    def managed_paths(self) -> List[str]:
        return [str(item) for item in self._setting(MANAGED_PATHS_KEY, []) or []]

    def managed_apps(self) -> List[str]:
        return [str(item) for item in self._setting(MANAGED_APPS_KEY, []) or []]

    # ── 只读快照 ────────────────────────────────────────────────────
    def assess(self, info: hidhide.DriverInfo, state: hidhide.HidHideState,
               prune: bool = False) -> Dict[str, object]:
        """从已经读到的状态判断偏差；对账只报告，不擅自写驱动。

        隐藏是持久且手动的，所以“上次留下了什么”是用户意图而不是垃圾；真正危险的是
        过滤已生效但本程序不在白名单里——那会让人以为手柄坏了。
        """
        result = {'status': 'ready', 'message': '', 'stale_paths': []}
        if not info.installed:
            result['status'] = 'not_installed'
            return result
        if state.occupied:
            result['status'] = 'occupied'
            result['message'] = state.error
            return result
        if not state.loaded:
            result['status'] = 'not_running'
            return result

        managed = self.managed_paths()
        hidden = {item.upper() for item in state.blacklist}
        stale = [path for path in managed if path.upper() not in hidden]
        if stale:
            result['stale_paths'] = stale
            if prune:
                self._save_setting(MANAGED_PATHS_KEY, [path for path in managed if path.upper() in hidden])
        if state.active and not state.inverse:
            missing = [path for path in hidhide.self_app_paths()
                       if not any(existing.upper() == path.upper() for existing in state.whitelist)]
            if missing:
                result['status'] = 'access_missing'
                result['message'] = ('设备隐身已开启，但本程序不在允许访问的列表里，手柄可能显示为已断开。'
                                     '点「重新登记本程序」恢复。')
        return result

    def summary(self) -> Dict[str, object]:
        """Cheap read for the overview row: driver + switch state + drift, no device tree."""
        info = hidhide.driver_info()
        state = hidhide.read_state()
        return {'info': asdict(info),
                'state': {'loaded': state.loaded, 'active': state.active, 'inverse': state.inverse,
                          'whitelist': list(state.whitelist), 'blacklist': list(state.blacklist),
                          'occupied': state.occupied, 'holders': list(state.holders), 'error': state.error},
                'managed': {'paths': self.managed_paths(), 'apps': self.managed_apps()},
                'reconcile': self.assess(info, state, prune=True)}

    def created_group_keys(self) -> set:
        """本程序此刻创建的虚拟手柄所属设备组（大写 key 集合）。

        没有 provider（例如远控模式下由另一侧持有 pad）就返回空集 —— 宁可少标。
        """
        provider = self._own_keys_provider
        if provider is None:
            return set()
        try:
            return {str(item).upper() for item in (provider() or ()) if item}
        except Exception:
            return set()

    def snapshot(self) -> Dict[str, object]:
        info = hidhide.driver_info()
        state = hidhide.read_state()
        groups = hidhide_devices.enumerate_groups() if state.loaded else []
        hidden = {item.upper() for item in state.blacklist}
        self_paths = hidhide.self_app_paths() if sys.platform == 'win32' else []
        managed_paths = self.managed_paths()
        managed_apps = {item.upper() for item in self.managed_apps()}

        apps = [self._app_entry(item, self_paths, managed_apps) for item in state.whitelist]
        # 未安装或未授权时自身条目可能还没进白名单，界面仍要能看到它们会被登记。
        listed = {entry.nt_path.upper() for entry in apps}
        for path in self_paths:
            if path.upper() not in listed:
                apps.append(self._app_entry(path, self_paths, managed_apps, registered=False))

        device_rows = []
        own_keys = self.created_group_keys()
        for group in groups:
            paths = hidhide_devices.hide_paths_for(group, tuple(item.instance_id for item in group.interfaces))
            device_rows.append({
                'key': group.key,
                'name': group.name,
                'vid_pid': group.vid_pid_text(),
                'connected': group.connected,
                'gaming': group.gaming,
                'origin': group.origin,
                'origin_detail': group.origin_detail,
                # owned=黑名单里我们写的条目；created_by_us=设备是本程序拉起的虚拟手柄
                'created_by_us': group.key.upper() in own_keys,
                'hidden': bool(paths) and all(item.upper() in hidden for item in paths),
                'partly_hidden': any(item.upper() in hidden for item in paths) and not all(
                    item.upper() in hidden for item in paths),
                'owned': any(item.upper() in {owned.upper() for owned in managed_paths} for item in paths),
                'base_container': group.base_container,
                'base_class': group.base_class,
                'interfaces': [{
                    'instance_id': item.instance_id,
                    'label': item.label,
                    'collection': item.collection,
                    'present': item.present,
                    'readable': item.readable,
                    'denied': item.denied,
                    'gaming': item.gaming,
                    'xinput': item.xinput_instance_id,
                    'hidden': item.instance_id.upper() in hidden,
                } for item in group.interfaces],
            })

        return {
            'info': asdict(info),
            'state': {'loaded': state.loaded, 'active': state.active, 'inverse': state.inverse,
                      'whitelist': list(state.whitelist), 'blacklist': list(state.blacklist),
                      'occupied': state.occupied, 'holders': list(state.holders), 'error': state.error},
            'apps': [asdict(entry) for entry in apps],
            'devices': device_rows,
            'managed': {'paths': managed_paths, 'apps': self.managed_apps()},
            'enabled': self.enabled(state),
            'can_write': bool(state.loaded),
            'reconcile': self.assess(info, state, prune=True),
        }

    def _app_entry(self, nt_path: str, self_paths: List[str], managed_apps,
                   registered: bool = True) -> AppEntry:
        display = nt_to_dos(nt_path)
        lower = nt_path.upper()
        source = 'external'
        if any(item.upper() == lower for item in self_paths):
            source = 'self' if registered else 'self_pending'
        elif lower in managed_apps:
            source = 'managed'
        return AppEntry(nt_path=nt_path, display=display, name=os.path.basename(display.replace('\\', os.sep)),
                        source=source, exists=os.path.isfile(display), editable=source != 'self')

    def enabled(self, state: hidhide.HidHideState) -> bool:
        """The master switch is HidHide's own `active` flag, not a stored preference."""
        return bool(state.loaded and state.active)

    # ── 写操作 ──────────────────────────────────────────────────────
    def set_enabled(self, wanted: bool) -> Dict[str, object]:
        self_paths = hidhide.self_app_paths()
        if wanted and not self_paths:
            return self._result(hidhide.Operation(
                False, '无法确定本程序的运行路径，开启后我们会读不到手柄，因此拒绝开启。'))

        def mutate(state: hidhide.HidHideState):
            if not wanted:
                return hidhide.Change(active=False)
            whitelist = list(state.whitelist)
            if not state.inverse:
                whitelist += [path for path in self_paths
                              if not any(item.upper() == path.upper() for item in whitelist)]
            return hidhide.Change(whitelist=tuple(whitelist) if whitelist != list(state.whitelist) else None,
                                  active=True)

        result = hidhide.transact(mutate)
        if result.ok:
            self._save_setting(ENABLED_KEY, bool(wanted))
        return self._result(result)

    def _self_registered(self) -> bool:
        state = hidhide.read_state()
        if not state.loaded or state.inverse:
            return False
        return all(any(existing.upper() == path.upper() for existing in state.whitelist)
                   for path in hidhide.self_app_paths())

    def add_apps(self, paths: List[str]) -> Dict[str, object]:
        nt_paths = []
        for path in paths:
            cleaned = str(path or '').strip().strip('"')
            if not cleaned:
                continue
            nt = hidhide.dos_to_nt_path(cleaned) or cleaned
            nt_paths.append(nt)
        if not nt_paths:
            return self._result(hidhide.Operation(False, '没有可添加的程序。'))

        def mutate(state: hidhide.HidHideState):
            whitelist = list(state.whitelist)
            changed = False
            for path in nt_paths:
                if not any(existing.upper() == path.upper() for existing in whitelist):
                    whitelist.append(path)
                    changed = True
            return hidhide.Change(whitelist=tuple(whitelist)) if changed else None

        result = hidhide.transact(mutate)
        if result.ok:
            stored = self.managed_apps()
            added = {item.upper() for item in stored}
            for path in nt_paths:
                if path.upper() not in added and not any(item.upper() == path.upper() for item in hidhide.self_app_paths()):
                    stored.append(path)
                    added.add(path.upper())
            self._save_setting(MANAGED_APPS_KEY, stored)
        return self._result(result)

    def remove_app(self, nt_path: str) -> Dict[str, object]:
        target = str(nt_path or '').strip()
        if not target:
            return self._result(hidhide.Operation(False, '未选择要移除的程序。'))
        if any(item.upper() == target.upper() for item in hidhide.self_app_paths()):
            return self._result(hidhide.Operation(False, '本程序自身必须保留访问权限，否则开启隐身后会读不到手柄。'))

        def mutate(state: hidhide.HidHideState):
            remaining = [item for item in state.whitelist if item.upper() != target.upper()]
            return hidhide.Change(whitelist=tuple(remaining)) if len(remaining) != len(state.whitelist) else None

        result = hidhide.transact(mutate)
        if result.ok:
            stored = [item for item in self.managed_apps() if item.upper() != target.upper()]
            self._save_setting(MANAGED_APPS_KEY, stored)
        return self._result(result)

    def set_paths_hidden(self, paths: List[str], hidden: bool) -> Dict[str, object]:
        wanted = list(dict.fromkeys(str(item) for item in paths if str(item).strip()))
        if not wanted:
            return self._result(hidhide.Operation(False, '未选择设备。'))
        self_paths = hidhide.self_app_paths()

        def mutate(state: hidhide.HidHideState):
            blacklist = list(state.blacklist)
            existing = {item.upper() for item in blacklist}
            drop = {item.upper() for item in wanted}
            if hidden:
                additions = [path for path in wanted if path.upper() not in existing]
                blacklist += additions
            else:
                blacklist = [item for item in blacklist if item.upper() not in drop]
            if hidden and not additions and state.inverse:
                return None

            whitelist = list(state.whitelist)
            if hidden and not state.inverse:
                whitelist += [path for path in self_paths
                              if not any(item.upper() == path.upper() for item in whitelist)]

            active = state.active
            if hidden:
                active = True
            elif not blacklist:
                active = False

            return hidhide.Change(
                whitelist=tuple(whitelist) if whitelist != list(state.whitelist) else None,
                blacklist=tuple(blacklist) if blacklist != list(state.blacklist) else None,
                active=active if active != state.active else None)

        result = hidhide.transact(mutate)
        if result.ok:
            stored = self.managed_paths()
            upper = {item.upper() for item in stored}
            for path in wanted:
                owned = path.upper() in upper
                if hidden and not owned:
                    stored.append(path)
                    upper.add(path.upper())
                elif not hidden and owned:
                    stored = [item for item in stored if item.upper() != path.upper()]
                    upper = {item.upper() for item in stored}
            self._save_setting(MANAGED_PATHS_KEY, stored)
        return self._result(result)

    def hide_current_device(self, vendor: Optional[int], product: Optional[int],
                            device_path: str = '') -> Dict[str, object]:
        group = hidhide_devices.group_for_device(vendor, product, device_path)
        if group is None:
            return self._result(hidhide.Operation(
                False, '无法唯一匹配当前手柄的 HID 节点，请重新连接或手动在设备列表中选择。'))
        paths = hidhide_devices.hide_paths_for(group, tuple(item.instance_id for item in group.interfaces))
        if not paths:
            return self._result(hidhide.Operation(False, '当前手柄没有可隐藏的 HID 节点。'))
        return self.set_paths_hidden(paths, True)

    def reveal_all(self) -> Dict[str, object]:
        """Emergency path: HidHide's blacklist is ours to clear here, active off too.

        Unlike the per-device actions this is deliberately blunt: the user pressed
        “一键恢复所有共享访问” because something is stuck.
        """
        result = hidhide.transact(lambda state: hidhide.Change(blacklist=(), active=False))
        if result.ok:
            self._save_setting(MANAGED_PATHS_KEY, [])
            self._save_setting(ENABLED_KEY, False)
        return self._result(result)

    def set_group_hidden(self, key: str, hidden: bool) -> Dict[str, object]:
        groups = hidhide_devices.enumerate_groups()
        group = next((item for item in groups if item.key == key), None)
        if group is None:
            return self._result(hidhide.Operation(
                False, '设备已断开或列表已过期，请点「刷新」后重试。'))
        paths = hidhide_devices.hide_paths_for(group, tuple(item.instance_id for item in group.interfaces))
        return self.set_paths_hidden(paths, hidden)

    def open_client(self) -> Dict[str, object]:
        """Launch HidHide's own configuration UI; it then holds the driver exclusively."""
        info = hidhide.driver_info()
        path = info.client_path
        if not path or not os.path.isfile(path):
            return self._result(hidhide.Operation(False, '未找到 HidHide 配置界面，请先安装 HidHide 驱动。'))
        try:
            import subprocess
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if sys.platform == 'win32' else 0
            subprocess.Popen([path], creationflags=flags)
            return {'ok': True,
                    'message': '已打开 HidHide 配置界面。它会独占驱动配置，本面板随后转为只读，关闭它再点「刷新」即可恢复。',
                    'occupied': False, 'holders': [], 'state': None}
        except OSError as exc:
            return self._result(hidhide.Operation(False, f'无法打开 HidHide 配置界面：{exc}'))

    def install_driver(self) -> Dict[str, object]:
        result = hidhide.install_driver()
        return {'ok': bool(result.ok), 'message': result.message,
                'occupied': False, 'holders': [], 'reboot_required': result.reboot_required,
                'state': None}

    def repair_driver(self) -> Dict[str, object]:
        result = hidhide.start_driver_service()
        return {'ok': bool(result.ok), 'message': result.message,
                'occupied': False, 'holders': [], 'reboot_required': False, 'state': None}

    def register_self(self) -> Dict[str, object]:
        """显式登记本程序的手柄访问权限（唯一由界面触发的“修复型”写入）。"""
        paths = hidhide.self_app_paths()
        if not paths:
            return self._result(hidhide.Operation(False, '无法确定本程序的运行路径。'))

        def mutate(state: hidhide.HidHideState):
            if state.inverse:
                return None
            whitelist = list(state.whitelist)
            for path in paths:
                if not any(existing.upper() == path.upper() for existing in whitelist):
                    whitelist.append(path)
            return hidhide.Change(whitelist=tuple(whitelist)) if whitelist != list(state.whitelist) else None

        return self._result(hidhide.transact(mutate))

    def reconcile_on_start(self) -> Dict[str, object]:
        info = hidhide.driver_info()
        state = hidhide.read_state()
        result = self.assess(info, state, prune=True)
        result.update({'installed': info.installed, 'loaded': state.loaded, 'active': state.active,
                       'inverse': state.inverse, 'occupied': state.occupied,
                       'holders': list(state.holders)})
        return result

    # ── 单一入口（Studio 本地 / Agent 远程共用） ────────────────────
    def dispatch(self, action: str, payload: Optional[Dict] = None) -> Dict[str, object]:
        values = payload or {}
        if action == 'snapshot':
            return self.snapshot()
        if action == 'summary':
            return self.summary()
        if action == 'reconcile':
            return self.reconcile_on_start()
        if action == 'register_self':
            return self.register_self()
        if action == 'install_driver':
            return self.install_driver()
        if action == 'repair_driver':
            return self.repair_driver()
        if action == 'set_enabled':
            return self.set_enabled(bool(values.get('enabled')))
        if action == 'add_apps':
            return self.add_apps([str(item) for item in values.get('paths') or []])
        if action == 'remove_app':
            return self.remove_app(str(values.get('nt_path') or ''))
        if action == 'set_group_hidden':
            return self.set_group_hidden(str(values.get('key') or ''), bool(values.get('hidden')))
        if action == 'set_node_hidden':
            return self.set_paths_hidden([str(item) for item in values.get('paths') or []],
                                         bool(values.get('hidden')))
        if action == 'hide_current':
            return self.hide_current_device(values.get('vendor'), values.get('product'),
                                            str(values.get('device_path') or ''))
        if action == 'restore_all':
            return self.reveal_all()
        if action == 'open_client':
            return self.open_client()
        return self._result(hidhide.Operation(False, f'未知的设备隐身操作：{action}'))

    def reconcile_on_exit(self) -> Dict[str, object]:
        """Remove what this program hid; leave other tools' entries alone."""
        owned = self.managed_paths()
        if not owned:
            return {'ok': True, 'message': '', 'occupied': False, 'holders': [], 'changed': False}
        result = self.set_paths_hidden(owned, False)
        if result.get('ok'):
            self._save_setting(MANAGED_PATHS_KEY, [])
        return result

    @staticmethod
    def _result(operation: hidhide.Operation) -> Dict[str, object]:
        return {'ok': bool(operation.ok), 'message': operation.message,
                'occupied': bool(operation.occupied), 'holders': list(operation.holders),
                'state': None if operation.state is None else {
                    'loaded': operation.state.loaded, 'active': operation.state.active,
                    'inverse': operation.state.inverse, 'whitelist': list(operation.state.whitelist),
                    'blacklist': list(operation.state.blacklist)}}
