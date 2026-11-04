import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QWidget

from gamepadstudio.application_profiles import uipi_elevation_warning
from gamepadstudio.controller_catalog import button_labels, capture_button, family_for
from gamepadstudio.device_quirks import (
    DUALSENSE_EDGE_PRODUCT_ID,
    SONY_VENDOR_ID,
    ControllerDatabase,
    GyroBiasCalibrator,
    adapt_sdl_mapping_for_guid,
    detect_input_protocol,
    extract_vid_pid_from_sdl_guid,
    gyro_protocol_hint,
    identify_controller_hardware,
    is_dualsense_edge,
    normalize_dinput_gamepad_state,
    parse_sdl_gamecontrollerdb,
    parse_usb_ids,
    partition_dinput_buttons_by_sdl_db,
)
from gamepadstudio.hidhide import install_driver, is_apple_bootcamp_environment
from gamepadstudio.mapping_engine import (
    INPUTS,
    MappingRuntime,
    binding_label,
    canonical_trigger,
    input_sources,
    validate_binding,
    validate_mappings,
)
from gamepadstudio.mapping_ui import BindingDialog
from gamepadstudio.profile_transfer import export_profile, preview_profile_import
from gamepadstudio.schema_registry import (
    GYRO_TRIGGERS,
    HOLD_MODES,
    INPUT_OPTION_FIELDS,
    MOUSE_OPTION_FIELDS,
    STICK_SOURCES,
)
from gamepadstudio.studio_core import default_config, profile_scope
from gamepadstudio.virtual_kbm_ui import KbmFeelDialog, VirtualKbmPage
from tests.mapping_fixtures import MappingOwner


class FakeActions:
    def __init__(self):
        self.events = []

    def hold(self, value, down):
        self.events.append(('hold', value, down))

    def mouse_button(self, button, down):
        self.events.append(('mouse', button, down))

    def move_mouse(self, dx, dy):
        self.events.append(('move', dx, dy))

    def scroll(self, delta):
        self.events.append(('scroll', delta))


class FakeMouseThread:
    def __init__(self):
        self.stick_updates = []
        self.gyro_updates = []
        self.configured = []

    def configure(self, opts):
        self.configured.append(copy.deepcopy(opts))

    def update_stick(self, rx, ry, is_desktop=False):
        self.stick_updates.append((rx, ry, is_desktop))

    def update_gyro(self, gx, gy, gz):
        self.gyro_updates.append((gx, gy, gz))

    def set_click_lock(self, locked):
        pass

    def stop(self):
        pass

    def join(self, timeout=None):
        pass


class _DummyOwner(QWidget):
    def __init__(self, snapshot, config):
        super().__init__()
        self.snapshot = snapshot
        self.config = config

    def width(self):
        return 1000

    def height(self):
        return 800


def _ensure_app():
    return QApplication.instance() or QApplication([])


class TestArchitectureAndNewFeatures(unittest.TestCase):
    def setUp(self):
        import tempfile
        _ensure_app()
        self._owners = []
        self.state = {
            'connected': True,
            'family': 'dualsense',
            'is_gamecontroller': True,
            'device_key': 'dualsense:test',
            'profile_key': 'dualsense:test',
            'instance_id': 1,
            'available_buttons': list(range(15)),
            'available_axes': list(range(6)),
            'buttons': [],
            'axes': [0.0] * 6,
            'gyro': [0.0, 0.0, 0.0],
        }
        self.config = default_config(Path(tempfile.gettempdir()) / 'gamepadstudio-test')
        scope = profile_scope(self.state)
        self.profile = self.config['active_profile']
        self.config['profile_devices'][self.profile] = scope
        self.config['profile_modes'][self.profile] = 'kbm'

    def tearDown(self):
        from PySide6.QtCore import QEvent
        app = QApplication.instance()
        for w in getattr(self, '_owners', []):
            try:
                w.close()
                w.deleteLater()
            except Exception:
                pass
        if app is not None:
            app.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()

    def test_schema_registry_constants(self):
        self.assertIn('double_tap_hold', HOLD_MODES)
        self.assertEqual(STICK_SOURCES, ('right', 'left'))
        self.assertIn('suppress_ls_up', INPUT_OPTION_FIELDS)
        self.assertTrue({'gyro_enabled', 'gyro_trigger', 'gyro_sensitivity', 'gyro_invert_y', 'stick_source'} <= MOUSE_OPTION_FIELDS)
        self.assertIn('LT', GYRO_TRIGGERS)

    def test_double_tap_hold_runtime_sequence_and_release(self):
        actions = FakeActions()
        runtime = MappingRuntime(actions, lambda b, d: None, start_mouse=False)
        self.config['profiles'][self.profile] = {
            'LS:up:outer': {
                'short': {'action': 'hold', 'value': 'W', 'mode': 'double_tap_hold'},
                'long': {'action': 'none'},
            }
        }

        state_pushed = dict(self.state, axes=[0.0, -0.95, 0.0, 0.0, 0.0, 0.0])
        runtime.update(state_pushed, self.config, enabled=True, now=1.000)
        self.assertEqual(actions.events, [('hold', 'W', True)])

        # At t=1.045s (45ms): first tap releases
        runtime.update(state_pushed, self.config, enabled=True, now=1.046)
        self.assertEqual(actions.events, [('hold', 'W', True), ('hold', 'W', False)])

        # At t=1.090s (90ms): second press holds continuously
        runtime.update(state_pushed, self.config, enabled=True, now=1.091)
        self.assertEqual(
            actions.events,
            [('hold', 'W', True), ('hold', 'W', False), ('hold', 'W', True)],
        )

        # Still held at t=2.000s without extra toggles
        runtime.update(state_pushed, self.config, enabled=True, now=2.000)
        self.assertEqual(len(actions.events), 3)

        # Releasing the stick releases the held key
        state_neutral = dict(self.state, axes=[0.0] * 6)
        runtime.update(state_neutral, self.config, enabled=True, now=2.050)
        self.assertEqual(
            actions.events,
            [('hold', 'W', True), ('hold', 'W', False), ('hold', 'W', True), ('hold', 'W', False)],
        )

    def test_double_tap_hold_validation_and_ui_roundtrip(self):
        valid = validate_binding({'action': 'hold', 'value': 'W', 'mode': 'double_tap_hold'})
        self.assertEqual(valid['mode'], 'double_tap_hold')
        self.assertIn('双击并按住', binding_label(valid))

        with self.assertRaises(ValueError):
            validate_binding({'action': 'hold', 'value': 'CapsLock', 'mode': 'double_tap_hold'})
        with self.assertRaises(ValueError):
            validate_mappings({
                'TP:tap': {
                    'short': {'action': 'hold', 'value': 'W', 'mode': 'double_tap_hold'},
                    'long': {'action': 'none'},
                }
            })

        owner = _DummyOwner(self.state, self.config)
        self._owners.append(owner)
        mapping = {
            'short': {'action': 'hold', 'value': 'W', 'mode': 'double_tap_hold'},
            'long': {'action': 'none'},
            'long_press': 0.65,
        }
        dlg = BindingDialog(owner, trigger='LS:up:outer', mapping=mapping, profile=self.profile, mode='kbm')
        try:
            self.assertEqual(dlg.hold_modes['short'].currentData(), 'double_tap_hold')
            saved = dlg.value()
            self.assertEqual(saved['short']['mode'], 'double_tap_hold')
        finally:
            dlg.close()

    def test_left_stick_mouse_source_runtime_and_feel_dialog(self):
        actions = FakeActions()
        runtime = MappingRuntime(actions, lambda b, d: None, start_mouse=False)
        fake_mouse = FakeMouseThread()
        runtime.mouse_thread = fake_mouse
        self.config['profile_options'][self.profile] = {
            'right_stick_mouse': True,
            'mouse': {'mode': 'game', 'stick_source': 'left'},
        }

        state = dict(self.state, axes=[0.65, -0.45, 0.1, 0.2, 0.0, 0.0])
        runtime.update(state, self.config, enabled=True, now=1.0)
        self.assertEqual(fake_mouse.stick_updates[-1][:2], (0.65, -0.45))

        owner = _DummyOwner(self.state, self.config)
        self._owners.append(owner)
        dlg = KbmFeelDialog(owner, self.profile)
        try:
            self.assertEqual(dlg.stick_source.currentData(), 'left')
            opts = dlg.options()
            self.assertEqual(opts['mouse'].get('stick_source'), 'left')
            dlg.stick_source.setCurrentIndex(0)  # 'right'
            opts_default = dlg.options()
            self.assertNotIn('stick_source', opts_default['mouse'])
        finally:
            dlg.close()

    def test_portable_profile_roundtrip_with_new_schema_fields(self):
        self.config['profiles'][self.profile] = {
            'LS:up:outer': {
                'short': {'action': 'hold', 'value': 'W', 'mode': 'double_tap_hold'},
                'long': {'action': 'none'},
            },
            '0': {
                'short': {'action': 'hold', 'value': 'J', 'mode': 'turbo', 'rate_hz': 20},
                'long': {'action': 'none'},
            },
        }
        self.config['profile_options'][self.profile] = {
            'right_stick_mouse': True,
            'input': {
                'stick_press': 0.50,
                'stick_release': 0.35,
                'outer_up_press': 0.88,
                'outer_up_release': 0.76,
                'suppress_ls_up': 'filter',
            },
            'mouse': {
                'mode': 'game',
                'stick_source': 'left',
                'sensitivity': 32.0,
                'deadzone': 0.06,
                'y_ratio': 0.75,
                'edge_boost': 1.8,
                'invert_y': False,
                'gyro_enabled': True,
                'gyro_trigger': 'LT',
                'gyro_sensitivity': 24.0,
                'gyro_invert_y': True,
            },
        }
        portable = export_profile(self.config, self.state, self.profile)
        validated = preview_profile_import(portable, self.state)
        mouse = validated['profile']['options']['mouse']
        inp = validated['profile']['options']['input']
        self.assertTrue(mouse['gyro_enabled'])
        self.assertEqual(mouse['gyro_trigger'], 'LT')
        self.assertEqual(mouse['gyro_sensitivity'], 24.0)
        self.assertTrue(mouse['gyro_invert_y'])
        self.assertEqual(mouse['stick_source'], 'left')
        self.assertEqual(inp['suppress_ls_up'], 'filter')
        self.assertEqual(
            validated['profile']['mappings']['LS:up:outer']['short']['mode'],
            'double_tap_hold',
        )

    def test_device_quirks_and_gyro_protocol_hint(self):
        edge_state = {
            'connected': True,
            'vendor_id': SONY_VENDOR_ID,
            'product_id': DUALSENSE_EDGE_PRODUCT_ID,
            'name': 'DualSense Edge Wireless Controller',
        }
        self.assertTrue(is_dualsense_edge(edge_state))

        xinput_state = dict(self.state, family='xbox', input_backend='xinput', gyro=[])
        hint = gyro_protocol_hint(xinput_state)
        self.assertIn('XInput', hint)

        owner = _DummyOwner(xinput_state, self.config)
        self._owners.append(owner)
        dlg = KbmFeelDialog(owner, self.profile)
        try:
            self.assertFalse(dlg.has_gyro)
            self.assertTrue(dlg.gyro_hint_label.isHidden() is False or bool(dlg.gyro_protocol_hint))
        finally:
            dlg.close()

        calibrator = GyroBiasCalibrator(required_samples=5, stationary_threshold=0.05)
        for _ in range(5):
            out = calibrator.feed([0.01, -0.02, 0.005])
        self.assertTrue(calibrator.calibrated)
        self.assertAlmostEqual(out[0], 0.0, places=5)
        self.assertAlmostEqual(out[1], 0.0, places=5)

    def test_windows_uipi_elevation_warning(self):
        with patch.object(sys, 'platform', 'win32'), patch(
            'gamepadstudio.application_profiles.is_process_elevated',
            side_effect=lambda pid=None: False if pid is None else True,
        ):
            msg = uipi_elevation_warning({'pid': 99999, 'executable': 'C:/Games/Nikki.exe'})
            self.assertIn('UIPI', msg)
            self.assertIn('Nikki.exe', msg)

    def test_128_button_flight_stick_and_stadia_controller_support(self):
        self.assertIn('32', INPUTS)
        self.assertIn('64', INPUTS)
        self.assertIn('127', INPUTS)
        self.assertEqual(canonical_trigger('120+35'), '35+120')

        hotas_state = {
            'is_gamecontroller': False,
            'family': 'flightstick',
            'available_buttons': list(range(96)),
            'num_axes': 4,
            'num_hats': 1,
        }
        sources = set(input_sources(hotas_state))
        self.assertIn('35', sources)
        self.assertIn('95', sources)
        self.assertEqual(family_for(0, 0x231D, 0x0200, 'VKBsim Gladiator EVO'), 'flightstick')
        self.assertEqual(button_labels('flightstick')[95], '按键 96')

        self.assertEqual(family_for(0, 0x18D1, 0x9400, 'Google Stadia Controller'), 'stadia')
        self.assertEqual(button_labels('stadia')[15], 'Capture')
        self.assertEqual(capture_button('stadia', range(16)), 15)

    def test_hidhide_install_refuses_apple_bootcamp(self):
        """Boot Camp 上装 HidHide 会连带弄坏内置键盘/触控板，必须直接拒绝。"""
        with patch('gamepadstudio.hidhide.is_apple_bootcamp_environment', return_value=True):
            result = install_driver()
        self.assertFalse(result.ok)
        self.assertIn('Boot Camp', result.message)

    def test_quick_scene_templates_in_virtual_kbm_page(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            owner = MappingOwner(Path(tmp))
            try:
                page = VirtualKbmPage(owner, store=owner.store)
                scheme = page.current_scheme()
                page.apply_scene_template('dnf_dash')
                mappings = owner.config['profiles'][scheme]
                self.assertEqual(mappings['LS:left:outer']['short'], {'action': 'hold', 'value': 'Left', 'mode': 'double_tap_hold'})
                self.assertEqual(mappings['LS:right:outer']['short'], {'action': 'hold', 'value': 'Right', 'mode': 'double_tap_hold'})

                page.apply_scene_template('sprint_3d')
                mappings = owner.config['profiles'][scheme]
                self.assertEqual(mappings['LS:up:outer']['short'], {'action': 'hold', 'value': 'W', 'mode': 'double_tap_hold'})

                page.apply_scene_template('fps_gyro')
                opts = owner.config['profile_options'][scheme]
                self.assertTrue(opts['right_stick_mouse'])
                self.assertTrue(opts['mouse']['gyro_enabled'])
                self.assertEqual(opts['mouse']['gyro_trigger'], 'LT')

                page.apply_scene_template('left_stick_mouse')
                opts = owner.config['profile_options'][scheme]
                self.assertEqual(opts['mouse']['stick_source'], 'left')
                page.close()
            finally:
                owner.close()

    def test_common_controller_recognition_and_dinput_normalization(self):
        # 1. Common DInput & multi-mode controllers recognition
        eightbitdo = identify_controller_hardware({'vendor': 0x2DC8, 'product': 0x6002, 'name': '8BitDo Pro 2', 'is_gamecontroller': False})
        self.assertEqual(eightbitdo['brand'], '8BitDo 八位堂')
        self.assertEqual(eightbitdo['protocol'], 'dinput')
        self.assertEqual(eightbitdo['back_paddles'], 2)
        self.assertEqual(eightbitdo['recommended_preset'], 'DInput · 默认')

        flydigi = identify_controller_hardware({'vendor': 0x04B4, 'product': 0x2412, 'name': 'Flydigi Vader 4 Pro', 'is_gamecontroller': False})
        self.assertEqual(flydigi['brand'], 'Flydigi 飞智')
        self.assertEqual(flydigi['protocol'], 'dinput')
        self.assertEqual(flydigi['back_paddles'], 4)

        beitong = identify_controller_hardware({'vendor': 0x20BC, 'product': 0x5500, 'name': 'BEITONG Asura 2Pro', 'is_gamecontroller': False})
        self.assertEqual(beitong['brand'], 'Beitong 北通')
        self.assertEqual(beitong['protocol'], 'dinput')

        logitech = identify_controller_hardware({'vendor': 0x046D, 'product': 0xC218, 'name': 'Logitech RumblePad 2 USB', 'is_gamecontroller': False})
        self.assertEqual(logitech['brand'], 'Logitech 罗技')
        self.assertEqual(logitech['protocol'], 'dinput')
        self.assertEqual(logitech['dinput_layout'], 'type_a')

        # 2. DInput button/axis/hat normalization without R3 (11) vs D-Pad Up (11) collision
        # Raw button 11 is R3/RS, raw button 6 is L2/LT, Hat 0x01 is D-Pad Up
        norm_btns, norm_axes = normalize_dinput_gamepad_state(
            raw_buttons=[0, 4, 6, 11, 14],
            raw_axes=[0.0, 0.0, 0.0, 0.0],
            hat_mask=0x01,
            dinput_layout='type_b',
            num_buttons=16,
        )
        # 0 -> 0(A), 4 -> 9(LB), 6 -> axes[4]=1.0(LT), 11 -> 8(R3), 14 -> 16(P1), hat 0x01 -> 11(D-Pad Up)
        self.assertEqual(norm_btns, [0, 8, 9, 11, 16])
        self.assertEqual(norm_axes[4], 1.0)
        self.assertEqual(detect_input_protocol(is_gamecontroller=False, vendor=0x0079, product=0x0006), 'dinput')

    def test_online_controller_db_and_usb_ids_realtime_fetch_and_cache(self):
        import tempfile

        # GUID for VID=0x3537 (little-endian '3735'), PID=0x1099 (little-endian '9910')
        sample_guid = '03000000373500009910000011010000'
        vid, pid = extract_vid_pid_from_sdl_guid(sample_guid)
        self.assertEqual((vid, pid), (0x3537, 0x1099))

        sample_sdl_db = (
            "# SDL_GameControllerDB sample\n"
            f"{sample_guid},GameSir Tarantula Pro DInput,a:b0,b:b1,x:b2,y:b3,back:b8,start:b9,"
            "leftstick:b10,rightstick:b11,leftshoulder:b4,rightshoulder:b5,dpup:h0.1,dpdown:h0.4,"
            "dpleft:h0.8,dpright:h0.2,paddle1:b14,paddle2:b15,leftx:a0,lefty:a1,rightx:a2,righty:a3,"
            "platform:Mac OS X,\n"
        )
        sample_usb_ids = (
            "# usb.ids sample\n"
            "3537  Guangzhou Chicken Run Network Technology Co., Ltd.\n"
            "\t1099  GameSir Tarantula Pro Wireless Controller\n"
        )

        def fake_fetcher(url: str, timeout: float = 3.5) -> str:
            if 'gamecontrollerdb' in url:
                return sample_sdl_db
            if 'usb.ids' in url:
                return sample_usb_ids
            return ''

        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / 'device_db_cache.json'
            db = ControllerDatabase(cache_path=cache_file, network_enabled=True, fetcher=fake_fetcher)
            entry = db.fetch_and_cache_sync(0x3537, 0x1099, guid=sample_guid)
            self.assertIsNotNone(entry)
            self.assertEqual(entry['model'], 'GameSir Tarantula Pro DInput')
            self.assertEqual(entry['back_paddles'], 2)
            self.assertIn('paddle1:b14', entry['sdl_mapping'])
            self.assertTrue(cache_file.exists())

            pending = db.drain_pending_sdl_mappings()
            self.assertEqual(len(pending), 1)
            self.assertTrue(pending[0].startswith(sample_guid))

            # Reload from persistent disk cache without network
            db_reloaded = ControllerDatabase(cache_path=cache_file, network_enabled=False)
            cached = db_reloaded.get_cached_entry(0x3537, 0x1099)
            self.assertIsNotNone(cached)
            self.assertEqual(cached['model'], 'GameSir Tarantula Pro DInput')

            # Adapt mapping for another instance GUID of the same VID:PID
            other_guid = '03000000373500009910000099990000'
            adapted = adapt_sdl_mapping_for_guid(cached['sdl_mapping'], other_guid, 'Custom Name')
            self.assertTrue(adapted.startswith(f'{other_guid},Custom Name,'))

            # Partition DInput buttons via SDL_GameControllerDB mapping line:
            # Notice raw buttons b5, b8, b9, b12, b15, b16 are NOT mapped to standard XInput slots!
            pro2_sdl_line = (
                "03000000c82d00000260000000000000,8BitDo Pro 2,"
                "a:b1,b:b0,x:b4,y:b3,back:b10,guide:b2,start:b11,"
                "leftstick:b13,rightstick:b14,leftshoulder:b6,rightshoulder:b7,"
                "misc1:b12,paddle1:b15,paddle2:b16,dpup:h0.1,dpdown:h0.4,dpleft:h0.8,dpright:h0.2,"
                "leftx:a0,lefty:a1,rightx:a2,righty:a3,lefttrigger:a5,righttrigger:a4,platform:Windows,"
            )
            part = partition_dinput_buttons_by_sdl_db(num_buttons=17, sdl_mapping=pro2_sdl_line)
            self.assertEqual(part['xinput_mapped'][1], '0')   # b1 -> A
            self.assertEqual(part['xinput_mapped'][0], '1')   # b0 -> B
            self.assertEqual(part['xinput_mapped'][4], '2')   # b4 -> X
            self.assertEqual(part['xinput_mapped'][6], '9')   # b6 -> LB
            self.assertEqual(part['xinput_mapped'][13], '7')  # b13 -> LS
            self.assertEqual(part['unused_buttons'], [5, 8, 9, 12, 15, 16])
            self.assertEqual(part['extended_hints'][12], 'Capture / Share')
            self.assertEqual(part['extended_hints'][15], 'Paddle P1')
            self.assertEqual(part['allocated_sequences'][5], {'action': 'gamepad_chord', 'value': '9+0'})
            self.assertEqual(part['allocated_sequences'][15], {'action': 'gamepad_chord', 'value': '9+11'})
            # Conflict-free canonical slots (>= 15) for mid-range holes b5, b8, b9, b12:
            self.assertEqual(part['canonical_unused_slots'], {5: 17, 8: 18, 9: 19, 12: 20, 15: 15, 16: 16})

            # Normalize raw DInput buttons using SDL_GameControllerDB mapping line:
            norm_b, norm_a = normalize_dinput_gamepad_state(
                raw_buttons=[1, 5, 6, 15],
                raw_axes=[0.0, 0.0, 0.0, 0.0],
                hat_mask=0x01,
                num_buttons=17,
                sdl_mapping=pro2_sdl_line,
            )
            # b1 -> 0(A), b5(unused) -> 17, b6 -> 9(LB), b15(unused) -> 15, hat 0x01 -> 11(Up)
            self.assertEqual(norm_b, [0, 9, 11, 15, 17])

    def test_virtual_xinput_emitter_and_l1_l2_stream_synthesis(self):
        import tempfile
        from gamepadstudio.virtual_xinput import (
            VIGEM_ERROR_NONE,
            XUSB_BUTTON_MASKS,
            VirtualXInputPad,
            compose_xusb_report,
        )

        # 1. Test compose_xusb_report bitmasks, analog triggers, Y-axis inversion & Share ('15')
        rep, snap = compose_xusb_report(buttons=['0', '9', 'LT', '15'], axes=[0.5, -1.0, 0.0, 0.0, 0.0, 0.4])
        self.assertEqual(snap['wButtons'], XUSB_BUTTON_MASKS['0'] | XUSB_BUTTON_MASKS['9'])
        self.assertEqual(snap['bLeftTrigger'], 255)
        self.assertEqual(snap['bRightTrigger'], int(round(0.4 * 255)))
        self.assertEqual(snap['sThumbLY'], 32767)  # SDL -1.0 (up) -> XUSB +32767 (up)
        self.assertTrue(snap['share_pressed'])

        # 2. Test VirtualXInputPad with mock ViGEmClient library + rumble & share callbacks
        class FakeViGEmLib:
            def __init__(self):
                self.updated_reports = []
                self.notify_cb = None
                self.removed = False

            def vigem_alloc(self):
                return 0x1001

            def vigem_free(self, client):
                pass

            def vigem_connect(self, client):
                return VIGEM_ERROR_NONE

            def vigem_disconnect(self, client):
                pass

            def vigem_target_x360_alloc(self):
                return 0x2002

            def vigem_target_free(self, target):
                pass

            def vigem_target_add(self, client, target):
                return VIGEM_ERROR_NONE

            def vigem_target_remove(self, client, target):
                self.removed = True
                return VIGEM_ERROR_NONE

            def vigem_target_x360_update(self, client, target, report):
                self.updated_reports.append(report.wButtons)
                return VIGEM_ERROR_NONE

            def vigem_target_x360_register_notification(self, client, target, cb, userdata):
                self.notify_cb = cb
                return VIGEM_ERROR_NONE

        fake_lib = FakeViGEmLib()
        share_hits = []
        rumble_hits = []
        pad = VirtualXInputPad(lib=fake_lib, on_share=lambda: share_hits.append(True))
        pad.register_rumble_callback(lambda l_m, s_m: rumble_hits.append((round(l_m, 2), round(s_m, 2))))
        self.assertTrue(pad.connect())
        pad.emit_frame(buttons=['0', '10', '15'])
        self.assertEqual(fake_lib.updated_reports[-1], XUSB_BUTTON_MASKS['0'] | XUSB_BUTTON_MASKS['10'])
        self.assertEqual(len(share_hits), 1)
        # Simulate game sending rumble (255, 128) to virtual pad
        fake_lib.notify_cb(0x1001, 0x2002, 255, 128, 0, None)
        self.assertEqual(rumble_hits, [(1.0, 0.5)])
        pad.close()
        self.assertTrue(fake_lib.removed)

        # 3. Test L1 allocate_unused_xinput + L2 gesture interception & gamepad_macro runner
        with tempfile.TemporaryDirectory() as tmp:
            owner = MappingOwner(Path(tmp))
            try:
                pro2_sdl_line = (
                    "03000000c82d00000260000000000000,8BitDo Pro 2,"
                    "a:b1,b:b0,x:b4,y:b3,back:b10,guide:b2,start:b11,"
                    "leftstick:b13,rightstick:b14,leftshoulder:b6,rightshoulder:b7,"
                    "misc1:b12,paddle1:b15,paddle2:b16,platform:Windows,"
                )
                dinput_state = dict(
                    self.state,
                    family='generic',
                    is_gamecontroller=False,
                    available_buttons=list(range(17)),
                    sdl_mapping=pro2_sdl_line,
                )
                owner.store.activate_controller(dinput_state)
                gp_profile = owner.store.data['active_profile']
                owner.store.apply_mapping_change(
                    {'op': 'allocate_unused_xinput', 'profile': gp_profile},
                    state=dinput_state,
                )
                prof_map = owner.store.data['profiles'][gp_profile]
                # Unused buttons [5, 8, 9, 12, 15, 16] automatically received XInput chord sequences!
                self.assertEqual(prof_map['5']['short'], {'action': 'gamepad_chord', 'value': '9+0'})
                self.assertEqual(prof_map['15']['short'], {'action': 'gamepad_chord', 'value': '9+11'})

                # Configure L2 rewrite on button '0' (short -> B('1'), long -> multi-step macro)
                prof_map['0'] = {
                    'short': {'action': 'gamepad_button', 'value': '1'},
                    'long': {
                        'action': 'gamepad_macro',
                        'sequence': [
                            {'buttons': ['9', '0'], 'hold_ms': 40, 'gap_ms': 20},
                            {'buttons': ['2'], 'hold_ms': 50, 'gap_ms': 0},
                        ],
                    },
                    'long_press': 0.30,
                }

                dispatched = []
                runtime = MappingRuntime(FakeActions(), lambda b, d: dispatched.append((b['action'], b.get('value'), d)), start_mouse=False)

                # Press button 0 (L2 rewritten) and button 3 (unmapped standard button -> 0ms passthrough)
                s_down = dict(dinput_state, buttons=[0, 3])
                runtime.update(s_down, owner.store.data, enabled=True, now=1.00)
                vx = runtime.virtual_xinput_state
                # Button '3' passes through at 0ms, while '0' is intercepted during L2 arbitration!
                self.assertIn('3', vx['passthrough_buttons'])
                self.assertIn('0', vx['intercepted_buttons'])
                self.assertNotIn('0', vx['passthrough_buttons'])
                self.assertEqual(vx['wButtons'], XUSB_BUTTON_MASKS['3'])

                # Hold past 0.30s -> triggers L2 long-press multi-step macro!
                runtime.update(s_down, owner.store.data, enabled=True, now=1.35)
                vx = runtime.virtual_xinput_state
                # Step 0 of macro holds '9' (LB) + '0' (A) alongside passthrough '3' (Y)
                self.assertEqual(
                    vx['wButtons'],
                    XUSB_BUTTON_MASKS['3'] | XUSB_BUTTON_MASKS['9'] | XUSB_BUTTON_MASKS['0'],
                )

                # Advance to t=1.40s (step 0 hold_ms=40ms finished, in 20ms gap)
                runtime.update(s_down, owner.store.data, enabled=True, now=1.40)
                self.assertEqual(runtime.virtual_xinput_state['wButtons'], XUSB_BUTTON_MASKS['3'])

                # Advance to t=1.42s (step 1 starts holding '2' (X))
                runtime.update(s_down, owner.store.data, enabled=True, now=1.42)
                self.assertEqual(
                    runtime.virtual_xinput_state['wButtons'],
                    XUSB_BUTTON_MASKS['3'] | XUSB_BUTTON_MASKS['2'],
                )
            finally:
                owner.close()

    def test_09_macos_hid_report_and_sdl_gamecontroller_config_parity(self):
        from gamepadstudio.virtual_xinput import (
            HID_GAMEPAD_REPORT,
            VirtualXInputPad,
            compose_hid_gamepad_report,
            export_sdl_gamecontroller_config,
        )
        import ctypes as C

        # 1. Verify 11-byte HID_GAMEPAD_REPORT synthesis for macOS DriverKit / VirtualHID parity
        self.assertEqual(C.sizeof(HID_GAMEPAD_REPORT), 11)
        hid_rep, hid_snap = compose_hid_gamepad_report(
            buttons=['0', '10', '11', '14', 'RT', '15'],
            axes=[0.0, -1.0, 0.0, 0.0, 0.0, 0.0],
        )
        self.assertEqual(hid_snap['reportId'], 1)
        self.assertEqual(hid_snap['buttons'], (1 << 0) | (1 << 10))
        self.assertEqual(hid_snap['hat'], 1)  # Up (11) + Right (14) -> NE (1)
        self.assertEqual(hid_snap['ly'], 0)   # Up (-1.0 SDL) -> 0 in unsigned HID [0..255]
        self.assertEqual(hid_snap['rt'], 255)
        self.assertTrue(hid_snap['share_pressed'])

        # 2. Verify cross-platform frame callback on VirtualXInputPad
        frames = []
        pad = VirtualXInputPad()
        pad.register_frame_callback(lambda xusb_s, hid_s: frames.append((xusb_s['wButtons'], hid_s['hat'])))
        pad.emit_frame(buttons=['0', '11'])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0][1], 0)  # Up -> hat 0

        # 3. Verify SDL_GAMECONTROLLERCONFIG export combining L1 SDL DB + L2 button remap
        sdl_cfg = export_sdl_gamecontroller_config(
            guid='03000000c82d00000260000000000000',
            name='8BitDo Pro 2',
            sdl_mapping='03000000c82d00000260000000000000,8BitDo Pro 2,a:b1,b:b0,x:b4,y:b3,platform:Mac OS X,',
            mappings={'0': {'short': {'action': 'gamepad_button', 'value': '1'}}},
            platform='Mac OS X',
        )
        self.assertIn('platform:Mac OS X,', sdl_cfg)
        # Slot '0' (a -> physical b1) remapped to '1' (b) means logical 'b' now binds to 'b1'
        self.assertIn('b:b1', sdl_cfg)


if __name__ == '__main__':
    unittest.main()
