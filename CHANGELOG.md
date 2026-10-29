# Changelog

All notable changes to **GamePad Studio** are documented in this file.

## [Unreleased]

### 🎮 Gamepad Action Macros (手柄动作宏)
- **Action Macro Editor (`mapping_ui.py`)**: Exposed the previously engine-only `gamepad_macro` action in the gamepad target-action list as **手柄动作宏 (连招序列)**, built on the existing multi-button chord selector. Each grid click appends one ordered step in a row below the selection summary; key chips delete on click (red hover as the delete affordance) and `+ 延迟` inserts an editable per-step delay chip with its own delete. A global **每步按住** spin box writes `hold_ms` across every step, so the persisted `sequence` stays in exactly the shape the runner replays.
- **Interrupt & Loop Semantics (`mapping_engine.py`)**: Added the `interrupt` (可打断) and `loop` (连续) binding flags. An interruptible macro releases its in-flight step *before* the incoming action claims a button, so two runners never race for the same XInput bit. `loop` restarts the sequence after the final step's `gap_ms` and stops on key-up; that release is routed through a dedicated `macro_loops()` predicate instead of adding `gamepad_macro` to `HOLD_ACTIONS`, which would have emitted a spurious release for one-shot macros and pushed the macro binding itself into `_hold`.
- **Short-Press Macro Fix (`mapping_engine.py`)**: `gamepad_macro` on the short gesture previously never dispatched — the instant-fire branch admitted only completed chords and `HOLD_ACTIONS` members, so macros were reachable solely through the long-press threshold. They now fire on press.
- **Macro Validation (`mapping_engine.py`)**: `validate_binding` now bounds and normalizes macro sequences (1–64 steps, targets restricted to `GAMEPAD_TARGETS`, `hold_ms` 10–500, `gap_ms` 0–5000, strict boolean flags) and rejects `interrupt`/`loop` on non-macro actions. Two legacy step shapes tolerated only by the card preview renderer (`{'button': …, 'duration': …}` and `{'action': 'down'|'up'|'delay'}`) were never executable by the runner and are now rejected; the affected test fixtures moved to the canonical form.

### 📟 Trigger Observability
- **Trigger Log (`mapping_ui.py` / `studio.py`)**: Added a collapsible (collapsed by default), 200-row-capped rolling record to the mapping workspace that distinguishes dispatched actions (`A 短按 → 手柄连招 (3 步)`), bare presses of unmapped buttons (`RB 按下 · 未映射`), chord prefixes (`LB 按下 · 组合前缀`) and suppressed keys (`· 已屏蔽`), with a `仅动作` filter that hides without discarding, and an amber `⚠` treatment while 安全试按 preview is active. Entries are deduplicated on the engine's monotonic `sequence`, which also detects an agent restart renumbering from zero.
- **Macro Lifecycle Notes (`mapping_engine.py`)**: `MappingRuntime` now reports how each run ended on a dedicated `macro_events` channel — `宏完成 · 3 步 · 0.24s`, `宏循环 4 轮后松手停止 · 0.70s`, `宏被打断 · 停在第 1/3 步 · 已释放 LB` — carrying ending reason, step progress, cycle count and elapsed time. It is deliberately separate from `events`, whose tail the mapping deck reads to choose which gesture card to highlight.
- **List & Card Labels (`mapping_engine.py`)**: `binding_label` marks macro flags inline (`手柄连招 (2 步)·可打断·循环`) so looping and interruptible combos are distinguishable in the mapping list at a glance.

### 🧪 Tests
- Added 29 cases (17 functions, one parametrized over 13 rejection shapes) to `tests/test_gamepad_macros.py`: short-press dispatch with an early key-up that must not truncate the combo, interrupt-before-new-action ordering, loop cycles with a single clean release, non-interruptible survival against an unrelated key, `output_counts` never left latched, macro validation and normalization, selector record/edit/delete round-trips, and trigger-log verdict classification, filtering, collapse state and history cap.

---

## [2.2.0] - 2026-10-27

### ✨ Design Aesthetics & UI Architecture
- **Modern Industrial Brand Iconography (`studio.svg` / `glass.py`)**: Redesigned the application logo and global controller glyphs with constant-curvature circular arcs, $45^\circ$ ergonomic inner grip tangents, warm titanium body gradients, and Teenage Engineering safety-orange (`#ff5722`) primary action accents.
- **Single-Contour Controller Schematic (`reference_xbox.svg` / `controller_photo.py` / `controller_schematic.py`)**: Removed floating shoulder/trigger pills from idle display diagrams to preserve a clean front-elevation silhouette, while adding subtle `LB/RB` and `LT/RT` micro-captions and mouse hover glow (`ControllerInput`) on the interactive mapping canvas.
- **Core Cognitive Convergence**: Hid experimental AI/voice assistant navigation by default behind the `GPS_ENABLE_AI=1` feature gate so the workspace remains focused on deterministic hardware control.
- **Typographic & Layout Polish**: Fixed icon clipping on `28x28` `IconButton` controls (`padding: 0`), prevented status pill text truncation across Chinese/English locales, and enabled dynamic width adaptation (`AdjustToContents`) on all toolbar dropdowns.

### 🚀 Core Mapping & Hardware Capabilities
- **3-Tier Hardware Identification (`device_quirks.py`)**: Added zero-latency built-in VID/PID model lookup, offline-cached `SDL_GameControllerDB` + `usb.ids` resolution, automatic DirectInput Type-A/Type-B button normalization, and gyro bias auto-calibration.
- **Gyro Aiming & Left/Right Stick Pointer Selection (`virtual_kbm.py` / `virtual_kbm_ui.py` / `schema_registry.py`)**: Added high-frequency gyroscope camera/cursor aiming with configurable activation triggers (`LT`, `RT`, `LB`, `RB`, `L3`, `R3`, or always-on), sensitivity, and Y-axis inversion, plus selectable left/right stick pointer sources.
- **Directional Outer-Ring Thresholds & Double-Tap Hold (`mapping_engine.py`)**: Added independent per-direction (`up`, `down`, `left`, `right`) outer-ring stick thresholds, `LS:up`誤触过滤 (`suppress_ls_up`), Hold-to-Repeat Turbo (`turbo`, 5–30 Hz), and automatic Double-Tap Hold (`double_tap_hold`) for sprint/dash mechanics.
- **Unified Virtual Gamepad Bus (`virtual_xinput.py`)**: Added `XUSB_REPORT` (Windows `ViGEmBus`) and `HID_GAMEPAD_REPORT` (macOS `VirtualHID`) synthesis with bidirectional rumble notification callbacks and dynamic backend availability detection in `mapping_ui.py`.
- **Cross-Platform Windows & macOS Parity (`macos_actions.py` / `hidhide.py`)**: Aligned macOS `CGEvent` keyboard/mouse synthesis, media/brightness/Mission Control system actions, and UIPI / HidHide guidance with Windows behavior.

### 📦 Packaging & Build Pipeline
- Synchronized version `2.2.0` across `pyproject.toml`, `gamepadstudio/__init__.py`, `GamePadStudio.spec`, and `installer/GamePadStudio.iss`.
- Removed unused `requests` and `qrcode` entries from `requirements.txt` and added explicit PyInstaller hidden imports for new core modules.

---

## [2.1.0] - 2026-10-15

- Initial cross-platform Windows x64 & macOS 14+ release with 300Hz Spring-Mass-Damper virtual KBM, ANSI 87 interactive keybinding workbench, touchpad 10-gesture recognition, per-device response curves, and 4K HEVC/H.264 replay buffer capture.
