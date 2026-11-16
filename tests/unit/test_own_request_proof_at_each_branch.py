"""Every branch of the service that treats a relaunch request as the seat's own reads the proof enqueue recorded.

A request whose metadata names the seat as its caller, edited by hand so that it carries no proof (the proof removed
from a request the seat filed, or another caller's request rewritten to name the seat), is another caller's at each
branch: the person's later input, the handoff's fresh start, the seat's own word, and the report of a held request.
A proved request, filed from the seat's pane, is shown beside it where the branch's outcome differs.

The service's own request loop and relaunch decision run here unchanged, on the stand-ins of the request tests.
"""

from __future__ import annotations

import pytest

from quarterdeck import request_queue

from .test_request_human_claude_validation import SEAT, state, turn
from .test_request_own_fresh_after_handoff import PEER, desk, handoff  # noqa: F401 (desk: the fixture)
from .test_seat_own_word import PENDING, ask, crew, told, unread

FORGED = ['proof removed', 'caller rewritten']


def file(desk, how, *, seconds=0.0, resume=False, release=False):
    """The request as filed, then its metadata edited by hand as ``how`` says, or left as filed ('proved')."""
    if how == 'caller rewritten':
        path = ask(desk, release=release, caller=PEER, seconds=seconds, resume=resume)
        meta = request_queue.metadata(path)
        assert 'own' not in meta
        meta['caller'] = SEAT
    else:
        path = ask(desk, release=release, seconds=seconds, resume=resume)  # filed by this process: the pane's
        meta = request_queue.metadata(path)
        assert meta['caller'] == SEAT and meta['own']['pane'] == desk.launch.pid
        if how == 'proof removed':
            del meta['own']
    request_queue.save_metadata(path, meta)
    return path


@pytest.mark.parametrize('person', ['earlier', 'later'])
@pytest.mark.parametrize('how', ['proved', *FORGED])
def test_the_persons_input_decides_only_a_proved_own_request(desk, how, person):
    """A proved own restart waits out the person's turn, or is retired by the person's later input; one with no
    proof is another caller's: asked during the person's turn, it is refused at once."""
    desk.state = state(open_=True, input_at=-40)
    desk.person = [desk.at(-600)] if person == 'earlier' else [desk.at(-600), desk.at(0.5)]
    path = file(desk, how, resume=True)
    desk.tick(1)
    assert desk.applied == []
    if how != 'proved':
        assert 'not proved at an immediate break' in desk.answer(path) and not path.exists()
    elif person == 'later':
        assert "superseded by the person's later input" in desk.answer(path) and not path.exists()
    else:
        assert desk.answer(path) is None and path.exists(), 'held for the end of its asking turn'


@pytest.mark.parametrize('how', FORGED)
def test_a_handoff_never_defers_a_fresh_start_without_the_proof(desk, how):
    handoff(desk)
    desk.state = state(open_=True, input_at=-40)
    path = file(desk, how)
    desk.tick(1)
    assert desk.applied == [] and 'not proved at an immediate break' in desk.answer(path) and not path.exists()


@pytest.mark.parametrize('how', FORGED)
def test_a_word_in_a_request_without_the_proof_releases_nothing(desk, how):
    crew(desk)
    desk.hold, desk.state = PENDING, unread(open_=False, input_at=-5, turns=[turn(-5, 3)])
    path = file(desk, how, seconds=4, release=True, resume=True)
    desk.tick(5)
    assert desk.applied == [] and path.exists(), 'held as any other caller\'s request: the word lifts nothing'


@pytest.mark.parametrize('how', FORGED)
def test_a_held_request_without_the_proof_is_reported_as_another_callers_not_as_the_seats_own(desk, how):
    crew(desk)
    desk.hold, desk.state = PENDING, unread(open_=False, input_at=-5, turns=[turn(-5, 3)])
    path = file(desk, how, seconds=4)
    desk.tick(5)
    desk.tick(66)
    [message] = told(desk)
    assert message.body.startswith(f'a fresh start of this seat, asked by {SEAT}, waits · tools pending: '), \
        'the caller the metadata names, reported as any other caller\'s; never "your own"'
    assert message.body.endswith(f'qd fresh {SEAT} --release · request {path.stem}')


def test_a_proved_own_request_refused_at_its_turns_end_is_reported_to_the_seat(desk):
    """With no classified person input, the asking turn's own rule decides: the turn ended with the request held."""
    del desk.seat.harness.person_inputs
    desk.hold, desk.state = PENDING, unread(open_=True, input_at=-5)
    path = file(desk, 'proved')
    desk.tick(1)
    desk.state = unread(open_=False, input_at=-5, turns=[turn(-5, 3)])
    desk.tick(4)
    assert desk.applied == [] and desk.answer(path).startswith('refused:') and not path.exists()
    [message] = told(desk)
    assert message.body.startswith('your own fresh start was not applied: your turn ended with it held')


@pytest.mark.parametrize('how', FORGED)
def test_a_refused_request_without_the_proof_is_not_reported_to_the_seat_as_its_own(desk, how):
    """Asked after the person's turn ended, a request without the proof is another caller's: tried at once, refused
    to that caller under the hold, and the seat is told nothing of it."""
    del desk.seat.harness.person_inputs
    desk.hold, desk.state = PENDING, unread(open_=False, input_at=-5, turns=[turn(-5, -1)])
    path = file(desk, how)
    desk.tick(1)
    assert desk.applied == [] and not path.exists()
    assert desk.answer(path) == f'refused: {SEAT} waits for {PENDING.reason}; human request not deferred'
    assert told(desk) == []
