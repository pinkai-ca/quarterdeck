"""The person's own seat after a sign-in failure inside the program: the one fixed waking line, on today's terms.

While the pool's sign-in reads valid, a single sign-in failure no longer holds the waking line for the person;
any other failure, an invalid pool sign-in, or a second sign-in failure (the waking turn's own) keeps the hold and
names the seat, and nothing more is typed. The drydock desk and its stand-in composer come from
test_human_wake_claude_validation, which records every paste and key; the record's reading and the pool's
sign-in are stood in, and no credential is read.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from quarterdeck import auth_failure, human_wake, post, turn_failure
from quarterdeck.harness.types import AUTHENTICATION, State, Tokens, Turn, TurnFailure
from tests.unit.test_human_wake_claude_validation import EXACT, desk, received  # noqa: F401  (the fixture)

PROVIDER = 'provider error'


def turn(index, started, ended, failure=None):
    return Turn(index, started, ended, 'synthetic input', 'synthetic reply', None, Tokens(), timed=True, failure=failure)


def reading(d, monkeypatch, *, kind=AUTHENTICATION, failed=1, pool='valid'):
    """The record's reading as the stand-in harness gives it, and the failure as the service published it."""
    now = post.now()
    failure = TurnFailure('synthetic failure words', now - timedelta(seconds=30), kind=kind)
    turns = [turn(0, now - timedelta(minutes=10), now - timedelta(minutes=9))]
    turns += [turn(i + 1, now - timedelta(seconds=60 - i), now - timedelta(seconds=30 - i), failure)
              for i in range(failed)]
    state = State(turn_open=False, pending_tools=(), queued=(), failure=failure, completed_turns=tuple(turns))
    monkeypatch.setattr(d.seat.harness, 'state', lambda record: state)
    monkeypatch.setattr(turn_failure, 'current', lambda seat: failure)
    monkeypatch.setattr(auth_failure, 'pool_observation', lambda seat, now=None: (pool, [1, 2, 3, 4, 5], True))
    return failure


def test_a_seat_of_his_after_a_sign_in_failure_gets_the_line_once(desk, monkeypatch):
    reading(desk, monkeypatch)
    assert auth_failure.waiting(auth_failure.status(desk.seat)), 'not held for him: no alarm'
    assert turn_failure.obstruction(desk.seat) is None
    desk.order()
    human_wake.run(desk.seat)
    assert desk.pastes() == [EXACT] and desk.keys() == [('Enter',)], 'the fixed line only'
    assert received(desk), 'confirmed from the record'
    for _ in range(3):
        human_wake.run(desk.seat)
    assert desk.pastes() == [EXACT] and desk.keys() == [('Enter',)], 'once'


def test_a_box_holding_his_text_is_never_touched(desk, monkeypatch):
    reading(desk, monkeypatch)
    desk.tmux.box = 'his own half-typed words'
    desk.order()
    for _ in range(3):
        reason = human_wake.run(desk.seat)
    assert desk.tmux.typed() == [], 'no paste, no key, no clearing'
    assert reason == human_wake.BOX and desk.tmux.box == 'his own half-typed words'


def test_a_second_sign_in_failure_names_it_and_types_nothing_more(desk, monkeypatch):
    reading(desk, monkeypatch)
    desk.order()
    human_wake.run(desk.seat)
    assert desk.pastes() == [EXACT]
    # The waking turn fails at sign-in too: two in a row, after the line went.
    now = post.now() + timedelta(seconds=5)
    second = TurnFailure('synthetic failure words', now, kind=AUTHENTICATION)
    first = turn_failure.current(desk.seat)
    turns = (turn(0, now - timedelta(minutes=10), now - timedelta(minutes=9)),
             turn(1, first.at - timedelta(seconds=1), first.at, first), turn(2, now - timedelta(seconds=1), now, second))
    state = State(turn_open=False, pending_tools=(), queued=(), failure=second, completed_turns=turns)
    monkeypatch.setattr(desk.seat.harness, 'state', lambda record: state)
    monkeypatch.setattr(turn_failure, 'current', lambda seat: second)
    assert not auth_failure.human_wakeable(desk.seat), 'the line already went before this failure'
    named = auth_failure.status(desk.seat)
    assert not auth_failure.waiting(named) and 'needs attention' in named, 'named on the status line, as today'
    desk.order('A second synthetic order')
    for _ in range(3):
        human_wake.run(desk.seat)
    assert desk.pastes() == [EXACT] and desk.keys() == [('Enter',)], 'nothing more is typed'


@pytest.mark.parametrize('case', ['other-failure', 'pool-expired', 'pool-unknown', 'two-in-a-row'])
def test_the_hold_stays_for_every_other_failure(desk, monkeypatch, case):
    reading(desk, monkeypatch, kind=PROVIDER if case == 'other-failure' else AUTHENTICATION,
            pool={'pool-expired': 'expired', 'pool-unknown': 'unknown'}.get(case, 'valid'),
            failed=2 if case == 'two-in-a-row' else 1)
    desk.order()
    reason = human_wake.run(desk.seat)
    assert desk.tmux.typed() == []
    if case != 'other-failure' and case != 'two-in-a-row':
        assert not auth_failure.waiting(auth_failure.status(desk.seat))
    if case == 'two-in-a-row':
        assert reason == 'human wake recorded turn error; person must intervene'
