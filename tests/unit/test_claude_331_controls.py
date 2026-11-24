"""Measured model panel and native direct effort result on a stand-in Claude seat."""

from dataclasses import replace
from datetime import datetime, timedelta
import json

import pytest

from quarterdeck import model_mismatch, post, request_queue
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.types import ControlAnswer

from .test_control_confirmation_claude_gates import (
    AFTER, DISPLAY, FIXTURES, TARGET, answer_for, append, record_with_prefix, ship_seat, stand_in, triple,
)


VERSION = '2.1.331'
QUESTION = FIXTURES.parent / VERSION / 'switch-model-question-120x50'
PLAIN = QUESTION.with_suffix('.txt').read_text()
STYLED = QUESTION.with_suffix('.ansi').read_text()
EFFORT = 'high'


def panel(text, name):
    if name == 'model':
        return text
    return (text.replace('Switch model?', 'Change effort level?')
            .replace('current model.', 'current effort level.').replace(DISPLAY, EFFORT))


@pytest.mark.parametrize(('width', 'height'), [(120, 50), (273, 65), (319, 78), (151, 54)])
def test_a_complete_native_model_panel_uses_the_current_pane_dimensions(tmp_path, width, height):
    plain, styled = ('\n' * (height - 50) + text.replace('▔' * 120, '▔' * width)
                     for text in (PLAIN, STYLED))
    # The styled pane can retain the trailing background cell of a history row.
    styled = '\x1b[48;2;55;55;55m \x1b[49m' + styled if height > 50 else styled
    seat = stand_in(tmp_path, version=VERSION, width=width, height=height, styled=styled)
    context, answer = answer_for(seat, plain=plain, version=VERSION)
    assert context is not None and context.scope[:4] == ('%7', 4242, width, height)
    assert answer == ControlAnswer('model', TARGET, f'Yes, switch to {DISPLAY}', ('Enter',))


@pytest.mark.parametrize('variant', ['wrong target', 'visible cursor', 'wrong pane',
                                    'unhighlighted yes', 'new wording', 'wrong focus', 'plain frame',
                                    'incomplete frame', 'clipped rule', 'wrapped panel', 'clipped body',
                                    'changed plain capture', 'second dialog'])
def test_candidate_model_requires_the_measured_panel_and_owned_focus(tmp_path, variant):
    plain, styled, value, width = PLAIN, STYLED, TARGET, 120
    if variant == 'wrong target':
        value = 'claude-opus-5-5'
    elif variant == 'unhighlighted yes':
        styled = styled.replace(f'\x1b[38;2;177;185;249mYes, switch to {DISPLAY}',
                                f'\x1b[38;2;153;153;153mYes, switch to {DISPLAY}')
    elif variant == 'new wording':
        plain, styled = (text.replace('Enter to confirm', 'Enter to accept') for text in (plain, styled))
    elif variant == 'wrong focus':
        plain, styled = (text.replace('❯', ' ') for text in (plain, styled))
    elif variant == 'plain frame':
        styled = plain
    elif variant == 'incomplete frame':
        plain, styled = (text.split('\n', 1)[1] for text in (plain, styled))
    elif variant == 'clipped rule':
        width = 121
    elif variant == 'wrapped panel':
        plain, styled = (text.split('\n', 1)[1].replace(' means the full history', '\n  means the full history')
                         for text in (plain, styled))
    elif variant == 'clipped body':
        width = 111
        plain, styled = (text.replace('▔' * 120, '▔' * width) for text in (plain, styled))
    elif variant == 'changed plain capture':
        plain = plain.replace('Switch model?', 'Switch model?!')
    elif variant == 'second dialog':
        plain, styled = ('  Switch model?\n' + text.split('\n', 1)[1] for text in (plain, styled))
    seat = stand_in(tmp_path, version=VERSION, width=width, height=50, styled=styled,
                    cursor=variant == 'visible cursor')
    if variant == 'wrong pane':
        frame = seat.read_control_frame()
        seat.read_control_frame = lambda: replace(frame, pane=replace(frame.pane, id='%8'))
    context, answer = answer_for(seat, value=value, plain=plain, version=VERSION)
    assert answer is None
    if variant != 'second dialog':
        assert context is None


@pytest.mark.parametrize('version', ['2.1.317', VERSION])
def test_fable_identity_is_bound_to_the_measured_version(tmp_path, version):
    adapter = ClaudeHarness()
    seat = stand_in(tmp_path, version=version)
    record = record_with_prefix(tmp_path)
    target, label = 'claude-fable-5-2', 'Fable 5.2'
    catalog, = seat.home.glob('cache/model-catalog/*.json')
    data = json.loads(catalog.read_text())
    data['catalog']['config']['models'].append(dict(id=target, name=label))
    catalog.write_text(json.dumps(data))

    origin = adapter.model_control_origin(seat, record, target)
    expected = target if version == VERSION else None
    if version == '2.1.317':
        assert origin is None
        # Recreate an already reserved origin from the old unscoped implementation.
        origin = adapter.model_control_origin(seat, record, TARGET)
        origin['display']['display'] = label
    assert origin['display']['display'] == label
    rows = triple(target, display=label)
    rows[1]['message']['content'] = rows[1]['message']['content'].replace('\n', '\n            ')
    for row in rows:
        row.update(version=version, sessionId=record.stem)
    append(record, rows)
    assert adapter.model_control_result(seat, record, target, origin) == expected

    append(record, [dict(type='assistant', version=version, uuid='fable-reply', parentUuid=rows[-1]['uuid'],
                         message=dict(role='assistant', model=target, content=[dict(type='text', text='ok')]))])
    assert adapter.record_model(record) == expected
    comparison = model_mismatch.compare(adapter, TARGET, target, version=version, source='record')
    assert comparison.status == ('mismatch' if version == VERSION else 'unknown')
    assert adapter.model_identity(label, version=version) is None


def native_result(world, name, value, *, missing=False, arguments=None, output=None):
    rows = triple(value, name=f'/{name}')
    rows[1]['message']['content'] = (f'<command-name>/{name}</command-name>\n'
                                    f'            <command-message>{name}</command-message>\n'
                                    f'            <command-args>{arguments or value}</command-args>')
    if output is None:
        output = ('Set effort level to high (saved as your default for new sessions): '
                  'Comprehensive implementation with extensive testing and documentation' if name == 'effort'
                  else f'Set model to `{DISPLAY}` and saved as your default for new sessions')
    rows[2]['message']['content'] = f'<local-command-stdout>{output}</local-command-stdout>'
    for row in rows:
        row.update(version=VERSION, sessionId=world.record.stem, timestamp=post.now().isoformat())
    append(world.record, rows[:2] if missing else rows)


@pytest.fixture
def candidate(ship_seat):
    world = ship_seat()
    path = world.seat.trusted / 'launch.json'
    launch = json.loads(path.read_text())
    path.write_text(json.dumps(dict(launch, version=VERSION)))
    common = dict(isSidechain=False, version=VERSION, sessionId=world.record.stem, timestamp=post.now().isoformat())
    append(world.record, [
        dict(common, type='assistant', uuid='a-0', parentUuid='u-0', effort=EFFORT,
             message=dict(role='assistant', model='claude-fable-5-2', stop_reason='end_turn',
                          content=[dict(type='text', text='Synthetic reply.')])),
        dict(common, type='system', subtype='turn_duration', uuid='d-0', parentUuid='a-0', durationMs=1),
    ])
    world.live.update(frame=AFTER, pane=replace(world.live['pane'], height=50))
    return world


def file_control(world, name, value, *, plain=None, styled=None, immediate=False):
    path = request_queue.enqueue(world.svc.runtime, 'control', dict(seat='worker', name=name, value=value))
    if plain is None:
        plain = panel(PLAIN, name)
    if styled is None:
        styled = panel(STYLED, name)

    def paste(pane, text):
        assert pane == world.live['pane'].id
        world.live['pasted'].append(text)
        world.live['frame'] = AFTER.replace('❯\u00a0', f'❯\u00a0{text}', 1)

    def keys(pane, *pressed):
        assert pane == world.live['pane'].id
        world.live['keys'].append(pressed)
        if len(world.live['keys']) == 1:
            assert request_queue.control_continuation(path).phase == 'reserved'
            if immediate:
                world.live['frame'] = AFTER
                native_result(world, name, value)
            else:
                world.live.update(frame=plain, styled=styled)
        else:
            assert request_queue.control_continuation(path).phase == 'answering'

    world.svc.tmux.paste = paste
    world.svc.tmux.keys = keys
    return path


def test_owned_model_control_can_send_one_enter_and_confirms_only_its_new_native_result(candidate):
    world = candidate
    name, value = 'model', TARGET
    path = file_control(world, name, value)
    world.svc.requests()
    assert world.live['pasted'] == [f'/{name} {value}'], request_queue.metadata(path).get('waiting')
    assert world.live['keys'] == [('Enter',)], 'only the command submission'
    assert request_queue.control_continuation(path).phase == 'submitted'
    world.svc.requests()
    assert world.live['keys'] == [('Enter',), ('Enter',)]
    assert request_queue.control_continuation(path).phase == 'answered'
    world.svc.requests()
    assert world.live['keys'] == [('Enter',), ('Enter',)]
    assert not path.with_suffix('.done').exists()
    # Native saves can change settings and mark the per-seat catalog stale after the gesture.
    (world.seat.home / 'settings.json').write_text(json.dumps(dict(model=value)))
    for catalog in (world.seat.home / 'cache' / 'model-catalog').glob('*.json'):
        catalog.unlink()
    native_result(world, name, value)
    world.svc.requests()
    assert 'program asked; ship answered yes; record confirmed · confirmed by record' in (
        path.with_suffix('.done').read_text())
    assert world.live['pasted'] == [f'/{name} {value}']
    assert world.live['keys'] == [('Enter',), ('Enter',)]


def test_effort_without_a_dialog_uses_the_native_result_without_a_second_enter(candidate):
    path = file_control(candidate, 'effort', EFFORT, immediate=True)
    candidate.svc.requests()
    answer = path.with_suffix('.done').read_text()
    assert 'confirmed by record' in answer and 'program asked' not in answer
    assert candidate.live['keys'] == [('Enter',)]


@pytest.mark.parametrize('variant', ['wrong value', 'no result', 'stale record', 'effort dialog', 'changed request'])
def test_effort_holds_without_an_extra_gesture(candidate, monkeypatch, variant):
    world = candidate
    if variant == 'stale record':
        native_result(world, 'effort', EFFORT)
    path = file_control(world, 'effort', EFFORT)
    world.svc.requests()
    state = request_queue.control_continuation(path)
    assert state is not None, request_queue.metadata(path).get('waiting')
    expires = datetime.fromisoformat(state.deadline)
    if variant == 'wrong value':
        native_result(world, 'effort', EFFORT, arguments='low')
    elif variant == 'no result':
        native_result(world, 'effort', EFFORT, missing=True)
    elif variant == 'changed request':
        original = path.read_text()
        path.write_text(original.replace(EFFORT, 'medium'))
        world.svc.requests()
        assert request_queue.metadata(path)['waiting']['dimension'] == 'control-confirmation'
        assert world.live['keys'] == [('Enter',)]
        return
    elif variant != 'effort dialog':
        world.live.update(frame=AFTER)
    world.svc.requests()
    assert not path.with_suffix('.done').exists()
    if variant == 'effort dialog':
        held = request_queue.control_continuation(path)
        assert held.phase == 'submitted' and held.asked
        assert 'focus/context or option/value/gesture unknown' in request_queue.metadata(path)['waiting']['reason']
        world.svc.requests()
        assert world.live['keys'] == [('Enter',)]
    monkeypatch.setattr(post, 'now', lambda: expires + timedelta(seconds=1))
    world.svc.requests()
    assert 'record unconfirmed' in path.with_suffix('.done').read_text()
    assert world.live['pasted'] == [f'/effort {EFFORT}']
    assert world.live['keys'] == [('Enter',)]
