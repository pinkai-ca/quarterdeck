"""A seat's tokens today: the Ledger's zero is read as a count where its records are accounted, and only a record it
has not accounted is read for today's turns, never every record the seat ever owned.

Synthetic fake-program records, accounted by the service's own pass; record reads counted. No program runs.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import record_owner, ship as shipmod, watch
from quarterdeck.harness import json_record, synthetic_record
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.seat import Seat
from quarterdeck.service import Service

pytestmark = pytest.mark.usefixtures('synthetic_disk_room')


@pytest.fixture
def seat(tmp_path, monkeypatch):
    """A down seat with five records, every turn of them yesterday's."""
    pool = shipmod.Pool('fake', 'synthetic')
    spec = shipmod.SeatSpec('worker', 'crew', 'worker', 'fake', ('fake',), up=False)
    ship = shipmod.Ship(tmp_path, 'synthetic', 'operator', tmp_path, {'fake': pool},
                        {'crew': shipmod.Session('crew', 'Synthetic')}, {'worker': spec})
    made = Seat(ship, spec, SimpleNamespace(pane=lambda ident: None))
    monkeypatch.setattr(synthetic_record, 'now', lambda: datetime.now(timezone.utc) - timedelta(days=1))
    made.writers = []
    more(made, tmp_path, 5)
    return made


def more(seat, tmp_path, count):
    """Owned records, each with one turn of yesterday's; the newest is the seat's current record."""
    for number in range(len(seat.writers), len(seat.writers) + count):
        writer = synthetic_record.ClaudeShape(seat.home, tmp_path, f'synthetic-{number}', 'synthetic-model')
        writer.begin('synthetic input', 'synthetic-model', 'high')
        writer.end('synthetic output')
        record_owner.register(seat.trusted, writer.path, 'fake')
        seat.writers.append(writer)
    (seat.trusted / 'record').write_text(str(seat.writers[-1].path))


def account(seat):
    svc = Service(seat.ship, seat.tmux)
    svc.resolved_seats = lambda: [seat]
    try:
        svc.account_records()
    finally:
        svc.ledger.close()


class Reads(list):
    """The records read, in order, beside whether each read was made in passing."""


@pytest.fixture
def reads(monkeypatch):
    found = Reads()
    found.passing = []  # whether each read was made in passing (json_record.passing): the cache admits none of them
    native = FakeHarness.read

    def read(self, record):
        found.append(record)
        found.passing.append(json_record._PASSING.get())
        return native(self, record)
    monkeypatch.setattr(FakeHarness, 'read', read)
    return found


def test_a_seat_whose_ledger_knows_zero_today_reads_no_record(seat, reads):
    account(seat)
    reads.clear()
    reads.passing.clear()
    today = watch.tokens_today(seat)
    assert (today.known, today.unmeasured) == (0, 0)
    assert reads == [], 'a counted zero reads no record'


def test_one_record_the_ledger_has_not_accounted_is_the_only_one_read(seat, reads, tmp_path):
    """The trace's seat: down, with 59 owned records, one of them empty and never accounted."""
    more(seat, tmp_path, 53)  # 58 records with turns, accounted
    account(seat)
    empty = seat.home / 'synthetic-empty.jsonl'
    empty.write_text('')  # an empty record, owned after the accounting pass: not accounted at its reader
    record_owner.register(seat.trusted, empty, 'fake')
    assert len(record_owner.records(seat, publish=False)) == 59
    reads.clear()
    reads.passing.clear()
    today = watch.tokens_today(seat)
    assert today.known == 0
    assert reads == [empty], 'the unaccounted record alone, never every record the seat owned'
    assert reads.passing == [True], 'read in passing: the record cache keeps no sweep'


def test_a_turn_of_today_in_a_record_not_yet_accounted_is_counted(seat, reads, monkeypatch):
    account(seat)
    monkeypatch.setattr(synthetic_record, 'now', lambda: datetime.now(timezone.utc))
    seat.writers[0].begin('synthetic new input', 'synthetic-model', 'high')
    seat.writers[0].end('synthetic new output')
    reads.clear()
    reads.passing.clear()
    today = watch.tokens_today(seat)
    assert reads == [seat.writers[0].path], 'the record not yet accounted, alone'
    day = datetime.now().date().isoformat()
    _, turns = FakeHarness().read(seat.writers[0].path)
    turn = [t for t in turns if (t.ended or t.started).astimezone().date().isoformat() == day]
    assert len(turn) == 1 and turn[0].tokens.total > 0
    assert today.known == turn[0].tokens.total, "exactly that turn's tokens: the ledger's zero holds none of today's"


def test_with_no_accounting_at_all_every_record_is_still_read(seat, reads):
    reads.clear()
    reads.passing.clear()
    assert watch.tokens_today(seat).known == 0
    assert sorted(reads) == sorted(writer.path for writer in seat.writers), 'nothing proves the zero: read as before'
