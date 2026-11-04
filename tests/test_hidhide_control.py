"""设备隐身新分层（会话 / 控制 / 面板）的行为契约。

全部用假驱动，测试机上不碰真实 HidHide 配置。
"""

import sys
from types import SimpleNamespace

import pytest

from gamepadstudio import hidhide
from gamepadstudio import hidhide_control as control
from gamepadstudio import hidhide_devices


def state(active=False, inverse=False, whitelist=(), blacklist=(), loaded=True,
          occupied=False, error=''):
    return hidhide.HidHideState(loaded=loaded, active=active, inverse=inverse,
                                whitelist=tuple(whitelist), blacklist=tuple(blacklist),
                                occupied=occupied, error=error)


class FakeDriver:
    """Records every write and applies it to in-memory lists, like the driver would."""

    def __init__(self, current=None, open_error=''):
        self.current = current or state()
        self.open_error = open_error
        self.writes = []

    def session(self, *args, **kwargs):
        driver = self

        class Session:
            def __init__(self_inner):
                self_inner.occupied = bool(driver.open_error)
                self_inner.holders = ()
                self_inner.error = driver.open_error
                self_inner._handle = None if driver.open_error else object()

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def open(self_inner):
                return self_inner._handle is not None

            def close(self_inner):
                pass

            def read(self_inner):
                if self_inner._handle is None:
                    return state(loaded=False, occupied=self_inner.occupied, error=driver.open_error)
                return driver.current

            def apply(self_inner, change):
                if self_inner._handle is None:
                    return hidhide.Operation(False, driver.open_error, True)
                driver.writes.append(change)
                merged = SimpleNamespace(**driver.current.__dict__)
                if change.whitelist is not None:
                    merged.whitelist = tuple(change.whitelist)
                if change.blacklist is not None:
                    merged.blacklist = tuple(change.blacklist)
                if change.active is not None:
                    merged.active = bool(change.active)
                driver.current = hidhide.HidHideState(**vars(merged))
                return hidhide.Operation(True, '', False, (), driver.current)

        return Session()


@pytest.fixture
def isolated(monkeypatch):
    """No registry, no real driver, no device enumeration."""
    monkeypatch.setattr(hidhide, 'win32_kernel32', lambda: None)
    return monkeypatch


def controller_with(driver, store=None):
    hidhide.HidHideSession = driver.session
    return control.HidHideController(store=store)


# ── 会话层 ──────────────────────────────────────────────────────────
def test_read_failure_is_never_treated_as_empty_lists(isolated):
    """被占用时读不到列表；把“读不到”当“空”会清掉别的工具的条目。"""
    driver = FakeDriver(open_error=hidhide.occupied_reason(['HandheldCompanion.exe']))
    controller = control.HidHideController()
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    snapshot = controller.snapshot()
    assert snapshot['state']['loaded'] is False
    assert snapshot['state']['occupied'] is True
    assert snapshot['devices'] == []


def test_occupied_write_is_refused_with_reason(isolated):
    driver = FakeDriver(open_error=hidhide.occupied_reason(['HidHideClient.exe']))
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    result = control.HidHideController().set_enabled(True)
    assert result['ok'] is False and result['occupied'] is True
    assert 'HidHideClient.exe' in result['message']
    assert driver.writes == []


def test_empty_blacklist_roundtrip_is_not_read_as_failure(isolated):
    """驱动对空列表只回一个 UTF-16 结束符，解码不能把它判成“读失败”。"""
    assert hidhide.decode_list(b'\x00\x00') == []
    assert hidhide.decode_list(b'\x00\x00\x00\x00') == []
    assert hidhide.decode_list(None) is None
    assert hidhide.decode_list(b'\x00') is None


# ── 应用列表 ────────────────────────────────────────────────────────
def test_adding_app_keeps_foreign_entries(isolated, monkeypatch):
    foreign = [r'\Device\HarddiskVolume3\Other\tool.exe']
    driver = FakeDriver(current=state(whitelist=foreign))
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    monkeypatch.setattr(hidhide, 'self_app_paths', lambda: [r'\Device\HarddiskVolume3\gps\GamePadStudio.exe'])
    result = control.HidHideController().add_apps([r'C:\Games\poegame.exe'])
    assert result['ok'] is True
    written = driver.writes[-1].whitelist
    assert foreign[0] in written
    assert any('poegame.exe' in item for item in written)


def test_self_entry_cannot_be_removed(isolated, monkeypatch):
    self_path = r'\Device\HarddiskVolume3\gps\GamePadStudio.exe'
    driver = FakeDriver(current=state(whitelist=[self_path]))
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    monkeypatch.setattr(hidhide, 'self_app_paths', lambda: [self_path])
    result = control.HidHideController().remove_app(self_path)
    assert result['ok'] is False and '自身' in result['message']
    assert driver.writes == []


def test_inverse_mode_does_not_register_self(isolated, monkeypatch):
    """反向白名单下列表含义反转，登记自身反而会把自己禁掉。"""
    self_path = r'\Device\HarddiskVolume3\gps\GamePadStudio.exe'
    driver = FakeDriver(current=state(inverse=True, whitelist=[]))
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    monkeypatch.setattr(hidhide, 'self_app_paths', lambda: [self_path])
    control.HidHideController().set_enabled(True)
    assert all(change.whitelist is None for change in driver.writes)


# ── 设备隐藏路径 ────────────────────────────────────────────────────
def group(children, base='USB\\VID_054C&PID_0CE6\\1&2&3', base_class='usb', child_count=0):
    interfaces = tuple(hidhide_devices.HidInterface(instance_id=node, present=True,
                                                    xinput_instance_id=xusb, base_container=base,
                                                    base_class=base_class, child_count=child_count)
                       for node, xusb in children)
    return hidhide_devices.DeviceGroup(key=base, name='Test Pad', interfaces=interfaces,
                                       base_container=base, base_class=base_class,
                                       child_count=child_count)


def test_whole_group_adds_container_children_and_xusb():
    g = group([('HID\\PAD\\COL01', 'USB\\VID_054C&PID_0CE6&MI_03\\1'),
               ('HID\\PAD\\COL02', '')], base_class='hidclass', child_count=2)
    paths = hidhide_devices.hide_paths_for(g, tuple(item.instance_id for item in g.interfaces))
    assert 'USB\\VID_054C&PID_0CE6\\1&2&3' in paths          # 容器节点
    assert 'HID\\PAD\\COL01' in paths and 'HID\\PAD\\COL02' in paths
    assert 'USB\\VID_054C&PID_0CE6&MI_03\\1' in paths        # XInput 通道


def test_single_child_does_not_hide_the_whole_container():
    g = group([('HID\\PAD\\COL01', 'USB\\XUSB1'), ('HID\\PAD\\COL02', '')], child_count=2)
    paths = hidhide_devices.hide_paths_for(g, ('HID\\PAD\\COL01',))
    assert paths == ['HID\\PAD\\COL01', 'USB\\XUSB1']


def test_container_is_not_blocked_when_class_is_not_hid_or_xbox():
    g = group([('HID\\PAD\\COL01', '')], base='BTHUSB\\RAD1', base_class='bthusb', child_count=1)
    paths = hidhide_devices.hide_paths_for(g, ('HID\\PAD\\COL01',))
    assert paths == ['HID\\PAD\\COL01']


def test_xinput_composite_target_blocks_the_container_that_carries_xinput():
    """360 接收器实测结构：容器节点本身就是 XnaComposite，HID 子节点没有 XInput 兄弟节点。
    只写 HID 子节点的话 XInput 游戏照旧读得到 —— 双重输入的老病根就在这。"""
    g = group([('HID\\VID_045E&PID_028E\\7&1&0', '')],
              base='USB\\VID_045E&PID_028E\\01', base_class='xnacomposite', child_count=1)
    paths = hidhide_devices.hide_paths_for(g, tuple(item.instance_id for item in g.interfaces))
    assert paths == ['USB\\VID_045E&PID_028E\\01', 'HID\\VID_045E&PID_028E\\7&1&0']


def test_xbox_composite_target_blocks_the_container_too():
    """Xbox One/Series 走 XboxComposite 时同理。"""
    g = group([('HID\\VID_045E&PID_0B12\\7&2&0', '')],
              base='USB\\VID_045E&PID_0B12\\0', base_class='xboxcomposite', child_count=1)
    paths = hidhide_devices.hide_paths_for(g, tuple(item.instance_id for item in g.interfaces))
    assert 'USB\\VID_045E&PID_0B12\\0' in paths


def test_hiding_a_container_class_target_writes_both_nodes(isolated):
    """控制层拿到的黑名单增量必须同时含容器与 HID 子节点。"""
    g = group([('HID\\VID_045E&PID_028E\\7&1&0', '')],
              base='USB\\VID_045E&PID_028E\\01', base_class='xnacomposite', child_count=1)
    driver = FakeDriver(current=state(active=False, blacklist=[]))
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    paths = hidhide_devices.hide_paths_for(g, tuple(item.instance_id for item in g.interfaces))
    control.HidHideController().set_paths_hidden(paths, True)
    assert sorted(driver.writes[-1].blacklist) == sorted(paths)
    assert driver.writes[-1].active is True


def test_hiding_a_device_registers_self_first(isolated, monkeypatch):
    self_path = r'\Device\HarddiskVolume3\gps\GamePadStudio.exe'
    driver = FakeDriver(current=state(whitelist=[]))
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    monkeypatch.setattr(hidhide, 'self_app_paths', lambda: [self_path])
    result = control.HidHideController().set_paths_hidden(['HID\\PAD\\COL01'], True)
    assert result['ok'] is True
    change = driver.writes[-1]
    assert self_path in change.whitelist and 'HID\\PAD\\COL01' in change.blacklist
    assert change.active is True


def test_revealing_keeps_foreign_hidden_devices(isolated):
    foreign = 'HID\\OTHER\\WHEEL'
    driver = FakeDriver(current=state(active=True, blacklist=[foreign, 'HID\\PAD\\COL01']))
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    control.HidHideController().set_paths_hidden(['HID\\PAD\\COL01'], False)
    change = driver.writes[-1]
    assert change.blacklist == (foreign,)
    assert change.active is None, '仍有其他隐藏设备时不得关掉总开关'


def test_reveal_all_clears_blacklist_and_switch(isolated):
    driver = FakeDriver(current=state(active=True, blacklist=['HID\\A', 'HID\\B']))
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    result = control.HidHideController().reveal_all()
    assert result['ok'] is True
    assert driver.writes[-1].blacklist == () and driver.writes[-1].active is False


def test_exit_reconcile_only_touches_our_paths(isolated):
    driver = FakeDriver(current=state(active=True, blacklist=['HID\\OURS', 'HID\\THEIRS']))
    isolated.setattr(hidhide, 'HidHideSession', driver.session)

    class Store:
        data = {control.MANAGED_PATHS_KEY: ['HID\\OURS']}

        def save(self):
            pass

    result = control.HidHideController(store=Store()).reconcile_on_exit()
    assert result['ok'] is True
    assert driver.writes[-1].blacklist == ('HID\\THEIRS',)
    assert Store.data[control.MANAGED_PATHS_KEY] == []


def test_unknown_group_is_reported_not_guessed(isolated, monkeypatch):
    driver = FakeDriver(current=state())
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    monkeypatch.setattr(hidhide_devices, 'enumerate_groups', lambda: [group([('HID\\PAD\\C1', '')])])
    result = control.HidHideController().set_group_hidden('HID\\GONE', True)
    assert result['ok'] is False and '刷新' in result['message']
    assert driver.writes == []


# ── 面板状态 ────────────────────────────────────────────────────────
def panel_for(monkeypatch, snapshot):
    from gamepadstudio.hidhide_ui import HidHidePanel
    from PySide6.QtWidgets import QApplication, QWidget
    app = QApplication.instance() or QApplication([])

    class Owner(QWidget):
        def __init__(self):
            super().__init__()
            self.snapshot = {}
            self.calls = []

        def hidhide(self, action, **payload):
            self.calls.append((action, payload))
            return dict(snapshot)

        def notify(self, text):
            pass

    monkeypatch.setattr(control.HidHideController, 'snapshot', lambda self: snapshot)
    owner = Owner()
    return HidHidePanel(owner), owner


BASE_SNAPSHOT = {
    'info': {'installed': True, 'loaded': True, 'package_version': '1.5.230',
             'driver_version': '1.4.181.0', 'install_path': 'C:\\x', 'occupied': False},
    'state': {'loaded': True, 'active': False, 'inverse': False, 'whitelist': [], 'blacklist': [],
              'occupied': False, 'holders': [], 'error': ''},
    'apps': [], 'devices': [], 'managed': {'paths': [], 'apps': []}, 'enabled': False,
    'can_write': True,
}


def test_panel_without_driver_explains_and_blocks(monkeypatch):
    snapshot = dict(BASE_SNAPSHOT,
                    info=dict(BASE_SNAPSHOT['info'], installed=False, loaded=False),
                    state=dict(BASE_SNAPSHOT['state'], loaded=False))
    panel, owner = panel_for(monkeypatch, snapshot)
    assert panel.master.isEnabled() is False
    assert panel.btn_add_app.isEnabled() is False
    assert '未安装' in panel.banner.text()
    assert '未安装' in panel.status_pill.text()


def test_panel_occupied_names_the_holder(monkeypatch):
    snapshot = dict(BASE_SNAPSHOT,
                    state=dict(BASE_SNAPSHOT['state'], occupied=True,
                               holders=['HandheldCompanion.exe'],
                               error=hidhide.occupied_reason(['HandheldCompanion.exe'])))
    panel, owner = panel_for(monkeypatch, snapshot)
    assert panel.master.isEnabled() is False
    assert 'HandheldCompanion.exe' in panel.banner.text()


def test_panel_inverse_mode_warns(monkeypatch):
    snapshot = dict(BASE_SNAPSHOT, state=dict(BASE_SNAPSHOT['state'], inverse=True))
    panel, owner = panel_for(monkeypatch, snapshot)
    assert '反向' in panel.banner.text()


def test_panel_master_toggle_writes_only_on_user_action(monkeypatch):
    panel, owner = panel_for(monkeypatch, BASE_SNAPSHOT)
    owner.calls.clear()
    panel.master.setChecked(True)
    assert ('set_enabled', {'enabled': True}) in owner.calls


def test_panel_group_checkbox_targets_that_group_only(monkeypatch):
    device = {'key': 'USB\\PAD', 'name': 'Test Pad', 'vid_pid': '054C:0CE6', 'connected': True,
              'gaming': True, 'hidden': False, 'partly_hidden': False, 'owned': False,
              'base_container': 'USB\\PAD', 'base_class': 'usb',
              'interfaces': [{'instance_id': 'HID\\PAD\\C1', 'label': 'Gamepad', 'collection': '01/04',
                              'present': True, 'readable': True, 'denied': False, 'gaming': True,
                              'xinput': 'USB\\PAD\\XUSB', 'hidden': False}]}
    snapshot = dict(BASE_SNAPSHOT, devices=[device])
    panel, owner = panel_for(monkeypatch, snapshot)
    panel.filter_present.setChecked(False)
    panel.fill_devices()
    owner.calls.clear()
    from PySide6.QtCore import Qt
    panel.tree.topLevelItem(0).setCheckState(0, Qt.Checked)
    calls = [call for call in owner.calls if call[0] == 'set_group_hidden']
    assert calls == [('set_group_hidden', {'key': 'USB\\PAD', 'hidden': True})]


def test_panel_empty_lists_explain_themselves(monkeypatch):
    panel, owner = panel_for(monkeypatch, BASE_SNAPSHOT)
    assert '列表为空' in panel.apps.item(0).text()
    assert '没有符合条件的设备' in panel.tree.topLevelItem(0).text(0)


# ── 启动对账与显式登记 ─────────────────────────────────────────────
def test_reconcile_flags_missing_self_registration(isolated, monkeypatch):
    """过滤已生效但我们不在白名单里 = 用户会以为手柄坏了，必须报出来。"""
    self_path = r'\Device\HarddiskVolume3\gps\GamePadStudio.exe'
    driver = FakeDriver(current=state(active=True, whitelist=[], blacklist=[r'HID\PAD']))
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    monkeypatch.setattr(hidhide, 'self_app_paths', lambda: [self_path])
    monkeypatch.setattr(hidhide, 'driver_info',
                        lambda: hidhide.DriverInfo(installed=True, loaded=True))
    result = control.HidHideController().reconcile_on_start()
    assert result['status'] == 'access_missing' and '重新登记' in result['message']
    assert driver.writes == [], '对账只报告，不擅自写驱动'


def test_reconcile_prunes_stale_managed_paths(isolated, monkeypatch):
    driver = FakeDriver(current=state(blacklist=[]))
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    monkeypatch.setattr(hidhide, 'driver_info',
                        lambda: hidhide.DriverInfo(installed=True, loaded=True))

    class Store:
        data = {control.MANAGED_PATHS_KEY: [r'HID\GONE']}

        def save(self):
            pass

    store = Store()
    result = control.HidHideController(store=store).reconcile_on_start()
    assert result['status'] == 'ready' and result['stale_paths'] == [r'HID\GONE']
    assert store.data[control.MANAGED_PATHS_KEY] == []


def test_reconcile_reports_occupied_without_guessing(isolated):
    driver = FakeDriver(open_error=hidhide.occupied_reason(['DS4Windows.exe']))
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    isolated.setattr(hidhide, 'driver_info',
                             lambda: hidhide.DriverInfo(installed=True, loaded=False, occupied=True))
    result = control.HidHideController().reconcile_on_start()
    assert result['status'] == 'occupied' and 'DS4Windows.exe' in result['message']


def test_register_self_appends_without_touching_others(isolated, monkeypatch):
    foreign = r'\Device\HarddiskVolume3\Other	ool.exe'
    self_path = r'\Device\HarddiskVolume3\gps\GamePadStudio.exe'
    driver = FakeDriver(current=state(whitelist=[foreign]))
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    monkeypatch.setattr(hidhide, 'self_app_paths', lambda: [self_path])
    result = control.HidHideController().register_self()
    assert result['ok'] is True
    assert set(driver.writes[-1].whitelist) == {foreign, self_path}


def test_install_and_repair_actions_are_dispatchable(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(hidhide, 'install_driver',
                        lambda: calls.append('install') or hidhide.InstallResult(True, '需要重启', reboot_required=True))
    monkeypatch.setattr(hidhide, 'start_driver_service',
                        lambda: calls.append('repair') or hidhide.InstallResult(True, '已启动'))
    controller = control.HidHideController()
    assert controller.dispatch('install_driver')['reboot_required'] is True
    assert controller.dispatch('repair_driver')['ok'] is True
    assert calls == ['install', 'repair']


def owner_for(responses):
    """一个只按脚本回话的 owner；面板必须通过它访问驱动，自己不碰 IOCTL。"""
    from PySide6.QtWidgets import QWidget

    class Owner(QWidget):
        def __init__(self):
            super().__init__()
            self.snapshot = {}
            self.calls = []

        def hidhide(self, action, **payload):
            self.calls.append((action, payload))
            return dict(responses.get(action, {'ok': True, 'message': ''}))

        def notify(self, text):
            pass

    return Owner()


def test_panel_surfaces_access_warning_and_register_button():
    """对账发现“过滤生效但我们没登记”时，面板要显示原因并给出修复按钮。"""
    from gamepadstudio.hidhide_ui import HidHidePanel
    snapshot = dict(BASE_SNAPSHOT,
                    state=dict(BASE_SNAPSHOT['state'], active=True, blacklist=[r'HID\PAD']),
                    reconcile={'status': 'access_missing',
                               'message': '设备隐身已开启，但本程序不在允许访问的列表里，点「重新登记本程序」恢复。'})
    owner = owner_for({'snapshot': snapshot})
    panel = HidHidePanel(owner)
    # 对话框本身没 show，所以只能断言“未被显式隐藏”。
    assert panel.btn_register.isHidden() is False
    assert '重新登记' in panel.banner.text()
    owner.calls.clear()
    panel.btn_register.click()
    assert ('register_self', {}) in owner.calls


def test_panel_install_button_switches_between_install_and_repair():
    from gamepadstudio.hidhide_ui import HidHidePanel
    not_installed = dict(BASE_SNAPSHOT,
                         info=dict(BASE_SNAPSHOT['info'], installed=False, loaded=False),
                         state=dict(BASE_SNAPSHOT['state'], loaded=False))
    installed_only = dict(BASE_SNAPSHOT,
                          info=dict(BASE_SNAPSHOT['info'], installed=True, loaded=False),
                          state=dict(BASE_SNAPSHOT['state'], loaded=False))
    fresh = HidHidePanel(owner_for({'snapshot': not_installed}))
    assert fresh.btn_driver.text() == '一键安装驱动'
    repair = HidHidePanel(owner_for({'snapshot': installed_only}))
    assert '修复' in repair.btn_driver.text()
