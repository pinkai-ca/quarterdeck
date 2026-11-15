"""The seat's own word on the two paths of a human seat's own request that test_seat_own_word.py does not reach.

A human seat's own fresh start after an open handoff to itself is bound to that handoff and applies at its next
quiet moment, whatever deliveries come between. A human seat's own request whose person's input is known waits for
its quiet moment by the same reading. On both, the word reaches the relaunch decision, and it still covers only
the turn it was given in: after a later turn it lifts nothing, and the request waits as one without the word.

The service's own request loop runs unchanged on the stand-ins of test_request_own_fresh_after_handoff.py.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from quarterdeck import door, idle, post, request_queue
from quarterdeck.harness.types import ActiveTool, Screen

from .test_request_own_fresh_after_handoff import deliver, desk, handoff, waiting  # noqa: F401 (desk: the fixture)
from .test_request_human_claude_validation import SEAT, T0, state, turn

PENDING = idle.Hold('tools-pending', 'tools pending: its screen shows 2 shells in the background')
WORD = 'at rest by its record and its own word: what it had running was released'


@pytest.fixture
def quiet(desk, monkeypatch):
    """The door as the idle proof reads it: the same screen the service's quiet-moment check is given."""
    class Door:
        def __init__(self, seat):
            assert seat is desk.seat

        def screen(self):
            return desk.screen

    monkeypatch.setattr(door, 'Door', Door)
    desk.hold = PENDING
    return desk


def unread(**fields):
    built = state(**fields)
    return replace(built, pending_tools=None, active_tools=(ActiveTool('Bash', T0 - timedelta(seconds=30)),))


def ask(desk, *, release=True, seconds=0.0, resume=False):
    desk.now = desk.at(seconds)
    data = dict(seat=SEAT, resume=resume, caller=SEAT, origin=request_queue.launch_binding(desk.seat),
                **(dict(release=True) if release else {}))
    return request_queue.enqueue(desk.runtime, 'relaunch', data, name=f'p{int(seconds * 10):05d}-larch')


def told(desk):
    return [m for m in post.Store(desk.runtime).for_seat(SEAT) if m.kind == 'report' and m.sender == post.SHIP]


def test_the_word_applies_a_fresh_start_after_its_handoff_at_the_end_of_the_asking_turn(quiet):
    handoff(quiet)
    quiet.state = unread(open_=True, input_at=-40)
    path = ask(quiet)
    quiet.tick(1)
    assert quiet.applied == [] and waiting(quiet)[0] == 'turn-open'
    assert request_queue.metadata(path)['handoff'], 'bound to its handoff: the path under test'
    quiet.state = unread(open_=False, input_at=-40, turns=[turn(-40, 2)])
    quiet.tick(3)
    assert quiet.applied == ['fresh'] and quiet.answer(path).endswith(WORD)
    assert told(quiet) == []


def test_without_the_word_the_same_fresh_start_waits_on_what_runs(quiet):
    handoff(quiet)
    quiet.state = unread(open_=True, input_at=-40)
    path = ask(quiet, release=False)
    quiet.tick(1)
    quiet.state = unread(open_=False, input_at=-40, turns=[turn(-40, 2)])
    quiet.tick(3)
    assert quiet.applied == [] and path.exists() and waiting(quiet) == ('tools-pending', PENDING.reason)
    quiet.tick(64)
    [message] = told(quiet)
    assert message.body.startswith('your own fresh start waits · tools pending: ')


def test_after_a_later_turn_the_word_of_the_asking_turn_lifts_nothing(quiet):
    """A delivery gives the seat a later turn before the fresh start applies: what it started in that turn was
    never released, so the request waits on the tool state as one without the word."""
    handoff(quiet)
    quiet.state = unread(open_=True, input_at=-40)
    path = ask(quiet)
    quiet.tick(1)
    deliver(quiet, 3)
    quiet.state = unread(open_=True, input_at=4, input_id='input-larch-notice', turns=[turn(-40, 2)])
    quiet.tick(5)
    assert quiet.applied == [] and waiting(quiet)[0] == 'turn-open'
    quiet.state = unread(open_=False, input_at=4, input_id='input-larch-notice', turns=[turn(-40, 2), turn(4, 6, 1)])
    quiet.tick(7)
    assert quiet.applied == [] and path.exists() and waiting(quiet) == ('tools-pending', PENDING.reason)
    quiet.hold = None
    quiet.state = replace(quiet.state, pending_tools=(), active_tools=())
    quiet.tick(8)
    assert quiet.applied == ['fresh'] and not quiet.answer(path).endswith(WORD), 'applied on the record alone'


def test_the_word_applies_an_own_restart_at_its_quiet_moment_where_the_persons_input_is_known(quiet):
    quiet.state = unread(open_=True, input_at=-40)
    path = ask(quiet, resume=True)
    quiet.tick(1)
    assert quiet.applied == [] and waiting(quiet)[0] == 'turn-open'
    assert 'handoff' not in request_queue.metadata(path), 'a restart is never bound to a handoff'
    quiet.state = unread(open_=False, input_at=-40, turns=[turn(-40, 2)])
    quiet.tick(3)
    assert quiet.applied == ['restart'] and quiet.answer(path).endswith(WORD)


def test_on_that_path_too_the_box_must_be_empty_before_the_word_counts(quiet):
    quiet.state = unread(open_=True, input_at=-40)
    path = ask(quiet, resume=True)
    quiet.tick(1)
    quiet.state = unread(open_=False, input_at=-40, turns=[turn(-40, 2)])
    quiet.screen = Screen(text='', box='half a line', working=False)
    quiet.tick(3)
    assert quiet.applied == [] and path.exists() and waiting(quiet)[0] == 'box'
    quiet.screen = Screen(text='', box='', working=False)
    quiet.tick(4)
    assert quiet.applied == ['restart'] and quiet.answer(path).endswith(WORD)


def test_the_persons_later_input_retires_a_request_with_the_word_as_any_other(quiet):
    quiet.state = unread(open_=True, input_at=-40)
    path = ask(quiet, resume=True)
    quiet.tick(1)
    quiet.person = [quiet.at(2)]  # the person typed after the request
    quiet.state = unread(open_=False, input_at=-40, turns=[turn(-40, 3)])
    quiet.tick(4)
    assert quiet.applied == [] and "superseded by the person's later input" in quiet.answer(path)
