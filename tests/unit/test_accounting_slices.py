"""A real low-priority planning child, bounded commits, and unchanged replay results."""

from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext
import os
import threading
import time

import pytest

from quarterdeck import accounting_slices, quota, record_owner, service, watch
from quarterdeck.accounting_slices import Driver
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.ledger import Ledger
from quarterdeck.service import Service
from quarterdeck.ship import Pool, Ship


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


def test_deliveries_and_spent_pool_move_do_not_wait_for_backlog(tmp_path, monkeypatch):
    svc, seat, _, _ = world(tmp_path, monkeypatch, 300)
    steps = ('server_recovery', 'reconcile', 'groups', 'cascades', 'inboxes', 'recoveries', 'refusals',
             'answers', 'looks', 'refresh_usage', 'follow', 'requests', 'compactions', 'person_quiet',
             'fresh_after_order', 'rebrief', 'credentials', 'authentications', 'model_differences')
    for name in steps:
        monkeypatch.setattr(svc, name, lambda: None)
    svc.status_due = lambda: None
    moved, delivered = [], []
    svc.deliveries = lambda: delivered.append(time.monotonic())
    svc.not_idle = lambda seat: None
    svc.ship.pools.update({name: Pool(name, 'synthetic', endpoint='http://synthetic.invalid')
                           for name in ('spent', 'available')})
    seat.ship, seat.pool, seat.is_up = svc.ship, svc.ship.pools['spent'], True
    seat.spec = SimpleNamespace(pools=('spent', 'available'))
    seat.failover_to = lambda target: moved.append((target, time.monotonic(), svc.account_driver.active))
    monkeypatch.setattr(service.recovery, 'held', lambda seat: None)
    monkeypatch.setattr(quota, 'current_walled', lambda seat, book: True)
    monkeypatch.setattr(accounting_slices, 'BUDGET', 0.02)
    svc._accounting_tick = True
    start = time.monotonic()
    try:
        svc._tick_steps()
        assert len(delivered) == len(moved) == 1
        target, at, backlog = moved[0]
        assert delivered[0] - start < 0.1
        assert target == 'available' and backlog
        assert at - delivered[0] < 0.5
        assert svc.ledger.catching_up()
        assert str(watch.tokens_today(seat)) == 'unknown'
        assert not svc.ledger.db.in_transaction
    finally:
        svc.account_driver.close()
        svc.ledger.close()


def test_changed_reader_identity_discards_old_plans_then_retries(tmp_path, monkeypatch):
    svc, seat, record, _ = world(tmp_path, monkeypatch, 20)
    driver = Driver()
    monkeypatch.setattr(accounting_slices, 'BUDGET', 0.0005)
    try:
        driver.step(svc)
        assert driver.active and svc.ledger.ingested(record) == 0
        # A new registration changes the source binding after the child snapshot.
        with record_owner.registry(seat.home.parent).open('a') as stream:
            stream.write('\n')
        monkeypatch.setattr(accounting_slices, 'BUDGET', 0.02)
        finish(driver, svc)
        assert svc.ledger.catching_up()
        assert svc.ledger.ingested(record) == 0
        finish(driver, svc)
        assert svc.ledger.ingested(record) == 20
        assert not svc.ledger.catching_up()
    finally:
        driver.close()
        svc.ledger.close()


def test_heartbeat_continues_inside_a_long_service_step(tmp_path, monkeypatch):
    from quarterdeck import shutdown
    svc, _, _, _ = world(tmp_path, monkeypatch)
    beat = threading.Event()
    monkeypatch.setattr(service, 'acquire_lock', lambda runtime: None)
    monkeypatch.setattr(service.servicectl, 'acknowledge', lambda *args: None)
    monkeypatch.setattr(service, 'write_heartbeat', lambda runtime: beat.set())
    monkeypatch.setattr(shutdown, 'Stop', lambda *args: SimpleNamespace(installed=nullcontext))
    class EndProof(Exception):
        pass
    def long_step():
        assert beat.wait(5), 'a step does not have to return before its heartbeat is written'
        raise EndProof
    svc.tick = long_step
    try:
        with pytest.raises(EndProof):
            svc.run()
        assert not any(t.name == 'service-heartbeat' for t in threading.enumerate())
    finally:
        svc.ledger.close()


def test_restart_resumes_committed_prefix_without_duplicate_turns(tmp_path, monkeypatch):
    svc, seat, record, _ = world(tmp_path, monkeypatch, 1000)
    driver = Driver()
    monkeypatch.setattr(accounting_slices, 'BUDGET', 0.0005)
    try:
        until = time.monotonic() + 20
        while svc.ledger.ingested(record) == 0 and time.monotonic() < until:
            driver.step(svc)
            time.sleep(0.005)
        assert 0 < svc.ledger.ingested(record) < 1000
        assert svc.ledger.catching_up()
        assert not svc.ledger.db.in_transaction
    finally:
        driver.close()
        svc.ledger.close()
    restarted = Service(svc.ship, SimpleNamespace())
    restarted.resolved_seats = lambda: [seat]
    restarted.heartbeat = lambda: None
    restarted.log = lambda *args, **kwargs: None
    restarted.publish_accounting_health = lambda **kwargs: None
    driver = Driver()
    monkeypatch.setattr(accounting_slices, 'BUDGET', 0.02)
    try:
        finish(driver, restarted)
        assert restarted.ledger.ingested(record) == 1000
        assert restarted.ledger.db.execute('SELECT COUNT(*) FROM tool_calls').fetchone() == (1000,)
        assert not restarted.ledger.catching_up()
    finally:
        driver.close()
        restarted.ledger.close()
