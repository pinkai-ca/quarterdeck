"""A link a seat's jail plants where its home belongs is refused (seat_files.LinkRefused) when the service joins
its record, and the join reads it as the refusal it is: named, never raised. Every link is planted in the test's own
throwaway seat folder, and the decoy it points at is checked unchanged."""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import recovery, seat_files, trusted_files
from quarterdeck.seat import Seat
from quarterdeck.ship import Pool, SeatSpec, Session, Ship

REFUSED = 'a link where a folder belongs in the seat folder'


def inventory(folder: Path) -> dict:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()}


@pytest.fixture
def world(tmp_path):
    seats = {name: SeatSpec(name, 'bridge', 'worker', 'codex', ('synthetic',), cwd=tmp_path)
             for name in ('builder-synthetic', 'sibling-synthetic')}
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {'synthetic': Pool('synthetic', 'codex')},
                {'bridge': Session('bridge', 'Synthetic')}, seats)
    made = {}
    for name in seats:
        seat = Seat(ship, ship.seat(name), None)
        seat.folder.mkdir(parents=True)
        seat.trusted.mkdir(parents=True, exist_ok=True)
        made[name] = seat
    return SimpleNamespace(ship=ship, seats=made, tmp=tmp_path)


def plant(world, name):
    """The jail's link where its home belongs, to a decoy outside the seat; the pin names a record through it."""
    seat = world.seats[name]
    outside = world.tmp / f'outside-{name}'
    (outside / 'sessions').mkdir(parents=True)
    (outside / 'conversations.owned').write_text('decoy line\n')
    (outside / 'sessions' / 'record.jsonl').write_text('{"decoy": true}\n')
    seat.home.symlink_to(outside, target_is_directory=True)
    (seat.trusted / trusted_files.RECORD).write_text(str(seat.home / 'sessions' / 'record.jsonl') + '\n')
    return outside, inventory(outside)


@pytest.mark.parametrize('publish', [True, False])
def test_a_join_through_a_planted_home_link_is_named_never_raised(world, publish):
    seat = world.seats['builder-synthetic']
    outside, before = plant(world, seat.name)
    joined = seat._pinned_join(publish=publish)
    assert joined is not None and joined.record is None
    assert joined.reason.startswith('record reader unknown: ') and REFUSED in joined.reason
    assert joined.reason.endswith(f'inspect with qd watch {seat.name}')
    assert inventory(outside) == before, 'nothing written through the link'


def test_a_sibling_behind_a_planted_link_cannot_certify_shared_state(world, monkeypatch):
    monkeypatch.setattr(Seat, 'is_up', property(lambda seat: True))
    outside, before = plant(world, 'sibling-synthetic')
    assert world.seats['builder-synthetic']._shared_state_contended() is True
    assert inventory(outside) == before


def test_the_thinking_recovery_pass_logs_a_refusal_and_goes_on_to_the_next_seat(world, monkeypatch):
    seen, logs = [], []

    def one(service, seat, pending):
        seen.append(seat.name)
        if seat.name == 'builder-synthetic':
            raise seat_files.LinkRefused(f'{seat.home}: {REFUSED}; nothing written through it')

    monkeypatch.setattr(recovery, '_seat', one)
    service = SimpleNamespace(resolved_seats=lambda: list(world.seats.values()),
                              log=lambda event, **data: logs.append((event, data)))
    recovery.run(service)  # never raised out of the pass
    assert seen == ['builder-synthetic', 'sibling-synthetic'], 'the next seat is still read'
    [(event, data)] = logs
    assert event == 'error' and data['step'] == 'thinking-recovery' and REFUSED in data['error']


def test_a_first_join_found_through_a_planted_home_link_is_refused_never_raised(world, monkeypatch):
    """join(): the program's finder reads the home through the link and names a record there; noting it as owned
    (make_folder of the home) refuses the link, and the join with it: that record is not this seat's own."""
    import json
    from datetime import datetime, timezone
    from quarterdeck.harness.base import Join
    seat = world.seats['builder-synthetic']
    outside = world.tmp / 'outside-first'
    (outside / 'sessions').mkdir(parents=True)
    (outside / 'sessions' / 'record.jsonl').write_text('{"decoy": true}\n')
    before = inventory(outside)
    seat.home.symlink_to(outside, target_is_directory=True)
    launch = dict(pane='%1', pid=123, at=datetime.now(timezone.utc).isoformat(), pool='synthetic', argv=['codex'],
                  harness='codex', resume=None, version='synthetic')
    (seat.trusted / trusted_files.LAUNCH).write_text(json.dumps(launch))
    monkeypatch.setattr(type(seat.harness), 'join',
                        lambda self, home, when, earlier: Join(home / 'sessions' / 'record.jsonl'))
    joined = seat.join()  # never raised out of the join
    assert joined.record is None and joined.reason.startswith('record ownership refused: ') and REFUSED in joined.reason
    assert inventory(outside) == before, 'nothing written through the link'
