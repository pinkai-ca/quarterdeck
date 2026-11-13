"""Independent Claude gates for the control that answers its own model confirmation, by text, never by cursor.

Recorded 2.1.317 120x40 frames (scrubbed) and synthetic variants of them; synthetic records and requests. Each gate
reads the production adapter, door and request owner directly; none reuses the change's own tests or helpers.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import control_confirmation as confirmation
from quarterdeck.harness import claude
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.types import ControlAnswer, ControlInterruption
from quarterdeck.ship import ShipError
from quarterdeck.tmux import ControlFrame, Pane

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'claude' / '2.1.317'
STYLED = (FIXTURES / 'switch-model-question-120x40.ansi').read_text()
PLAIN = (FIXTURES / 'switch-model-question-120x40.txt').read_text()
AFTER = (FIXTURES / 'after-model-switch-120x40.txt').read_text()
TARGET, DISPLAY = 'claude-haiku-5-5', 'Haiku 5.5'
NOW = datetime.now(timezone.utc)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# -- a 2.1.317 seat with a fresh model catalog, and its control frame ---------------------------------------------


def stand_in(tmp_path, *, version='2.1.317', width=120, height=40, styled=STYLED, cursor=False, catalog=True,
             settings=None, setting_sources='user'):
    home = tmp_path / 'home'
    (home / 'cache' / 'model-catalog').mkdir(parents=True, exist_ok=True)
    (home / 'settings.json').write_text(json.dumps(settings if settings is not None else {}))
    if catalog:
        now_ms = int(NOW.timestamp() * 1000)
        (home / 'cache' / 'model-catalog' / 'synthetic.json').write_text(json.dumps({
            'version': 2, 'fetchedAt': now_ms - 1000, 'staleAt': now_ms + 3_600_000,
            'catalog': {'config': {'models': [{'id': TARGET, 'name': DISPLAY},
                                              {'id': 'claude-opus-5-5', 'name': 'Opus 5.5'}]}}}))
    argv = ['claude', '--setting-sources', setting_sources]
    launched = SimpleNamespace(version=version, argv=argv, at=(NOW - timedelta(minutes=5)).isoformat(),
                               when=NOW - timedelta(minutes=5), pane='%7', pid=4242)
    pane = Pane('%7', 'Synthetic', 'worker', 4242, width, height, False, False, None)
    frame = ControlFrame(pane, styled, cursor)
    return SimpleNamespace(home=home, launched=lambda: launched, read_control_frame=lambda: frame)


def question_screen(text=PLAIN):
    screen = ClaudeHarness().screen(text)
    assert isinstance(screen.interruption, ControlInterruption) and screen.interruption.control == 'model'
    return screen


def answer_for(seat, *, value=TARGET, plain=PLAIN, version='2.1.317'):
    adapter = ClaudeHarness()
    control = adapter.control('model')
    screen = adapter.screen(plain)
    context = adapter.control_context(seat, control, value, screen, version=version, question=digest(screen.text))
    return context, adapter.control_answer(control, value, screen, version=version, context=context)


# -- gate 1: only the measured 2.1.317 120x40 form is admitted --------------------------------------------------


def test_the_measured_frame_is_admitted_with_enter_only_and_never_a_digit(tmp_path):
    question_screen()
    context, answer = answer_for(stand_in(tmp_path))
    assert context is not None and context.display == DISPLAY and context.cursor_visible is False
    assert answer == ControlAnswer('model', TARGET, f'Yes, switch to {DISPLAY}', ('Enter',))
    assert not any(key.isdigit() for key in answer.keys), 'the digit accelerator was never measured'


def swap_rows(text, first, second):
    rows = text.split('\n')
    rows[first], rows[second] = rows[second], rows[first]
    return '\n'.join(rows)


def styled_row(text, index, old, new):
    rows = text.split('\n')
    assert old in rows[index], (index, old)
    rows[index] = rows[index].replace(old, new, 1)
    return '\n'.join(rows)


VARIANTS = {
    'newer installed version': dict(version='2.1.318'),
    'older version': dict(version='2.1.316'),
    'unknown version': dict(version=None),
    'wider pane': dict(width=121),
    'shorter pane': dict(height=39),
    'cursor visible': dict(cursor=True),
    'no fresh catalog': dict(catalog=False),
    'a model picker table in settings': dict(settings={'modelPicker': {}}),
    'other setting sources': dict(setting_sources='user,project'),
    'yes no longer highlighted': dict(styled=styled_row(STYLED, 37, '\x1b[38;5;153m', '\x1b[38;5;246m')),
    'no option highlighted': dict(styled=styled_row(STYLED, 38, '2.', '2.\x1b[38;5;153m')),
    'an unknown terminal escape': dict(styled=STYLED.replace('\x1b[0m', '\x1b[0K', 1)),
}


@pytest.mark.parametrize('name', sorted(VARIANTS))
def test_any_other_version_geometry_style_or_catalog_holds_as_unknown(tmp_path, name):
    kwargs = dict(VARIANTS[name])
    version = kwargs.pop('version', '2.1.317')
    context, answer = answer_for(stand_in(tmp_path, version=version, **kwargs), version=version)
    assert context is None and answer is None, name


PLAIN_VARIANTS = {
    'cursor on no': (lambda t: t.replace('❯ 1. Yes', '  1. Yes').replace('  2. No, go back', '❯ 2. No, go back')),
    'a third option': (lambda t: t.replace('     2. No, go back\n', '     2. No, go back\n     3. Ask later\n')),
    'another model named': (lambda t: t.replace('Yes, switch to Haiku 5.5', 'Yes, switch to Opus 5.5')),
    'the body line replaced': (lambda t: t.replace('Your next response will be slower and use more tokens',
                                                   'Your next response is synthetic')),
    'a composer shown': (lambda t: swap_rows(t, 36, 39).replace('\n\n', '\n❯ \n', 1)),
}


@pytest.mark.parametrize('name', sorted(PLAIN_VARIANTS))
def test_a_frame_that_differs_from_the_measured_question_holds_with_no_action(tmp_path, name):
    plain = PLAIN_VARIANTS[name](PLAIN)
    assert plain != PLAIN
    # The physical frame no longer agrees with the plain one, or the plain one is not the measured form.
    context, answer = answer_for(stand_in(tmp_path), plain=plain)
    assert answer is None, name


def test_the_answer_follows_the_value_never_the_cursor(tmp_path):
    seat = stand_in(tmp_path)
    context, answer = answer_for(seat, value='claude-opus-5-5')
    assert context is None and answer is None, 'the frame asks for Haiku: a request for Opus answers nothing'
    context, answer = answer_for(seat, value='haiku')
    assert answer is None, 'no alias is resolved'


def test_physical_and_plain_frames_must_agree(tmp_path):
    other = PLAIN.replace('Switching to Haiku 5.5', 'Switching to Haiku 5.5 ')
    seat = stand_in(tmp_path)
    adapter = ClaudeHarness()
    screen = adapter.screen(other)
    context = adapter.control_context(seat, adapter.control('model'), TARGET, screen, version='2.1.317',
                                      question=digest(screen.text))
    assert context is None
    screen = adapter.screen(PLAIN)
    assert adapter.control_context(seat, adapter.control('model'), TARGET, screen, version='2.1.317',
                                   question=digest('another question')) is None, 'the question digest binds the frame'


# -- gate 3: the record result is the exact triple after an unchanged prefix -------------------------------------


def record_with_prefix(tmp_path):
    record = tmp_path / 'record.jsonl'
    rows = [{'type': 'user', 'uuid': 'u-0', 'parentUuid': None, 'promptId': 'p-0', 'isSidechain': False,
             'message': {'role': 'user', 'content': 'Synthetic earlier input.'}},
            {'type': 'assistant', 'uuid': 'a-0', 'parentUuid': 'u-0', 'isSidechain': False,
             'message': {'role': 'assistant', 'model': 'claude-sonnet-5-5', 'content': [{'type': 'text', 'text': 'ok'}]}}]
    record.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return record


def triple(arguments=TARGET, *, display=DISPLAY, name='/model', prompt='p-1', ids=('c-1', 'm-1', 's-1'),
           links=True, meta=True, stdout=True):
    caveat = {'type': 'user', 'uuid': ids[0], 'parentUuid': 'a-0', 'promptId': prompt, 'isMeta': meta,
              'isSidechain': False, 'message': {'role': 'user', 'content': '<local-command-caveat>Synthetic caveat.'
                                                                              '</local-command-caveat>'}}
    command = {'type': 'user', 'uuid': ids[1], 'parentUuid': ids[0] if links else 'elsewhere', 'promptId': prompt,
               'isSidechain': False, 'message': {'role': 'user', 'content':
                   f'<command-name>{name}</command-name>\n<command-message>model</command-message>\n'
                   f'<command-args>{arguments}</command-args>'}}
    result = {'type': 'user', 'uuid': ids[2], 'parentUuid': ids[1], 'promptId': prompt, 'isSidechain': False,
              'message': {'role': 'user', 'content':
                  f'<local-command-stdout>Set model to {display} and saved as your default for new sessions'
                  f'</local-command-stdout>'}}
    return [caveat, command, result] if stdout else [caveat, command]


def append(record, rows):
    with record.open('a') as stream:
        stream.write(''.join(json.dumps(row) + '\n' for row in rows))


@pytest.fixture
def bound(tmp_path):
    seat = stand_in(tmp_path)
    record = record_with_prefix(tmp_path)
    adapter = ClaudeHarness()
    origin = adapter.model_control_origin(seat, record, TARGET)
    assert origin is not None and origin['display']['display'] == DISPLAY
    return SimpleNamespace(seat=seat, record=record, adapter=adapter, origin=origin)


def test_the_exact_triple_after_the_unchanged_prefix_confirms(bound):
    append(bound.record, triple())
    assert bound.adapter.model_control_result(bound.seat, bound.record, TARGET, bound.origin) == TARGET


RESULT_VARIANTS = {
    'an alias typed': dict(arguments='haiku'),
    'another model typed': dict(arguments='claude-opus-5-5'),
    'the target with a trailing space': dict(arguments=TARGET + ' '),
    'another command name': dict(name='/models'),
    'another display confirmed': dict(display='Opus 5.5'),
    'no result row': dict(stdout=False),
    'a broken parent link': dict(links=False),
    'no caveat meta flag': dict(meta=False),
    'a repeated row identity': dict(ids=('c-1', 'c-1', 's-1')),
}


@pytest.mark.parametrize('name', sorted(RESULT_VARIANTS))
def test_anything_short_of_the_exact_triple_confirms_nothing(bound, name):
    append(bound.record, triple(**RESULT_VARIANTS[name]))
    assert bound.adapter.model_control_result(bound.seat, bound.record, TARGET, bound.origin) is None, name


def test_two_model_commands_after_the_prefix_are_ambiguous(bound):
    append(bound.record, triple())
    append(bound.record, triple(prompt='p-2', ids=('c-2', 'm-2', 's-2')))
    assert bound.adapter.model_control_result(bound.seat, bound.record, TARGET, bound.origin) is None


def test_a_changed_prefix_a_partial_row_or_a_replaced_record_confirms_nothing(bound):
    append(bound.record, triple())
    text = bound.record.read_text()
    bound.record.write_text(text.replace('Synthetic earlier input.', 'Synthetic earlier input!'))
    assert bound.adapter.model_control_result(bound.seat, bound.record, TARGET, bound.origin) is None, 'prefix'
    bound.record.write_text(text + '{"type": "user"')
    assert bound.adapter.model_control_result(bound.seat, bound.record, TARGET, bound.origin) is None, 'partial row'
    replacement = bound.record.with_name('replacement.jsonl')
    replacement.write_text(text)  # written while the original still holds its inode, then renamed over it
    replacement.replace(bound.record)
    assert bound.record.stat().st_ino != bound.origin['inode']
    assert bound.adapter.model_control_result(bound.seat, bound.record, TARGET, bound.origin) is None, 'new inode'


def test_a_result_already_in_the_prefix_never_confirms_a_new_request(tmp_path):
    seat = stand_in(tmp_path)
    record = record_with_prefix(tmp_path)
    append(record, triple())
    adapter = ClaudeHarness()
    origin = adapter.model_control_origin(seat, record, TARGET)
    assert adapter.model_control_result(seat, record, TARGET, origin) is None, 'only rows after the request count'


# -- gate 2: request currentness, through the one owner ------------------------------------------------------------


def begun(now=NOW):
    return confirmation.Continuation.begin('model', TARGET, {'seat': 'worker'}, now)


def test_the_reservation_and_the_thirty_second_deadline_persist_and_are_checked():
    state = begun()
    again = confirmation.Continuation.from_metadata(json.loads(json.dumps(state.metadata())))
    assert again == state and datetime.fromisoformat(again.deadline) - datetime.fromisoformat(again.started) == \
        timedelta(seconds=30)
    stretched = dict(state.metadata(), deadline=(NOW + timedelta(seconds=31)).isoformat())
    with pytest.raises(ShipError, match='retained without retyping'):
        confirmation.Continuation.from_metadata(stretched)
    for phase, asked, answered in (('reserved', True, False), ('answered', True, False), ('typed', False, False)):
        with pytest.raises(ShipError):
            confirmation.Continuation.from_metadata(dict(state.metadata(), phase=phase, asked=asked, answered=answered))


def test_past_the_deadline_it_is_unconfirmed_and_never_retyped_or_answered():
    state = confirmation.submitted(begun())
    answer = ControlAnswer('model', TARGET, f'Yes, switch to {DISPLAY}', ('Enter',))
    late = confirmation.advance(state, NOW + timedelta(seconds=30), confirmed=None, answer=answer)
    assert late.action == 'unconfirmed' and 'not retyped' in late.reason
    early = confirmation.advance(state, NOW - timedelta(seconds=1), confirmed=True, answer=answer)
    assert early.action == 'unconfirmed', 'a clock before the start is unknown, not confirmation'


def test_only_a_submitted_request_may_answer_and_only_its_own_value():
    answer = ControlAnswer('model', TARGET, f'Yes, switch to {DISPLAY}', ('Enter',))
    reserved = begun()
    assert confirmation.advance(reserved, NOW + timedelta(seconds=1), confirmed=None, answer=answer).action == 'wait'
    submitted = confirmation.submitted(reserved)
    foreign = replace(answer, value='claude-opus-5-5')
    assert confirmation.advance(submitted, NOW + timedelta(seconds=1), confirmed=None, answer=foreign).action == 'wait'
    go = confirmation.advance(submitted, NOW + timedelta(seconds=1), confirmed=None, answer=answer)
    assert go.action == 'answer' and go.state.phase == 'answering'
    once = confirmation.answered(go.state, typed=True)
    assert confirmation.advance(once, NOW + timedelta(seconds=2), confirmed=None, answer=answer).action == 'wait', \
        'an answered request never answers again'
    changed = replace(submitted, question='0' * 64, question_changed=True)
    assert confirmation.advance(changed, NOW + timedelta(seconds=1), confirmed=None, answer=answer).action == 'wait'


# -- gates 2, 4 and 5 at the door: a Claude seat on a synthetic pane, the real service, door and owner ------------


@pytest.fixture
def ship_seat(tmp_path, monkeypatch):
    from quarterdeck import post
    from quarterdeck.door import Door
    from quarterdeck.service import Service
    from quarterdeck.ship import Pool, SeatSpec, Session, Ship

    def build(human=False):
        root = tmp_path / ('human' if human else 'crew')
        root.mkdir()
        pool = Pool('synthetic', 'anthropic')
        spec = SeatSpec('worker', 'bridge', 'worker', 'claude', ('synthetic',), cwd=root)
        ship = Ship(root, 'example', 'operator', root, {'synthetic': pool},
                    {'bridge': Session('bridge', 'Synthetic', human=human)}, {'worker': spec})
        live = dict(frame=PLAIN, styled=STYLED, cursor=False, pasted=[], keys=[],
                    pane=Pane('%7', 'Synthetic', 'worker', 4242, 120, 40, False, False, None))

        def keys(pane, *pressed):
            live['keys'].append(pressed)

        def paste(pane, text, *rest):
            live['pasted'].append(text)

        tmux = SimpleNamespace(pane=lambda ident: live['pane'], capture=lambda *a, **k: live['frame'],
                               keys=keys, paste=paste,
                               read_control_frame=lambda ident: ControlFrame(live['pane'], live['styled'],
                                                                             live['cursor']))
        svc = Service(ship, tmux)
        seat = svc.seat('worker')
        seat.folder.mkdir(parents=True)
        at = (post.now() - timedelta(minutes=5)).isoformat()
        (seat.folder / 'launch.json').write_text(json.dumps(dict(
            pane='%7', pid=4242, at=at, pool='synthetic', resume=None, harness='claude', version='2.1.317',
            argv=['claude', '--setting-sources', 'user'])))
        home = stand_in(root).home
        seat.home.mkdir(parents=True, exist_ok=True)
        for path in home.rglob('*'):
            if path.is_file():
                target = seat.home / path.relative_to(home)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        (seat.home / '.claude.json').write_text(json.dumps({'hasSeenAutoDefaultNudge': True}))  # past startup
        record = seat.home / 'projects' / 'synthetic' / 'synthetic-session.jsonl'
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({'type': 'user', 'uuid': 'u-0', 'parentUuid': None, 'isSidechain': False,
                                      'timestamp': at, 'sessionId': 'synthetic-session', 'version': '2.1.317',
                                      'message': {'role': 'user', 'content': 'Synthetic earlier input.'}}) + '\n')
        (seat.folder / 'record').write_text(str(record) + '\n')
        from quarterdeck.record_owner import register
        register(seat.folder, record, 'claude')
        original = Door.confirm
        monkeypatch.setattr(Door, 'confirm', lambda self, text: original(self, text, seconds=0))
        return SimpleNamespace(svc=svc, seat=seat, live=live, record=record, door=Door(seat))

    made = []
    yield lambda human=False: made.append(build(human)) or made[-1]
    for world in made:
        world.svc.ledger.close()


def proved_answer(world):
    adapter, control = world.seat.harness, world.seat.harness.control('model')
    screen = adapter.screen(world.live['frame'])
    question = digest(screen.text)
    context = adapter.control_context(world.seat, control, TARGET, screen, version='2.1.317', question=question)
    return control, adapter.control_answer(control, TARGET, screen, version='2.1.317', context=context), question


def test_the_owned_request_answers_its_proved_question_once_with_enter_alone(ship_seat):
    world = ship_seat()
    control, answer, question = proved_answer(world)
    assert answer is not None and answer.keys == ('Enter',)
    delivery = world.door.answer_control(control, TARGET, answer, lambda: True, question=question)
    assert delivery.result == 'typed' and world.live['keys'] == [('Enter',)] and world.live['pasted'] == []


@pytest.mark.parametrize('how', ['stale question', 'request no longer current', 'currentness unknown',
                                 'another pane', 'question gone', 'unproved answer', 'foreign value'])
def test_a_stale_or_foreign_request_never_answers_and_nothing_is_replayed(ship_seat, how):
    world = ship_seat()
    control, answer, question = proved_answer(world)
    verify = lambda: True
    if how == 'stale question':
        question = digest('an earlier question frame')
    elif how == 'request no longer current':
        verify = lambda: False
    elif how == 'currentness unknown':
        verify = lambda: None
    elif how == 'another pane':
        world.live['pane'] = replace(world.live['pane'], pid=9999)
    elif how == 'question gone':
        world.live['frame'] = AFTER
    elif how == 'unproved answer':
        answer = replace(answer, keys=('1',))
    elif how == 'foreign value':
        answer = replace(answer, value='claude-opus-5-5')
    delivery = world.door.answer_control(control, TARGET, answer, verify, question=question)
    assert delivery.result == 'withheld', how
    assert world.live['keys'] == [] and world.live['pasted'] == [], 'no key, no command, no replay'


def test_ordinary_delivery_holds_at_the_question_and_delivers_once_after_it(ship_seat):
    world = ship_seat()
    message = world.svc.store.new('order', 'desk', 'worker', 'Synthetic order waiting behind the question.')
    world.svc.deliver(world.seat, [message])
    assert world.live['pasted'] == [] and world.live['keys'] == [], 'the question is never answered or dismissed'
    hold = world.door.reading(world.seat.harness.screen(PLAIN))
    assert not hold.open and 'switch-model' in hold.reason
    world.live['frame'] = AFTER  # the question is gone: the composer is back and empty
    world.svc.deliver(world.seat, [message])
    world.svc.deliver(world.seat, [message])
    assert [text for text in world.live['pasted'] if message.body in text] and \
        sum(message.body in text for text in world.live['pasted']) == 1, 'delivered once, afterwards'


def test_the_control_never_types_into_a_box_holding_a_ship_message(ship_seat):
    world = ship_seat()
    control, answer, question = proved_answer(world)
    world.live['frame'] = AFTER.replace('❯ ', '❯ qd > order from desk · synthetic', 1)
    delivery = world.door.answer_control(control, TARGET, answer, lambda: True, question=question)
    assert delivery.result == 'withheld' and world.live['keys'] == [] and world.live['pasted'] == []


def test_on_a_seat_the_admiral_owns_nothing_is_typed(ship_seat):
    world = ship_seat(human=True)
    control, answer, question = proved_answer(world)
    assert world.door.answer_control(control, TARGET, answer, lambda: True, question=question).result == 'withheld'
    command = world.door.deliver(control.command(TARGET), confirm=False)
    assert command.result == 'withheld', 'the human rules allow only the fixed waking line'
    message = world.svc.store.new('order', 'desk', 'worker', 'Synthetic order for a person.')
    world.svc.deliver(world.seat, [message])
    assert world.live['keys'] == [] and world.live['pasted'] == []


# -- gate 2 at the owner: a matching continuation, before the generic claim hold; a foreign one never ------------


def owned_request(world, *, seat='worker', value=TARGET, phase='submitted', claim=True, origin_seat=None):
    from quarterdeck import post, request_queue
    folder = world.svc.runtime / 'requests'
    folder.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(kind='control', seat=seat, name='model', value=value)) + '\n'
    path = folder / f'synthetic-{len(list(folder.glob("*.json")))}.json'
    path.write_text(payload)
    if claim:
        path.with_suffix('.claimed').write_text(payload)
    spec = world.seat.spec
    origin = dict(seat=origin_seat or seat, launch=request_queue.launch_binding(world.seat),
                  inputs=dict(record=str(world.record), device=1, inode=2, input_count=1, inputs_digest='a' * 64),
                  record=dict(path=str(world.record), device=1, inode=2, offset=10, prefix='b' * 64, display={}),
                  declaration=[world.seat.harness.name, spec.model_mode, spec.model])
    state = confirmation.Continuation.begin('model', value, origin, post.now())
    if phase == 'submitted':
        state = confirmation.submitted(state)
    request_queue.save_metadata(path, {'control_confirmation': state.metadata()})
    return path


def test_a_matching_continuation_resumes_before_the_generic_claim_hold_and_types_nothing(ship_seat, monkeypatch):
    from quarterdeck.service import Service
    world = ship_seat()
    path = owned_request(world)
    resumed = []
    monkeypatch.setattr(Service, 'continue_model_control',
                        lambda self, seat, name, value: resumed.append((seat.name, name, value)) or None)
    world.svc.requests()
    assert resumed == [('worker', 'model', TARGET)], 'the consumed claim resumes its own continuation'
    assert path.with_suffix('.claimed').exists(), 'its claim stays: the command was consumed and is never retyped'
    assert world.live['pasted'] == [] and world.live['keys'] == []


def test_a_claim_with_no_continuation_stays_held_and_nothing_resumes_or_replays(ship_seat, monkeypatch):
    from quarterdeck import request_queue
    from quarterdeck.service import Service
    world = ship_seat()
    path = owned_request(world)
    request_queue.save_metadata(path, {})
    monkeypatch.setattr(Service, 'continue_model_control', lambda *a: pytest.fail('no continuation to resume'))
    world.svc.requests()
    assert path.exists() and path.with_suffix('.claimed').exists()
    assert world.live['pasted'] == [] and world.live['keys'] == []


@pytest.mark.parametrize('how', ['another seat', 'another value', 'claim differs', 'no claim'])
def test_a_foreign_or_altered_continuation_is_unknown_and_never_resumed(ship_seat, monkeypatch, how):
    from quarterdeck import request_queue
    from quarterdeck.service import Service
    world = ship_seat()
    path = owned_request(world, claim=how != 'no claim')
    payload = json.loads(path.read_text())
    if how == 'another seat':
        path.write_text(json.dumps(dict(payload, seat='someone-else')) + '\n')
    elif how == 'another value':
        path.write_text(json.dumps(dict(payload, value='claude-opus-5-5')) + '\n')
    elif how == 'claim differs':
        path.with_suffix('.claimed').write_text(json.dumps(dict(payload, value='claude-opus-5-5')) + '\n')
    with pytest.raises(ShipError, match='retained without retyping'):
        request_queue.control_continuation(path)
    monkeypatch.setattr(Service, 'continue_model_control', lambda *a: pytest.fail('a foreign request resumed'))
    world.svc.requests()
    assert world.live['pasted'] == [] and world.live['keys'] == []


def test_only_this_seats_own_current_continuation_suppresses_its_model_difference(ship_seat):
    from quarterdeck import request_queue
    world = ship_seat()
    owned_request(world, origin_seat='someone-else')
    assert request_queue.pending_model(world.seat, TARGET) is False, 'another seat\'s request suppresses nothing'
    owned_request(world)
    assert request_queue.pending_model(world.seat, TARGET) is True
    assert request_queue.pending_model(world.seat, 'claude-opus-5-5') is False, 'only for the value it sets'


# -- each check alone: no gate above relies only on a later layer ---------------------------------------------------


def test_a_reading_claiming_another_version_holds_even_on_a_317_launch(tmp_path):
    seat = stand_in(tmp_path)  # the launch says 2.1.317; the reading names a newer installed form
    adapter, screen = ClaudeHarness(), ClaudeHarness().screen(PLAIN)
    for version in ('2.1.318', '2.1.316', None):
        assert adapter.control_context(seat, adapter.control('model'), TARGET, screen, version=version,
                                       question=digest(screen.text)) is None, version
    context, _ = answer_for(seat)
    assert adapter.control_answer(adapter.control('model'), TARGET, screen, version='2.1.318', context=context) is None


def test_the_answer_itself_requires_the_selection_on_yes(tmp_path):
    adapter = ClaudeHarness()
    context, answer = answer_for(stand_in(tmp_path))
    assert answer is not None
    moved = PLAIN.replace('   ❯ 1. Yes', '     1. Yes').replace('     2. No, go back', '   ❯ 2. No, go back')
    screen = adapter.screen(moved)
    assert adapter.control_answer(adapter.control('model'), TARGET, screen, version='2.1.317', context=context) is None


def test_a_request_of_another_seat_never_continues_here_even_with_its_claim_intact(ship_seat):
    from quarterdeck import request_queue
    world = ship_seat()
    path = owned_request(world, seat='someone-else', origin_seat='worker')
    with pytest.raises(ShipError, match='retained without retyping'):
        request_queue.control_continuation(path)


def test_a_wellformed_request_of_another_seat_on_the_same_launch_suppresses_nothing(ship_seat):
    from quarterdeck import request_queue
    world = ship_seat()
    path = owned_request(world, seat='someone-else')  # payload and origin agree; only the seat differs
    assert request_queue.control_continuation(path) is not None
    assert request_queue.pending_model(world.seat, TARGET) is False
