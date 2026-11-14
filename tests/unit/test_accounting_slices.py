"""A real low-priority planning child, bounded commits, and unchanged replay results."""

from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext
import os
import signal
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
             'fresh_after_order', 'rebrief', 'push_reminders', 'credentials', 'authentications', 'model_differences')
    for name in steps:
        monkeypatch.setattr(svc, name, lambda: None)
    svc.status_due = lambda: None
    moved, delivered = [], []
    svc.deliveries = lambda: delivered.append(time.monotonic())
    svc.not_idle = lambda seat: None
    operational = []
    svc.push_reminders = lambda: operational.append('push')
    svc.authentications = lambda: operational.append('signin')
    native_account = svc.account
    def account():
        operational.append('account')
        return native_account()
    svc.account = account
    svc.ship.pools.update({name: Pool(name, 'synthetic', endpoint='http://synthetic.invalid')
                           for name in ('spent', 'available')})
    seat.ship, seat.pool, seat.is_up = svc.ship, svc.ship.pools['spent'], True
    seat.spec = SimpleNamespace(pools=('spent', 'available'))
    seat.failover_to = lambda target: moved.append((target, time.monotonic(), svc.ledger.catching_up()))
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
        assert operational == ['push', 'signin', 'account']
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
    starts = []
    start = Driver.start

    def change_identity_after_snapshot(driver, service):
        starts.append(driver.reset_child)
        if len(starts) == 2:
            # The invalid snapshot was discarded before the retry was planned.
            assert svc.ledger.ingested(record) == 0
            assert svc.ledger.catching_up()
        start(driver, service)
        if len(starts) == 1:
            # Change the binding after the job and its identities are captured,
            # regardless of how much work a single service slice can finish.
            with record_owner.registry(seat.home.parent).open('a') as stream:
                stream.write('\n')

    monkeypatch.setattr(Driver, 'start', change_identity_after_snapshot)
    monkeypatch.setattr(accounting_slices, 'BUDGET', 0.02)
    try:
        until = time.monotonic() + 20
        while svc.ledger.catching_up():
            finish(driver, svc, timeout=max(0, until - time.monotonic()))
        assert starts == [False, True], 'the invalid plan causes one reset retry'
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


def test_completed_native_turn_precedes_commit_when_planning_child_is_paused(tmp_path, monkeypatch):
    svc, seat, record, _ = world(tmp_path, monkeypatch)
    driver = Driver()
    start = Driver.start
    def paused(driver, service):
        start(driver, service)
        os.kill(driver.process.pid, signal.SIGSTOP)
        assert os.WIFSTOPPED(os.waitpid(driver.process.pid, os.WUNTRACED)[1])
    monkeypatch.setattr(Driver, 'start', paused)
    monkeypatch.setattr(accounting_slices, 'BUDGET', 0.03)
    try:
        turns = seat.harness.turns(record)
        assert len(turns) == 1 and turns[0].ended is not None
        driver.step(svc)
        assert driver.active and not svc.accounting_health.checked
        assert svc.ledger.rollup('seat') == []
        # Releasing exactly this child, rather than sleeping, makes the same
        # native turn eventually available with its usage and tool count.
        os.kill(driver.process.pid, signal.SIGCONT)
        monkeypatch.setattr(Driver, 'start', start)
        finish(driver, svc, timeout=60)
        row, = svc.ledger.rollup('seat')
        assert row.key == seat.name and row.turns == 1 and row.read_cached > 0 and row.tool_calls == 1
        assert not svc.ledger.catching_up()
    finally:
        if driver.process is not None:
            os.kill(driver.process.pid, signal.SIGCONT)
        driver.close()
        svc.ledger.close()


@pytest.mark.parametrize('when', ['start', 'completion'])
def test_identity_retry_keeps_stable_seats_and_never_reopens_history(tmp_path, monkeypatch, when):
    svc, changed, record, _ = world(tmp_path, monkeypatch, 10)
    home = svc.runtime / 'seats' / 'synthetic-other' / 'home'
    writer = ClaudeShape(home, tmp_path / 'workspace', 'other-session', 'synthetic-model')
    for _ in range(10):
        writer.begin('synthetic input', 'synthetic-model', 'high')
        writer.end('synthetic output')
    record_owner.register(home.parent, writer.path, 'fake')
    other = SimpleNamespace(name='synthetic-other', home=home, harness=FakeHarness(),
                            session=SimpleNamespace(human=False), pane=lambda: None,
                            launched=lambda: None, record=lambda: writer.path)
    svc.resolved_seats = lambda: [changed, other]
    sources = {changed.name: record, other.name: writer.path}
    monkeypatch.setattr(record_owner, 'records', lambda s: [
        record_owner.Owned(sources[s.name], 'fake', s.harness)])
    forced = []
    begin = svc.ledger.begin_catchup
    def begin_catchup(epoch, *, force=False):
        forced.append(force)
        return begin(epoch, force=force)
    monkeypatch.setattr(svc.ledger, 'begin_catchup', begin_catchup)
    notices = []
    svc.log = lambda event, **fields: notices.append((event, fields))
    starts, contexts = [], []
    class Measured(Driver):
        touched = False
        def touch(self):
            with record_owner.registry(changed.home.parent).open('a') as stream:
                stream.write('\n')
            self.touched = True
        def start(self, service):
            starts.append(([s['name'] for s in self.seats], self.scope))
            contexts.append(set())
            super().start(service)
            if len(starts) == 1 and when == 'start':
                self.touch()
            elif len(starts) == 2:
                # Unrelated churn must not invalidate the affected seat's retry.
                with record_owner.registry(other.home.parent).open('a') as stream:
                    stream.write('\n')
        def pump(self):
            before = len(self.ready)
            result = super().pump()
            for packet in list(self.ready)[before:]:
                if packet[0] == 'context' and packet[1]:
                    contexts[-1].add(Path(packet[1]))
                if when == 'completion' and not self.touched and packet[0] == 'sql' and any(
                        'accounting_progress' in sql for sql, *_ in packet[1]):
                    self.touch()
            return result
    monkeypatch.setattr(accounting_slices, 'BUDGET', 0.02)
    driver = Measured()
    try:
        finish(driver, svc)
        assert driver.touched
        assert svc.ledger.catching_up(), 'retirement stays held until the affected work is accepted'
        assert svc.ledger.ingested(writer.path) == 10, 'stable-seat work remains committed'
        if when == 'start':
            assert svc.ledger.ingested(record) == 0, 'the stale plan is still rejected'
        finish(driver, svc)
        assert not svc.ledger.catching_up()
        assert svc.ledger.ingested(record) == svc.ledger.ingested(writer.path) == 10
        assert forced == [], 'identity churn must never force a global catch-up epoch'
        assert starts == [([changed.name, other.name], None), ([changed.name], {changed.name})]
        assert contexts == [{record, writer.path}, {record}], 'retry never replays the stable seat'
        assert [e for e, _ in notices].count('accounting-job-start') == 2
        ends = [data for event, data in notices if event == 'accounting-job-end']
        assert len(ends) == 2 and ends[0]['pending'] and not ends[1]['pending']
        assert ends[0]['records'] == 2 and ends[1]['records'] == 1
        assert all(data['seconds'] >= 0 and data['counts']['done'] == 1 for data in ends)
        resets = [data for event, data in notices if event == 'accounting-job-reset']
        assert resets == [dict(job=1, seats=[changed.name])]
    finally:
        driver.close()
        svc.ledger.close()


def test_scoped_replay_includes_owned_records_with_another_recorded_seat(tmp_path, monkeypatch):
    svc, seat, record, _ = world(tmp_path, monkeypatch)
    driver = Driver()
    try:
        finish(driver, svc)
        visited = []
        with svc.ledger.record_pass() as book:
            book.attributions.replay_stream(svc.store, svc.runtime, svc.accounting_epoch,
                                            lambda path, names: visited.append((path, names)), lambda: None,
                                            seats={'synthetic-new-owner'}, records={str(record)})
        assert visited == [(str(record), [seat.name])]
    finally:
        driver.close()
        svc.ledger.close()
