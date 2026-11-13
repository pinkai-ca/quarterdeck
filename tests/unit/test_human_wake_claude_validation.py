"""Independent gates for the guarded human-seat waking line (law: one-door).

A drydock desk in the human Office session, a stand-in tmux pane that draws the fake
program's composer and writes its record when Enter takes the box, and the real Door,
human_wake, post, service and check code. Every case says what was typed, so a case that
expects no input fails on any key or paste at all.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import check as checkmod, cli, drill, human_wake, input_receipt, post, ship as shipmod
from quarterdeck.door import Door
from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.harness.types import Join, Screen, State
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.tmux import Pane

EXACT = 'qd > ship: messages are waiting. Run qd inbox --wait now to read them.'  # captain 400c, 70 characters
MARKER = 'synthetic-planted-payload-wqzv'
RULE = '─' * 40
DESK = Pane('%7', 'Office', 'desk', 4242, 400, 40, False, False, None)
SPOCK = Pane('%8', 'Bridge', 'spock', 4343, 400, 40, False, False, None)


class Composer:
    """A stand-in tmux: the fake program's frame, every paste and key recorded, Enter writes the record."""

    def __init__(self, *panes: Pane):
        self.panes = {p.id: p for p in panes}
        self.box = ''
        self.dialog: str | None = None
        self.working = False
        self.sent: list[tuple] = []
        self.record: ClaudeShape | None = None
        self.on_submit = None

    def frame(self) -> str:
        lines = ['⏺ earlier synthetic work', '']
        if self.dialog:
            lines += [f'┌ {self.dialog} ' + '─' * 12 + '┐', '│ Synthetic choice one or two │', '└' + '─' * 30 + '┘']
        lines += [RULE, *[('> ' if i == 0 else '  ') + line for i, line in enumerate(self.box.split('\n'))], RULE,
                  f"fake claude · model fake-1 · effort medium · {'working' if self.working else 'idle'}"]
        return '\n'.join(lines) + '\n'

    def pane(self, ident):
        return self.panes.get(ident)

    def read_pane(self, ident, history=0, **options):
        pane = self.panes.get(ident)
        return (pane, self.frame()) if pane else None

    def capture(self, ident, history=0, **options):
        return self.frame()

    def paste(self, ident, text, bracketed=True):
        self.sent.append(('paste', ident, text))
        self.box += text

    def keys(self, ident, *keys):
        self.sent.append(('keys', ident, keys))
        if keys == ('Enter',):
            taken, self.box = self.box, ''
            if self.record is not None:
                self.record.begin(taken, 'fake-1', 'medium')
            if self.on_submit:
                self.on_submit(taken)

    def typed(self) -> list[tuple]:
        return [s for s in self.sent if s[1] == DESK.id]


def launch(seat: Seat, pane: Pane, minutes: int = 1) -> None:
    Launched(pane.id, pane.pid, (post.now() - timedelta(minutes=minutes)).isoformat(), seat.pool.name, None,
             ['qd-fakeharness'], 'synthetic', harness='fake').write(seat.folder / 'launch.json')


@pytest.fixture
def desk(drydock, monkeypatch):
    """The drydock's human desk on a known-empty idle composer, its record holding one ended turn."""
    ship = shipmod.load(drydock)
    ship.seats['spock'] = replace(ship.seat('captain'), name='spock', role='systems', persona=None)
    tmux = Composer(DESK)
    seat = Seat(ship, ship.seat('desk'), tmux)
    seat.folder.mkdir(parents=True, exist_ok=True)
    seat.home.mkdir(parents=True, exist_ok=True)
    launch(seat, DESK)
    record = ClaudeShape(seat.home, ship.workspace, 'synthetic-desk-record', 'fake-1')
    record.begin('Synthetic earlier request', 'fake-1', 'medium')
    record.end('synthetic earlier answer')
    (seat.folder / 'record').write_text(f'{record.path}\n')
    monkeypatch.setattr(seat, 'join', lambda: Join(record.path))
    tmux.record = record
    store = post.Store(ship.runtime)

    def order(body='Synthetic waiting order'):
        return store.new('order', 'captain', 'desk', f'{body}\n{MARKER}', subject=f'subject {MARKER}')

    def pastes():
        return [s[2] for s in tmux.typed() if s[0] == 'paste']

    def keys():
        return [s[2] for s in tmux.typed() if s[0] == 'keys']

    def watch(pid, generation=None):
        path = post.watch_file(ship.runtime, 'desk')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'{pid} {post.now().isoformat(timespec="seconds")}\n')
        if generation is not None:
            (path.parent / 'watch-generation').write_text(generation + '\n')

    return SimpleNamespace(ship=ship, seat=seat, tmux=tmux, record=record, store=store, order=order,
                           pastes=pastes, keys=keys, watch=watch, runtime=ship.runtime)


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, '-c', ''])
    child.wait()
    return child.pid


def reservation(d) -> dict:
    return json.loads((d.seat.folder / 'human-wake.json').read_text())


def received(d) -> bool:
    """The reservation is delivered and the owned record holds the fixed line after its own time."""
    data = reservation(d)
    since = datetime.fromisoformat(data['since'])
    return (data['phase'] == 'delivered' and data['record'] == str(d.record.path)
            and input_receipt.confirmed(d.seat.harness, d.record.path, EXACT, since) == 'delivered')


def confirmed_wake(d):
    """Success is the reservation's phase and the record's receipt; run()'s return is no contract."""
    d.order()
    human_wake.run(d.seat)
    assert d.pastes() == [EXACT] and d.keys() == [('Enter',)]
    assert received(d)


# -- the line and the one wake -----------------------------------------------------------------


def test_the_line_is_the_captains_exact_fixed_text_with_no_control_characters():
    assert human_wake.LINE == EXACT and len(EXACT) == 70
    assert '\n' not in EXACT and not any(ord(c) < 32 or ord(c) == 127 for c in EXACT)


def test_a_waiting_message_with_no_watch_and_an_empty_box_gets_one_fixed_line_confirmed_by_the_record(desk):
    message = desk.order()
    human_wake.run(desk.seat)
    assert desk.pastes() == [EXACT], 'the fixed line only, never the waiting payload'
    assert desk.keys() == [('Enter',)], 'one submit, no clear, no Escape, no other key'
    data = reservation(desk)
    assert received(desk) and data['payload'] == EXACT and data['seat'] == 'desk'
    assert [a.result for a in post.Attempts(desk.runtime).for_message(message.id)] == [], \
        'the wake is no delivery of the waiting message; only the watch delivers it'
    assert message.id in [m.id for m in post.Attempts(desk.runtime).undelivered(desk.store, 'desk')]
    for _ in range(3):
        human_wake.run(desk.seat)
    assert desk.pastes() == [EXACT] and desk.keys() == [('Enter',)], 'once per episode'
    assert human_wake.status(desk.seat) == human_wake.WAITING


def test_no_waiting_message_means_no_input(desk):
    for _ in range(3):
        human_wake.run(desk.seat)
    assert desk.tmux.typed() == []
    assert not (desk.seat.folder / 'human-wake.json').exists()
    assert human_wake.status(desk.seat) is None


def test_an_armed_watch_receives_no_input_and_shows_no_wake_state(desk):
    desk.watch(os.getpid(), 'a' * 32)
    desk.order()
    human_wake.run(desk.seat)
    assert desk.tmux.typed() == []
    assert human_wake.status(desk.seat) is None


def test_an_old_style_watch_without_generation_is_armed_by_its_live_process(desk):
    desk.watch(os.getpid())
    reading = post.watch_reading(desk.runtime, 'desk')
    assert reading.armed is True and reading.generation is None
    desk.order()
    human_wake.run(desk.seat)
    assert desk.tmux.typed() == []


def test_a_dead_watch_process_is_proved_absent_and_wakes_once(desk):
    desk.watch(dead_pid(), 'b' * 32)
    assert post.watch_reading(desk.runtime, 'desk').armed is False
    confirmed_wake(desk)


# -- the box: only a positively known empty composer -------------------------------------------


@pytest.mark.parametrize('case', ['draft', 'multiline-draft', 'known-notice', 'unknown-dialog', 'training-question',
                                  'working', 'blank', 'no-box'])
def test_any_box_not_known_empty_is_never_typed_into_and_the_reason_is_shown(desk, case):
    t = desk.tmux
    if case == 'draft':
        t.box = 'half a sentence by the operator'
    elif case == 'multiline-draft':
        t.box = 'first line\nsecond line'
    elif case == 'known-notice':
        t.dialog = 'Daybreak'
    elif case == 'unknown-dialog':
        t.dialog = 'Something else'
    elif case == 'training-question':
        t.dialog = 'Help improve the model'
    elif case == 'working':
        t.working = True
    before = t.box
    blank = case == 'blank'
    if blank:
        t.frame = lambda: '\n'
    if case == 'no-box':
        t.frame = lambda: 'synthetic transcript only\nno composer drawn\n'
    desk.order()
    for _ in range(2):
        reason = human_wake.run(desk.seat)
        assert reason, 'a withheld wake always says why'
    assert desk.tmux.typed() == [], 'no paste, no clear, no Escape, no choice pressed'
    assert t.box == before
    assert human_wake.status(desk.seat), 'the waiting state is visible'


@pytest.mark.parametrize('box', [' ', '   ', '\t', '\n'])
def test_a_whitespace_box_is_not_empty_for_the_wake(desk, box):
    desk.order()
    screen = Screen('synthetic', box=box)
    assert human_wake.eligible(desk.seat, screen) == 'human wake box not empty; untouched'


@pytest.mark.parametrize('screen', [None, Screen('synthetic', box=None),
                                    Screen('synthetic', box='', starting=True),
                                    Screen('synthetic', box='', unknown='synthetic dialog')])
def test_an_unknown_box_is_never_empty_for_the_wake(desk, screen):
    desk.order()
    assert human_wake.eligible(desk.seat, screen) is not None


# -- record first: unknown activity holds -------------------------------------------------------


@pytest.mark.parametrize('state', [
    State(turn_open=True, pending_tools=()),
    State(turn_open=None, pending_tools=()),
    State(turn_open=False, pending_tools=None),
    State(turn_open=False, pending_tools=('Bash',)),
    State(turn_open=False, pending_tools=(), queued=('synthetic queued input',)),
    State(turn_open=False, pending_tools=(), queue_error='synthetic unresolved queue'),
], ids=['open', 'unknown-turn', 'unknown-tools', 'tools', 'queued', 'queue-unknown'])
def test_recorded_work_or_unknown_activity_holds_the_wake_visibly(desk, monkeypatch, state):
    monkeypatch.setattr(desk.seat.harness, 'state', lambda record: state)
    desk.order()
    reason = human_wake.run(desk.seat)
    assert reason and desk.tmux.typed() == []
    assert human_wake.status(desk.seat)


def test_an_unreadable_record_holds_the_wake(desk, monkeypatch):
    def unreadable(record):
        raise OSError('synthetic unreadable record')
    monkeypatch.setattr(desk.seat.harness, 'state', unreadable)
    desk.order()
    assert human_wake.run(desk.seat) and desk.tmux.typed() == []


def test_an_old_identical_line_at_the_boundary_vetoes_without_input(desk, monkeypatch):
    """The receipt matcher must not take an old fixed line for this wake's receipt."""
    from quarterdeck import input_receipt
    desk.order()
    monkeypatch.setattr(input_receipt, 'confirmed', lambda *args, **kwargs: 'delivered')
    reason = human_wake.run(desk.seat)
    assert reason and 'receipt' in reason and desk.tmux.typed() == []
    assert not (desk.seat.folder / 'human-wake.json').exists() or reservation(desk)['phase'] == 'cancelled'


# -- the watch: tri-state, malformed evidence holds --------------------------------------------


@pytest.mark.parametrize('content', ['', 'garbage', '4242', '-5 2030-01-01T00:00:00+00:00',
                                     '4242 2030-01-01T00:00:00', '4242 2030-01-01T00:00:00+00:00 extra'])
def test_malformed_watch_evidence_is_unknown_never_absence(desk, content):
    path = post.watch_file(desk.runtime, 'desk')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    assert post.watch_reading(desk.runtime, 'desk').armed is None
    desk.order()
    reason = human_wake.run(desk.seat)
    assert reason and 'watch' in reason and desk.tmux.typed() == []
    assert human_wake.status(desk.seat)


@pytest.mark.parametrize('generation', ['xyz', 'A' * 32, 'a' * 31, ''])
def test_a_malformed_watch_generation_is_unknown(desk, generation):
    path = post.watch_file(desk.runtime, 'desk')
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / 'watch-generation').write_text(generation)
    assert post.watch_reading(desk.runtime, 'desk').armed is None
    desk.order()
    assert human_wake.run(desk.seat) and desk.tmux.typed() == []


def test_a_watch_registration_writes_a_generation_and_removes_only_its_own(desk):
    assert post.wait(desk.runtime, 'desk', interval=0.01, stop_after=0.05) is None
    folder = post.watch_file(desk.runtime, 'desk').parent
    first = (folder / 'watch-generation').read_text().strip()
    assert re.fullmatch(r'[0-9a-f]{32}', first)
    assert not post.watch_file(desk.runtime, 'desk').exists(), 'an exited watch is not armed'
    assert post.watch_reading(desk.runtime, 'desk') == post.WatchReading(False, first)
    post.wait(desk.runtime, 'desk', interval=0.01, stop_after=0.05)
    assert (folder / 'watch-generation').read_text().strip() != first, 'each registration is a new generation'


def test_a_missing_generation_at_watch_exit_removes_nothing_and_keeps_the_watch_result(desk, monkeypatch):
    """Ruling: a missing marker counts as not ours and never replaces the watch's return."""
    folder = post.watch_file(desk.runtime, 'desk').parent
    real_sleep = post.time.sleep

    def vanish(seconds):
        (folder / 'watch-generation').unlink(missing_ok=True)
        real_sleep(0)
    monkeypatch.setattr(post.time, 'sleep', vanish)
    assert post.wait(desk.runtime, 'desk', interval=0.01, stop_after=0.05) is None
    assert post.watch_file(desk.runtime, 'desk').exists(), 'an unproved owner removes nothing'
    reading = post.watch_reading(desk.runtime, 'desk')
    assert reading.armed is None and 'retirement unconfirmed' in reading.reason, 'unknown, and visible'
    desk.order()
    assert human_wake.run(desk.seat) and desk.tmux.typed() == []


# -- the episode ------------------------------------------------------------------------------


def test_a_confirmed_wake_whose_turn_ends_without_a_watch_names_the_person_and_never_types_again(desk):
    confirmed_wake(desk)
    desk.record.end('synthetic answer without arming')
    human_wake.run(desk.seat)
    assert human_wake.status(desk.seat) == 'human wake confirmed but watch not armed; person must intervene'
    desk.order('A second synthetic order')
    for _ in range(3):
        human_wake.run(desk.seat)
    assert desk.pastes() == [EXACT] and desk.keys() == [('Enter',)]


def test_a_short_lived_watch_rearm_releases_the_episode_for_exactly_one_new_wake(desk):
    confirmed_wake(desk)
    arrived = post.wait(desk.runtime, 'desk', interval=0.01, stop_after=0.05)
    post.printed(desk.runtime, arrived[1])  # the watch read what waited, then exited between ticks
    desk.record.end('synthetic answer')
    human_wake.run(desk.seat)
    assert human_wake.status(desk.seat) is None, 'a real registration released the episode'
    desk.order('A later synthetic order')
    human_wake.run(desk.seat)
    human_wake.run(desk.seat)
    assert desk.pastes() == [EXACT, EXACT] and desk.keys() == [('Enter',), ('Enter',)]


def test_a_foreground_watch_running_during_the_woken_turn_is_armed_and_releases(desk):
    confirmed_wake(desk)
    desk.watch(os.getpid(), 'c' * 32)  # the seat runs qd inbox --wait in the foreground, its turn still open
    assert human_wake.status(desk.seat) is None
    human_wake.run(desk.seat)
    assert desk.pastes() == [EXACT], 'an armed watch takes no input'
    post.watch_file(desk.runtime, 'desk').unlink()
    arrived = post.wait(desk.runtime, 'desk', interval=0.01, stop_after=0.05)  # the command prints and returns
    post.printed(desk.runtime, arrived[1])
    desk.record.end('synthetic answer')
    human_wake.run(desk.seat)
    assert human_wake.status(desk.seat) is None
    desk.order('A later synthetic order')
    human_wake.run(desk.seat)
    human_wake.run(desk.seat)
    assert desk.pastes() == [EXACT, EXACT]


@pytest.mark.parametrize('case', ['known-then-missing', 'known-then-new', 'none-then-new', 'none-then-legacy'])
def test_only_a_known_new_watch_generation_releases_a_confirmed_wake(desk, monkeypatch, case):
    """A confirmed wake is released only by an actual known generation that differs from the one it saved. A missing
    marker where one was saved is unknown: no release, no new wake, no native read. A wake saved before markers
    existed (none) is still released by a real new registration, and an old-style registration proves nothing."""
    if case.startswith('known'):
        desk.watch(dead_pid(), 'b' * 32)
    confirmed_wake(desk)
    assert reservation(desk)['generation'] == ('b' * 32 if case.startswith('known') else None)
    desk.record.end('synthetic answer')
    human_wake.run(desk.seat)  # the turn is seen ended before the watch changes
    folder = post.watch_file(desk.runtime, 'desk').parent
    if case == 'known-then-missing':
        (folder / 'watch-generation').unlink()
    elif case.endswith('new'):
        arrived = post.wait(desk.runtime, 'desk', interval=0.01, stop_after=0.05)  # a real registration, then it exits
        if arrived:
            post.printed(desk.runtime, arrived[1])
    else:
        desk.watch(os.getpid())  # an old-style registration: pid and time only, no generation marker
        post.watch_file(desk.runtime, 'desk').unlink()
    desk.order('A later synthetic order')
    released = case.endswith('new')
    if not released:
        def forbidden(*args, **kwargs):
            pytest.fail('an unproved rearm reads nothing native')
        for obj, name in ((desk.tmux, 'read_pane'), (desk.tmux, 'capture'), (desk.tmux, 'paste'), (desk.tmux, 'keys'),
                          (desk.seat.harness, 'screen'), (desk.seat.harness, 'inputs'), (desk.seat.harness, 'state')):
            monkeypatch.setattr(obj, name, forbidden)
    for _ in range(2):
        human_wake.run(desk.seat)
    if released:
        assert desk.pastes() == [EXACT, EXACT] and desk.keys() == [('Enter',), ('Enter',)], 'exactly one new wake'
    else:
        assert desk.pastes() == [EXACT] and desk.keys() == [('Enter',)], 'no new wake without a proved rearm'
        assert reservation(desk)['phase'] == 'delivered', 'the episode is not released'
        assert human_wake.status(desk.seat), 'and the hold is visible'


def test_a_relaunch_after_a_confirmed_wake_releases_it_for_exactly_one_new_wake(desk):
    """Ruling: the old wake belonged to the old launch; the new launch's episode is new."""
    confirmed_wake(desk)
    desk.record.end('synthetic answer before the relaunch')
    moved = replace(DESK, pid=DESK.pid + 1)
    desk.tmux.panes[DESK.id] = moved
    launch(desk.seat, moved, minutes=0)
    desk.order('A synthetic order after the relaunch')
    for _ in range(3):
        human_wake.run(desk.seat)
    assert desk.pastes() == [EXACT, EXACT] and desk.keys() == [('Enter',), ('Enter',)]
    assert received(desk), 'the new launch\'s wake has its own receipt'
    assert 'person must intervene' not in (human_wake.status(desk.seat) or '')
    history = list((desk.seat.folder / 'human-wake-history').iterdir())
    assert len(history) == 1 and json.loads(history[0].read_text())['phase'] == 'delivered', \
        'the old launch\'s wake is retained, not erased'


# -- crashes and uncertainty ---------------------------------------------------------------------


@pytest.mark.parametrize('phase', ['pasting', 'submitting', 'submitted'])
def test_a_crash_left_typing_phase_without_a_receipt_is_never_resent(desk, phase):
    desk.order()
    desk.tmux.record = None  # the program never takes it: no receipt will exist
    human_wake.run(desk.seat)
    data = reservation(desk)
    data['phase'] = phase
    (desk.seat.folder / 'human-wake.json').write_text(json.dumps(data))
    sent = len(desk.tmux.typed())
    for _ in range(3):
        human_wake.run(desk.seat)
    assert len(desk.tmux.typed()) == sent, 'never resent'
    assert 'never resent' in human_wake.status(desk.seat)


@pytest.mark.parametrize('phase', ['pasting', 'submitting', 'submitted'])
def test_a_crash_left_typing_phase_with_its_receipt_is_recovered_without_input(desk, phase):
    confirmed_wake(desk)
    data = reservation(desk)
    data['phase'] = phase
    (desk.seat.folder / 'human-wake.json').write_text(json.dumps(data))
    human_wake.run(desk.seat)
    assert received(desk)
    assert desk.pastes() == [EXACT] and desk.keys() == [('Enter',)]


class Crash(BaseException):
    """The service dies at a chosen point; nothing catches it."""


def test_a_crash_left_prepared_reservation_is_provably_unsent_and_no_person_hold(desk, monkeypatch):
    """Ruling: 'prepared' is written before any key, so it reads as cancelled, never uncertain."""
    desk.order()

    def dies(self, *args, **kwargs):
        raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(Door, 'deliver', dies)
        with pytest.raises(Crash):
            human_wake.run(desk.seat)
    assert reservation(desk)['phase'] == 'prepared' and desk.tmux.typed() == []
    assert 'uncertain' not in (human_wake.status(desk.seat) or '')
    human_wake.run(desk.seat)
    assert desk.pastes() == [EXACT] and desk.keys() == [('Enter',)]
    assert received(desk)


def test_a_pre_paste_launch_change_cancels_without_keys_or_a_person_hold(desk, monkeypatch):
    """Ruling: a pre-paste blind is provably unsent and leaves no person hold."""
    desk.order()
    authorized = human_wake.authorized
    moved = replace(DESK, pid=DESK.pid + 7)

    def then_relaunched(seat, proof, text):
        allowed = authorized(seat, proof, text)
        desk.tmux.panes[DESK.id] = moved
        launch(desk.seat, moved, minutes=0)
        return allowed
    monkeypatch.setattr(human_wake, 'authorized', then_relaunched)
    human_wake.run(desk.seat)
    assert desk.tmux.typed() == []
    status = human_wake.status(desk.seat) or ''
    assert 'uncertain' not in status and 'person must intervene' not in status
    assert reservation(desk)['phase'] == 'cancelled'


@pytest.mark.parametrize('content', ['garbage', '{}', json.dumps({'format': 2})])
def test_a_malformed_reservation_holds_without_input(desk, content):
    (desk.seat.folder / 'human-wake.json').write_text(content)
    desk.order()
    reason = human_wake.run(desk.seat)
    assert reason and 'never resent' in reason and desk.tmux.typed() == []
    assert human_wake.status(desk.seat)


@pytest.mark.parametrize('hold', ['recovery', 'detached', 'watch-unreadable', 'reservation-unreadable'])
def test_every_input_hold_is_checked_before_any_record_or_screen_read(desk, monkeypatch, hold):
    """The captain's ruling: a held human seat is never read at all, only its owned metadata."""
    desk.order()
    if hold == 'recovery':
        monkeypatch.setattr(human_wake.recovery, 'held', lambda seat: 'synthetic mode unresolved')
    elif hold == 'detached':
        monkeypatch.setattr(human_wake.pending, 'detached', lambda seat: ['synthetic prior unconfirmed delivery'])
    elif hold == 'watch-unreadable':
        path = post.watch_file(desk.runtime, 'desk')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('garbage')
    else:
        (desk.seat.folder / 'human-wake.json').write_text('garbage')

    def forbidden(*args, **kwargs):
        pytest.fail('a held human seat is never read: no record, state, inputs or screen')
    for obj, name in ((desk.seat, 'join'), (desk.seat, 'record'), (desk.seat.harness, 'state'),
                      (desk.seat.harness, 'inputs'), (desk.seat.harness, 'screen'),
                      (desk.tmux, 'read_pane'), (desk.tmux, 'capture'), (desk.tmux, 'paste'), (desk.tmux, 'keys')):
        monkeypatch.setattr(obj, name, forbidden)
    for _ in range(2):
        assert human_wake.run(desk.seat), 'the hold is named'
    assert human_wake.status(desk.seat), 'and visible'
    assert desk.tmux.sent == []


@pytest.mark.parametrize('held', [False, True])
@pytest.mark.parametrize('phase', ['submitted', 'queued', 'delivered-open'])
def test_a_standing_reservation_reads_only_its_own_receipt_or_ended_state_and_a_hold_stops_even_that(
        desk, monkeypatch, phase, held):
    """The captain's ruling: after every metadata hold, a standing reservation may read only its own receipt from its
    pinned record at its original time (submitted, queued), or its own pinned record's ended state (delivered, turn
    not ended). There is no screen read, no new candidate and no input; a metadata hold stops even those reads."""
    desk.order()
    if phase == 'delivered-open':
        human_wake.run(desk.seat)
        assert received(desk) and desk.seat.harness.state(desk.record.path).turn_open is True
    else:
        desk.tmux.record = None  # the program never takes it, so no receipt will be found
        human_wake.run(desk.seat)
        assert reservation(desk)['phase'] == 'submitted'
        if phase == 'queued':
            data = reservation(desk)
            data['phase'] = 'queued'
            (desk.seat.folder / 'human-wake.json').write_text(json.dumps(data))
    desk.order('A later synthetic order')
    sent, before, states = list(desk.tmux.sent), reservation(desk)['phase'], []

    def forbidden(*args, **kwargs):
        pytest.fail('a standing reservation reads nothing but its own receipt or its own ended state')
    for obj, name in ((desk.tmux, 'read_pane'), (desk.tmux, 'capture'), (desk.tmux, 'paste'), (desk.tmux, 'keys'),
                      (desk.seat.harness, 'screen'), (desk.seat.harness, 'inputs')):
        monkeypatch.setattr(obj, name, forbidden)
    real_state = desk.seat.harness.state

    def state(record):
        states.append(Path(record))
        return real_state(record)
    monkeypatch.setattr(desk.seat.harness, 'state', state)
    if held:
        monkeypatch.setattr(human_wake.recovery, 'held', lambda seat: 'synthetic mode unresolved')
        monkeypatch.setattr(human_wake.pending.Proof, 'confirmed', forbidden)
        monkeypatch.setattr(desk.seat.harness, 'state', forbidden)
    for _ in range(2):
        human_wake.run(desk.seat)
    assert desk.tmux.sent == sent and reservation(desk)['phase'] == before, 'no input, no new candidate'
    assert all(path == desk.record.path for path in states), 'only its own pinned record'
    if held or phase != 'delivered-open':
        assert states == [], 'no state read for a receipt-only or held reservation'
    assert human_wake.status(desk.seat), 'the standing state is visible'


def test_one_human_seats_wake_failure_is_isolated_detailed_only_and_logged_once(alarms, monkeypatch):
    """The captain's ruling: an exception from one human seat's wake pass holds that seat, never the whole pass.
    Only a fixed category is logged, once; the seat's own wake state shows it in the detailed readings, and it
    neither reaches the status line nor posts a report."""
    from quarterdeck import watch
    ship, svc, d = alarms.desk.ship, alarms.svc, alarms.desk
    ship.seats['counsel'] = replace(ship.seat('desk'), name='counsel', role='counsel', persona=None)
    counsel_pane = Pane('%9', 'Office', 'counsel', 4747, 400, 40, False, False, None)
    d.tmux.panes[counsel_pane.id] = counsel_pane
    counsel = Seat(ship, ship.seat('counsel'), d.tmux)
    counsel.folder.mkdir(parents=True, exist_ok=True)
    launch(counsel, counsel_pane)  # both human seats are up, so each gets its own wake pass
    secret = 'synthetic unreadable desk pointer'
    ran, alarmed, logged = [], [], []
    real_needs_human = svc.needs_human

    def wake(seat):
        ran.append(seat.name)
        if seat.name == 'desk':
            raise OSError(secret)
        return None

    def needs_human(seat):
        alarmed.append(seat.name)
        return real_needs_human(seat)
    monkeypatch.setattr(human_wake, 'run', wake)
    monkeypatch.setattr(svc, 'needs_human', needs_human)
    monkeypatch.setattr(svc, 'log', lambda event, **fields: logged.append((event, fields)))
    for _ in range(2):
        svc.deliveries()
    assert ran.count('desk') == 2 and ran.count('counsel') == 2, 'the pass continued past the failing seat'
    assert alarmed.count('desk') == 2 and alarmed.count('counsel') == 2, 'every seat still reaches its alarm check'
    reason = 'human wake service failure (pass: I/O)'
    about = [fields for _, fields in logged if fields.get('seat') == 'desk']
    assert len(about) == 1 and about[0].get('reason') == reason, 'one fixed category, logged once'
    assert secret not in json.dumps(logged, default=str), 'no exception text is logged'
    assert secret not in (d.seat.folder / 'human-wake-status.json').read_text()
    # The public per-seat hold: the seat's wake state, the declared field qd status detail and qd watch render.
    assert human_wake.status(d.seat) == reason
    assert watch.reading(d.seat).wake == reason
    assert human_wake.status(counsel) != reason
    # Detailed only: never on the status line, and no new report.
    monkeypatch.setattr(watch, 'service_state', lambda _: 'ok')
    monkeypatch.setattr(watch, 'disks', lambda _: [])
    monkeypatch.setattr(watch.credential, 'login_needed', lambda _: [])
    facts = watch.facts(ship, d.tmux, walls=[], login=[])
    assert 'desk' not in facts.needs_human and 'service failure' not in facts.line()
    assert not [m for m in svc.store.all() if m.sender == 'ship' and m.body.startswith('desk ')]


# -- the door: nothing else ever enters a human session ----------------------------------------


def test_the_door_refuses_every_other_text_and_key_in_a_human_session(desk):
    desk.order()
    door = Door(desk.seat)
    assert door.deliver('synthetic message').result == 'withheld'
    assert door.deliver(EXACT).result == 'withheld', 'the line without its reservation'
    assert door.deliver(EXACT, human_wake=True).result == 'withheld', 'no proof'
    foreign = human_wake.pending.Proof(desk.seat.folder / 'not-the-reservation.json',
                                       {'phase': 'prepared', 'payload': EXACT})
    assert door.deliver(EXACT, proof=foreign, human_wake=True).result == 'withheld'
    assert 'no mode-control keys' in door.control_keys(('C-c',), None, None).detail
    assert 'no interrupt key' in door.interrupt()
    assert desk.tmux.typed() == []


def test_a_crew_seat_never_takes_the_human_exception(desk):
    captain = Seat(desk.ship, desk.ship.seat('captain'), desk.tmux)
    assert Door(captain).deliver(EXACT, human_wake=True).result == 'withheld'
    with pytest.raises(shipmod.ShipError):
        human_wake.run(captain)
    assert desk.tmux.sent == []


# -- status: operational metadata only, and stale evidence is unknown ---------------------------


def test_status_evidence_is_operational_only_and_never_carries_the_message(desk):
    desk.tmux.box = 'operator draft'
    desk.order()
    reason = human_wake.run(desk.seat)
    data = json.loads((desk.seat.folder / 'human-wake-status.json').read_text())
    assert set(data) == {'format', 'identity', 'source', 'reason'} and data['reason'] == reason
    assert human_wake.status(desk.seat) == reason
    for path in desk.seat.folder.rglob('*'):
        if path.is_file() and path.suffix == '.json':
            assert MARKER not in path.read_text(), path.name
    assert MARKER not in (reason or '')


def test_a_record_written_after_observation_reads_as_stale(desk):
    desk.tmux.box = 'operator draft'
    desk.order()
    human_wake.run(desk.seat)
    desk.record.begin('Synthetic later input', 'fake-1', 'medium')
    assert human_wake.status(desk.seat) == 'human wake source changed; awaiting service observation'


# -- alarms (ruling 1) ----------------------------------------------------------------------------


@pytest.fixture
def alarms(desk):
    desk.tmux.panes[SPOCK.id] = SPOCK
    spock = Seat(desk.ship, desk.ship.seat('spock'), desk.tmux)
    spock.folder.mkdir(parents=True, exist_ok=True)
    launch(spock, SPOCK)
    svc = Service(desk.ship, desk.tmux)
    seen = {m.id for m in desk.store.all()}

    def told():
        found = [m for m in svc.store.all() if m.id not in seen and m.sender == 'ship']
        seen.update(m.id for m in svc.store.all())
        return sorted((m.to, m.body) for m in found)
    yield SimpleNamespace(svc=svc, told=told, desk=desk)
    svc.ledger.close()


def test_a_work_wait_posts_no_alarm_and_the_desk_is_never_told_about_itself(alarms, monkeypatch):
    d = alarms.desk
    d.record.begin('Synthetic work in progress', 'fake-1', 'medium')  # its turn is open
    d.order()
    for _ in range(3):
        human_wake.run(d.seat)
        alarms.svc.needs_human(d.seat)
    assert alarms.told() == [], 'a wait for the seat\'s own work raises no report'
    assert d.tmux.typed() == []
    assert human_wake.status(d.seat), 'the status line still shows it'


def test_a_draft_in_the_box_shows_in_status_and_posts_no_alarm(alarms):
    d = alarms.desk
    d.tmux.box = 'operator draft'
    d.order()
    for _ in range(3):
        human_wake.run(d.seat)
        alarms.svc.needs_human(d.seat)
    assert alarms.told() == [] and d.tmux.typed() == []
    assert human_wake.status(d.seat) == 'human wake box not empty; untouched'


def test_a_person_end_state_tells_spock_once_and_never_the_obstructed_desk(alarms):
    d = alarms.desk
    confirmed_wake(d)
    d.record.end('synthetic answer without arming')
    for _ in range(3):
        human_wake.run(d.seat)
        alarms.svc.needs_human(d.seat)
    told = alarms.told()
    assert [to for to, _ in told] == ['spock']
    assert 'watch not armed' in told[0][1]


# -- qd check: pass, fail, not applicable --------------------------------------------------------


def test_not_applicable_needs_its_named_reason_and_renders_na():
    with pytest.raises(ValueError):
        drill.Check('human-session waking line', None, 'Google pool reading')
    na = drill.Check('human-session waking line', None, 'not applicable: Google pool reading')
    assert str(na) == 'N/A  human-session waking line · not applicable: Google pool reading'
    assert str(drill.Check('x', True)) == 'PASS  x' and str(drill.Check('x', False)) == 'FAIL  x'


@pytest.mark.parametrize('results,code', [([True, None], 0), ([True, True], 0), ([True, None, False], 1), ([False], 1)])
def test_check_exit_is_a_failure_only_for_a_failed_step(monkeypatch, results, code):
    monkeypatch.setattr(cli, 'load_ship', lambda args: None)
    monkeypatch.setattr(cli, 'Tmux', SimpleNamespace(default=lambda: None))
    monkeypatch.setattr(cli.check, 'check', lambda ship, name, tmux: [
        drill.Check(f'step {i}', passed, 'not applicable: synthetic' if passed is None else '')
        for i, passed in enumerate(results)])
    args = SimpleNamespace(harness='fake')
    lines = cli.cmd_check(args)
    assert args.exit_code == code and len(lines) == len(results)


@pytest.fixture
def checking(desk, monkeypatch):
    """check.check with its normal probe and its seats stood in, counting every launch it asks for."""
    from quarterdeck.harness.antigravity import AntigravityHarness

    launched, version = [], {'value': 'synthetic'}
    # The real adapters, so any reason a program's waking step is not applicable comes from the adapter itself.
    monkeypatch.setattr(AntigravityHarness, 'accepts', lambda self, pool: None)
    monkeypatch.setattr(checkmod, '_version_in_runtime', lambda harness, runtime: version['value'])
    normal = {'passed': True}
    monkeypatch.setattr(checkmod, 'probe', lambda ship, seat, orders: [drill.Check('normal contract', normal['passed'])])

    from contextlib import contextmanager

    @contextmanager
    def temporary(ship, tmux, spec, orders, purpose, *, human=False, check_inbox=False):
        assert check_inbox is True, 'every check seat is admitted to its own inbox by the check alone'
        launched.append((spec.name, human))
        seat = SimpleNamespace(name=spec.name, ship=ship, folder=ship.runtime / 'seats' / spec.name)
        seat.folder.mkdir(parents=True, exist_ok=True)
        yield seat
    monkeypatch.setattr(checkmod, 'temporary_seat', temporary)
    return SimpleNamespace(ship=desk.ship, launched=launched, normal=normal, version=version)


def test_antigravity_reports_exactly_not_applicable_and_starts_no_human_session(checking):
    checks = checkmod.check(checking.ship, 'antigravity', None)
    wake = [c for c in checks if c.name.startswith('human-session waking line')]
    assert [(c.passed, c.detail) for c in wake] == [(None, 'not applicable: Google pool reading')]
    assert [human for _, human in checking.launched] == [False]


@pytest.mark.parametrize('name', ['fake', 'antigravity'])
def test_a_failed_normal_contract_adds_no_waking_row_and_no_human_session(checking, name):
    """Ruling: the waking step runs only after the normal checks pass; the check has already failed."""
    checking.normal['passed'] = False
    checks = checkmod.check(checking.ship, name, None)
    assert [c for c in checks if c.name.startswith('human-session waking line')] == []
    assert any(c.passed is False for c in checks)
    assert [human for _, human in checking.launched] == [False]
    assert not any(c.name.startswith('proven') for c in checks)


@pytest.mark.parametrize('name', ['fake', 'antigravity'])
def test_a_missing_program_adds_no_waking_row_and_launches_nothing(checking, name):
    checking.version['value'] = None
    checks = checkmod.check(checking.ship, name, None)
    assert [(c.name, c.passed) for c in checks] == [('installed', False)]
    assert checking.launched == []


def test_the_wake_reservation_is_retained_before_teardown_and_a_failed_proof_fails(checking, monkeypatch):
    def probe_wake(seat, orders):
        (seat.folder / 'human-wake.json').write_text('{"synthetic": "reservation"}\n')
        return [drill.Check('human-session waking line: known-empty paste and record receipt', False, 'synthetic')]
    monkeypatch.setattr(checkmod, 'probe_wake', probe_wake)
    checks = checkmod.check(checking.ship, 'fake', None)
    assert [human for _, human in checking.launched] == [False, True]
    name = checking.launched[1][0]
    kept = checking.ship.runtime / 'check' / f'{name}-human-wake.json'
    assert kept.read_text() == '{"synthetic": "reservation"}\n'
    assert not any(c.name.startswith('proven') for c in checks)


@pytest.mark.parametrize('dialog', ['Help improve the model', 'Something else'])
def test_the_wake_probe_fails_on_an_account_holder_question_and_presses_nothing(desk, monkeypatch, dialog):
    monkeypatch.setattr(checkmod, 'PATIENCE', 0.9)
    monkeypatch.setattr(desk.seat, 'up', lambda resume=False: 'synthetic up')
    desk.tmux.dialog = dialog
    checks = checkmod.probe_wake(desk.seat, [])
    assert [c.passed for c in checks] == [False]
    assert desk.tmux.typed() == [], 'no choice and no dismissal'
    assert post.Store(desk.runtime).all() == [], 'no order is published before a known-empty box'


def test_the_wake_probe_passes_end_to_end_through_the_watch_and_the_answer(desk, monkeypatch):
    """The seat is woken, arms its watch, reads the order through it and answers it exactly."""
    monkeypatch.setattr(checkmod, 'PATIENCE', 15.0)
    monkeypatch.setattr(desk.seat, 'up', lambda resume=False: 'synthetic up')
    done = []

    def agent(taken):
        assert taken == EXACT

        def act():
            arrived = post.wait(desk.runtime, 'desk', interval=0.05, stop_after=10)
            post.printed(desk.runtime, arrived[1])
            token = re.search(r'with exactly this body: (\S+)', arrived[0]).group(1)
            order = next(m for m in desk.store.open_for('desk') if m.kind == 'order')
            done.append(token)
            desk.store.new('report', 'desk', order.sender, token, re=order.id)
            desk.record.end('synthetic answered')
        threading.Thread(target=act, daemon=True).start()
    desk.tmux.on_submit = agent
    checks = checkmod.probe_wake(desk.seat, [])
    assert [c.passed for c in checks] == [True, True, True, True], [str(c) for c in checks]
    assert desk.pastes() == [EXACT] and desk.keys() == [('Enter',)] and done


@pytest.mark.parametrize('outcome', ['success', 'exception', 'obstructed'])
def test_the_wake_steps_own_human_seat_retires_its_transport_and_cleans_up(tmp_path, monkeypatch, outcome):
    """The real temporary_seat on the fake harness: the wake seat is human, owned while probed,
    and retired, its session killed, its order closed and its home removed whatever happens,
    with its reservation kept as evidence first."""
    from quarterdeck import pending, seat as seatmod
    from quarterdeck.harness.fake import FakeHarness
    from tests.unit.test_check_lifetime import synthetic_ship
    from tests.unit.test_temporary_seat import Sessions

    ship, tmux = synthetic_ship(tmp_path), Sessions()
    monkeypatch.setattr(FakeHarness, 'version', lambda self: 'synthetic')
    monkeypatch.setattr(checkmod, 'probe', lambda checked, seat, orders: [drill.Check('normal contract', True)])
    seen, downs = {}, []

    def probe_wake(seat, orders):
        assert seat.session.human and seat.name.startswith('check-wake-fake-') and seat.name not in ship.seats
        with pending.transient_lock(ship.runtime, seat.name) as available:
            assert not available, 'the check owns the wake seat while it probes'
        order = post.Store(ship.runtime).new('order', 'ship', seat.name, 'Synthetic wake order')
        orders.append(order)
        (seat.folder / 'human-wake.json').write_text('{"synthetic": "wake reservation"}\n')
        seen.update(seat=seat, order=order)
        if outcome == 'exception':
            raise RuntimeError('synthetic wake exception')
        if outcome == 'obstructed':
            raise shipmod.ShipError('synthetic wake obstruction')
        return [drill.Check('human-session waking line: synthetic', True)]

    def down(seat):
        with pending.transient_lock(ship.runtime, seat.name) as available:
            assert not available, 'ownership lasts through cleanup'
        downs.append(seat.name)
        return 'synthetic stop'
    monkeypatch.setattr(checkmod, 'probe_wake', probe_wake)
    monkeypatch.setattr(seatmod.Seat, 'down', down)
    if outcome == 'exception':
        with pytest.raises(RuntimeError, match='synthetic wake exception'):
            checkmod.check(ship, 'fake', tmux)
    else:
        checks = checkmod.check(ship, 'fake', tmux)
        wake = [c for c in checks if c.name.startswith('human-session waking line')]
        proven = [c for c in checks if c.name.startswith('proven')]
        if outcome == 'success':
            assert wake and all(c.passed is True for c in wake) and len(proven) == 1
        else:
            assert [c.passed for c in wake] == [False] and 'synthetic wake obstruction' in wake[0].detail
            assert proven == []
    seat = seen['seat']
    assert len(downs) == 2 and downs[1] == seat.name, 'the normal seat, then the wake seat, each stopped'
    assert tmux.names == {'pre-existing': '$0'} and tmux.killed == ['$1', '$1'], 'both sessions retired'
    assert not seat.folder.exists(), 'the wake seat home is removed'
    with pending.transient_lock(ship.runtime, seat.name) as available:
        assert available, 'ownership is released'
    assert not post.Store(ship.runtime).is_open(seen['order']), 'its own order is closed by teardown'
    kept = ship.runtime / 'check' / f'{seat.name}-human-wake.json'
    assert kept.read_text() == '{"synthetic": "wake reservation"}\n', 'the reservation is kept before teardown'


# Captain 400c (2) and ans-0d86: the waking line must fit on one row of the program's box at the pane's present
# width, by an adapter-declared margin, before the reservation and again at the last pre-paste capture.

NEEDED = {'claude': 78, 'codex': 73, 'pi': 71, 'opencode': 77, 'grok': 80, 'fake': 72}  # len(LINE) + declared margin


@pytest.mark.parametrize('name', sorted(NEEDED))
def test_every_adapter_with_a_box_fixture_holds_one_column_narrow_and_admits_its_one_row(name):
    from quarterdeck.harness import get
    harness, needed = get(name), NEEDED[name]
    assert human_wake.width_hold(harness, SimpleNamespace(width=needed)) is None
    assert human_wake.width_hold(harness, SimpleNamespace(width=400)) is None
    assert human_wake.width_hold(harness, SimpleNamespace(width=needed - 1)) == \
        f'human wake held: pane too narrow for the waking line ({needed} columns needed)'


def test_an_adapter_that_declares_no_margin_holds_and_unknown_widths_hold():
    from quarterdeck.harness import get
    assert human_wake.width_hold(get('antigravity'), SimpleNamespace(width=400)) == \
        'human wake held: adapter box margin unknown for the waking line'
    for pane in (None, SimpleNamespace(width=None), SimpleNamespace(width=0), SimpleNamespace(width='80')):
        assert human_wake.width_hold(get('fake'), pane) == 'human wake held: pane width unknown for the waking line'


def narrowed(d, width, after_reservation=False):
    """The desk's pane at another width: at once, or only once the wake has been reserved."""
    real = d.tmux.read_pane

    def read_pane(ident, history=0, **options):
        found = real(ident, history, **options)
        if found and (not after_reservation or (d.seat.folder / 'human-wake.json').exists()):
            return replace(found[0], width=width), found[1]
        return found
    d.tmux.read_pane = read_pane


def test_a_pane_one_column_too_narrow_is_never_typed_into_and_says_why(desk):
    desk.order()
    narrowed(desk, NEEDED['fake'] - 1)
    for _ in range(2):
        said = human_wake.run(desk.seat)
        assert said == f"human wake held: pane too narrow for the waking line ({NEEDED['fake']} columns needed)"
    assert desk.tmux.typed() == [] and not (desk.seat.folder / 'human-wake.json').exists(), 'no reservation'
    assert human_wake.status(desk.seat) == said, 'the hold is named in the detailed reading'


def test_a_pane_exactly_wide_enough_takes_the_one_row_line(desk):
    desk.order()
    narrowed(desk, NEEDED['fake'])
    human_wake.run(desk.seat)
    assert desk.pastes() == [EXACT] and received(desk)


def test_a_pane_narrowed_after_the_reservation_is_caught_at_the_last_capture_and_provably_unsent(desk):
    desk.order()
    narrowed(desk, NEEDED['fake'] - 1, after_reservation=True)
    said = human_wake.run(desk.seat)
    assert said == f"human wake held: pane too narrow for the waking line ({NEEDED['fake']} columns needed)"
    assert desk.tmux.typed() == [], 'nothing pasted, nothing pressed'
    data = reservation(desk)
    assert data['phase'] == 'cancelled' and data['attempts'] == [] and data['shown'] is None, 'reserved, never pasted'


# Ans-0d86 (3): Claude Code 2.1.317 wraps a pasted line at width - 4. Two retained captures: a wrapped paste that
# matches at 76 and not at the inherited 78, and folded pastes that match exactly as before.

def claude_door(text, width=80):
    from quarterdeck.harness.claude import ClaudeHarness
    seat = SimpleNamespace(harness=ClaudeHarness(), tmux=SimpleNamespace(capture=lambda *a, **k: text),
                           pane=lambda: SimpleNamespace(id='%1', width=width, dead=False),
                           session=SimpleNamespace(human=False), spec=SimpleNamespace(), home=None)
    return Door(seat)


def claude_capture(name):
    from tests.conftest import FIXTURES
    return (FIXTURES / 'claude' / '2.1.317' / f'{name}.txt').read_text()


def test_claude_wraps_a_shown_paste_at_width_less_four_not_less_two(monkeypatch):
    from quarterdeck.harness.claude import ClaudeHarness
    three = ("qd > order from desk · ord-7f3a · Spike\nAnswer MARKER-LOADED if you read a CLAUDE.md marker, else NO-MARKER. "
             "Then run exactly: qd --version\nqd > reply: qd post report --re ord-7f3a · inbox: post/inbox/captain.md")
    door = claude_door(claude_capture('paste-3-lines-shown'))
    shown = door.screen()
    assert ClaudeHarness().box_columns(80) == 76
    assert door.matches(ClaudeHarness().box(three), shown), 'matched at the measured 76'
    monkeypatch.setattr(ClaudeHarness, 'box_columns', lambda self, width: width - 2)
    assert not door.matches(ClaudeHarness().box(three), shown), 'unmatched at the inherited 78'


@pytest.mark.parametrize('capture,text', [
    ('paste-4-lines-folded', "qd > order from desk · ord-9abc · Four\na\nb\nqd > reply: qd post report --re ord-9abc"),
    ('paste-long-one-line-folded', "qd probe A: " + " ".join(f"word{i}" for i in range(260))),
])
@pytest.mark.parametrize('columns', [76, 78])
def test_claude_folded_pastes_match_unchanged_at_either_width(capture, text, columns, monkeypatch):
    from quarterdeck.harness.claude import ClaudeHarness
    monkeypatch.setattr(ClaudeHarness, 'box_columns', lambda self, width: columns)
    door = claude_door(claude_capture(capture))
    assert door.matches(ClaudeHarness().box(text), door.screen())
    if capture == 'paste-4-lines-folded':  # a placeholder carries its line count, and only that
        assert not door.matches(ClaudeHarness().box(text + '\nadded'), door.screen())


# Captain ans-6b9f: Claude's 2-column right inset (columns width-4 to width-2 of a row, after the 2-column prompt)
# is painted padding: spaces there are dropped, any other character fails. Text never reaches past width - 4.

INSET_PAYLOADS = ['keep  both spaces\nlast line', 'first\n\nafter a blank row', 'keep  both spaces\n\n  preserve indentation\nlast line']


def claude_rows(payload, paint, width=80, inset=None):
    rows = [('❯ ' if i == 0 else '  ') + row for i, row in enumerate(payload.split('\n'))]
    if inset is not None:  # a character placed in the inset of the last box row
        rows[-1] = rows[-1].ljust(width - 2) + inset
    framed = ['─' * width, *[row.ljust(paint) for row in rows], '─' * width]
    return '\n'.join(framed) + '\n  ○ low · /effort\n'


@pytest.mark.parametrize('payload', INSET_PAYLOADS)
@pytest.mark.parametrize('paint', [78, 79, 80])
def test_claude_rows_painted_to_the_text_area_or_to_the_edge_verify_blank_rows_included(payload, paint):
    from quarterdeck.harness.claude import ClaudeHarness
    door = claude_door(claude_rows(payload, paint))
    assert door.matches(ClaudeHarness().box(payload + '\n'), door.screen()), paint


@pytest.mark.parametrize('payload', INSET_PAYLOADS)
@pytest.mark.parametrize('inset', ['x', ' x', 'xx', '─ '])  # the screen reader already reads a no-break space as a space
def test_a_non_space_in_claudes_right_inset_fails(payload, inset):
    from quarterdeck.harness.claude import ClaudeHarness
    door = claude_door(claude_rows(payload, 80, inset=inset))
    assert not door.matches(ClaudeHarness().box(payload + '\n'), door.screen()), repr(inset)


def test_a_space_only_inset_is_padding_but_never_supplies_a_text_difference():
    from quarterdeck.harness.claude import ClaudeHarness
    payload = 'keep  both spaces\nlast line'
    door = claude_door(claude_rows(payload, 80))
    for edited in ('keep both spaces\nlast line', 'keep  both spaces\nlast lin', 'keep  both spaces\nlast line  x'):
        assert not door.matches(ClaudeHarness().box(edited + '\n'), door.screen()), edited


# Captain 400c (3) and dfe6 (7): the check keeps, under runtime/check and before teardown, the Door's first delivery
# result and the box as shown beside the final observation, and the woken seat's own last tool calls, bounded.

def test_the_first_door_result_and_the_box_as_shown_are_observed_once(desk):
    desk.order()
    seen = []
    human_wake.run(desk.seat, observe_delivery=lambda delivery, shown: seen.append((delivery.result, shown)))
    assert seen == [('delivered', EXACT)], 'the first delivery and the box as the program showed it'
    human_wake.run(desk.seat, observe_delivery=lambda delivery, shown: seen.append((delivery.result, shown)))
    assert len(seen) == 1, 'a standing wake is not delivered again'


def evidence(d) -> tuple[dict, dict]:
    folder = d.runtime / 'check'
    return (json.loads((folder / 'desk-wake-observation.json').read_text()),
            json.loads((folder / 'desk-tool-evidence.json').read_text()))


def test_wake_evidence_keeps_the_first_delivery_beside_the_final_observation_in_the_runtime(desk):
    desk.order()
    human_wake.run(desk.seat)
    first = dict(result='blind', detail='d' * 256, shown='s' * 256, shown_truncated=True)
    checkmod.retain_wake_evidence(desk.seat, dict(reason='synthetic final reason', first_delivery=first))
    observation, tools = evidence(desk)
    assert observation['first_delivery'] == first and observation['seat'] == 'desk'
    final = observation['final_observation']
    assert final['reason'] == 'synthetic final reason' and final['phase'] == reservation(desk)['phase'] == 'delivered'
    assert final['watch_armed'] is False and 'first_delivery' not in final
    assert tools == dict(status='unknown', reason='adapter tool call/results unsupported', pairs=[]), \
        'an adapter with no decoder says unknown; nothing is invented'
    for path in (desk.runtime / 'check').iterdir():
        assert path.resolve().is_relative_to(desk.runtime.resolve()), 'runtime only, never the repository'
        assert MARKER not in path.read_text(), 'no waiting order body or subject'


def test_an_oversized_observation_is_replaced_by_a_named_unknown(desk):
    checkmod.retain_wake_evidence(desk.seat, dict(reason='x' * 9000, first_delivery=None))
    observation, _ = evidence(desk)
    assert observation['first_delivery'] is None
    assert observation['final_observation'] == dict(reason='waking observation exceeded 8 KiB bound; unknown')


def test_tool_evidence_is_read_only_from_the_seats_own_pinned_record(desk, tmp_path):
    from quarterdeck.harness.claude import ClaudeHarness
    read = []
    desk.seat.harness.check_tool_evidence = lambda record: read.append(record) or dict(status='known', reason='r', pairs=[])
    elsewhere = ClaudeShape(tmp_path / 'elsewhere', desk.ship.workspace, 'synthetic-foreign-record', 'fake-1')
    elsewhere.begin('Synthetic foreign request', 'fake-1', 'medium')
    (desk.seat.folder / 'record').write_text(f'{elsewhere.path}\n')
    checkmod.retain_wake_evidence(desk.seat, dict(reason='synthetic', first_delivery=None))
    assert read == [] and evidence(desk)[1]['status'] == 'unknown', 'a record outside the seat home is never read'
    (desk.seat.folder / 'record').write_text(f'{desk.record.path}\n')
    checkmod.retain_wake_evidence(desk.seat, dict(reason='synthetic', first_delivery=None))
    assert read == [desk.record.path] and evidence(desk)[1]['status'] == 'known', 'its own pinned record, once'
    desk.tmux.panes[DESK.id] = replace(DESK, pid=DESK.pid + 1)  # the pane no longer belongs to the launch
    checkmod.retain_wake_evidence(desk.seat, dict(reason='synthetic', first_delivery=None))
    assert read == [desk.record.path] and evidence(desk)[1]['status'] == 'unknown'


def test_claude_tool_evidence_keeps_only_the_last_eight_pairs_and_names_its_truncation(tmp_path):
    from quarterdeck.harness.claude import ClaudeHarness
    record, rows = tmp_path / 'record-tamarack.jsonl', []
    for i in range(10):
        command = ('y' * 2000) if i == 9 else f'synthetic step {i}'
        rows.append(dict(type='assistant', message=dict(role='assistant', content=[dict(
            type='tool_use', id=f'call-tamarack-{i}', name='Bash', input=dict(command=command))])))
        rows.append(dict(type='user', message=dict(role='user', content=[dict(
            type='tool_result', tool_use_id=f'call-tamarack-{i}', content=f'synthetic result {i}', is_error=False)])))
    record.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    found = ClaudeHarness().check_tool_evidence(record)
    assert [pair['id'] for pair in found['pairs']] == [f'call-tamarack-{i}' for i in range(2, 10)]
    last = found['pairs'][-1]
    assert last['call_truncated'] is True and len(last['call']) == 1024 and found['status'] == 'partial'
    assert len(json.dumps(found, ensure_ascii=True).encode()) <= 32768


def test_the_probe_keeps_its_evidence_when_it_raises_and_an_evidence_failure_fails_the_check(desk, monkeypatch):
    def raising(seat, orders, observed):
        observed['first_delivery'] = dict(result='withheld', detail='synthetic', shown=None, shown_truncated=False)
        raise RuntimeError('synthetic probe failure')
    monkeypatch.setattr(checkmod, '_probe_wake', raising)
    with pytest.raises(RuntimeError):
        checkmod.probe_wake(desk.seat, [])
    observation, _ = evidence(desk)
    assert observation['final_observation']['probe_outcome_unknown'] is True
    assert observation['first_delivery']['result'] == 'withheld', 'kept beside the unknown outcome'

    monkeypatch.setattr(checkmod, '_probe_wake', lambda seat, orders, observed: [checkmod.Check('synthetic step', True, '')])
    monkeypatch.setattr(checkmod, 'retain_wake_evidence', lambda seat, observed: (_ for _ in ()).throw(OSError('full')))
    checks = checkmod.probe_wake(desk.seat, [])
    assert [(c.name, c.passed) for c in checks] == [('synthetic step', True),
                                                   ('human-session waking line: runtime evidence retained', False)]
