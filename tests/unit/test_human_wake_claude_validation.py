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

EXACT = ('qd > ship: messages are waiting for you. Run `qd inbox --wait` now and follow what it prints; '
         'if your program can keep a command running in the background, keep it running there.')
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
    assert human_wake.LINE == EXACT
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


def test_one_human_seats_wake_failure_is_isolated_and_logged_once(alarms, monkeypatch):
    """The captain's ruling: an exception from one human seat's wake pass holds that seat, never the whole pass."""
    ship, svc = alarms.desk.ship, alarms.svc
    ship.seats['counsel'] = replace(ship.seat('desk'), name='counsel', role='counsel', persona=None)
    counsel_pane = Pane('%9', 'Office', 'counsel', 4747, 400, 40, False, False, None)
    alarms.desk.tmux.panes[counsel_pane.id] = counsel_pane
    counsel = Seat(ship, ship.seat('counsel'), alarms.desk.tmux)
    counsel.folder.mkdir(parents=True, exist_ok=True)
    launch(counsel, counsel_pane)  # both human seats are up, so each gets its own wake pass
    ran, alarmed, logged = [], [], []
    real_needs_human = svc.needs_human

    def wake(seat):
        ran.append(seat.name)
        if seat.name == 'desk':
            raise OSError('synthetic unreadable desk pointer')
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
    about = [entry for entry in logged if 'desk' in json.dumps(entry, default=str)
             and 'synthetic unreadable desk pointer' in json.dumps(entry, default=str)]
    assert len(about) == 1, 'logged once by reason'


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
    def temporary(ship, tmux, spec, orders, purpose, *, human=False):
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
