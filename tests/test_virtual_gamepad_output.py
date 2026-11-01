"""Bus-control and real OS-visible output checks for the virtual gamepad.

The unit fakes can only prove that the right numbers reach a `CDLL` binding. These
tests prove the parts that matter to a player: the ViGEm bus accepts our client, a
target really appears as an XInput device in the operating system, the report we
submit is byte-for-byte what a game reads back, turbo pulses actually arrive, and a
fresh target does not hand the game a phantom stick offset.

They are skipped automatically wherever there is no usable backend (CI runners, macOS
without the VirtualHID socket) and can be forced off with GPS_SKIP_VIRTUAL_PAD_HW=1.
"""
import ctypes as C
import json
import os
import subprocess
import sys
import time

import pytest

from gamepadstudio import virtual_xinput as vx

# X360 button bits, so the expectation tables read like the profile JSON does.
BTN = {'A': 0x1000, 'B': 0x2000, 'X': 0x4000, 'Y': 0x8000, 'LB': 0x0100, 'RB': 0x0200,
       'BACK': 0x0020, 'START': 0x0010, 'LS': 0x0040, 'RS': 0x0080,
       'UP': 0x0001, 'DOWN': 0x0002, 'LEFT': 0x0004, 'RIGHT': 0x0008}
# Signature trigger value used to find *our* pad among possibly several XInput slots.
SIGNATURE_RT = 200


class _XUSB(C.Structure):
    _fields_ = [('wButtons', C.c_ushort), ('bLeftTrigger', C.c_ubyte), ('bRightTrigger', C.c_ubyte),
                ('sThumbLX', C.c_short), ('sThumbLY', C.c_short), ('sThumbRX', C.c_short),
                ('sThumbRY', C.c_short)]


class _STATE(C.Structure):
    _fields_ = [('dwPacketNumber', C.c_uint), ('gp', _XUSB)]


def _xinput():
    try:
        dll = C.WinDLL('xinput1_4.dll')
    except OSError:
        return None
    dll.XInputGetState.argtypes = [C.c_uint, C.POINTER(_STATE)]
    dll.XInputGetState.restype = C.c_uint
    return dll


XINPUT = _xinput()

hw_unsupported = not (sys.platform == 'win32' and XINPUT is not None)

pytestmark = pytest.mark.skipif(
    hw_unsupported or os.environ.get('GPS_SKIP_VIRTUAL_PAD_HW') == '1',
    reason='requires Windows with xinput1_4.dll (set GPS_SKIP_VIRTUAL_PAD_HW=1 to opt out), got platform=%s'
           % sys.platform,
)


def connected_slots():
    """Every XInput user index a game can currently read, with its live report."""
    out = {}
    for slot in range(4):
        state = _STATE()
        if XINPUT.XInputGetState(slot, C.byref(state)) == 0:
            out[slot] = state
    return out


def our_slot():
    for slot, state in connected_slots().items():
        if state.gp.bRightTrigger == SIGNATURE_RT:
            return slot
    return None


def read_report():
    slot = our_slot()
    if slot is None:
        return None
    gp = connected_slots()[slot].gp
    return (gp.wButtons, gp.bLeftTrigger, gp.bRightTrigger,
            gp.sThumbLX, gp.sThumbLY, gp.sThumbRX, gp.sThumbRY)


def wait_for_report(expected, timeout=2.0):
    """Poll until the OS-visible report equals `expected` (XInput settles asynchronously)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = read_report()
        if last == expected:
            return last
        time.sleep(0.02)
    return last


def backend_or_skip():
    status = vx.vigem_backend_status(force=True)
    if not status['available']:
        pytest.skip('virtual gamepad backend not usable here: reason=%s driver_installed=%s '
                    'dll=%s connect_error=%s load_error=%s candidates=%s'
                    % (status.get('reason'), status.get('driver_installed'), status.get('dll_path'),
                       status.get('connect_error'), status.get('load_error'), status.get('candidates')))
    return status


@pytest.fixture()
def bus():
    """A connected PVIGEM_CLIENT, released on teardown, with the pad never left behind."""
    status = backend_or_skip()
    if status['transport'] != 'vigem':
        pytest.skip('this bus is ViGEm-specific, transport=%s' % status['transport'])
    lib = vx.VirtualXInputPad()._load_library()
    assert lib is not None, 'backend probe said available but the client DLL will not load'
    client = lib.vigem_alloc()
    assert client, 'vigem_alloc returned NULL'
    err = lib.vigem_connect(client)
    if err != vx.VIGEM_ERROR_NONE:
        lib.vigem_free(client)
        pytest.fail('vigem_connect failed: ' + vx.vigem_error_name(err))
    try:
        yield lib, client
    finally:
        try:
            lib.vigem_disconnect(client)
        finally:
            lib.vigem_free(client)


@pytest.fixture()
def pad():
    """One real X360 target, always unplugged again even if the test body raises."""
    backend_or_skip()
    created = vx.VirtualXInputPad()
    try:
        ok = created.connect()
        if not ok and 'NO_FREE_SLOT' in (created.last_error or ''):
            busy = sorted(connected_slots())
            created.close()
            pytest.skip('all four XInput slots are taken (busy slots %s) by another app or a '
                        'pad leaked by a crashed process; close it and re-run' % busy)
        assert ok, 'connect() failed: %s' % created.last_error
        yield created
    finally:
        created.close()
        # Never leave a pad behind for the other tests or for the user's desktop.
        for _ in range(50):
            if our_slot() is None:
                break
            time.sleep(0.05)


def test_bus_control_handshake_creates_no_device(bus):
    """The bus answers a client, and a control probe must not plug anything in.

    Compared as a before/after diff rather than "no slots at all": a real controller on
    the desk legitimately occupies one, and that must not read as a leak.
    """
    lib, client = bus
    before = set(connected_slots())
    target = lib.vigem_target_x360_alloc()          # allocation alone must not create a device
    assert target, 'vigem_target_x360_alloc returned NULL'
    try:
        if hasattr(lib, 'vigem_target_is_waitable_add_supported'):
            lib.vigem_target_is_waitable_add_supported.argtypes = [C.c_void_p]
            lib.vigem_target_is_waitable_add_supported.restype = C.c_ubyte
            assert lib.vigem_target_is_waitable_add_supported(target) in (0, 1)
    finally:
        lib.vigem_target_free(target)
    assert set(connected_slots()) == before, 'a control probe must not publish an XInput device'


def _is_attached(pad):
    """Query the driver for the target's plug state (argtypes are ours to declare here)."""
    lib = pad._lib
    lib.vigem_target_is_attached.argtypes = [C.c_void_p]
    lib.vigem_target_is_attached.restype = C.c_ubyte
    return bool(lib.vigem_target_is_attached(pad._target))


def test_virtual_pad_appears_as_a_real_xinput_device(pad):
    """The target is a device the operating system itself enumerates, not app state."""
    pad.emit_frame(buttons=[], axes=[0, 0, 0, 0, 0, SIGNATURE_RT / 255.0])
    slot = our_slot()
    assert slot is not None, ('no XInput slot carries our report; slots=%s read=%s'
                              % (sorted(connected_slots()), read_report()))
    assert _is_attached(pad), 'XInput sees a pad but ViGEm reports the target detached'
    assert pad.is_connected is True


def test_connect_leaves_no_phantom_stick_offset(pad):
    """A fresh target otherwise shows ~10% stick deflection that a neutral frame cannot clear.

    Measured on real hardware: LS=(-3356,-1869) RS=(-3255,-848), byte-identical on every
    connect, and ViGEmBus drops a report equal to its internal zeros - so connect() has
    to prime with a changed frame before the neutral one lands.
    """
    pad.emit_frame(buttons=[], axes=[0, 0, 0, 0, 0, SIGNATURE_RT / 255.0])
    report = wait_for_report((0, 0, SIGNATURE_RT, 0, 0, 0, 0))
    assert report is not None, 'our pad never appeared on any XInput slot'
    _, lt, rt, lx, ly, rx, ry = report
    assert (lx, ly, rx, ry) == (0, 0, 0, 0), 'phantom stick offset reached the game: %s' % (report,)
    assert rt == SIGNATURE_RT and lt == 0


@pytest.mark.parametrize('buttons,axes,expected', [
    # buttons                                   normalized axes [lx,ly,rx,ry,lt,rt] -> XUSB fields
    (['0'],                        [0, 0, 0, 0, 0, 0],        (BTN['A'], 0, SIGNATURE_RT, 0, 0, 0, 0)),
    (['0', '1', '2', '3'],         [0, 0, 0, 0, 0, 0],        (BTN['A'] | BTN['B'] | BTN['X'] | BTN['Y'],
                                                               0, SIGNATURE_RT, 0, 0, 0, 0)),
    (['9', '10', '4', '6', '7', '8'], [0, 0, 0, 0, 0, 0],    (BTN['LB'] | BTN['RB'] | BTN['BACK'] | BTN['START']
                                                               | BTN['LS'] | BTN['RS'], 0, SIGNATURE_RT, 0, 0, 0, 0)),
    (['11', '12', '13', '14'],     [0, 0, 0, 0, 0, 0],        (BTN['UP'] | BTN['DOWN'] | BTN['LEFT'] | BTN['RIGHT'],
                                                               0, SIGNATURE_RT, 0, 0, 0, 0)),
    ([],                           [1.0, 0, 0, 0, 0, 0],      (0, 0, SIGNATURE_RT, 32767, 0, 0, 0)),
    ([],                           [-1.0, 0, 0, 0, 0, 0],     (0, 0, SIGNATURE_RT, -32767, 0, 0, 0)),
    ([],                           [0, -1.0, 0, 0, 0, 0],     (0, 0, SIGNATURE_RT, 0, 32767, 0, 0)),
    ([],                           [0, 1.0, 0, 0, 0, 0],      (0, 0, SIGNATURE_RT, 0, -32767, 0, 0)),
    ([],                           [0, 0, 1.0, -1.0, 0, 0],   (0, 0, SIGNATURE_RT, 0, 0, 32767, 32767)),
    ([],                           [0, 0, 1.0, 1.0, 0, 0],    (0, 0, SIGNATURE_RT, 0, 0, 32767, -32767)),
    (['0', '11'],                  [0.5, -0.25, 0, 0, 0.5, 0], (BTN['A'] | BTN['UP'], 128, SIGNATURE_RT,
                                                                16384, 8192, 0, 0)),
], ids=['A', 'face4', 'shoulders+sticks', 'dpad4', 'LS full right', 'LS full left', 'LS up',
        'LS down', 'RS up-right (Y inverted)', 'RS down-right', 'combo A+dpad+LS+LT'])
def test_emitted_report_is_what_a_game_reads(pad, buttons, axes, expected):
    """One emitted frame must arrive bit-exact through XInputGetState - the API games use.

    RT is pinned to the signature value on every frame so the reader can tell our pad
    apart from any physical controller that happens to be connected. Axes are ordered
    [lx, ly, rx, ry, lt, rt], so the override writes index 5 rather than appending.
    """
    signed = list(axes) + [0.0] * (6 - len(axes))
    signed[5] = SIGNATURE_RT / 255.0
    snapshot = pad.emit_frame(buttons=buttons, axes=signed)
    assert snapshot['bRightTrigger'] == SIGNATURE_RT, 'compose layer lost the signature trigger'
    got = wait_for_report(expected)
    assert got == expected, ('game read %s but the app emitted %s for buttons=%s axes=%s'
                             % (got, expected, buttons, axes))


def test_packet_number_advances_per_changed_frame(pad):
    """XInput only re-delivers on change; a frozen counter means the bus stopped refreshing."""
    pad.emit_frame(buttons=['0'], axes=[0, 0, 0, 0, 0, SIGNATURE_RT / 255.0])
    wait_for_report((BTN['A'], 0, SIGNATURE_RT, 0, 0, 0, 0))
    packets = []
    for i in range(6):
        pad.emit_frame(buttons=(['0'] if i % 2 == 0 else ['1']),
                       axes=[0, 0, 0, 0, 0, SIGNATURE_RT / 255.0])
        wait_for_report(((BTN['A'] if i % 2 == 0 else BTN['B']), 0, SIGNATURE_RT, 0, 0, 0, 0))
        slot = our_slot()
        packets.append(connected_slots()[slot].dwPacketNumber)
    assert len(set(packets)) == len(packets), 'dwPacketNumber stopped advancing: %s' % packets
    assert packets == sorted(packets), 'packet numbers went backwards: %s' % packets


def test_release_clears_every_output(pad):
    """Nothing may stay latched on the OS device after the app lets go."""
    pad.emit_frame(buttons=['0', '9', '11'], axes=[1.0, -1.0, 0, 0, 1.0, SIGNATURE_RT / 255.0])
    assert wait_for_report((BTN['A'] | BTN['LB'] | BTN['UP'], 255, SIGNATURE_RT, 32767, 32767, 0, 0))
    pad.reset()
    pad.emit_frame(axes=[0, 0, 0, 0, 0, SIGNATURE_RT / 255.0])   # keep the signature for the reader
    cleared = wait_for_report((0, 0, SIGNATURE_RT, 0, 0, 0, 0))
    assert cleared == (0, 0, SIGNATURE_RT, 0, 0, 0, 0), 'output latched on the virtual pad: %s' % (cleared,)


_READER_SNIPPET = r'''
import ctypes as C, json, sys, time
class GP(C.Structure):
    _fields_=[('wButtons',C.c_ushort),('bLeftTrigger',C.c_ubyte),('bRightTrigger',C.c_ubyte),
              ('sThumbLX',C.c_short),('sThumbLY',C.c_short),('sThumbRX',C.c_short),('sThumbRY',C.c_short)]
class ST(C.Structure):
    _fields_=[('dwPacketNumber',C.c_uint),('gp',GP)]
xi=C.WinDLL('xinput1_4.dll'); xi.XInputGetState.argtypes=[C.c_uint,C.POINTER(ST)]
secs=float(sys.argv[1]); end=time.time()+secs

# First find *which* slot carries the signature, then poll only that slot: reading all
# four every iteration costs 4x the syscalls and under a loaded CI box that alone would
# alias the pulse train and look like dropped frames.
slot=None
while time.time()<end and slot is None:
    for u in range(4):
        s=ST()
        if xi.XInputGetState(u,C.byref(s))==0 and s.gp.bRightTrigger==200:
            slot=u; break
    if slot is None: time.sleep(0.005)
if slot is None:
    print(json.dumps({'error':'signature never seen'})); raise SystemExit

counters=[]; phase_ok=0; pkts=[]
while time.time()<end:
    s=ST()
    if xi.XInputGetState(slot,C.byref(s))==0 and s.gp.bRightTrigger==200:
        counters.append(s.gp.bLeftTrigger); pkts.append(s.dwPacketNumber)
        if (s.gp.bLeftTrigger%2==0)==(bool(s.gp.wButtons&0x1000)): phase_ok+=1
    time.sleep(0.002)

deltas=[(b-a)%256 for a,b in zip(counters,counters[1:]) if (b-a)%256]
print(json.dumps({'slot':slot,'samples':len(counters),'last_counter':counters[-1] if counters else -1,
                  'distinct':len(set(counters)),'max_delta':max(deltas) if deltas else 0,
                  'phase_ratio':round(phase_ok/max(1,len(counters)),3),
                  'distinct_packets':len(set(pkts))}))
'''


def test_turbo_pulses_reach_an_independent_process(pad):
    """A game polls from its own process; delivery must survive that, at turbo rates.

    Same-process reads can be served from an XInput cache, so delivery is judged by a
    child process that has never touched the device. The criteria are load-proof on
    purpose - the suite runs four pytest processes in parallel and a sampling-density
    threshold failed there while every frame had in fact arrived. What must hold is:
    the *final* frame is observable (no mid-run wedge, the bug that locked-session
    readings once suggested), the counter never jumps more than a few frames (no burst
    loss), the A bit stays in phase with the frame counter (no torn report), and each
    state change really advances XInput's packet number.
    """
    hz, seconds = 20.0, 3.0
    lib = pad._lib
    frames = int(hz * seconds)
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    reader = subprocess.Popen([sys.executable, '-c', _READER_SNIPPET, str(seconds + 2.0)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              encoding='utf-8', env=env)
    t0 = time.perf_counter()
    period = 1.0 / hz
    for i in range(frames):
        report = vx.XUSB_REPORT(wButtons=(BTN['A'] if i % 2 == 0 else 0),
                               bLeftTrigger=i % 256, bRightTrigger=SIGNATURE_RT,
                               sThumbLX=0, sThumbLY=0, sThumbRX=0, sThumbRY=0)
        rc = lib.vigem_target_x360_update(pad._client, pad._target, report)
        assert rc == vx.VIGEM_ERROR_NONE, 'frame %d rejected: %s' % (i, vx.vigem_error_name(rc))
        time.sleep(max(0.0, t0 + (i + 1) * period - time.perf_counter()))
    # hold the last frame open so a slow reader can still catch it
    time.sleep(0.5)
    out, _ = reader.communicate(timeout=30)
    try:
        stats = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pytest.fail('reader produced no stats: %r' % out[-400:])
    if 'error' in stats:
        pytest.fail('the virtual pad was invisible to a second process: %s' % stats)
    assert stats['last_counter'] == (frames - 1) % 256, (
        'the final turbo frame never reached another process (reader sent %d frames, '
        'last observed counter %s): %s' % (frames, stats['last_counter'], stats))
    assert stats['max_delta'] <= 8, (
        'the pulse train lost a burst: consecutive observed frames differ by %d frames - %s'
        % (stats['max_delta'], stats))
    assert stats['phase_ratio'] >= 0.95, 'A bit torn against the frame counter: %s' % stats
    assert stats['distinct_packets'] >= 0.5 * frames, (
        'XInput reported too few distinct packet numbers for %d frames: %s' % (frames, stats))


def test_close_unplugs_the_device_and_frees_the_slot(pad):
    pad.emit_frame(buttons=[], axes=[0, 0, 0, 0, 0, SIGNATURE_RT / 255.0])
    assert wait_for_report((0, 0, SIGNATURE_RT, 0, 0, 0, 0)) is not None, 'pad never appeared'
    pad.close()
    assert pad.is_connected is False
    assert our_slot() is None, 'the virtual pad is still published to XInput after close()'


def test_two_pads_coexist_and_are_independent():
    """Mapping several profiles may need more than one pad; slots must not collide."""
    backend_or_skip()
    first = vx.VirtualXInputPad()
    second = vx.VirtualXInputPad()
    try:
        if not first.connect():
            pytest.skip('could not create the first pad: %s' % first.last_error)
        second_ok = second.connect()
        if 'NO_FREE_SLOT' in (second.last_error or ''):
            pytest.skip('bus reports all slots busy; two-pad coexistence cannot be checked here')
        assert second_ok, 'second pad failed without NO_FREE_SLOT: %s' % second.last_error
        first.emit_frame(buttons=['0'], axes=[0, 0, 0, 0, 0, SIGNATURE_RT / 255.0])
        second.emit_frame(buttons=['1'], axes=[0, 0, 0, 0, 0, 0])
        time.sleep(0.3)
        slots = connected_slots()
        assert len(slots) >= 2, 'two targets but only %s XInput slot(s) readable' % sorted(slots)
        mine = [s for s in slots.values() if s.gp.bRightTrigger == SIGNATURE_RT and s.gp.wButtons == BTN['A']]
        other = [s for s in slots.values() if s.gp.wButtons == BTN['B']]
        assert mine and other, 'the two pads did not both publish their own report: %s' % [
            (s.gp.wButtons, s.gp.bRightTrigger) for s in slots.values()]
    finally:
        second.close()
        first.close()
        time.sleep(0.3)
        assert our_slot() is None, 'a pad leaked after close()'


def test_no_physical_controller_is_assumed(tmp_path, monkeypatch):
    """The signature must be ours alone: a real pad on the desk cannot satisfy these tests."""
    backend_or_skip()
    pad = vx.VirtualXInputPad()
    assert pad.connect(), pad.last_error
    try:
        pad.emit_frame(buttons=['0'], axes=[0, 0, 0, 0, 0, SIGNATURE_RT / 255.0])
        wait_for_report((BTN['A'], 0, SIGNATURE_RT, 0, 0, 0, 0))
        holders = [slot for slot, s in connected_slots().items() if s.gp.bRightTrigger == SIGNATURE_RT]
        assert len(holders) == 1, 'the signature pad count is %s - output is ambiguous' % holders
    finally:
        pad.close()
