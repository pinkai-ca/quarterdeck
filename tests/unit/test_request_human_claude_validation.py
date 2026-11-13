"""Independent gates for queued relaunch requests on human seats (ord-34ca, ord-bef5).

A human seat's own request applies only at the end of its own asking turn, and never after the person speaks
again; a request from anyone else applies only at a break proved at the moment it was asked, and is never held
for later. A changed launch, input, work or conversation holds nothing over: it refuses. Cancellation never
succeeds against a claimed action, and an outcome that is unknown is never guessed. Status shows each wait at
once by kind, since and reason, without reading a human's record.

The service's own request loop and relaunch decision run here unchanged; only the seat, its idle reading and the
clock are stood in, so every proof of the asking turn is set exactly. Names are synthetic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import idle, post, recovery, request_queue, service as servicemod, servicectl
from quarterdeck.harness.types import State, Tokens, Turn
from quarterdeck.ship import ShipError

T0 = datetime(2032, 7, 8, 9, 10, tzinfo=timezone.utc)
SEAT = 'desk-larch'
LAUNCH = SimpleNamespace(pane='%71', pid=707070, at=(T0 - timedelta(hours=1)).isoformat(), harness='claude')


class Desk:
    """A human seat, its launch, its idle reading and the clock, each set by the test."""

    def __init__(self, tmp_path, monkeypatch):
        self.runtime = tmp_path / 'runtime'
        self.now = T0
        self.launch = LAUNCH
        self.state: State | None = None
        self.hold = None
        self.applied: list[str] = []
        self.reads = 0
        self.seat = SimpleNamespace(
            name=SEAT, session=SimpleNamespace(human=True), launched=lambda: self.launch,
            pane=lambda: SimpleNamespace(id=self.launch.pane, pid=self.launch.pid, dead=False),
            harness=SimpleNamespace(waiting_for_human=lambda state: False),
            fresh=lambda: self.applied.append('fresh') or f'{SEAT} fresh (synthetic proof)',
            restart=lambda: self.applied.append('restart') or f'{SEAT} restarted (synthetic proof)')
        monkeypatch.setattr(post, 'now', lambda: self.now)
        monkeypatch.setattr(recovery, 'held', lambda seat: None)

        def reading(seat, *, explicit=False):
            self.reads += 1
            return idle.Reading(self.state, self.hold) if hasattr(idle, 'Reading') else (self.state, self.hold)
        monkeypatch.setattr(servicemod.idle, 'reading', reading)
        self.service = servicemod.Service.__new__(servicemod.Service)
        self.service.runtime = self.runtime
        self.service.log = lambda *args, **kwargs: None
        self.service.seat = lambda name: self.seat if name == SEAT else (_ for _ in ()).throw(ShipError(name))

    def at(self, seconds: float) -> datetime:
        return T0 + timedelta(seconds=seconds)

    def ask(self, caller=SEAT, seconds=0.0, resume=False, name=None):
        """The request as `qd fresh`/`qd restart` files it, stamped with the asking time and launch."""
        self.now = self.at(seconds)
        data = dict(seat=SEAT, resume=resume, caller=caller, origin=dict(
            pane=self.launch.pane, pid=self.launch.pid, at=self.launch.at, harness=self.launch.harness),
            requested_at=self.at(seconds).isoformat())
        return request_queue.enqueue(self.runtime, 'relaunch', data, name=name or f'r{int(seconds * 10):05d}-larch')

    def tick(self, seconds: float):
        self.now = self.at(seconds)
        self.service.requests()

    def answer(self, path) -> str | None:
        done = path.with_suffix('.done')
        return done.read_text().strip() if done.exists() else None


def turn(started, ended, index=0, timed=True):
    return Turn(index, T0 + timedelta(seconds=started), None if ended is None else T0 + timedelta(seconds=ended),
                'synthetic input', 'synthetic reply', None, Tokens(), timed=timed)


def state(*, open_, input_at, turns=(), input_id='input-larch', work_id='work-larch', conversation='conv-larch'):
    return State(turn_open=open_, last_input_at=T0 + timedelta(seconds=input_at), completed_turns=tuple(turns),
                 pending_tools=(), queued=(), input_id=input_id, work_id=work_id, conversation=conversation)


@pytest.fixture
def desk(tmp_path, monkeypatch):
    return Desk(tmp_path, monkeypatch)


# The person's own request: only at the end of its own asking turn.

def test_an_own_request_waits_visibly_for_its_asking_turn_and_applies_once_at_its_end(desk):
    desk.state = state(open_=True, input_at=-5)  # the person asked inside the turn that began at -5
    path = desk.ask(seconds=0)
    desk.tick(1)
    assert desk.applied == [] and path.exists() and desk.answer(path) is None
    [row] = request_queue.pending(desk.runtime)
    assert (row['kind'], row['reason'], row['dimension']) == ('fresh', 'own asking turn has not ended', 'turn-open')
    desk.tick(2)
    assert desk.applied == [], 'still the same open turn'
    desk.state = state(open_=False, input_at=-5, turns=[turn(-5, 3)])
    desk.tick(4)
    assert desk.applied == ['fresh'] and desk.answer(path) == f'{SEAT} fresh (synthetic proof)'
    assert not path.exists() and request_queue.pending(desk.runtime) == []
    desk.tick(5)
    assert desk.applied == ['fresh'], 'exactly once'


def test_an_own_request_is_retired_when_the_person_speaks_again_before_it_applies(desk):
    desk.state = state(open_=True, input_at=-5)
    path = desk.ask(seconds=0)
    desk.tick(1)
    desk.state = state(open_=True, input_at=6, input_id='input-larch-later')  # the person typed again
    desk.tick(7)
    assert desk.applied == [] and 'superseded by later input' in desk.answer(path)
    assert not path.exists(), 'retired: it can never apply at a later turn'


@pytest.mark.parametrize('changed', [dict(input_id='input-other'), dict(work_id='work-other'),
                                     dict(conversation='conv-other')])
def test_an_own_request_bound_to_its_asking_turn_refuses_when_that_turn_changes(desk, changed):
    desk.state = state(open_=True, input_at=-5)
    path = desk.ask(seconds=0)
    desk.tick(1)
    assert request_queue.metadata(path)['asking_turn']['input_at'] == desk.at(-5).isoformat()
    desk.state = state(open_=False, input_at=-5, turns=[turn(-5, 3)], **changed)
    desk.tick(4)
    assert desk.applied == [] and 'asking turn changed' in desk.answer(path)


def test_a_request_whose_launch_changed_is_refused_never_applied(desk):
    desk.state = state(open_=True, input_at=-5)
    path = desk.ask(seconds=0)
    desk.tick(1)
    desk.launch = SimpleNamespace(pane='%72', pid=717171, at=T0.isoformat(), harness='claude')
    desk.state = state(open_=False, input_at=-5, turns=[turn(-5, 3)])
    desk.tick(4)
    assert desk.applied == [] and 'launch changed' in desk.answer(path)


@pytest.mark.parametrize('ending', [turn(1, 3), turn(-5, 3, timed=False), turn(-5, None)],
                         ids=['started-after-asking', 'untimed-end', 'no-end'])
def test_an_own_request_never_applies_at_an_end_that_is_not_its_asking_turns(desk, ending):
    desk.state = state(open_=False, input_at=-5, turns=[ending])
    path = desk.ask(seconds=0)
    desk.tick(4)
    assert desk.applied == [] and desk.answer(path).startswith('refused:')


def test_an_own_request_filed_after_its_turn_already_ended_is_refused(desk):
    desk.state = state(open_=True, input_at=-5, turns=[turn(-9, 0.5, index=0)])
    path = desk.ask(seconds=0)
    desk.tick(1)
    assert desk.applied == [] and 'asking turn already ended' in desk.answer(path)


# Another seat's request on a human seat: an immediate proved break, or nothing.

def test_another_seats_request_applies_at_a_break_proved_when_it_was_asked(desk):
    desk.state = state(open_=False, input_at=-20, turns=[turn(-20, -10)])
    path = desk.ask(caller='captain-larch', seconds=0, resume=True)
    desk.tick(1)
    assert desk.applied == ['restart'] and 'restarted' in desk.answer(path)


def test_another_seats_request_during_the_persons_turn_is_refused_not_held(desk):
    desk.state = state(open_=True, input_at=-5)
    path = desk.ask(caller='captain-larch', seconds=0)
    desk.tick(1)
    assert desk.applied == [] and 'not proved at an immediate break' in desk.answer(path)
    assert not path.exists(), 'never held over for the turn to end'
    desk.state = state(open_=False, input_at=-5, turns=[turn(-5, 3)])
    desk.tick(4)
    assert desk.applied == []


def test_another_seats_request_older_than_the_answer_window_is_refused(desk):
    desk.state = state(open_=False, input_at=-20, turns=[turn(-20, -10)])
    path = desk.ask(caller='captain-larch', seconds=0)
    desk.tick(servicectl.ANSWER_SECONDS + 1)
    assert desk.applied == [] and desk.answer(path).startswith('refused:')


def test_another_seats_request_cannot_apply_after_the_person_speaks(desk):
    desk.state = state(open_=False, input_at=-20, turns=[turn(-20, -10)])
    path = desk.ask(caller='captain-larch', seconds=0)
    desk.state = state(open_=True, input_at=0.5, input_id='input-larch-later')
    desk.tick(1)
    assert desk.applied == [] and 'superseded by later input' in desk.answer(path)


# Cancellation, supersession and unknown outcomes.

def test_cancel_by_id_settles_without_applying_and_names_the_request(desk):
    desk.state = state(open_=True, input_at=-5)
    path = desk.ask(seconds=0)
    desk.tick(1)
    said = request_queue.cancel(desk.runtime, SEAT, path.stem)
    assert path.stem in said and 'settled without applying' in said
    assert 'never applied' in desk.answer(path) and not path.exists()
    desk.state = state(open_=False, input_at=-5, turns=[turn(-5, 3)])
    desk.tick(4)
    assert desk.applied == []


def test_cancel_never_succeeds_against_a_claimed_action(desk):
    path = desk.ask(seconds=0)
    path.with_suffix('.claimed').write_text(path.read_text())  # the service claimed it before dispatch
    with pytest.raises(ShipError, match='already claimed; outcome unknown; retained'):
        request_queue.cancel(desk.runtime, SEAT, path.stem)
    with pytest.raises(ShipError, match='already claimed'):
        request_queue.cancel(desk.runtime, SEAT)
    assert path.exists() and desk.answer(path) is None


def test_a_claimed_request_is_never_replayed_and_shows_its_outcome_unknown(desk):
    desk.state = state(open_=False, input_at=-5, turns=[turn(-5, 3)])
    path = desk.ask(seconds=0)
    path.with_suffix('.claimed').write_text(path.read_text())
    desk.tick(4)
    desk.tick(5)
    assert desk.applied == [] and path.exists() and desk.answer(path) is None
    text = request_queue.describe(request_queue.pending(desk.runtime)[0])
    assert 'outcome unknown, already claimed' in text and '--cancel' not in text


def test_a_cancel_interrupted_after_its_intent_recovers_without_dispatch(desk, monkeypatch):
    desk.state = state(open_=False, input_at=-5, turns=[turn(-5, 3)])
    path = desk.ask(seconds=0)
    real = request_queue.settle
    monkeypatch.setattr(request_queue, 'settle', lambda *a, **k: (_ for _ in ()).throw(OSError('synthetic crash')))
    with pytest.raises(OSError):
        request_queue.cancel(desk.runtime, SEAT, path.stem)
    monkeypatch.setattr(request_queue, 'settle', real)
    desk.tick(4)
    assert desk.applied == [] and 'cancelled' in desk.answer(path)


def test_a_newer_relaunch_supersedes_an_older_one_which_never_applies(desk):
    desk.state = state(open_=False, input_at=-20, turns=[turn(-20, -10)])
    older = desk.ask(caller='captain-larch', seconds=0, name='r00000-larch')
    newer = desk.ask(caller='captain-larch', seconds=1, name='r00010-larch')
    desk.tick(2)
    assert desk.applied == ['fresh']
    assert f'superseded by {newer.stem}' in desk.answer(older) and 'never applied' in desk.answer(older)
    assert older.stem in desk.answer(newer)


def test_supersession_with_equal_times_is_unknown_and_retained(desk):
    desk.state = state(open_=False, input_at=-20, turns=[turn(-20, -10)])
    first = desk.ask(caller='captain-larch', seconds=0, name='r00000-larch')
    second = desk.ask(caller='captain-larch', seconds=0, name='r00001-larch')
    desk.tick(1)
    assert desk.applied == [] and first.exists() and second.exists()


# Status: immediate, operational, and never a human record read.

def test_status_shows_kind_since_and_reason_at_once_without_reading_the_human_record(desk):
    path = desk.ask(seconds=0, resume=True)
    [row] = request_queue.pending(desk.runtime)
    assert (row['kind'], row['since'], row['seat']) == ('restart', T0.isoformat(), SEAT)
    assert row['reason'] == 'service has not supplied its decision yet'
    text = request_queue.describe(row)
    assert f'cancel: qd restart {SEAT} --cancel {path.stem}' in text
    assert 'captain' not in text and 'origin' not in text and 'pid' not in text, 'no arguments or identities'
    assert desk.reads == 0, 'the status path never reads the seat'
