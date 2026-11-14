"""A real low-priority planning child, bounded commits, and unchanged replay results."""

from pathlib import Path
from types import SimpleNamespace
import os
import time

import pytest

from quarterdeck import accounting_slices, record_owner, service
from quarterdeck.accounting_slices import Driver
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.ledger import Ledger
from quarterdeck.service import Service
from quarterdeck.ship import Ship


def world(tmp_path, monkeypatch, count=1):
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {}, {}, {})
    home = ship.runtime / 'seats' / 'worker' / 'home'
    writer = ClaudeShape(home, tmp_path / 'workspace', 'synthetic-session', 'synthetic-model')
    for _ in range(count):
        writer.begin('synthetic input', 'synthetic-model', 'high')
        writer.tool('synthetic-tool', 'synthetic argument', 'synthetic result', False)
        writer.end('synthetic output')
    record_owner.register(home.parent, writer.path, 'fake')
    reader = FakeHarness()
    seat = SimpleNamespace(name='worker', home=home, harness=reader, session=SimpleNamespace(human=False),
                           pane=lambda: None, launched=lambda: None, record=lambda: writer.path)
    svc = Service(ship, SimpleNamespace())
    pulses, notices = [], []
    svc.resolved_seats = lambda: [seat]
    svc.heartbeat = lambda: pulses.append(time.monotonic())
    svc.log = lambda *args, **kwargs: notices.append((args, kwargs))
    svc.publish_accounting_health = lambda **kwargs: None
    monkeypatch.setattr(record_owner, 'records', lambda s: [record_owner.Owned(writer.path, 'fake', reader)])
    return svc, seat, writer.path, pulses


def finish(driver, svc, timeout=20):
    times = []
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        before = time.monotonic()
        driver.step(svc)
        times.append(time.monotonic() - before)
        assert not svc.ledger.db.in_transaction, 'each slice releases its write transaction'
        if not driver.active:
            assert svc.accounting_health.failures == 0
            return times
        time.sleep(0.005)
    pytest.fail('planning child did not finish')


def rows(book):
    return {table: book.db.execute(f'SELECT * FROM {table} ORDER BY record,turn').fetchall()
            for table in ('turns', 'tool_calls', 'turn_costs', 'turn_attribution', 'record_accounting', 'attribution_records')
            if table not in ('record_accounting', 'attribution_records')}


def test_child_plan_matches_full_replay_and_releases_every_slice(tmp_path, monkeypatch):
    svc, seat, record, pulses = world(tmp_path, monkeypatch, 300)
    reference = Service(svc.ship, SimpleNamespace())
    reference.ledger.close()
    reference.ledger = Ledger(tmp_path / 'reference.sqlite')
    reference.resolved_seats = svc.resolved_seats
    reference.account_records(source_records={'worker': record_owner.records(seat)}, source_screens={})
    expected = rows(reference.ledger)
    reference.ledger.close()
    driver = Driver()
    monkeypatch.setattr(accounting_slices, 'BUDGET', 0.02)
    try:
        durations = finish(driver, svc)
        assert len(durations) > 1
        assert max(durations) < 0.5, 'native parsing and replay must not block the service'
        assert os.getpriority(os.PRIO_PROCESS, driver.process.pid) >= 10
        assert rows(svc.ledger) == expected
        assert not svc.ledger.catching_up()
        assert pulses, 'long apply loops refresh the heartbeat'
    finally:
        driver.close()
        svc.ledger.close()
    assert driver.process is None


def test_dead_child_retries_without_counting_partial_work_twice(tmp_path, monkeypatch):
    svc, _, record, _ = world(tmp_path, monkeypatch, 20)
    driver = Driver()
    try:
        driver.step(svc)
        driver.process.kill()
        driver.process.wait(timeout=2)
        driver.step(svc)
        assert svc.ledger.catching_up()
        finish(driver, svc)
        assert svc.ledger.ingested(record) == 20
        assert svc.ledger.db.execute('SELECT COUNT(*) FROM tool_calls').fetchone() == (20,)
    finally:
        driver.close()
        svc.ledger.close()
