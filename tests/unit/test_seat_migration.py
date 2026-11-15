"""The move, once per seat: a seat's old files are taken into its trusted folder only where live state confirms them.

On a throwaway drydock and a private tmux server. A seat that was up before the move has a window tmux started with
``sh runtime/seats/<seat>/launch.sh``; its old launch record, in the jail-writable seat folder, is a claim. It is taken
only when it names that very window, the pane and pid then come from tmux, and everything else either touches the
seat alone or is rebuilt. Names only: no content is printed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import alarms, seat_migration, trusted_files, ship as shipmod
from quarterdeck.seat import LaunchUnreadable, Seat, process_start


@pytest.fixture
def ship(drydock: Path) -> shipmod.Ship:
    loaded = shipmod.load(drydock)
    shipmod.fence(loaded)
    return loaded


def old_folder(ship, name: str) -> Path:
    folder = ship.runtime / 'seats' / name
    (folder / 'home').mkdir(parents=True, exist_ok=True)
    return folder


def old_window(ship, tmux, name: str):
    """A window started for the seat as the earlier code started it: sh runtime/seats/<seat>/launch.sh."""
    folder = old_folder(ship, name)
    (folder / 'launch.sh').write_text('#!/bin/sh\nexec sleep 300\n')
    if not tmux.has_session('Crew'):
        tmux.new_session('Crew')
    return tmux.new_window('Crew', name, ship.folder, ['sh', str(folder / 'launch.sh')])


def other_window(tmux):
    """A live window started for no seat: what a forged record might name."""
    if not tmux.has_session('Crew'):
        tmux.new_session('Crew')
    return tmux.new_window('Crew', 'elsewhere', Path('/'), ['sleep', '300'])


def old_launch(ship, name: str, pane: str, pid: int, **fields) -> dict:
    data = dict(pane=pane, pid=pid, at=datetime.now(timezone.utc).isoformat(), pool='fake', resume=None,
                argv=['fake'], version=None, harness='fake', records_before=[], **fields)
    (old_folder(ship, name) / 'launch.json').write_text(json.dumps(data))
    return data


def evidence(ship, name: str) -> Path:
    found = sorted((ship.runtime / 'evidence' / 'seat-move').glob(f'{name}-*'))
    assert len(found) == 1, found
    return found[0]


def test_a_launch_confirmed_by_its_own_live_window_is_taken_with_tmuxs_pane_and_pid(ship, tmux):
    pane = old_window(ship, tmux, 'worker-1')
    old_launch(ship, 'worker-1', pane.id, pane.pid)

    outcome = seat_migration.migrate(ship, ship.seat('worker-1'), tmux)

    assert outcome['launch.json'] == 'taken: confirmed by its live window'
    seat = Seat(ship, ship.seat('worker-1'), tmux)
    launched = seat.launched()
    assert (launched.pane, launched.pid) == (pane.id, pane.pid) and launched.start == process_start(pane.pid)
    assert seat.is_up
    assert not (seat.folder / 'launch.json').exists(), 'the old copy is set aside'
    assert (evidence(ship, 'worker-1') / 'launch.json').exists(), 'unchanged, in the evidence'
    assert seat_migration.migrate(ship, ship.seat('worker-1'), tmux) == {}, 'once'
    tmux.kill_window(pane.id, expected_pid=pane.pid)


def test_a_record_naming_another_live_window_holds_the_seat_and_qd_down_closes_only_its_own(ship, tmux):
    own = old_window(ship, tmux, 'worker-1')
    other = other_window(tmux)
    old_launch(ship, 'worker-1', other.id, other.pid)  # a jail's forgery: another live pane and its pid

    outcome = seat_migration.migrate(ship, ship.seat('worker-1'), tmux)

    assert outcome['launch.json'].startswith('held: its old launch record names another window')
    seat = Seat(ship, ship.seat('worker-1'), tmux)
    with pytest.raises(LaunchUnreadable, match='this seat is held, no other'):
        seat.launched()
    words = seat.down()
    assert 'hold the files move left is lifted' in words
    assert tmux.pane(own.id) is None, "the seat's own window is closed"
    assert tmux.pane(other.id) is not None, 'the window the forgery named is never touched'
    assert seat.launched() is None
    tmux.kill_window(other.id, expected_pid=other.pid)


def test_with_no_live_window_the_seat_reads_down_and_nothing_of_its_launch_is_taken(ship, tmux):
    other = other_window(tmux)
    old_launch(ship, 'worker-1', other.id, other.pid)

    outcome = seat_migration.migrate(ship, ship.seat('worker-1'), tmux)

    assert outcome['launch.json'] == 'set aside: no live window of the seat'
    assert Seat(ship, ship.seat('worker-1'), tmux).launched() is None
    tmux.kill_window(other.id, expected_pid=other.pid)


def test_a_record_naming_a_pool_not_declared_for_the_seat_holds_it(ship, tmux):
    pane = old_window(ship, tmux, 'worker-1')
    old_launch(ship, 'worker-1', pane.id, pane.pid)
    path = old_folder(ship, 'worker-1') / 'launch.json'
    path.write_text(json.dumps({**json.loads(path.read_text()), 'pool': 'claude-primary'}))

    outcome = seat_migration.migrate(ship, ship.seat('worker-1'), tmux)

    assert outcome['launch.json'] == 'held: its old launch record names a pool not declared for it'
    tmux.kill_window(pane.id, expected_pid=pane.pid)


def test_registries_selection_pointer_and_holds_keep_only_what_is_the_seats_own(ship, tmux):
    pane = old_window(ship, tmux, 'worker-1')
    launch = old_launch(ship, 'worker-1', pane.id, pane.pid)
    folder = old_folder(ship, 'worker-1')
    own_record = folder / 'home' / 'own.jsonl'
    other_record = ship.runtime / 'seats' / 'captain' / 'home' / 'theirs.jsonl'
    rows = [dict(record=str(own_record), at=launch['at'], pool='fake', harness='fake', price=None),
            dict(record=str(other_record), at=launch['at'], pool='fake', harness='fake', price=None),
            dict(record=str(own_record), at=launch['at'], pool='claude-primary', harness='fake', price=None),
            dict(record=str(folder / 'home' / '..' / '..' / 'captain' / 'home' / 'x'), at=launch['at'],
                 pool='fake', harness='fake', price=None)]
    (folder / 'record-launches.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
    (folder / 'record-readers.jsonl').write_text(json.dumps(dict(record=str(own_record), harness='fake')) + '\n'
                                                + json.dumps(dict(record=str(other_record), harness='fake')) + '\n')
    (folder / 'record').write_text(str(own_record) + '\n')
    (folder / 'pool').write_text('fake\n')
    (folder / 'pool-explicit').write_text('claude-primary\n')
    (folder / 'retiring.json').write_text(json.dumps(dict(pid=1, start='x')))
    (folder / 'human-alert.json').write_text('{"reports": []}')
    (folder / 'compaction.json').write_text('{"intents": []}')
    (folder / 'refusal.json').write_text('{"events": []}')
    (folder / 'move-hold.json').write_text('{}')

    outcome = seat_migration.migrate(ship, ship.seat('worker-1'), tmux)

    trusted = trusted_files.folder(ship.runtime, 'worker-1')
    kept = [json.loads(line) for line in (trusted / 'record-launches.jsonl').read_text().splitlines()]
    assert kept == rows[:1], 'only its own record, on a pool declared for it'
    readers = [json.loads(line) for line in (trusted / 'record-readers.jsonl').read_text().splitlines()]
    assert readers == [dict(record=str(own_record), harness='fake')]
    assert (trusted / 'record').read_text().strip() == str(own_record), 'bound to the confirmed launch'
    assert (trusted / 'pool').read_text().strip() == 'fake' and not (trusted / 'pool-explicit').exists()
    assert (trusted / 'retiring.json').exists(), "a hold on the seat alone"
    for name in ('human-alert.json', 'compaction.json', 'refusal.json', 'move-hold.json'):
        assert not (trusted / name).exists(), f'{name} is the service\'s to write afresh'
        assert outcome[name] == 'set aside'
        assert (evidence(ship, 'worker-1') / name).exists()
    assert json.loads((evidence(ship, 'worker-1') / 'outcome.json').read_text()) == outcome
    tmux.kill_window(pane.id, expected_pid=pane.pid)


def test_where_windows_cannot_be_read_nothing_moves_and_a_running_seat_is_never_read_down(ship):
    folder = old_folder(ship, 'worker-1')
    old_launch(ship, 'worker-1', '%1', os.getpid())
    blind = SimpleNamespace(pane=lambda pane_id: None)  # no reading of the windows' start commands

    with pytest.raises(shipmod.ShipError, match='its windows cannot be read'):
        seat_migration.migrate(ship, ship.seat('worker-1'), blind)

    assert (folder / 'launch.json').exists() and not seat_migration.done(ship.runtime, 'worker-1')


def test_after_the_move_a_file_in_the_old_place_is_never_read(ship, tmux):
    seat_migration.migrate(ship, ship.seat('worker-1'), tmux)
    pane = other_window(tmux)
    old_launch(ship, 'worker-1', pane.id, pane.pid)  # written later, as a jail could
    (old_folder(ship, 'worker-1') / 'pool').write_text('claude-primary\n')

    seat = Seat(ship, ship.seat('worker-1'), tmux)
    assert seat.launched() is None and not seat.is_up and seat.pool.name == 'fake'
    assert seat_migration.migrate(ship, ship.seat('worker-1'), tmux) == {}, 'considered once, never again'
    tmux.kill_window(pane.id, expected_pid=pane.pid)


def test_every_alarm_journal_is_a_moved_file():
    """A journal added to alarms.FILES is written in the trusted folder (alarms.path); its old copy is set aside too."""
    assert set(trusted_files.ALARMS) == set(alarms.FILES.values())
    assert set(trusted_files.ALARMS) <= set(trusted_files.MOVED)
