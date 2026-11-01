"""Tests for ViGEm client-library discovery, honest backend probing and the fresh-target priming.

These lock the three defects that made the virtual XInput pad unusable:
  1. no `ViGEmClient.dll` anywhere (the ViGEmBus driver package never ships it);
  2. `virtual_gamepad_backend_available()` reporting True from `ViGEmBus.sys` alone;
  3. a brand-new target exposing a ~10% phantom stick offset that a neutral first
     frame cannot clear, because ViGEmBus drops a report equal to its internal zeros.
"""
import ctypes as C
import hashlib
import os
import sys
from pathlib import Path

import pytest

from gamepadstudio import virtual_xinput as vx

NATIVE = Path(vx.__file__).resolve().parent / 'native'
WINDOWS_ONLY = pytest.mark.skipif(sys.platform != 'win32', reason='ViGEm client DLL is the Windows transport')

requires_vendored_dll = pytest.mark.skipif(
    not (NATIVE / vx.VIGEM_DLL_NAME).is_file(),
    reason=f'packaged client library missing: {NATIVE / vx.VIGEM_DLL_NAME}',
)


@pytest.fixture(autouse=True)
def _fresh_probe():
    """The probe caches a loaded CDLL and its verdict for the process lifetime."""
    vx.reset_vigem_backend_probe()
    yield
    vx.reset_vigem_backend_probe()


@requires_vendored_dll
def test_vendored_client_library_is_shipped_with_its_license_and_checksum():
    dll = NATIVE / vx.VIGEM_DLL_NAME
    assert dll.is_file()
    sidecar = (NATIVE / (dll.name + '.sha256')).read_text(encoding='utf-8').split()[0].lower()
    assert hashlib.sha256(dll.read_bytes()).hexdigest() == sidecar, 'vendored DLL no longer matches its recorded digest'
    license_text = (NATIVE / 'ViGEmClient-LICENSE.txt').read_text(encoding='utf-8')
    assert 'Nefarius' in (NATIVE / 'ViGEmClient-PROVENANCE.txt').read_text(encoding='utf-8')
    assert 'MIT' in license_text or 'Permission is hereby granted' in license_text


def test_vendored_copy_is_the_first_candidate_and_env_override_wins(monkeypatch):
    monkeypatch.delenv('VIGEM_CLIENT_DLL', raising=False)
    assert vx.client_dll_candidates()[0] == NATIVE / vx.VIGEM_DLL_NAME
    other = NATIVE / 'not-real' / vx.VIGEM_DLL_NAME
    monkeypatch.setenv('VIGEM_CLIENT_DLL', str(other))
    assert vx.client_dll_candidates()[0] == other


def test_candidates_are_deduplicated_and_cover_frozen_layouts(monkeypatch):
    monkeypatch.delenv('VIGEM_CLIENT_DLL', raising=False)
    dirs = [str(p) for p in vx._client_dll_dirs()]
    assert any(p.replace('\\', '/').endswith('gamepadstudio/native') for p in dirs)
    assert any(p.replace('\\', '/').endswith('gamepadstudio/bin') for p in dirs)
    paths = [str(p) for p in vx.client_dll_candidates()]
    assert len(paths) == len(set(p.lower() for p in paths)), 'candidate list must not repeat a path'
    # A source checkout must resolve the in-package copy without sys.frozen.
    assert str(NATIVE / vx.VIGEM_DLL_NAME) in paths


@WINDOWS_ONLY
def test_a_loadable_dll_without_the_required_exports_is_rejected(tmp_path):
    """A wrong-but-loadable DLL must fail discovery, not half-load into silent no-op frames."""
    decoy = tmp_path / vx.VIGEM_DLL_NAME
    system_dll = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'xinput1_4.dll'
    if not system_dll.is_file():
        pytest.skip('xinput1_4.dll not available to act as an export-less decoy')
    import shutil
    shutil.copyfile(system_dll, decoy)
    lib, error = vx._try_load_client(decoy)
    assert lib is None
    assert 'missing exports' in error and 'vigem_alloc' in error


@WINDOWS_ONLY
def test_backend_probe_is_not_fooled_by_the_driver_alone(monkeypatch):
    """The historical false positive: ViGEmBus.sys present, client library absent."""
    monkeypatch.setattr(vx, '_driver_bus_present', lambda: True)
    monkeypatch.setattr(vx, 'client_dll_candidates', lambda: [Path(r'C:\nonexistent\ViGEmClient.dll')])
    monkeypatch.delenv('VIGEM_CLIENT_DLL', raising=False)
    status = vx.vigem_backend_status(force=True)
    assert status['available'] is False
    assert status['driver_installed'] is True
    assert status['reason'] == 'client_dll_missing'
    assert vx.virtual_gamepad_backend_available() is False


@WINDOWS_ONLY
def test_backend_probe_reports_a_real_verdict_and_caches_it(monkeypatch):
    status = vx.vigem_backend_status(force=True)
    assert status['platform'] == 'win32'
    assert isinstance(status['available'], bool)
    assert status['transport'] in (None, 'vigem')
    if status['available']:
        assert Path(status['dll_path']).is_file()
        assert vx.vigem_backend_status()['available'] is True  # cached copy agrees
    else:
        assert status['reason'] in ('client_dll_missing', 'driver_and_client_missing', 'connect_failed', 'connect_raised')


def test_error_codes_decode_to_driver_names():
    assert vx.vigem_error_name(vx.VIGEM_ERROR_NONE) == 'NONE(0x20000000)'
    assert vx.vigem_error_name(0xE0000001) == 'BUS_NOT_FOUND(0xE0000001)'
    assert vx.vigem_error_name(0xE0000008) == 'BUS_VERSION_MISMATCH(0xE0000008)'
    assert vx.vigem_error_name(0x00000005).startswith('UNKNOWN(0x')


class RecordingFakeLib:
    """Stands in for ViGEmClient.dll and keeps full report copies for assertion."""

    def __init__(self, add_result=None):
        self.reports = []
        self.removed = False
        self.add_result = add_result

    def vigem_alloc(self):
        return 0x1001

    def vigem_free(self, client):
        pass

    def vigem_connect(self, client):
        return vx.VIGEM_ERROR_NONE

    def vigem_disconnect(self, client):
        pass

    def vigem_target_x360_alloc(self):
        return 0x2002

    def vigem_target_free(self, target):
        pass

    def vigem_target_add(self, client, target):
        return self.add_result if self.add_result is not None else vx.VIGEM_ERROR_NONE

    def vigem_target_remove(self, client, target):
        self.removed = True
        return vx.VIGEM_ERROR_NONE

    def vigem_target_x360_update(self, client, target, report):
        self.reports.append((report.wButtons, report.bLeftTrigger, report.bRightTrigger,
                             report.sThumbLX, report.sThumbLY, report.sThumbRX, report.sThumbRY))
        return vx.VIGEM_ERROR_NONE


def _is_neutral(report):
    return report == (0, 0, 0, 0, 0, 0, 0)


def test_connect_primes_with_a_changed_frame_then_neutralises():
    """A neutral report alone does not reach XInput on a fresh target, so connect() must nudge first."""
    lib = RecordingFakeLib()
    pad = vx.VirtualXInputPad(lib=lib)
    assert pad.connect() is True
    assert len(lib.reports) == 2, f'connect() should submit exactly the nudge and the neutral report: {lib.reports}'
    nudge, neutral = lib.reports[0], lib.reports[1]
    assert nudge[3] == 1 and not _is_neutral(nudge), f'expected an sThumbLX=1 nudge, got {nudge}'
    assert _is_neutral(neutral), f'report after the nudge must be fully neutral, got {neutral}'
    assert pad.last_report['sThumbLX'] == 0 and pad.last_report['wButtons'] == 0
    pad.close()


def test_priming_never_appears_in_the_application_frame_stream():
    """The nudge is a transport quirk, not an engine output.

    `test_architecture_and_new_features.test_09_...` caught the first version of this fix
    routing through reset(), which pushed a phantom frame into the registered frame
    callbacks - the same stream the trigger log reads. Priming must stay invisible there.
    """
    lib = RecordingFakeLib()
    pad = vx.VirtualXInputPad(lib=lib)
    frames = []
    pad.register_frame_callback(lambda xusb, hid: frames.append(xusb['wButtons']))
    assert pad.connect() is True
    assert frames == [], f'transport priming emitted app-visible frames: {frames}'
    assert pad.last_report['wButtons'] == 0
    pad.emit_frame(buttons=['0'])
    assert frames == [vx.XUSB_BUTTON_MASKS['0']], f'the real frame must still be reported once: {frames}'
    pad.close()


def test_connect_records_the_driver_error_and_survives_retries():
    lib = RecordingFakeLib(add_result=0xE0000002)  # NO_FREE_SLOT: all four pads are taken
    pad = vx.VirtualXInputPad(lib=lib)
    assert pad.connect() is False
    assert 'NO_FREE_SLOT(0xE0000002)' in pad.last_error
    assert pad.is_connected is False


@WINDOWS_ONLY
def test_missing_client_dll_is_explained_rather_than_silently_false(monkeypatch):
    monkeypatch.setattr(vx, 'client_dll_candidates', lambda: [Path(r'C:\absent-a\ViGEmClient.dll'),
                                                              Path(r'C:\absent-b\ViGEmClient.dll')])
    monkeypatch.delenv('VIGEM_CLIENT_DLL', raising=False)
    pad = vx.VirtualXInputPad()
    assert pad.connect() is False
    assert 'not found' in pad.last_error and 'absent-a' in pad.last_error


@WINDOWS_ONLY
def test_a_failed_load_is_cached_instead_of_retried_every_frame(tmp_path, monkeypatch):
    """The poll loop emits ~125 frames/s; a missing DLL must not trigger 125 LoadLibrary
    attempts per second for the rest of the session. reset_vigem_backend_probe() is the
    documented way to re-probe after the user drops the file in."""
    system_dll = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'xinput1_4.dll'
    if not system_dll.is_file():
        pytest.skip('no loadable-but-wrong DLL available to drive the failure path')
    import shutil
    decoy = tmp_path / vx.VIGEM_DLL_NAME
    shutil.copyfile(system_dll, decoy)
    monkeypatch.setattr(vx, 'client_dll_candidates', lambda: [decoy])
    monkeypatch.delenv('VIGEM_CLIENT_DLL', raising=False)

    attempts = []
    real_try = vx._try_load_client
    monkeypatch.setattr(vx, '_try_load_client',
                        lambda path: (attempts.append(str(path)), real_try(path))[1])

    assert vx.VirtualXInputPad().connect() is False
    assert len(attempts) == 1
    for _ in range(50):
        assert vx.VirtualXInputPad().connect() is False
    assert len(attempts) == 1, 'the failed load should be cached, not retried per frame'

    vx.reset_vigem_backend_probe()
    assert vx.VirtualXInputPad().connect() is False
    assert len(attempts) == 2, 'reset_vigem_backend_probe() must force a re-probe'
