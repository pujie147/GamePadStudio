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


# ── 设备来源判定：下面全是本机实测抓下来的注册表值 ───────────────────
VIEM_HID = r'HID\VID_045E&PID_028E&IG_00\3&34f4d81e&0&0000'
VIEM_XUSB = r'USB\VID_045E&PID_028E&IG_00\2&DEE0F28&1&00'
VIEM_CONTAINER = r'USB\VID_045E&PID_028E\01'
VIEM_ROOT = r'ROOT\SYSTEM\0001'
HC_KBD = r'HID\HID_DEVICE_SYSTEM_VHF&Col01\2&88d67f4&0&0000'
HC_VHF = r'VHF\HID_DEVICE_SYSTEM_VHF\1&1A590E2C&0&{6225EBE9-31D6-5366-AB1A-2AF1FDD27999}'
HC_ROOT = r'ROOT\SYSTEM\0002'
BT_KBD = r'HID\{00001124-0000-1000-8000-00805f9b34fb}_VID&00020a5c_PID&8502&Col01\8&140e5071&0&0000'
BT_ENUM = r'BTHENUM\{00001124-0000-1000-8000-00805F9B34FB}_VID&00020A5C_PID&8502\7&935ECBD&0&0018003EE5CC_C00000000'
BT_RADIO = r'USB\VID_33FA&PID_0001\5&2EC8F72A&0&2'
LOOKALIKE = r'USB\VID_1234&PID_5678\0'

REGISTRY = {
    VIEM_ROOT: {'Service': ('ViGEmBus',), 'HardwareID': (r'Nefarius\ViGEmBus\Gen1',),
                'DeviceDesc': '@oem53.inf,%vigembus.devicedesc%;Nefarius Virtual Gamepad Emulation Bus'},
    VIEM_CONTAINER: {'Service': ('xusb22',), 'HardwareID': (r'USB\VID_045E&PID_028E',),
                     'DeviceDesc': '@xusb22.inf,%xusb22.devicename.wired%;Xbox 360 Controller for Windows'},
    HC_ROOT: {'Service': ('uuycinput',), 'HardwareID': (r'netease\uuycinput',),
              'DeviceDesc': '@oem51.inf,%devicedesc%;UUYCInput HID'},
    HC_VHF: {'Service': ('mshidkmdf',),
             'HardwareID': ('HID_DEVICE_SYSTEM_VHF', r'VHF\VID_5E2A&PID_71B4')},
    # 描述里写满 ViGEm，Service/HardwareID 干净 —— 它就不是 ViGEm 的
    LOOKALIKE: {'Service': ('somegamepad',), 'HardwareID': (r'USB\VID_1234&PID_5678',),
                'DeviceDesc': '@oem99.inf,%vigembus.devicedesc%;ViGEm lookalike'},
}
CHAINS = {VIEM_HID: [VIEM_XUSB, VIEM_CONTAINER, VIEM_ROOT, 'HTREE\\ROOT\\0'],
          HC_KBD: [HC_VHF, HC_ROOT, 'HTREE\\ROOT\\0'],
          BT_KBD: [BT_ENUM, r'BTH\MS_BTHBRB\6&F7CA2CA&0&1', BT_RADIO, r'USB\ROOT_HUB30\4&2FBF7A17&0&0'],
          LOOKALIKE: [r'USB\ROOT_HUB30\4&2FBF7A17&0&0']}


@pytest.fixture
def real_registry(monkeypatch):
    monkeypatch.setattr(hidhide_devices, '_multi_value',
                        lambda instance, name: REGISTRY.get(instance, {}).get(name, ()))
    monkeypatch.setattr(hidhide_devices, 'ancestors',
                        lambda instance, limit=6: list(CHAINS.get(instance, [])))


def test_vigem_pad_is_identified_by_its_bus_service(real_registry):
    origin, detail = hidhide_devices.classify_origin(VIEM_HID)
    assert origin == hidhide_devices.ORIGIN_VIGEM
    assert detail == f'Service=ViGEmBus @ {VIEM_ROOT}'


def test_other_tools_vhf_device_is_virtual_and_not_claimed(real_registry):
    origin, detail = hidhide_devices.classify_origin(HC_KBD)
    assert origin == hidhide_devices.ORIGIN_VHF
    assert 'HID_DEVICE_SYSTEM_VHF' in detail


def test_real_devices_stay_physical(real_registry):
    assert hidhide_devices.classify_origin(BT_KBD)[0] == hidhide_devices.ORIGIN_PHYSICAL


def test_localized_description_mentioning_vigem_does_not_fool_the_check(real_registry):
    """DeviceDesc 是 MUI 间接串，随系统语言变；驱动自定义 INF 还能随便写。"""
    assert hidhide_devices.classify_origin(LOOKALIKE)[0] == hidhide_devices.ORIGIN_PHYSICAL


def test_ancestors_walk_phantom_nodes(monkeypatch):
    """手柄拔掉以后仍要认得出是谁的虚拟设备：NORMAL 定位对不在场的节点直接失败，链子会断。"""
    seen = []
    monkeypatch.setattr(hidhide_devices, '_prepare', lambda: (None, object(), None))

    def fake_parent(cm, instance, flags=0):
        seen.append(flags)
        return {'a': 'b', 'b': 'c'}.get(instance, '')

    monkeypatch.setattr(hidhide_devices, 'parent_of', fake_parent)
    assert hidhide_devices.ancestors('a') == ['b', 'c']
    assert seen == [hidhide_devices.CM_LOCATE_DEVNODE_PHANTOM] * 3   # 含最后一次走到空为止


def synthetic_group(key, name, origin, detail):
    interface = hidhide_devices.HidInterface(instance_id=key + '\\C1', present=True)
    return hidhide_devices.DeviceGroup(key=key, name=name, interfaces=(interface,),
                                       base_container=key, base_class='xnacomposite',
                                       origin=origin, origin_detail=detail)


def test_snapshot_carries_origin_and_ownership(isolated, monkeypatch):
    driver = FakeDriver(current=state())
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    ours = synthetic_group(VIEM_CONTAINER, 'Controller (XBOX 360 For Windows)', 'vigem',
                           f'Service=ViGEmBus @ {VIEM_ROOT}')
    foreign = synthetic_group(HC_VHF, 'HID Keyboard Device', 'vhf', 'HardwareID=HID_DEVICE_SYSTEM_VHF')
    monkeypatch.setattr(hidhide_devices, 'enumerate_groups', lambda: [ours, foreign])
    rows = {row['key']: row for row in
            control.HidHideController(own_keys=lambda: (ours.key,)).snapshot()['devices']}
    assert rows[VIEM_CONTAINER]['created_by_us'] is True
    assert rows[VIEM_CONTAINER]['origin_detail'].startswith('Service=ViGEmBus')
    assert rows[HC_VHF]['created_by_us'] is False


def test_ownership_provider_failure_degrades_to_no_badge(isolated, monkeypatch):
    driver = FakeDriver(current=state())
    isolated.setattr(hidhide, 'HidHideSession', driver.session)
    monkeypatch.setattr(hidhide_devices, 'enumerate_groups',
                        lambda: [synthetic_group(VIEM_CONTAINER, 'Pad', 'vigem', '')])

    def boom():
        raise RuntimeError('拿不到持有者')

    rows = {row['key']: row for row in
            control.HidHideController(own_keys=boom).snapshot()['devices']}
    assert rows[VIEM_CONTAINER]['created_by_us'] is False


def test_panel_badges_the_origin_and_ownership(monkeypatch):
    def row(key, name, origin, created, detail):
        return {'key': key, 'name': name, 'vid_pid': '', 'connected': True, 'gaming': True,
                'hidden': False, 'partly_hidden': False, 'owned': False,
                'base_container': key, 'base_class': 'xnacomposite', 'origin': origin,
                'origin_detail': detail, 'created_by_us': created, 'interfaces': []}

    snapshot = dict(BASE_SNAPSHOT, devices=[
        row(VIEM_CONTAINER, 'Our Pad', 'vigem', True, f'Service=ViGEmBus @ {VIEM_ROOT}'),
        row(HC_VHF, 'Other Virtual', 'vhf', False, 'HardwareID=HID_DEVICE_SYSTEM_VHF'),
        row(BT_RADIO, 'Real Pad', 'physical', False, '')])
    panel, owner = panel_for(monkeypatch, snapshot)
    panel.fill_devices()
    texts = [panel.tree.topLevelItem(index).text(0)
             for index in range(panel.tree.topLevelItemCount())]
    assert 'Our Pad  ·  本程序创建' in texts
    assert 'Other Virtual  ·  虚拟设备 · VHF' in texts
    assert 'Real Pad' in texts
    tooltip = panel.tree.topLevelItem(0).toolTip(0)
    assert '判定依据' in tooltip and 'Service=ViGEmBus' in tooltip
