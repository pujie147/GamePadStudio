"""Tests for pure Gamepad-to-Gamepad macros: remap, chords, turbo, and profile modes."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import pytest
from PySide6.QtWidgets import QApplication

from gamepadstudio.mapping_engine import (GestureEngine, validate_mappings, canonical_trigger,
                                         binding_label, output_tokens)
from gamepadstudio.studio_core import profile_mode, default_config, ACTION_NAMES
from gamepadstudio.kbm_mapper import NIKKI_PROFILE_NAME
from gamepadstudio.mapping_ui import BindingDialog, GamepadTargetSelector
from tests.test_unified_mapping import frame


def test_profile_mode_classification(tmp_path):
    config = default_config(tmp_path)
    assert profile_mode(config, '主机体验') == 'gamepad'
    assert profile_mode(config, '动作与格斗宏') == 'gamepad'
    assert '桌面导航' not in config['profiles']
    assert profile_mode(config, NIKKI_PROFILE_NAME) == 'kbm'


def test_gamepad_macro_validation_and_tokens():
    mappings = {
        '0': {'short': {'action': 'none'}, 'long': {'action': 'gamepad_turbo', 'value': '0', 'rate_hz': 20}},
        '2': {'short': {'action': 'none'}, 'long': {'action': 'gamepad_chord', 'value': '2+3'}},
        '9+10': {'short': {'action': 'gamepad_chord', 'value': '9+10+3'}, 'long': {'action': 'none'}},
        '1': {'short': {'action': 'gamepad_button', 'value': '2'}, 'long': {'action': 'none'}},
    }
    validated = validate_mappings(mappings)
    assert '0' in validated and '2' in validated and '9+10' in validated and '1' in validated

    assert output_tokens({'action': 'gamepad_button', 'value': '2'}) == ['pad:2']
    assert output_tokens({'action': 'gamepad_chord', 'value': '2+3'}) == ['pad:2', 'pad:3']
    assert output_tokens({'action': 'gamepad_turbo', 'value': '0'}) == ['pad_turbo:0']


def test_gamepad_macro_gesture_dispatch():
    events = []
    def dispatch(binding, down):
        events.append((binding['action'], binding.get('value'), down))

    engine = GestureEngine(dispatch, threshold=0.5)
    maps = {
        '2': {'short': {'action': 'gamepad_button', 'value': '3'},
              'long': {'action': 'gamepad_chord', 'value': '2+3'}}
    }

    # Short press of button 2 (Square -> outputs Triangle)
    engine.update({'2'}, maps, 0.0)
    engine.update({'2'}, maps, 0.2)
    engine.update(set(), maps, 0.25)
    # At 0.35s, 65ms tap interval has elapsed, release fires
    engine.update(set(), maps, 0.35)
    assert ('gamepad_button', '3', True) in events
    assert ('gamepad_button', '3', False) in events

    events.clear()
    # Long press of button 2 (Square -> triggers Chord 2+3)
    engine.update({'2'}, maps, 1.0)
    engine.update({'2'}, maps, 1.6)
    assert ('gamepad_chord', '2+3', True) in events
    engine.update(set(), maps, 2.0)
    assert ('gamepad_chord', '2+3', False) in events


@pytest.mark.parametrize('action,value', [('gamepad_button', '3'),
                                        ('gamepad_chord', '2+3'), ('gamepad_turbo', '0')])
def test_mapping_runtime_routes_gamepad_holds_to_gamepad_dispatch(action, value):
    from gamepadstudio.mapping_engine import MappingRuntime
    from tests.test_unified_mapping import Actions, entry, frame
    actions = Actions()
    dispatched = []
    runtime = MappingRuntime(actions, lambda binding, down: dispatched.append(
        (binding['action'], binding['value'], down)), start_mouse=False)
    config = {'active_profile': 'test', 'profiles': {'test': {'0': entry(action, value)}}}
    runtime.update(frame([0]), config, now=0)
    runtime.update(frame(), config, now=.1)
    assert dispatched == [(action, value, True), (action, value, False)]
    assert not actions.calls and not runtime.feedback()['outputs']


def macro(steps, **flags):
    return dict({'action': 'gamepad_macro', 'sequence': steps}, **flags)


def step(button, hold=40, gap=20):
    return {'buttons': [button], 'hold_ms': hold, 'gap_ms': gap}


def pad_runtime(config):
    from gamepadstudio.mapping_engine import MappingRuntime
    from tests.test_unified_mapping import Actions
    events = []
    runtime = MappingRuntime(Actions(), lambda b, d: events.append(
        (b['action'], b.get('value'), d)), start_mouse=False)
    return runtime, events


def gamepad_config(binding, trigger='0'):
    return {'active_profile': 'test',
            'profiles': {'test': {trigger: {'short': binding, 'long': {'action': 'none'}}}}}


def test_macro_short_press_runs_to_completion_and_ignores_key_up():
    # Regression: gamepad_macro is deliberately absent from HOLD_ACTIONS, so the
    # short gesture must still fire instantly without a synthetic release.
    runtime, events = pad_runtime(None)
    combo = macro([step('9'), step('0'), step('2')])
    config = gamepad_config(combo)
    runtime.update(frame([0]), config, now=0.0)
    runtime.update(frame([0]), config, now=0.02)
    runtime.update(frame(), config, now=0.05)          # released far too early
    runtime.update(frame(), config, now=0.90)
    assert events == [('gamepad_button', '9', True), ('gamepad_button', '9', False),
                      ('gamepad_button', '0', True), ('gamepad_button', '0', False),
                      ('gamepad_button', '2', True), ('gamepad_button', '2', False)]
    assert not runtime.macro_runners


def test_interruptible_macro_yields_to_the_new_action():
    runtime, events = pad_runtime(None)
    config = {'active_profile': 'test', 'profiles': {'test': {
        '0': {'short': macro([step('9', gap=0), step('0', gap=0)], interrupt=True),
              'long': {'action': 'none'}},
        '1': {'short': {'action': 'gamepad_button', 'value': '2'}, 'long': {'action': 'none'}},
    }}}
    runtime.update(frame([0]), config, now=0.0)
    assert events == [('gamepad_button', '9', True)]
    events.clear()
    runtime.update(frame([0, 1]), config, now=0.02)
    # The interrupted step is released before the new action claims a button.
    assert events == [('gamepad_button', '9', False), ('gamepad_button', '2', True)]
    events.clear()
    runtime.update(frame(), config, now=0.90)
    assert events == [('gamepad_button', '2', False)]   # the macro never resumes
    assert not runtime.macro_runners


def test_plain_macro_survives_an_unrelated_key():
    runtime, events = pad_runtime(None)
    config = {'active_profile': 'test', 'profiles': {'test': {
        '0': {'short': macro([step('9', gap=0), step('0', gap=0)]), 'long': {'action': 'none'}},
        '1': {'short': {'action': 'gamepad_button', 'value': '2'}, 'long': {'action': 'none'}},
    }}}
    runtime.update(frame([0]), config, now=0.0)
    runtime.update(frame([0, 1]), config, now=0.02)
    runtime.update(frame(), config, now=0.90)
    assert ('gamepad_button', '0', True) in events      # second step still played
    assert events.count(('gamepad_button', '9', True)) == 1


def test_looping_macro_repeats_while_held_and_frees_the_held_step_on_release():
    runtime, events = pad_runtime(None)
    combo = macro([step('9', hold=100, gap=100)], loop=True)
    config = {'active_profile': 'test', 'profiles': {'test': {
        '0': {'short': {'action': 'none'}, 'long': combo, 'long_press': 0.30}}}}
    runtime.update(frame([0]), config, now=0.0)
    for now in (0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80):
        runtime.update(frame([0]), config, now=now)
    presses = [e for e in events if e == ('gamepad_button', '9', True)]
    assert len(presses) == 3                            # 0.35, 0.55, 0.75
    assert runtime.macro_runners                        # still cycling
    events.clear()
    runtime.update(frame(), config, now=0.80)           # lifted mid-step
    runtime.update(frame(), config, now=9.0)
    assert events == [('gamepad_button', '9', False)]   # released once, then silence
    assert not runtime.macro_runners


def test_non_looping_macro_ignores_its_release_edge():
    runtime, events = pad_runtime(None)
    combo = macro([step('9', hold=40, gap=20)])
    config = {'active_profile': 'test', 'profiles': {'test': {
        '0': {'short': {'action': 'none'}, 'long': combo, 'long_press': 0.30}}}}
    runtime.update(frame([0]), config, now=0.0)
    runtime.update(frame([0]), config, now=0.35)
    events.clear()
    runtime.update(frame(), config, now=0.36)           # release mid-cycle
    runtime.update(frame(), config, now=0.44)
    assert events == [('gamepad_button', '9', False)]   # self-terminates, no double up
    runtime.update(frame(), config, now=9.0)
    assert events == [('gamepad_button', '9', False)]
    assert not runtime.macro_runners


@pytest.mark.parametrize('binding, message', [
    (macro(None), '请录制手柄动作宏流程'),
    (macro([]), '宏流程为空'),
    (macro('nope'), '请录制手柄动作宏流程'),
    (macro([{'buttons': ['99']}]), '目标手柄按键'),
    (macro([{'value': 'TP:tap'}]), '目标手柄按键'),
    (macro([{}]), '目标手柄按键'),
    (macro([step('0', gap=99999)]), '延迟时长'),
    (macro([step('0', hold=1)]), '每步按住时长'),
    (macro([step('0', hold='soon')]), '每步按住时长'),
    (macro([{'buttons': '0'}]), '目标手柄按键'),
    (macro([step('0')] * 65), '最多 64 步'),
    (macro([step('0')], interrupt='yes'), '可打断'),
    (macro([step('0')], loop=1), '连续'),
])
def test_macro_sequence_rejects_unrunnable_bindings(binding, message):
    with pytest.raises(ValueError, match=message):
        validate_mappings({'0': {'short': binding, 'long': {'action': 'none'}}})


def test_macro_accepts_and_normalizes_a_recorded_combo():
    combo = macro([{'buttons': ['0', '9'], 'hold_ms': 50.0, 'gap_ms': 0},
                   {'value': 'LT', 'hold_ms': 60, 'gap_ms': 120}])
    validated = validate_mappings({'0': {'short': combo, 'long': {'action': 'none'}}})
    assert validated['0']['short']['sequence'] == [
        {'buttons': ['0', '9'], 'hold_ms': 50, 'gap_ms': 0},
        {'buttons': ['LT'], 'hold_ms': 60, 'gap_ms': 120}]


def test_macro_flags_are_macro_only():
    with pytest.raises(ValueError, match='仅用于手柄动作宏'):
        validate_mappings({'0': {'short': {'action': 'gamepad_button', 'value': '2', 'loop': True},
                                 'long': {'action': 'none'}}})


def test_trigger_log_separates_actions_from_bare_presses():
    from gamepadstudio.mapping_ui import TriggerLog, press_verdicts
    app = QApplication.instance() or QApplication([])
    mappings = {
        '0': {'short': {'action': 'gamepad_macro',
                        'sequence': [{'buttons': ['9'], 'hold_ms': 50, 'gap_ms': 0}]},
              'long': {'action': 'none'}},
        '9+0': {'short': {'action': 'gamepad_chord', 'value': '2'}, 'long': {'action': 'none'}},
        '10': {'short': {'action': 'suppress'}, 'long': {'action': 'none'}},
        '11': {'short': {'action': 'none'}, 'long': {'action': 'none'}},
    }
    dispatchable, prefix, blocked = press_verdicts(mappings)
    assert dispatchable == {'0'} and prefix == {'9', '0'} and blocked == {'10'}

    log = TriggerLog()
    assert log.toggle_button.isChecked() and log.list.isHidden()   # collapsed by default
    assert log.count_label.text() == '暂无触发'

    log.record([{'seq': 1, 'trigger': '0', 'gesture': 'short', 'action': '手柄连招 (1 步)'}],
               sequence=1, family='xbox', presses={'0', '9', '10', '1'}, mappings=mappings)
    log.toggle_button.click()
    texts = [log.list.item(i).text() for i in range(log.list.count())]
    assert any('A 短按 → 手柄连招' in t for t in texts)
    assert not any('A 按下' in t for t in texts)        # a dispatched key never gets a bare row
    assert sum('LB 按下 · 组合前缀' in t for t in texts) == 1
    assert sum('RB 按下 · 已屏蔽' in t for t in texts) == 1
    assert sum('按下 · 未映射' in t for t in texts) == 1
    assert log.count_label.text() == '4 条'

    log.only_actions.setChecked(True)                   # bare rows fold away, history survives
    assert log.list.count() == 1 and log.count_label.text() == '1 条'
    assert log.clear_button.isEnabled()
    log.only_actions.setChecked(False)
    assert log.list.count() == 4

    log.record([{'seq': 2, 'trigger': '0', 'gesture': 'long', 'action': '手柄连招 (1 步)'}],
               sequence=2, preview=True, family='xbox')
    assert not log.preview_badge.isHidden()
    assert log.list.item(0).text().startswith('⚠ ')
    assert log.list.item(0).foreground().color().name() == '#f59e0b'

    # The engine ships its live sequence alongside the ring, so a stale entry
    # that sits behind the cursor must never be re-rendered.
    log.record([{'seq': 1, 'trigger': '0', 'gesture': 'short', 'action': '环里的旧事件'}], sequence=2)
    assert log.list.count() == 5
    # A lower live sequence means the agent restarted and renumbered from zero.
    log.record([{'seq': 1, 'trigger': '2', 'gesture': 'short', 'action': '重启后的新事件'}], sequence=1)
    assert log.list.count() == 6 and log.cursor == 1


def test_trigger_log_bounds_history():
    from gamepadstudio.mapping_ui import TriggerLog
    app = QApplication.instance() or QApplication([])
    log = TriggerLog()
    for seq in range(1, 300):
        log.record([{'seq': seq, 'trigger': '0', 'gesture': 'short', 'action': f'动作 {seq}'}],
                   sequence=seq)
    assert len(log.rows) == TriggerLog.MAX_ROWS
    assert log.list.count() == TriggerLog.MAX_ROWS
    assert '动作 299' in log.list.item(0).text()         # newest survives the trim
    rendered = ''.join(log.list.item(i).text() for i in range(log.list.count()))
    assert '动作 100' in rendered and '动作 99' not in rendered   # oldest 99 were dropped


def macro_notes(profiles, keys_at, seconds):
    """Drive the real runtime on a fake clock and collect the lifecycle notes it emits."""
    from gamepadstudio.mapping_engine import MappingRuntime
    from tests.test_unified_mapping import Actions, frame
    runtime = MappingRuntime(Actions(), lambda b, d: None, start_mouse=False)
    config = {'active_profile': 'test', 'profiles': {'test': profiles}}
    seen = {}
    for i in range(int(seconds / 0.02) + 1):
        now = round(i * 0.02, 3)
        runtime.update(frame(keys_at(now)), config, enabled=True, now=now)
        for note in runtime.feedback()['macro_events']:
            seen[note['seq']] = note
    return list(seen.values()), runtime


def latched(runtime):
    return [token for token, count in runtime.output_counts.items() if count]


def test_macro_lifecycle_notes_report_how_each_run_ended():
    combo = macro([step('9', 40, 60), step('0', 40, 60), step('2', 40, 60)])
    notes, runtime = macro_notes({'0': {'short': combo, 'long': {'action': 'none'}}},
                                 lambda t: [0] if t < 0.02 else [], 0.5)
    assert len(notes) == 1
    note = notes[0]
    assert (note['reason'], note['steps'], note['reached'], note['trigger'], note['gesture']) == \
           ('complete', 3, 3, '0', 'short')
    assert 0.20 <= note['elapsed'] <= 0.28
    assert not latched(runtime)                     # nothing left held on the virtual pad

    held = macro([step('9', 400, 60), step('0')], interrupt=True)
    profiles = {'0': {'short': held, 'long': {'action': 'none'}},
                '1': {'short': {'action': 'gamepad_button', 'value': '3'}, 'long': {'action': 'none'}}}
    notes, runtime = macro_notes(profiles, lambda t: [0] if t < 0.20 else [0, 1], 0.5)
    assert [n['reason'] for n in notes] == ['interrupted']
    assert notes[0]['reached'] == 1 and notes[0]['held'] == ['9']
    assert not latched(runtime)                     # the interrupted step was freed

    looping = macro([step('9', 50, 50), step('0', 50, 50)], loop=True)
    notes, runtime = macro_notes(
        {'0': {'short': {'action': 'none'}, 'long': looping, 'long_press': 0.30}},
        lambda t: [0] if t < 1.0 else [], 1.2)
    assert notes[0]['reason'] == 'released' and notes[0]['loop'] is True
    assert notes[0]['cycles'] >= 3
    assert not latched(runtime)


def test_trigger_log_renders_macro_lifecycle_rows():
    from gamepadstudio.mapping_ui import TriggerLog
    app = QApplication.instance() or QApplication([])
    log = TriggerLog()
    log.record([], 0, False, 'xbox', (), {}, [
        {'seq': 1, 'trigger': '0', 'gesture': 'short', 'reason': 'complete', 'steps': 3,
         'reached': 3, 'cycles': 1, 'loop': False, 'elapsed': 0.24, 'held': []},
        {'seq': 2, 'trigger': '1', 'gesture': 'long', 'reason': 'released', 'steps': 2,
         'reached': 1, 'cycles': 4, 'loop': True, 'elapsed': 0.70, 'held': []},
        {'seq': 3, 'trigger': '2', 'gesture': 'short', 'reason': 'interrupted', 'steps': 3,
         'reached': 1, 'cycles': 1, 'loop': False, 'elapsed': 0.10, 'held': ['9']},
    ], 3)
    log.toggle_button.click()
    rows = [log.list.item(i).text() for i in range(log.list.count())]
    assert any('A 宏完成 · 3 步 · 0.24s' in r for r in rows)
    assert any('B 宏循环 4 轮后松手停止 · 0.70s' in r for r in rows)
    assert any('X 宏被打断 · 停在第 1/3 步 · 已释放 LB' in r for r in rows)


def test_macro_list_label_distinguishes_flags():
    assert binding_label(macro([step('9')])) == '手柄连招 (1 步)'
    assert binding_label(macro([step('9'), step('0')], interrupt=True)) == '手柄连招 (2 步)·可打断'
    assert binding_label(macro([step('9')], loop=True)) == '手柄连招 (1 步)·循环'


def test_macro_output_tokens_stay_scoped_to_sub_steps():
    # _hold must never receive the macro binding itself; its own token stays inert
    # so a stray call cannot latch a virtual pad button.
    assert output_tokens(macro([step('9'), step('0')])) == ['action:gamepad_macro']


def test_gamepad_target_selector_ui(tmp_path):
    app = QApplication.instance() or QApplication([])
    selector = GamepadTargetSelector('dualsense')
    selector.set_mode('single')
    selector._on_btn_clicked('1')
    assert selector.get_selected_single() == '1'

    selector.set_mode('chord')
    selector._on_btn_clicked('2')
    selector._on_btn_clicked('3')
    chord = selector.get_selected_chord()
    assert '2' in chord and '3' in chord

    selector.set_mode('turbo')
    selector.turbo_spin.setValue(18)
    assert selector.get_turbo_rate() == 18


def test_macro_selector_records_edits_and_deletes(tmp_path):
    app = QApplication.instance() or QApplication([])
    selector = GamepadTargetSelector('dualsense', gesture='long')
    selector.set_mode('macro')
    assert not selector.macro_box.isHidden()
    assert not selector.macro_loop.isHidden()            # loop belongs to the long press

    for key in ('9', '0', '2'):
        selector._on_btn_clicked(key)
    assert [s['button'] for s in selector.macro_steps] == ['9', '0', '2']
    assert selector.get_selected_single() == '0'         # the grid selection is untouched

    selector._append_delay()
    assert selector.macro_steps[-1]['gap_ms'] == 100
    assert not selector.macro_delay_btn.isEnabled()      # no stacking a second delay
    selector._remove_delay(2)
    assert selector.macro_steps[-1]['gap_ms'] == 0

    selector._remove_step(1)
    assert [s['button'] for s in selector.macro_steps] == ['9', '2']

    selector.macro_hold_spin.setValue(90)
    selector.macro_interrupt.setChecked(True)
    selector.macro_loop.setChecked(True)
    assert selector.get_selected_macro() == [
        {'buttons': ['9'], 'hold_ms': 90, 'gap_ms': 0},
        {'buttons': ['2'], 'hold_ms': 90, 'gap_ms': 0}]
    assert selector.get_macro_interrupt() and selector.get_macro_loop()

    selector._clear_macro()
    assert selector.get_selected_macro() == []


def test_macro_short_press_box_has_no_loop_option(tmp_path):
    app = QApplication.instance() or QApplication([])
    short = GamepadTargetSelector('dualsense', gesture='short')
    short.set_mode('macro')
    short.macro_loop.setChecked(True)                    # a stale flag cannot leak through
    assert short.get_macro_loop() is False


def test_macro_round_trips_through_the_binding_dialog(tmp_path):
    from gamepadstudio.studio import Studio
    app = QApplication.instance() or QApplication([])
    studio = Studio(tmp_path, standalone=True)
    try:
        combo = macro([step('9', hold=50, gap=120), step('0', hold=50, gap=0)], interrupt=True)
        binding = {'short': {'action': 'none'}, 'long': combo, 'long_press': 0.65}
        dialog = BindingDialog(studio, trigger='0', mapping=binding, mode='gamepad')
        assert dialog.action_combos['long'].currentData() == 'gamepad_macro'
        selector = dialog.target_selectors['long']
        assert selector.mode == 'macro'
        assert selector.macro_hold_spin.value() == 50
        assert selector.macro_interrupt.isChecked()
        assert not selector.macro_loop.isHidden()
        value = dialog.value()
        assert value['long']['sequence'] == combo['sequence']
        assert value['long']['interrupt'] is True
        assert value['long']['loop'] is False
        assert value['short']['action'] == 'none'
    finally:
        studio.cleanup()
        studio.hide()


def test_macro_chip_pane_scrolls_instead_of_growing_the_editor(tmp_path):
    """A long combo must not push the editor past the screen: the chip row scrolls."""
    from gamepadstudio.studio import Studio
    from gamepadstudio.mapping_ui import MACRO_FLOW_MAX_HEIGHT, MACRO_STEP_LIMIT
    app = QApplication.instance() or QApplication([])
    studio = Studio(tmp_path, standalone=True)

    def build(steps):
        sequence = [{'buttons': ['9'], 'hold_ms': 50, 'gap_ms': 80} for _ in range(steps)]
        binding = {'short': {'action': 'gamepad_macro', 'sequence': sequence},
                   'long': {'action': 'none'}, 'long_press': 0.65}
        dialog = BindingDialog(studio, trigger='0', mapping=binding, mode='gamepad')
        dialog.show()
        app.processEvents()
        return dialog

    try:
        heights = {}
        for steps in (1, 2, MACRO_STEP_LIMIT):
            dialog = build(steps)
            heights[steps] = dialog.height()
            dialog.deleteLater()
        assert len(set(heights.values())) == 1, heights   # length cannot grow the dialog

        empty = build(0)
        assert empty.target_selectors['short'].macro_scroll.isHidden()
        empty.deleteLater()

        big = build(MACRO_STEP_LIMIT)
        selector = big.target_selectors['short']
        assert selector.macro_scroll.height() == MACRO_FLOW_MAX_HEIGHT
        assert selector.macro_canvas.height() > selector.macro_scroll.height()
        assert selector.macro_scroll.verticalScrollBar().maximum() > 0
        big.deleteLater()
    finally:
        studio.cleanup()
        studio.hide()


def test_binding_dialog_fits_a_handheld_screen(tmp_path):
    """保存映射 stays clickable even when the screen is only ~540 logical pixels tall."""
    from gamepadstudio.studio import Studio
    from gamepadstudio.mapping_ui import MACRO_STEP_LIMIT
    app = QApplication.instance() or QApplication([])
    studio = Studio(tmp_path, standalone=True)
    try:
        sequence = [{'buttons': ['9'], 'hold_ms': 50, 'gap_ms': 80} for _ in range(MACRO_STEP_LIMIT)]
        binding = {'short': {'action': 'gamepad_macro', 'sequence': sequence},
                   'long': {'action': 'gamepad_macro', 'sequence': sequence},
                   'long_press': 0.65}
        dialog = BindingDialog(studio, trigger='0', mapping=binding, mode='gamepad')
        dialog.show()
        app.processEvents()
        try:
            limit = dialog.screen().availableGeometry().height()
            assert dialog.height() <= limit              # never starts off-screen
            dialog.resize(660, min(516, limit))          # ROG Ally at 1920x1080 / 200%
            app.processEvents()
            bottom = dialog.save_button.mapTo(dialog, dialog.save_button.pos()).y()
            bottom += dialog.save_button.height()
            assert bottom <= dialog.height()             # pinned outside the scroll body
            assert dialog.body_scroll.verticalScrollBar().maximum() > 0   # the rest scrolls in
        finally:
            dialog.hide()
            dialog.deleteLater()
    finally:
        studio.cleanup()
        studio.hide()


def test_binding_dialog_pure_gamepad_configuration(tmp_path, monkeypatch):
    from gamepadstudio.studio import Studio
    app = QApplication.instance() or QApplication([])
    studio = Studio(tmp_path, standalone=True)
    try:
        # Edit button 0 (A / Cross)
        mapping_data = {
            'short': {'action': 'none'},
            'long': {'action': 'gamepad_chord', 'value': '2+3'},
            'long_press': 0.65
        }
        dialog = BindingDialog(studio, trigger='0', mapping=mapping_data)
        val = dialog.value()
        assert val['short']['action'] == 'none'
        assert val['long']['action'] == 'gamepad_chord'
        assert '2' in val['long']['value'] and '3' in val['long']['value']
        assert val['long_press'] == 0.65
    finally:
        studio.cleanup()
        studio.hide()


def test_dropdown_isolation_gamepad_vs_kbm(tmp_path):
    from gamepadstudio.studio import Studio
    app = QApplication.instance() or QApplication([])
    studio = Studio(tmp_path, standalone=True)
    try:
        studio.refresh_mappings()
        # Page 1: Gamepad mapping combo MUST ONLY contain gamepad profiles
        mapping_items = [studio.mapping_combo.itemText(i) for i in range(studio.mapping_combo.count())]
        assert mapping_items == studio.store.profiles_for(studio.snapshot, 'gamepad')
        assert '主机体验' not in mapping_items
        assert '动作与格斗宏' not in mapping_items
        assert '桌面导航' not in mapping_items
        assert NIKKI_PROFILE_NAME not in mapping_items

        # Page 6: Virtual KBM scheme combo MUST ONLY contain KBM profiles
        scheme_items = [studio.virtual_kbm_page.scheme_combo.itemText(i) for i in range(studio.virtual_kbm_page.scheme_combo.count())]
        assert scheme_items == [NIKKI_PROFILE_NAME]
        assert '主机体验' not in scheme_items
        assert '动作与格斗宏' not in scheme_items

        # Page 0: Profile combo contains only the current input device's profiles.
        profile_items = [studio.profile_combo.itemText(i) for i in range(studio.profile_combo.count())]
        assert profile_items == studio.store.profiles_for(studio.snapshot)
        assert '主机体验' not in profile_items
        assert '动作与格斗宏' not in profile_items
        assert '桌面导航' not in profile_items
        assert NIKKI_PROFILE_NAME in profile_items
    finally:
        studio.cleanup()
        studio.hide()


def test_mode_inheritance_on_create_profile(tmp_path):
    from gamepadstudio.studio import Studio
    app = QApplication.instance() or QApplication([])
    studio = Studio(tmp_path, standalone=True)
    try:
        # Create a gamepad profile
        studio.mapping_change({'op': 'create', 'profile': '自定义手柄宏', 'mode': 'gamepad'})
        assert profile_mode(studio.config, '自定义手柄宏') == 'gamepad'
        mapping_items = [studio.mapping_combo.itemText(i) for i in range(studio.mapping_combo.count())]
        scheme_items = [studio.virtual_kbm_page.scheme_combo.itemText(i) for i in range(studio.virtual_kbm_page.scheme_combo.count())]
        assert '自定义手柄宏' in mapping_items
        assert '自定义手柄宏' not in scheme_items

        # Create a KBM profile
        studio.mapping_change({'op': 'create', 'profile': '自定义键鼠预设', 'mode': 'kbm'})
        assert profile_mode(studio.config, '自定义键鼠预设') == 'kbm'
        mapping_items = [studio.mapping_combo.itemText(i) for i in range(studio.mapping_combo.count())]
        scheme_items = [studio.virtual_kbm_page.scheme_combo.itemText(i) for i in range(studio.virtual_kbm_page.scheme_combo.count())]
        assert '自定义键鼠预设' in scheme_items
        assert '自定义键鼠预设' not in mapping_items
    finally:
        studio.cleanup()
        studio.hide()


def test_kbm_and_gamepad_dialog_modes(tmp_path):
    from gamepadstudio.studio import Studio
    app = QApplication.instance() or QApplication([])
    studio = Studio(tmp_path, standalone=True)
    try:
        # Gamepad mode dialog
        g_dialog = BindingDialog(studio, '0', profile=studio.current_gamepad_profile(), mode='gamepad')
        assert g_dialog.mode == 'gamepad'
        assert 'gamepad_button' in [g_dialog.action_combos['short'].itemData(i) for i in range(g_dialog.action_combos['short'].count())]
        assert 'hold' not in [g_dialog.action_combos['short'].itemData(i) for i in range(g_dialog.action_combos['short'].count())]

        # KBM mode dialog
        k_dialog = BindingDialog(studio, '0', profile=NIKKI_PROFILE_NAME, mode='kbm')
        assert k_dialog.mode == 'kbm'
        assert 'hold' in [k_dialog.action_combos['short'].itemData(i) for i in range(k_dialog.action_combos['short'].count())]
        assert 'gamepad_button' not in [k_dialog.action_combos['short'].itemData(i) for i in range(k_dialog.action_combos['short'].count())]
    finally:
        studio.cleanup()
        studio.hide()
