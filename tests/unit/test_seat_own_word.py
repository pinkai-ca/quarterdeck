"""The seat's own word, where neither its record nor its screen settles what it has running.

A seat that asks for its own restart or fresh start may release what it started: `qd restart <seat> --release`.
The word is the seat's alone, proved as its own request is proved, by the filer's descent from the seat's pane
process. It lifts a hold of the tool state and nothing else: the turn it was given in must have ended, its input
complete, the queue empty and the composer at rest. A word given in an earlier turn than the one that last ended
lifts nothing. Where a relaunch stops at the tool state, the ship tells the seat once, through the post, what holds
it and what its own word does.

The service's own request loop and relaunch decision run here unchanged, on the stand-ins of the request tests.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from quarterdeck import cli, door, idle, post, request_queue, service as servicemod
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.types import ActiveTool
from quarterdeck.ship import ShipError
from quarterdeck.tmux import TmuxError, TmuxUnanswered

from .test_request_human_claude_validation import SEAT, T0, desk, state, turn  # noqa: F401 (desk: the fixture)

RULE = '─' * 100
UNKNOWN = idle.Hold('tools-unknown', 'tool state unknown: its screen\'s bottom line is not a measured form')
PENDING = idle.Hold('tools-pending', 'tools pending: its screen shows 2 shells in the background')
WORD = 'at rest by its record and its own word: what it had running was released'


def composer(box='❯\xa0', sign=None):
    lines = ['', '● synthetic reply larch', '', *([sign, ''] if sign else []), RULE, box, RULE,
             '  Seat Larch  role · Model 0.0 · effort · context 5%', '  ⏵⏵ bypass permissions on · 2 shells']
    return ClaudeHarness().screen('\n'.join(lines) + '\n')


@pytest.fixture
def shown(desk, monkeypatch):
    """What the door reads of the seat's screen: a composer at rest unless the test sets another."""
    seen = SimpleNamespace(screen=composer(), reads=0)

    class Door:
        def __init__(self, seat):
            assert seat is desk.seat

        def screen(self):
            seen.reads += 1
            if isinstance(seen.screen, BaseException):
                raise seen.screen
            return seen.screen

    monkeypatch.setattr(door, 'Door', Door)
    return seen


def unread(*, open_, input_at, turns=(), tools=None, **more):
    built = state(open_=open_, input_at=input_at, turns=turns, **more)
    return replace(built, pending_tools=tools,
                   active_tools=(ActiveTool('Bash', T0 - timedelta(seconds=30)),) if tools != () else ())


def ask(desk, *, release=True, caller=SEAT, seconds=0.0, resume=False):
    """The request as `qd fresh --release` files it: the word rides in the body, the proof is made at enqueue."""
    desk.now = desk.at(seconds)
    data = dict(seat=SEAT, resume=resume, caller=caller, origin=request_queue.launch_binding(desk.seat),
                **(dict(release=True) if release else {}))
    return request_queue.enqueue(desk.runtime, 'relaunch', data, name=f'w{int(seconds * 10):05d}-larch')


def told(desk):
    return [message for message in post.Store(desk.runtime).for_seat(SEAT)
            if message.kind == 'report' and message.sender == post.SHIP]


def crew(desk):
    desk.seat.session = SimpleNamespace(human=False)


# -- the word lifts the hold of the tool state, at the end of the turn it was given in ---------------------------

@pytest.mark.parametrize('hold', [UNKNOWN, PENDING], ids=['unknown', 'pending'])
@pytest.mark.parametrize('resume', [False, True], ids=['fresh', 'restart'])
def test_a_human_seats_own_word_applies_its_request_at_the_end_of_the_asking_turn(desk, shown, hold, resume):
    desk.hold, desk.state = hold, unread(open_=True, input_at=-5)
    path = ask(desk, resume=resume)
    desk.tick(1)
    assert desk.applied == [] and request_queue.pending(desk.runtime)[0]['dimension'] == 'turn-open'
    desk.state = unread(open_=False, input_at=-5, turns=[turn(-5, 3)])
    desk.tick(4)
    assert desk.applied == ['restart' if resume else 'fresh']
    assert desk.answer(path).endswith(WORD), 'the proof line names the witness'
    assert told(desk) == [], 'a request that applied needs no telling'
    assert shown.reads == 1


@pytest.mark.parametrize('hold', [UNKNOWN, PENDING], ids=['unknown', 'pending'])
def test_a_crew_seats_own_word_applies_its_request_at_the_end_of_the_asking_turn(desk, shown, hold):
    crew(desk)
    desk.hold, desk.state = idle.Hold('turn-open', 'a turn is open'), unread(open_=True, input_at=-5)
    path = ask(desk, resume=True)
    desk.tick(1)
    assert desk.applied == [] and path.exists()
    desk.hold, desk.state = hold, unread(open_=False, input_at=-5, turns=[turn(-5, 3)])
    desk.tick(4)
    assert desk.applied == ['restart'] and desk.answer(path).endswith(WORD)


def test_the_same_request_without_the_word_is_held_and_the_seat_is_told_once_what_its_word_does(desk, shown):
    desk.hold, desk.state = PENDING, unread(open_=True, input_at=-5)
    path = ask(desk, release=False)
    desk.tick(1)
    desk.state = unread(open_=False, input_at=-5, turns=[turn(-5, 3)])
    desk.tick(4)
    assert desk.applied == [] and 'asking turn ended with tools pending' in desk.answer(path)
    [message] = told(desk)
    assert message.body == (
        'your own fresh start was not applied: your turn ended with it held · tools pending: its screen shows '
        '2 shells in the background · ask again once that has ended; or, losing what you have running, give your '
        f'own word as the last act of a turn: qd fresh {SEAT} --release · request {path.stem}')
    assert shown.reads == 0, 'no word, no reading of the box for it'
    desk.tick(5)
    assert len(told(desk)) == 1


def test_a_crew_seats_held_request_waits_and_the_seat_is_told_once_for_that_request(desk, shown):
    crew(desk)
    desk.hold, desk.state = UNKNOWN, unread(open_=False, input_at=-5, turns=[turn(-5, -1)])
    path = ask(desk, release=False, caller='systems-larch', resume=True)
    for second in (1, 2, 30, 60):
        desk.tick(second)
    assert desk.applied == [] and path.exists(), 'deferred, as before'
    assert told(desk) == [], 'not before the same hold has stood a minute, counted from the pass that first saw it'
    for second in (61.5, 62, 90, 300):
        desk.tick(second)
    [message] = told(desk)
    assert message.body == (
        f'a restart of this seat, asked by systems-larch, waits · {UNKNOWN.reason} · it applies once that has '
        'ended; to apply it without waiting, losing what you have running, give your own word as the last act of '
        f'a turn: qd restart {SEAT} --release · request {path.stem}')
    assert request_queue.metadata(path)['told'] == message.id
    desk.hold, desk.state = None, unread(open_=False, input_at=-5, turns=[turn(-5, -1)], tools=())
    desk.tick(301)
    assert desk.applied == ['restart'] and len(told(desk)) == 1


def test_a_hold_that_passes_within_the_minute_is_never_told(desk, shown):
    """A helper that just ended leaves a hint on the bottom line for some thirty seconds: the request applies by
    itself when the hint goes, and the seat is spared a report and a turn."""
    crew(desk)
    desk.hold, desk.state = UNKNOWN, unread(open_=False, input_at=-5, turns=[turn(-5, -1)])
    path = ask(desk, release=False, caller='systems-larch', resume=True)
    for second in (1, 15, 30, 45):
        desk.tick(second)
    desk.hold, desk.state = None, unread(open_=False, input_at=-5, turns=[turn(-5, -1)], tools=())
    desk.tick(46)
    assert desk.applied == ['restart'] and told(desk) == [] and not path.exists()


def test_a_hold_that_changes_is_counted_from_its_change(desk, shown):
    crew(desk)
    desk.hold, desk.state = UNKNOWN, unread(open_=False, input_at=-5, turns=[turn(-5, -1)])
    ask(desk, release=False, caller='systems-larch', resume=True)
    for second in (1, 50):
        desk.tick(second)
    desk.hold = PENDING  # the hint went and the line now names work
    for second in (55, 100):
        desk.tick(second)
    assert told(desk) == [], 'the new hold has stood forty-five seconds'
    desk.tick(116)
    [message] = told(desk)
    assert PENDING.reason in message.body and UNKNOWN.reason not in message.body
    assert servicemod.TELL_HELD_AFTER == 60.0


def test_an_own_crew_request_that_waits_is_told_as_its_own(desk, shown):
    crew(desk)
    desk.hold, desk.state = PENDING, unread(open_=False, input_at=-5, turns=[turn(-5, 3)])
    path = ask(desk, release=False, seconds=4)
    desk.tick(5)
    assert told(desk) == []
    desk.tick(66)
    [message] = told(desk)
    assert message.body.startswith('your own fresh start waits · tools pending: ')
    assert message.body.endswith(f'qd fresh {SEAT} --release · request {path.stem}')


# -- only the seat's own, and only what it has running ---------------------------------------------------------

def test_the_word_of_another_caller_releases_nothing(desk, shown):
    crew(desk)
    desk.hold, desk.state = PENDING, unread(open_=False, input_at=-5, turns=[turn(-5, 3)])
    path = ask(desk, caller='systems-larch', resume=True, seconds=1)
    desk.tick(2)
    assert desk.applied == [] and path.exists() and shown.reads == 0
    assert 'own' not in request_queue.metadata(path)


@pytest.mark.parametrize('forged', [
    dict(caller=SEAT),
    dict(caller=SEAT, own=dict(pane=1, pane_start='1')),
])
def test_a_word_written_into_a_request_by_hand_is_never_the_seats_own(desk, shown, forged):
    crew(desk)
    desk.hold, desk.state = PENDING, unread(open_=False, input_at=-5, turns=[turn(-5, 3)])
    folder = desk.runtime / 'requests'
    folder.mkdir(parents=True)
    path = folder / 'w-forged-larch.json'
    path.write_text(json.dumps(dict(kind='relaunch', seat=SEAT, resume=True, release=True, **forged)) + '\n')
    desk.now = desk.at(1)
    desk.tick(2)
    assert desk.applied == [] and shown.reads == 0, 'the body carries no proof: who asked is read from enqueue'


@pytest.mark.parametrize('hold', [
    idle.Hold('queue-pending', 'input is queued'),
    idle.Hold('queue-unknown', 'queue state unknown'),
    idle.Hold('completion', 'its last input has no recorded completion'),
    idle.Hold('record', 'its record is unreadable'),
    idle.Hold('identity', 'its launch or watch changed while reading'),
])
def test_a_hold_that_is_not_of_the_tool_state_is_not_the_seats_to_release(desk, shown, hold):
    crew(desk)
    desk.hold, desk.state = hold, unread(open_=False, input_at=-5, turns=[turn(-5, 3)])
    path = ask(desk, seconds=1)
    desk.tick(2)
    assert desk.applied == [] and path.exists() and shown.reads == 0
    assert told(desk) == [], 'the telling is for the tool state alone'


@pytest.mark.parametrize(('changes', 'why'), [
    (dict(queued=('synthetic queued input',)), 'input is queued'),
    (dict(queued=None), 'the queue is unknown'),
    (dict(queue_error='synthetic ambiguity'), 'the queue is unresolved, which an explicit relaunch alone may pass'),
    (dict(turn_open=True), 'a turn is open'),
    (dict(turn_open=None), 'the turn is unknown'),
    (dict(completed_turns=()), 'no completion'),
    (dict(last_input_at=None), 'no input time'),
])
def test_the_record_must_still_show_the_turn_the_queue_and_the_completion(desk, shown, changes, why):
    resting = unread(open_=False, input_at=-5, turns=[turn(-5, 3)])
    assert idle.release(desk.seat, resting, PENDING, desk.at(0)) is None
    assert idle.release(desk.seat, replace(resting, **changes), PENDING, desk.at(0)) is PENDING, why


@pytest.mark.parametrize(('asked', 'turns', 'input_at', 'why'), [
    (-6, [turn(-5, 3)], -5, 'given before the turn that ended began'),
    (4, [turn(-5, 3)], -5, 'given after that turn ended'),
    (0, [turn(-5, 1), turn(2, 3, index=1)], 2, 'a later turn has ended since'),
    (0, [turn(-5, 3)], 1, 'an input arrived after the word'),
    (0, [turn(-5, 3, timed=False)], -5, 'the end is not timed'),
    (0, [turn(-5, None)], -5, 'the turn has no end'),
])
def test_a_word_covers_the_turn_it_was_given_in_and_no_other(desk, shown, asked, turns, input_at, why):
    resting = unread(open_=False, input_at=input_at, turns=turns)
    assert idle.release(desk.seat, resting, UNKNOWN, desk.at(asked)) is UNKNOWN, why
    assert shown.reads == 0


def test_a_word_whose_time_is_unknown_or_a_reading_with_nothing_held_changes_nothing(desk, shown):
    resting = unread(open_=False, input_at=-5, turns=[turn(-5, 3)])
    assert idle.release(desk.seat, resting, UNKNOWN, None) is UNKNOWN
    assert idle.release(desk.seat, resting, UNKNOWN, desk.at(0).replace(tzinfo=None)) is UNKNOWN
    assert idle.release(desk.seat, None, UNKNOWN, desk.at(0)) is UNKNOWN
    assert idle.release(desk.seat, resting, None, desk.at(0)) is None
    assert shown.reads == 0


@pytest.mark.parametrize('screen', [
    composer(box='❯\xa0synthetic typed words'),
    composer(box='❯ Press up to edit queued messages'),
    composer(sign='✽ Pondering… (47s · ↓ 3.9k tokens)'),
    composer(box='synthetic layout with no composer'),
    None,
    OSError('synthetic unreadable pane'),
    TmuxError("tmux capture-pane: can't find pane: %71"),
    TmuxUnanswered('tmux capture-pane: private server did not answer'),
])
def test_the_box_must_still_be_read_empty_and_idle(desk, shown, screen):
    shown.screen = screen
    resting = unread(open_=False, input_at=-5, turns=[turn(-5, 3)])
    assert idle.release(desk.seat, resting, PENDING, desk.at(0)) == idle.Hold(
        'box', 'the box is not proved empty and idle')


def test_a_word_that_cannot_apply_leaves_the_request_held_under_what_still_holds(desk, shown):
    crew(desk)
    shown.screen = composer(box='❯\xa0synthetic typed words')
    desk.hold, desk.state = PENDING, unread(open_=False, input_at=-5, turns=[turn(-5, 3)])
    path = ask(desk, seconds=1)
    desk.tick(2)
    assert desk.applied == []
    [row] = request_queue.pending(desk.runtime)
    assert (row['dimension'], row['reason']) == ('box', 'the box is not proved empty and idle')
    shown.screen = composer()
    desk.tick(2.5)
    assert desk.applied == ['fresh'] and desk.answer(path).endswith(WORD)


# -- the command -------------------------------------------------------------------------------------------------

def test_only_the_seat_itself_can_give_the_word_at_the_command(desk, monkeypatch):
    ship = SimpleNamespace(runtime=desk.runtime)
    monkeypatch.setattr(cli, 'this_seat', lambda ship, name: name)
    monkeypatch.setattr(cli, 'seat_of', lambda ship, name, **options: desk.seat)
    for caller in ('systems-larch', None):
        if caller is None:
            monkeypatch.delenv('QD_SEAT', raising=False)
        else:
            monkeypatch.setenv('QD_SEAT', caller)
        with pytest.raises(ShipError, match=f"--release is {SEAT}'s own word"):
            cli.relaunch(ship, SEAT, resume=True, release=True)
    assert not (desk.runtime / 'requests').exists(), 'nothing is asked'
