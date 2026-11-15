"""Real native readers and planner child: scoped rows equal a frozen full job."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from quarterdeck import accounting_gate, post, record_owner, ship as shipmod
from quarterdeck.accounting_slices import Driver
from quarterdeck.harness import synthetic_record
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.ledger import Ledger
from quarterdeck.service import Service
from .test_accounting_slices import finish


def all_rows(book):
    result = {}
    tables = book.db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    for table, in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        values = sorted(book.db.execute('SELECT * FROM ' + quoted).fetchall(), key=repr)
        result[table] = json.dumps(values, ensure_ascii=False, separators=(',', ':'),
                                   default=lambda value: value.hex()).encode()
    return result


@pytest.mark.parametrize('overlap', [False, True])
def test_scoped_whole_inventory_equals_full_job_in_every_ledger_row(tmp_path, monkeypatch, overlap):
    ship = shipmod.Ship(tmp_path, 'ordinary', 'operator', tmp_path, {}, {}, {})
    ship.file.write_text('[ship]\nname="ordinary"\ncode="checkout"\n')
    pool = shipmod.Pool('ordinary-pool', 'synthetic', endpoint='http://ordinary.invalid')
    ship.pools[pool.name] = pool
    origin = datetime(2040, 1, 1, tzinfo=timezone.utc)
    at = [origin]
    monkeypatch.setattr(synthetic_record, 'now', lambda: at[0])
    future = time.time_ns() + 10_000_000_000
    monkeypatch.setattr(accounting_gate.time, 'time_ns', lambda: future)
    reader = FakeHarness()
    owners, selected, writers = {}, [], {}
    def create(name, label, start, end):
        home = ship.runtime / 'seats' / name / 'home'
        writer = ClaudeShape(home, tmp_path / 'workspace', label, 'ordinary-model')
        at[0] = origin + timedelta(minutes=start)
        writer.begin('Ordinary synthetic work.', 'ordinary-model', 'high')
        if end is not None:
            at[0] = origin + timedelta(minutes=end)
            writer.end('Ordinary synthetic result.')
        record_owner.register(home.parent, writer.path, 'fake')
        owners.setdefault(name, []).append(record_owner.Owned(writer.path, 'fake', reader))
        return writer
    older = create('ordinary-alpha', 'ordinary-older', 0, 10)
    for i in range(64):
        create('ordinary-alpha', f'ordinary-history-{i:03}', 100 + 20 * i, 110 + 20 * i)
    active = create('ordinary-alpha', 'ordinary-active', 4 if overlap else 3000, None if overlap else 3010)
    beta = create('ordinary-beta', 'ordinary-other-seat', 5000, 5010)
    for name, current in [('ordinary-alpha', active), ('ordinary-beta', beta)]:
        home = current.path.parents[2]
        spec = shipmod.SeatSpec(name, 'ordinary-session', 'worker', 'fake', (pool.name,))
        selected.append(SimpleNamespace(name=name, home=home, harness=reader, spec=spec, pool=pool,
                        session=SimpleNamespace(human=False), pane=lambda: None, launched=lambda: None,
                        record=lambda path=current.path: path))
        writers[name] = current
    svc = Service(ship, SimpleNamespace())
    svc.resolved_seats = lambda: selected
    svc.heartbeat = lambda: None
    logs = []
    svc.log = lambda event, **data: logs.append((event, data))
    svc.publish_accounting_health = lambda **kwargs: None
    monkeypatch.setattr(record_owner, 'records', lambda seat: owners[seat.name])
    if overlap:
        monkeypatch.setattr(post, 'now', lambda: origin - timedelta(minutes=20))
        order = svc.store.new('order', 'ordinary-supervisor', 'ordinary-alpha', 'Ordinary synthetic order.')
        monkeypatch.setattr(post, 'now', lambda: origin + timedelta(minutes=5))
        svc.store.new('answer', 'ordinary-alpha', 'ordinary-supervisor', 'Ordinary synthetic progress.', re=order.id)
    driver = Driver()
    try:
        finish(driver, svc)
        assert driver.gate.partitions.baseline is not None
        baseline = svc.ledger.db.serialize()
        if overlap:
            assert svc.ledger.db.execute('SELECT status FROM turn_attribution WHERE record=?',
                                        (str(older.path),)).fetchone() == ('attributed',)
            at[0] = origin + timedelta(minutes=6)
            active.end('Ordinary closure overlapping the old post.')
        else:
            at[0] = origin + timedelta(minutes=3020)
            active.begin('Ordinary new disjoint turn.', 'ordinary-model', 'high')
            at[0] = origin + timedelta(minutes=3030)
            active.end('Ordinary new result.')
        previous = driver.job
        finish(driver, svc)
        assert driver.job == previous + 1 and driver.scope == {'ordinary-alpha'}
        start = [data for event, data in logs if event == 'accounting-job-start'][-1]
        assert start['scope'] == ['ordinary-alpha']
        assert start['sources'] == len(owners['ordinary-alpha'])
        assert start['sources'] > 1, 'the entire affected seat, not just its changed native file'
        scoped = all_rows(svc.ledger)
        if overlap:
            assert svc.ledger.db.execute('SELECT status FROM turn_attribution WHERE record=?',
                                        (str(older.path),)).fetchone() == ('unresolved',)
        driver.close()
        svc.ledger.close()
        (ship.runtime / 'ledger.sqlite').write_bytes(baseline)
        svc.ledger = Ledger(ship.runtime / 'ledger.sqlite')
        reference = Driver()
        try:
            finish(reference, svc)
            assert reference.scope is None
            assert all_rows(svc.ledger) == scoped
        finally:
            reference.close()
    finally:
        driver.close()
        svc.ledger.close()
