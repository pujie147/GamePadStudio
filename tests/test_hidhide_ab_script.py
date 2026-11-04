"""The A/B script's own logic: verdict correctness and self-cleaning probes.

These run without a controller and never touch the driver — they pin the two
things that made a manual run misleading or harmful.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from gamepadstudio import hidhide, hidhide_devices

_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'hidhide_ab_test.py'
_SPEC = importlib.util.spec_from_file_location('hidhide_ab_script', _PATH)
ab = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ab)

GROUP = {'key': 'X', 'name': 'Wireless Controller', 'vid_pid': '0A5C:8500'}
XINPUT = 'xinput (xinput1_4.dll)'


def sdl_channel(devices, channel='sdl_dinput'):
    return {'channel': channel, 'error': '', 'devices': devices}


def xinput_channel(connected):
    return {'channel': XINPUT, 'error': '',
            'devices': [{'index': slot, 'connected': slot in connected, 'code': 0 if slot in connected else 1167}
                        for slot in range(4)]}


def probe(*channels):
    return {'channels': list(channels), 'whitelisted': False,
            'driver': {'loaded': True, 'active': False, 'inverse': False, 'blacklist': []}}


def pad(name='Wireless Controller'):
    return [{'index': 0, 'name': name, 'game_controller': True, 'open_ok': True,
             'vendor': 0x0A5C, 'product': 0x8500, 'path': 'hid'}]


# ── 枚举结果 → 每通道结论 ──────────────────────────────────────────
def test_channel_devices_lists_names_for_sdl_and_online_slots_for_xinput():
    assert ab.channel_devices(sdl_channel(pad())) == ['Wireless Controller [0A5C:8500]']
    assert ab.channel_devices(xinput_channel({0})) == ['slot0']
    assert ab.channel_devices(xinput_channel(set())) == []


def test_target_matches_uses_vid_pid_or_product_name():
    assert ab.target_matches('Wireless Controller [0A5C:8500]', GROUP)
    assert ab.target_matches('Wireless Controller', GROUP)
    assert not ab.target_matches('Logitech G29 [046D:C294]', GROUP)


def test_xinput_channel_counts_as_seeing_the_target():
    """槽位名字匹配不上 vid:pid —— 早先因此把「XInput 没被隐藏」判成了通过。"""
    observed = ab.observations(probe(xinput_channel({1})), dict(GROUP, xinput_backed=True))
    assert observed[XINPUT]['see_target'] is True
    assert observed[XINPUT]['xinput'] is True


def test_xinput_slots_are_ignored_when_the_target_is_not_xinput_backed():
    """本机同时插着 360 接收器（XInput slot0）和蓝牙 Series 手柄：
    蓝牙目标的结论不能被另一台设备的槽位污染。"""
    observed = ab.observations(probe(xinput_channel({0})), GROUP)
    assert observed[XINPUT]['see_target'] is False
    assert observed[XINPUT]['matches'] == []
    assert '不属于本目标' in observed[XINPUT]['note']


def test_a_failed_channel_reports_error_and_not_visibility():
    observed = ab.observations(probe({'channel': 'sdl_raw', 'devices': [], 'error': 'SDL_Init 失败'}), GROUP)
    assert observed['sdl_raw'] == {'see_target': False, 'items': [], 'matches': [], 'error': 'SDL_Init 失败'}


# ── 前后对比结论 ───────────────────────────────────────────────────
def test_compare_reports_xinput_still_visible_when_only_hid_got_hidden():
    backed = dict(GROUP, xinput_backed=True)
    before = ab.observations(probe(sdl_channel(pad()), xinput_channel({0})), backed)
    after = ab.observations(probe(sdl_channel([]), xinput_channel({0})), backed)
    verdict = ab.compare(before, after)
    assert verdict['gone'] == ['sdl_dinput']
    assert verdict['still'] == [XINPUT]


def test_compare_reports_total_hiding():
    backed = dict(GROUP, xinput_backed=True)
    before = ab.observations(probe(sdl_channel(pad()), xinput_channel({0})), backed)
    after = ab.observations(probe(sdl_channel([]), xinput_channel(set())), backed)
    assert ab.compare(before, after) == {'gone': ['sdl_dinput', XINPUT], 'still': []}


def test_show_surfaces_a_probe_that_never_started(capsys):
    assert ab.show('探针', {'error': 'boom'}, GROUP) == {}
    assert 'boom' in capsys.readouterr().out


# ── 「普通进程」必须是另一个二进制 ────────────────────────────────
def test_ordinary_probe_is_a_different_image_from_the_registered_ones(monkeypatch, tmp_path):
    """自我登记覆盖 python.exe/pythonw.exe/_base_executable 全家，
    所以未授权视角必须是另一个文件名的二进制。"""
    source = tmp_path / 'python.exe'
    source.write_bytes(b'MZ')
    monkeypatch.setattr(sys, '_base_executable', str(source), raising=False)
    exe, clone = ab.ordinary_exe('')
    assert exe == str(tmp_path / 'hidhide_padprobe.exe') and clone == exe
    assert Path(clone).exists()
    ab._drop_clone(clone)
    assert not Path(clone).exists()
    assert ab.ordinary_exe(r'C:\game.exe') == (r'C:\game.exe', '')


# ── XInput 类目标：槽位才是判据 ────────────────────────────────────
def composite_group(base_class, xinput=''):
    """360 接收器的实测结构：容器就是 XnaComposite 节点，HID 子节点没有 XInput 兄弟。"""
    return hidhide_devices.DeviceGroup(
        key='USB\\VID_045E&PID_028E\\01', name='Controller (XBOX 360 For Windows)',
        interfaces=(hidhide_devices.HidInterface(instance_id='HID\\VID_045E&PID_028E\\7&1&0',
                                                 present=True, vendor_id=0x045E, product_id=0x028E,
                                                 usage_page=0x05, usage=0x05,
                                                 xinput_instance_id=xinput),),
        base_container='USB\\VID_045E&PID_028E\\01', base_class=base_class, child_count=1)


def test_composite_targets_are_judged_through_the_xinput_channel():
    assert ab.xinput_backed(composite_group('xnacomposite')) is True
    assert ab.xinput_backed(composite_group('xboxcomposite')) is True
    assert ab.xinput_backed(composite_group('hidclass', xinput='USB\\XUSB\\1')) is True


def test_bluetooth_target_is_not_judged_by_foreign_xinput_slots():
    """本机实测：蓝牙 Series 手柄当目标时 slot0 属于 360 接收器。"""
    bluetooth = hidhide_devices.DeviceGroup(
        key='BTHLE\\DEV_14CB65E29FED', name='HID-compliant game controller',
        interfaces=(hidhide_devices.HidInterface(
            instance_id='HID\\{00001812-0000-1000-8000-00805F9B34FB}&DEV&VID_045E&PID_0B13\\9&1',
            present=True, vendor_id=0x045E, product_id=0x0B13, usage_page=0x05, usage=0x05),),
        base_container='BTHLE\\DEV_14CB65E29FED', base_class='bluetooth', child_count=1)
    assert ab.xinput_backed(bluetooth) is False


def test_xinput_target_fails_the_verdict_when_the_slot_survives():
    """隐藏了 HID 子节点、容器没进黑名单 —— XInput 游戏仍读得到，必须判不通过。"""
    backed = {'name': 'Controller (XBOX 360 For Windows)', 'vid_pid': '045E:028E', 'xinput_backed': True}
    pad360 = [{'index': 0, 'name': 'Controller (XBOX 360 For Windows) [045E:028E]',
               'game_controller': True, 'open_ok': True, 'vendor': 0x045E, 'product': 0x028E}]
    before = ab.observations(probe(sdl_channel(pad360), xinput_channel({0})), backed)
    after = ab.observations(probe(sdl_channel([]), xinput_channel({0})), backed)
    verdict = ab.compare(before, after)
    assert verdict['gone'] == ['sdl_dinput']
    assert verdict['still'] == [XINPUT]


def test_slot_parity_promotes_xinput_into_the_verdict():
    """实测：蓝牙 LE 手柄经微软「XInput 兼容输入设备」驱动同时有 HID 面和 slot0。
    只按设备类判定会漏报，按基线槽位一致性归因才判得准、也判得更严。"""
    unbacked = dict(GROUP, xinput_backed=False)
    backed = dict(GROUP, xinput_backed=True)
    raw_before = probe(sdl_channel(pad()), xinput_channel({0}))
    raw_before_allowed = probe(sdl_channel(pad()), xinput_channel({0}))
    assert ab.attributable_by_slot_parity(ab.observations(raw_before, unbacked),
                                          ab.observations(raw_before_allowed, unbacked)) is True
    raw_after = probe(sdl_channel([]), xinput_channel(set()))
    assert ab.compare(ab.observations(raw_before, unbacked),
                      ab.observations(raw_after, unbacked)) == {'gone': ['sdl_dinput'], 'still': []}
    assert ab.compare(ab.observations(raw_before, backed),
                      ab.observations(raw_after, backed)) == {'gone': ['sdl_dinput', XINPUT], 'still': []}


def test_divergent_or_empty_baseline_slots_are_not_attributed():
    unbacked = dict(GROUP, xinput_backed=False)
    before = ab.observations(probe(xinput_channel({0})), unbacked)
    allowed = ab.observations(probe(xinput_channel(set())), unbacked)
    assert ab.attributable_by_slot_parity(before, allowed) is False   # 基线就分裂，说明槽位不干净
    assert ab.attributable_by_slot_parity(before, before) is True
    assert ab.attributable_by_slot_parity({}, {}) is False            # 压根没有槽位


# ── 探针必须自我闭环，否则一次 --list 就把解释器永久留在白名单里 ──
@pytest.fixture
def fake_config(monkeypatch):
    state = hidhide.HidHideState(loaded=True, active=True, whitelist=(), blacklist=())
    writes = []

    def fake_read(session=None):
        return state

    def fake_transact(mutator):
        nonlocal state
        change = mutator(state)
        if change.whitelist is not None:
            writes.append(tuple(change.whitelist))
            state = hidhide.HidHideState(loaded=True, active=True,
                                         whitelist=change.whitelist, blacklist=state.blacklist)
        return hidhide.Operation(True, state=state)

    monkeypatch.setattr(hidhide, 'read_state', fake_read)
    monkeypatch.setattr(hidhide, 'transact', fake_transact)
    return writes


def test_probe_registers_then_unregisters_itself(monkeypatch, fake_config, capsys):
    nt_path = hidhide.dos_to_nt_path(sys.executable)
    monkeypatch.setattr(ab, 'probe_channel',
                        lambda channel: {'channel': channel, 'devices': [], 'error': ''})
    payload = ab.run_probe(True)
    assert payload['register_ok'] is True
    assert fake_config == [(nt_path,), ()]
    assert 'PROBE_JSON' in capsys.readouterr().out


def test_probe_unregisters_even_when_measurement_fails(monkeypatch, fake_config):
    def explode(channel):
        raise RuntimeError('SDL 崩了')

    monkeypatch.setattr(ab, 'probe_channel', explode)
    with pytest.raises(RuntimeError):
        ab.run_probe(True)
    nt_path = hidhide.dos_to_nt_path(sys.executable)
    assert fake_config == [(nt_path,), ()]


def test_probe_does_not_write_when_registration_was_not_requested(monkeypatch, fake_config):
    monkeypatch.setattr(ab, 'probe_channel',
                        lambda channel: {'channel': channel, 'devices': [], 'error': ''})
    ab.run_probe(False)
    assert fake_config == []
