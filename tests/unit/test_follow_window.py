"""A seat that starts working is shown in its session; never in a human's session, over a human, or by a resize."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace

import pytest

from quarterdeck import service, ship as shipmod
from quarterdeck.seat import Launched, Seat
from quarterdeck.tmux import Pane, SessionView, TmuxError


class FakeTmux:
    def __init__(self, ship):
        self.ship = ship
        self.panes = {}
        self.owner = str(ship.folder)
        self.views = {}
        self.selected = []
        self.refuse_selection = False

    def pane(self, ident):
        return self.panes.get(ident)

    def session_environment(self, session, name):
        assert name == 'QD_SHIP'
        return self.owner

    def session_view(self, session):
        return self.views.get(session)

    def select_window(self, pane_id, *, expected_pid):
        if self.refuse_selection:
            raise TmuxError(f'pane {pane_id} changed process identity; window not shown')
        self.selected.append((pane_id, expected_pid))


@pytest.fixture
def crew(drydock, monkeypatch, tmp_path):
    ship = shipmod.load(drydock)
    ship.seats['worker-2'] = replace(ship.seat('worker-1'), name='worker-2')
    tmux = FakeTmux(ship)
    turns, seats = {}, {}
    for index, name in enumerate(['worker-1', 'worker-2', 'desk', 'captain'], start=1):
        seat = Seat(ship, ship.seat(name), tmux)
        seat.folder.mkdir(parents=True, exist_ok=True)
        seat.trusted.mkdir(parents=True, exist_ok=True)
        pane = Pane(f'%{index}', seat.session.title, name, 100 + index, 120, 40, False, False, None)
        tmux.panes[pane.id] = pane
        Launched(pane.id, pane.pid, datetime.now(timezone.utc).isoformat(), seat.spec.pools[0], None,
                 ['fake'], None, 'fake').write(seat.trusted / 'launch.json')
        record = tmp_path / f'{name}.record'
        record.write_text('')
        seat.record = (lambda record=record: record)
        turns[record] = False
        seats[name] = seat
    states = {}

    def state(record):
        value = turns[record]
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(turn_open=value)
    for seat in seats.values():
        monkeypatch.setattr(seat.harness, 'state', state)
    monkeypatch.setattr(service, 'seats', lambda *args: list(seats.values()))
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    svc = service.Service(ship, tmux)
    crew_view = SessionView({'@1': (True, 120, 40), '@2': (False, 120, 40)}, {'%1': '@1', '%2': '@2'}, False, ())
    tmux.views['Crew'] = crew_view

    def turn(name, value):
        record = seats[name].record()
        turns[record] = value
        record.write_text(record.read_text() + 'x')  # the record changed, as a program writing a step does

    def logged():
        path = svc.log_path
        lines = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [(entry['event'], entry['seat'], entry.get('reason')) for entry in lines
                if entry['event'] in ('followed', 'not followed')]
    try:
        yield SimpleNamespace(svc=svc, tmux=tmux, seats=seats, turn=turn, logged=logged, view=crew_view)
    finally:
        svc.ledger.close()


def test_a_seat_that_starts_working_is_shown_once(crew):
    crew.svc.follow()  # baseline: every seat idle
    assert crew.tmux.selected == []
    crew.turn('worker-2', True)
    crew.svc.follow()
    assert crew.tmux.selected == [('%2', 102)]
    crew.svc.follow()
    crew.turn('worker-2', True)
    crew.svc.follow()
    assert crew.tmux.selected == [('%2', 102)], 'still working is no new start'
    crew.turn('worker-2', False)
    crew.svc.follow()
    crew.turn('worker-2', True)
    crew.svc.follow()
    assert crew.tmux.selected == [('%2', 102)] * 2, 'a turn after an idle reading is a new start'
    assert crew.logged() == [('followed', 'worker-2', None)] * 2


def test_the_first_reading_is_a_baseline_not_a_start(crew):
    crew.turn('worker-2', True)
    crew.svc.follow()
    assert crew.tmux.selected == [] and crew.logged() == []


def test_an_uncertain_reading_is_no_transition(crew):
    crew.svc.follow()
    crew.turn('worker-2', None)
    crew.svc.follow()
    crew.turn('worker-2', OSError('record unreadable'))
    crew.svc.follow()
    assert crew.tmux.selected == []
    crew.turn('worker-2', True)
    crew.svc.follow()
    assert crew.tmux.selected == [('%2', 102)], 'the last known idle reading still frames the start'


def test_a_seat_in_the_humans_session_is_never_followed(crew):
    assert crew.seats['desk'].session.human
    crew.svc.follow()
    crew.turn('desk', True)
    crew.svc.follow()
    assert crew.tmux.selected == [] and crew.logged() == []
    assert 'desk' not in crew.svc.working, 'the human session is not even read for following'


def test_the_window_already_shown_needs_no_switch(crew):
    crew.svc.follow()
    crew.turn('worker-1', True)
    crew.svc.follow()
    assert crew.tmux.selected == [] and crew.logged() == [('followed', 'worker-1', None)]


@pytest.mark.parametrize('case,reason', [
    ('scrolling', 'a human is scrolling in the session'),
    ('typing', 'a human is typing in the session'),
    ('resize', 'showing it would resize its window'),
    ('foreign session', "session 'Crew' is not this ship's"),
    ('moved pane', "its pane is in session 'Elsewhere', not 'Crew'"),
    ('no pane', 'no live pane of its launch'),
    ('unreadable', 'session unreadable'),
    ('identity', 'pane %2 changed process identity; window not shown'),
])
def test_refusals_leave_the_session_as_it_is(crew, case, reason):
    crew.svc.follow()
    view = crew.view
    if case == 'scrolling':
        crew.tmux.views['Crew'] = replace(view, scrolling=True)
    elif case == 'typing':
        crew.tmux.views['Crew'] = replace(view, inputs=(int(time.time()) - 2,))
    elif case == 'resize':
        windows = dict(view.windows, **{'@2': (False, 100, 30)})
        crew.tmux.views['Crew'] = replace(view, windows=windows, inputs=(int(time.time()) - 600,))
    elif case == 'foreign session':
        crew.tmux.owner = '/some/other/ship'
    elif case == 'moved pane':
        crew.tmux.panes['%2'] = replace(crew.tmux.panes['%2'], session='Elsewhere')
    elif case == 'no pane':
        crew.tmux.panes['%2'] = replace(crew.tmux.panes['%2'], pid=999)  # law: identity
    elif case == 'unreadable':
        crew.tmux.views['Crew'] = None
    else:
        crew.tmux.refuse_selection = True
    crew.turn('worker-2', True)
    crew.svc.follow()
    assert crew.tmux.selected == []
    assert crew.logged() == [('not followed', 'worker-2', reason)]


def test_a_quiet_client_and_an_equal_size_allow_the_switch(crew):
    crew.svc.follow()
    crew.tmux.views['Crew'] = replace(crew.view, inputs=(int(time.time()) - service.TYPING_QUIET - 5,))
    crew.turn('worker-2', True)
    crew.svc.follow()
    assert crew.tmux.selected == [('%2', 102)]


def test_with_no_client_attached_a_different_size_is_no_resize(crew):
    crew.svc.follow()
    windows = dict(crew.view.windows, **{'@2': (False, 100, 30)})
    crew.tmux.views['Crew'] = replace(crew.view, windows=windows, inputs=())
    crew.turn('worker-2', True)
    crew.svc.follow()
    assert crew.tmux.selected == [('%2', 102)]


def test_an_unchanged_record_is_not_read_again(crew, monkeypatch):
    crew.svc.follow()
    reads = []
    original = crew.seats['worker-2'].harness.state
    monkeypatch.setattr(crew.seats['worker-2'].harness, 'state', lambda record: reads.append(record) or original(record))
    crew.svc.follow()
    crew.svc.follow()
    assert reads == [], 'the working state is read only when a record changes'
