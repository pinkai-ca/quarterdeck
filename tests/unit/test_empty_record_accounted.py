"""A record read whole as no turns is accounted like any other; a reading that leaves its turn state unknown is not.

One synthetic empty record per reader form, owned by a down fake seat and taken through the service's own accounting
pass. Codex, Claude Code and pi read an empty JSON-lines record as no turns with no turn open, as the fake reads its
own: a complete reading of nothing. Grok Build reads one with its turn state unknown, and OpenCode's record is an
SQLite database, which an empty file is not: neither is accounted. No program runs.
"""
from types import SimpleNamespace

import pytest

from quarterdeck import record_owner, ship as shipmod
from quarterdeck.seat import Seat
from quarterdeck.service import Service

pytestmark = pytest.mark.usefixtures('synthetic_disk_room')


@pytest.mark.parametrize('reader,suffix,accounted', [
    ('codex', '.jsonl', True), ('claude', '.jsonl', True), ('pi', '.jsonl', True), ('fake', '.jsonl', True),
    ('grok', '.jsonl', False), ('opencode', '.db', False)])
def test_an_empty_record_is_accounted_only_where_it_reads_as_a_complete_nothing(tmp_path, reader, suffix, accounted):
    pool = shipmod.Pool('fake', 'synthetic')
    spec = shipmod.SeatSpec('worker', 'crew', 'worker', 'fake', ('fake',), up=False)
    ship = shipmod.Ship(tmp_path, 'synthetic', 'operator', tmp_path, {'fake': pool},
                        {'crew': shipmod.Session('crew', 'Synthetic')}, {'worker': spec})
    seat = Seat(ship, spec, SimpleNamespace(pane=lambda ident: None))
    seat.home.mkdir(parents=True, exist_ok=True)
    empty = seat.home / f'synthetic-empty{suffix}'
    empty.write_text('')
    record_owner.register(seat.trusted, empty, reader)
    (seat.trusted / 'record').write_text(str(empty))
    svc = Service(ship, seat.tmux)
    svc.resolved_seats = lambda: [seat]
    try:
        svc.account_records()
        certified = {row[0] for row in svc.ledger.db.execute('SELECT record FROM record_accounting')}
        assert (str(empty) in certified) is accounted
        assert svc.ledger.ingested(empty) == 0, 'no turn is written for it either way'
    finally:
        svc.ledger.close()
