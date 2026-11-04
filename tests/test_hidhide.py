"""HidHide 传输层与驱动探测的单元测试：全程不碰真实驱动。"""

import sys
from types import SimpleNamespace

import pytest

from gamepadstudio import hidhide
from gamepadstudio.hidhide import (
    Change,
    DriverInfo,
    HidHideSession,
    HidHideState,
    decode_list,
    dos_to_nt_path,
    encode_multi_sz,
    occupied_reason,
    same_paths,
    win_error_text,
)


@pytest.fixture(autouse=True)
def no_native_access(monkeypatch):
    """这个文件只测纯逻辑与探测分支，任何真实句柄都不许出现。"""
    monkeypatch.setattr(hidhide, 'win32_kernel32', lambda: None)
    monkeypatch.setattr(hidhide, 'running_hidhide_clients', lambda: [])


# ── MULTI_SZ 编解码 ────────────────────────────────────────────────
def test_multi_sz_roundtrip_including_unicode():
    items = [r'C:\Program Files\Nefarius\app.exe', r'D:\无限暖暖\game.exe',
             r'HID\VID_054C&PID_0CE6\7&123456&0&0000']
    assert decode_list(encode_multi_sz(items)) == items


def test_empty_list_encodes_with_double_terminator():
    assert encode_multi_sz([]) == b'\x00\x00\x00\x00'


def test_empty_blacklist_from_driver_is_not_a_read_failure():
    """驱动对空列表只回一个结束符；把它判成“读失败”会让所有写入被拒。"""
    assert decode_list(b'\x00\x00') == []
    assert decode_list(b'\x00\x00\x00\x00') == []


@pytest.mark.parametrize('payload', [None, b'', b'\x00', b'\x00\x00\x01'])
def test_unusable_payloads_are_reported_as_failure(payload):
    assert decode_list(payload) is None


def test_odd_length_payload_is_not_decoded():
    assert decode_list(b'\x00') is None


# ── 路径与条目比较 ─────────────────────────────────────────────────
def test_same_paths_ignores_case_and_order():
    assert same_paths([r'HID\VID_045E\a'], [r'hid\vid_045e\A'])
    assert not same_paths([r'HID\a'], [r'HID\a', r'HID\b'])


def test_dos_to_nt_path_passthrough_on_failure(monkeypatch):
    monkeypatch.setattr(hidhide, 'sys', SimpleNamespace(platform='darwin'))
    assert dos_to_nt_path(r'C:\a\b.exe') == r'C:\a\b.exe'


def test_self_app_paths_only_returns_existing_files(monkeypatch, tmp_path):
    monkeypatch.setattr(hidhide, 'sys', SimpleNamespace(platform='win32',
                                                        executable=str(tmp_path / 'missing.exe'),
                                                        _base_executable='', frozen=False))
    assert hidhide.self_app_paths() == []


def test_self_app_paths_uses_nt_form(monkeypatch, tmp_path):
    real = tmp_path / 'GamePadStudio.exe'
    real.write_bytes(b'MZ')
    monkeypatch.setattr(hidhide, 'sys', SimpleNamespace(platform='win32', executable=str(real),
                                                       _base_executable='', frozen=True))
    monkeypatch.setattr(hidhide, 'dos_to_nt_path',
                        lambda path: path.replace(str(tmp_path), '\\Device\\HarddiskVolume9\\App'))
    paths = hidhide.self_app_paths()
    assert paths and all(item.startswith('\\Device\\') for item in paths)


# ── 占用提示 ───────────────────────────────────────────────────────
def test_occupied_reason_names_known_holders(monkeypatch):
    monkeypatch.setattr(hidhide, 'running_hidhide_clients', lambda: ['HidHideClient.exe'])
    assert 'HidHideClient.exe' in occupied_reason()


def test_occupied_reason_without_known_holder_still_explains(monkeypatch):
    monkeypatch.setattr(hidhide, 'running_hidhide_clients', lambda: [])
    text = occupied_reason()
    assert '占用' in text and '刷新' in text


def test_win_error_text_includes_the_code():
    assert 'win32=5' in win_error_text(5)
    assert win_error_text(0) == ''


# ── 会话：不做抢占重试，读失败绝不写 ──────────────────────────────
def test_session_open_fails_without_kernel32():
    session = HidHideSession()
    assert session.open() is False
    assert session.error


def test_session_read_reports_not_loaded(monkeypatch):
    session = HidHideSession()
    monkeypatch.setattr(session, 'open', lambda: False)
    session.error = '无法打开设备隐身驱动。'
    state = session.read()
    assert state.loaded is False and state.error


def test_apply_refuses_when_device_is_closed(monkeypatch):
    session = HidHideSession()
    monkeypatch.setattr(session, 'open', lambda: False)
    session.error = 'busy'
    result = session.apply(Change(active=True))
    assert result.ok is False and result.message == 'busy'


def test_transact_never_writes_after_a_failed_read(monkeypatch):
    calls = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return HidHideState(loaded=False, error='读不到')

        def apply(self, change):
            calls.append(change)
            return hidhide.Operation(True)

    monkeypatch.setattr(hidhide, 'HidHideSession', lambda *a, **k: Session())
    result = hidhide.transact(lambda state: Change(blacklist=()))
    assert result.ok is False and result.message == '读不到'
    assert calls == []


def test_transact_without_change_reports_success(monkeypatch):
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return HidHideState(loaded=True, active=True, whitelist=('a',), blacklist=('b',))

    monkeypatch.setattr(hidhide, 'HidHideSession', lambda *a, **k: Session())
    result = hidhide.transact(lambda state: None)
    assert result.ok is True and result.state.active is True


# ── 驱动探测分层 ───────────────────────────────────────────────────
def test_driver_info_without_registry_or_kernel32(monkeypatch):
    monkeypatch.setattr(hidhide, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(hidhide, 'winreg', None)
    monkeypatch.setattr(hidhide.os.path, 'isfile', lambda path: False)
    monkeypatch.setattr(hidhide, '_service_state', lambda name: '')
    monkeypatch.setattr(hidhide, '_file_version', lambda path: '')

    class Closed:
        def __init__(self):
            self._handle = None
            self.occupied = False
            self.error = '未检测到运行中的设备隐身驱动。'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(hidhide, 'HidHideSession', lambda *a, **k: Closed())
    info = hidhide.driver_info()
    assert isinstance(info, DriverInfo)
    assert info.installed is False and info.loaded is False


def test_driver_info_reports_occupied_as_not_loaded(monkeypatch):
    monkeypatch.setattr(hidhide, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(hidhide, '_package_registration',
                        lambda: {'package_version': '1.5.230', 'install_path': 'C:\\x'})
    monkeypatch.setattr(hidhide, '_reg_value', lambda *a: '\\SystemRoot\\x.sys')
    monkeypatch.setattr(hidhide, '_service_state', lambda name: 'running')
    monkeypatch.setattr(hidhide, '_file_version', lambda path: '1.4.181.0')

    class Session:
        def __init__(self):
            self._handle = None
            self.occupied = True
            self.error = 'busy'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(hidhide, 'HidHideSession', lambda *a, **k: Session())
    info = hidhide.driver_info()
    assert info.installed is True and info.loaded is False and info.occupied is True


def test_ready_property_needs_both_installed_and_loaded():
    assert DriverInfo(installed=True, loaded=True).ready is True
    assert DriverInfo(installed=True, loaded=False).ready is False


# ── 安装入口的安全边界（不实际提权） ──────────────────────────────
def test_install_refuses_bootcamp(monkeypatch):
    monkeypatch.setattr(hidhide, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(hidhide, 'is_apple_bootcamp_environment', lambda: True)
    result = hidhide.install_driver()
    assert result.ok is False and 'Boot Camp' in result.message


def test_install_refuses_hash_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(hidhide, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(hidhide, 'is_apple_bootcamp_environment', lambda: False)
    fake = tmp_path / 'HidHide.exe'
    fake.write_bytes(b'not the official package')
    monkeypatch.setattr(hidhide, 'bundled_installer', lambda: str(fake))
    result = hidhide.install_driver()
    assert result.ok is False and 'SHA-256' in result.message


def test_install_reports_reboot_required(monkeypatch):
    monkeypatch.setattr(hidhide, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(hidhide, 'is_apple_bootcamp_environment', lambda: False)
    monkeypatch.setattr(hidhide, 'bundled_installer', lambda: 'C:\\setup.exe')
    monkeypatch.setattr(hidhide, '_sha256', lambda path: hidhide.INSTALLER_SHA256)
    monkeypatch.setattr(hidhide, '_run_elevated_wait', lambda program, arguments: 3010)
    result = hidhide.install_driver()
    assert result.ok is True and result.reboot_required is True


def test_cancelled_elevation_is_not_a_silent_failure(monkeypatch):
    monkeypatch.setattr(hidhide, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(hidhide, 'is_apple_bootcamp_environment', lambda: False)
    monkeypatch.setattr(hidhide, 'bundled_installer', lambda: 'C:\\setup.exe')
    monkeypatch.setattr(hidhide, '_sha256', lambda path: hidhide.INSTALLER_SHA256)
    monkeypatch.setattr(hidhide, '_run_elevated_wait', lambda program, arguments: 1223)
    result = hidhide.install_driver()
    assert result.ok is False and result.cancelled is True


def test_installer_is_bundled_and_hashed():
    path = hidhide.bundled_installer()
    assert path, '官方安装包必须随包发布，否则一键安装不可用'
    assert hidhide._sha256(path) == hidhide.INSTALLER_SHA256
