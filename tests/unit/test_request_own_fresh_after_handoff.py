"""A human seat's own fresh start, asked once it has posted an open handoff to itself.

Every message the watch delivers gives the seat a later turn, so a request bound to its asking turn never found
that turn again. Bound to the handoff instead, it applies at the seat's next quiet moment whatever deliveries
and program notices come between: each is an open message the fresh start carries. It never applies over a turn
in progress, a queued input, an unconfirmed delivery or text in the box, and the person's own later input retires
it. Without an open handoff, or where the person's input cannot be told from the program's notices, the asking
turn's rule decides as before. The service's own request loop runs unchanged; the seat, its readings, its screen
and the clock are stood in, as in test_request_human_claude_validation. Names are synthetic.
"""

from __future__ import annotations

import os

import pytest

from quarterdeck import post, request_queue, service as servicemod
from quarterdeck.harness.types import Screen
from tests.unit.test_request_human_claude_validation import SEAT, Desk, state, turn

PEER = 'engineer-larch'


@pytest.fixture
def desk(tmp_path, monkeypatch):
    desk = Desk(tmp_path, monkeypatch)
    desk.person = [desk.at(-600)]  # the person last typed ten minutes before the request
    desk.screen = Screen(text='', box='', working=False)
    desk.seat.record = lambda: tmp_path / 'record.jsonl'

    def person_inputs(record, excluded):
        if isinstance(desk.person, Exception):
            raise desk.person
        return list(desk.person)
    desk.seat.harness.person_inputs = person_inputs
    desk.seat.harness.name = 'claude'

    class Door:
        def __init__(self, seat):
            assert seat is desk.seat

        def screen(self):
            return desk.screen
    monkeypatch.setattr(servicemod, 'Door', Door)
    return desk


def message(desk, seconds, kind, sender, to, body, **fields):
    desk.now = desk.at(seconds)
    store = post.Store(desk.runtime)
    found = store.prepare(kind, sender, to, body, **fields)
    store.write(found)
    return found


def handoff(desk, seconds=-30):
    return message(desk, seconds, 'order', SEAT, SEAT, 'synthetic handoff: what this seat has in hand')


def deliver(desk, seconds):
    """A peer's order reaches the seat through its watch: an open message, confirmed, and a later turn."""
    found = message(desk, seconds, 'order', PEER, SEAT, f'synthetic order {seconds}')
    attempts = post.Attempts(desk.runtime)
    attempts.close(attempts.open(found, SEAT, None, None, None), 'watch', 'printed by the watch')
    return found


def waiting(desk):
    [row] = request_queue.pending(desk.runtime)
    return row['dimension'], row['reason']


def test_a_fresh_request_after_its_handoff_applies_when_quiet_and_carries_two_later_deliveries(desk):
    kept = handoff(desk)
    desk.state = state(open_=True, input_at=-40)  # the turn in which the seat posted its handoff and asked
    path = desk.ask(seconds=0)
    desk.tick(1)
    assert desk.applied == [] and waiting(desk)[0] == 'turn-open'
    desk.state = state(open_=False, input_at=-40, turns=[turn(-40, 2)])
    first, second = deliver(desk, 3), deliver(desk, 5)
    # Each delivery is a program notice in the seat's record: a later input, but not the person's.
    desk.state = state(open_=True, input_at=6, input_id='input-larch-notice', turns=[turn(-40, 2)])
    desk.tick(7)
    assert desk.applied == [] and path.exists(), 'later deliveries never retire it; it waits for the turn'
    assert waiting(desk)[0] == 'turn-open'
    desk.state = state(open_=False, input_at=6, input_id='input-larch-notice', turns=[turn(-40, 2), turn(6, 9, 1)])
    desk.tick(10)
    assert desk.applied == ['fresh'] and desk.answer(path) == f'{SEAT} fresh (synthetic proof)'
    assert not path.exists() and request_queue.pending(desk.runtime) == []
    carried = {m.id for m in post.Store(desk.runtime).open_for(SEAT)}
    assert {kept.id, first.id, second.id} <= carried, 'both deliveries are open messages the fresh start carries'
    desk.tick(11)
    assert desk.applied == ['fresh'], 'exactly once'


def test_a_fresh_request_after_its_handoff_never_applies_mid_turn(desk):
    handoff(desk)
    desk.state = state(open_=True, input_at=-40)
    desk.ask(seconds=0)
    for second in range(1, 6):
        deliver(desk, second)
        desk.state = state(open_=True, input_at=second, input_id=f'input-larch-{second}')
        desk.tick(second + 0.5)
    assert desk.applied == [] and waiting(desk) == (
        'turn-open', 'own fresh start after its handoff waits for the turn in progress')


@pytest.mark.parametrize('obstacle, dimension', [
    (lambda desk: setattr(desk, 'screen', Screen(text='', box='half a line', working=False)), 'box'),
    (lambda desk: setattr(desk, 'screen', None), 'box'),
    (lambda desk: setattr(desk, 'state', state(open_=False, input_at=-40, turns=[turn(-40, 2)])
                          .__class__(**{**desk.state.__dict__, 'queued': ('queued line',)})), 'queued'),
    (lambda desk: setattr(desk, 'state', state(open_=False, input_at=-40, turns=[turn(-40, 2, timed=False)])),
     'turn-end'),
    (lambda desk: post.Attempts(desk.runtime).open(
        message(desk, 3, 'order', PEER, SEAT, 'synthetic unconfirmed'), SEAT, None, None, None), 'delivery'),
], ids=['text-in-box', 'screen-unread', 'queued-input', 'end-not-proved', 'delivery-unconfirmed'])
def test_a_fresh_request_after_its_handoff_waits_while_the_moment_is_not_quiet(desk, obstacle, dimension):
    handoff(desk)
    desk.state = state(open_=False, input_at=-40, turns=[turn(-40, 2)])
    path = desk.ask(seconds=0)
    obstacle(desk)
    desk.tick(4)
    assert desk.applied == [] and path.exists() and waiting(desk)[0] == dimension


def test_the_persons_own_input_after_the_request_retires_it_without_a_fresh_start(desk):
    handoff(desk)
    desk.state = state(open_=True, input_at=-40)
    path = desk.ask(seconds=0)
    desk.tick(1)
    desk.person = [desk.at(-600), desk.at(3)]  # the person typed into the session
    desk.state = state(open_=False, input_at=3, input_id='input-larch-person', turns=[turn(3, 5)])
    desk.tick(6)
    assert desk.applied == [] and "superseded by the person's later input" in desk.answer(path)
    assert not path.exists(), 'retired: it never applies later'


def test_a_handoff_closed_before_the_fresh_start_applies_retires_it(desk):
    kept = handoff(desk)
    desk.state = state(open_=True, input_at=-40)
    path = desk.ask(seconds=0)
    desk.tick(1)
    message(desk, 2, 'report', SEAT, SEAT, 'synthetic: handled in this conversation', re=kept.id)
    desk.state = state(open_=False, input_at=-40, turns=[turn(-40, 3)])
    desk.tick(4)
    assert desk.applied == [] and f'handoff {kept.short} closed' in desk.answer(path)


@pytest.mark.parametrize('setup', ['no-handoff', 'handoff-after-request', 'person-unknown', 'restart'])
def test_otherwise_the_asking_turn_rule_decides_and_a_later_input_retires_it(desk, setup):
    if setup == 'handoff-after-request':
        handoff(desk, seconds=0.5)
    elif setup != 'no-handoff':
        handoff(desk)
    if setup == 'person-unknown':
        desk.person = ValueError('a queued input is malformed')
    desk.state = state(open_=True, input_at=-40)
    path = desk.ask(seconds=0, resume=setup == 'restart')
    desk.tick(1)
    deliver(desk, 2)
    desk.state = state(open_=True, input_at=3, input_id='input-larch-notice')
    desk.tick(4)
    if setup == 'person-unknown':
        # The person's input cannot be told from the program's notices: the conservative rule retires it.
        assert desk.applied == [] and 'superseded by later input' in desk.answer(path)
    else:
        # A notice is not the person's typing: it retires nothing, and the open turn still holds the restart.
        assert desk.applied == []
        answer = desk.answer(path)
        assert answer is None or 'superseded' not in answer


def test_another_seats_fresh_request_is_never_deferred_by_the_seats_handoff(desk):
    handoff(desk)
    desk.state = state(open_=True, input_at=-40)
    path = desk.ask(caller=PEER, seconds=0)
    desk.tick(1)
    assert desk.applied == [] and desk.answer(path).startswith('refused:') and not path.exists()



# The desk's exact shape, on a synthetic Claude Code record (measured 2.1.317 rows, synthetic identities) read by the
# real reader and idle proof. The seat starts its inbox watch in the background, posts its handoff and asks in its
# own turn, and that turn ends. Then the watch ends: the program writes its notice and gives it a turn of its own.
# Only the screen, the clock and the fresh act are stood in.

WATCH, ASK = 'call-watch-larch', 'call-ask-larch'


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    import json
    from datetime import timedelta
    from types import SimpleNamespace

    from quarterdeck import person_quiet
    from quarterdeck.harness.base import Join
    from quarterdeck.seat import Launched, Seat, process_start
    from quarterdeck.ship import Pool, SeatSpec, Session, Ship
    from tests.unit import test_claude_background_ordinary as shapes

    spec = SeatSpec('desk', 'office', 'desk', 'claude', ('synthetic',))
    ship = Ship(tmp_path / 'ship', 'synthetic', 'operator', tmp_path, {'synthetic': Pool('synthetic', 'claude')},
                {'office': Session('office', 'Synthetic office', True)}, {'desk': spec})
    # The test process stands in for the desk's pane process, by its own pid and start: its own requests are proved.
    pane = os.getpid()
    seat = Seat(ship, spec, SimpleNamespace(pane=lambda _: SimpleNamespace(id='%1', pid=pane, dead=False)))
    seat.folder.mkdir(parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    Launched('%1', pane, (shapes.AT - timedelta(hours=1)).isoformat(), 'synthetic', None, ['claude'], 'synthetic',
             harness='claude', start=process_start(pane)).write(seat.trusted / 'launch.json')
    record = seat.home / 'record-larch.jsonl'
    record.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(seat, 'record', lambda: record)
    monkeypatch.setattr(seat, 'join', lambda: Join(record))
    monkeypatch.setattr(person_quiet, 'waking_lines', lambda: ('qd > ship: synthetic waking line.',))
    clock = SimpleNamespace(now=shapes.AT)
    monkeypatch.setattr(post, 'now', lambda: clock.now)
    applied = []
    monkeypatch.setattr(seat, 'fresh', lambda: applied.append('fresh') or 'desk fresh (synthetic proof)')
    screen = SimpleNamespace(value=Screen(text='', box='', working=False))

    class Door:
        def __init__(self, given):
            assert given is seat

        def screen(self):
            return screen.value
    monkeypatch.setattr(servicemod, 'Door', Door)
    service = servicemod.Service.__new__(servicemod.Service)
    service.runtime = ship.runtime
    service.log = lambda *args, **kwargs: None
    service.seat = lambda name: seat
    rows = []

    def write(*added):
        rows.extend(added)
        record.write_text(''.join(json.dumps(entry) + '\n' for entry in rows))

    def at(second):
        clock.now = shapes.AT + timedelta(seconds=second)

    def ask(second):
        at(second)
        handed = post.Store(ship.runtime)
        handed.write(handed.prepare('order', 'desk', 'desk', 'synthetic handoff'))
        return request_queue.enqueue(ship.runtime, 'relaunch', dict(
            seat='desk', resume=False, caller='desk', origin=request_queue.launch_binding(seat),
            requested_at=clock.now.isoformat()), name='r-desk-shape')

    def tick(second):
        at(second)
        service.requests()

    def asking_turn():
        """The person's input; the seat starts its watch, posts its handoff, asks, and its turn ends."""
        write(shapes.prompt(0), shapes.call(1, WATCH), shapes.result(2, WATCH, task='task-watch-larch'),
              shapes.call(3, ASK, arguments=dict(command='synthetic fresh request')))
        path = ask(3.5)
        write(shapes.result(4, ASK, value=dict(stdout='', stderr='', interrupted=False, isImage=False,
                                               noOutputExpected=False)), shapes.ended(5))
        return path

    def notified(second=6, origin=None):
        """The watch's end as the desk's record wrote it: the queued notice, then a user row whose origin also
        names its producer and run, which the turn reader counts as an input and the person reading does not."""
        write(shapes.notice('enqueue', second, identity=WATCH, task='task-watch-larch'),
              shapes.row('user', second + 0.1, message=dict(role='user', content=shapes.text(
                  identity=WATCH, task='task-watch-larch')), origin=origin or dict(
                  kind='task-notification', producer='session-task', runId='synthetic-run-larch')))

    def replied(second=7):
        write(shapes.row('assistant', second, message=dict(role='assistant', content=[
            dict(type='text', text='synthetic reply')])), shapes.ended(second + 1))

    def watch_ends(second=6, origin=None):
        notified(second, origin)
        replied(second + 1)

    return SimpleNamespace(seat=seat, applied=applied, screen=screen, write=write, tick=tick, shapes=shapes,
                           asking_turn=asking_turn, watch_ends=watch_ends, notified=notified, replied=replied,
                           answer=lambda path: path.with_suffix('.done').read_text().strip()
                           if path.with_suffix('.done').exists() else None)


def test_the_desks_shape_a_request_then_its_watchs_notice_then_quiet_applies_the_fresh_start(recorded):
    path = recorded.asking_turn()
    recorded.tick(5.5)
    assert recorded.applied == [] and path.exists(), 'its watch still runs: the seat is not yet quiet'
    recorded.watch_ends(6)
    recorded.tick(9)
    assert recorded.applied == ['fresh'] and recorded.answer(path) == 'desk fresh (synthetic proof)'


def test_the_desks_shape_waits_while_the_notices_turn_runs_and_while_the_box_holds_text(recorded):
    path = recorded.asking_turn()
    recorded.notified(6)
    recorded.tick(6.5)
    assert recorded.applied == [] and path.exists(), 'the turn the notice gave is still running'
    recorded.replied(7)
    recorded.screen.value = Screen(text='', box='the person is typing', working=False)
    recorded.tick(9)
    assert recorded.applied == [] and path.exists(), 'text in the box holds it'
    recorded.screen.value = Screen(text='', box='', working=False)
    recorded.tick(10)
    assert recorded.applied == ['fresh']


def test_the_desks_shape_the_persons_own_input_after_the_request_retires_it(recorded):
    path = recorded.asking_turn()
    recorded.watch_ends(6)
    recorded.write(recorded.shapes.prompt(9, 'synthetic person input after the request'), recorded.shapes.ended(10))
    recorded.tick(11)
    assert recorded.applied == [] and "superseded by the person's later input" in recorded.answer(path)


def test_a_notice_the_turn_reader_sets_aside_gives_a_turn_without_input_and_still_ends_quiet(recorded):
    path = recorded.asking_turn()
    recorded.watch_ends(6, origin=dict(kind='task-notification'))  # the measured exact origin: no input recorded
    recorded.tick(9)
    assert recorded.applied == ['fresh'] and recorded.answer(path) == 'desk fresh (synthetic proof)'
