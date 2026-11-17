"""A real low-priority planning child, bounded commits, and unchanged replay results."""

from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext
import os
import signal
import threading
import time

import pytest

from quarterdeck import trusted_files
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
    record_owner.register(trusted_files.of_home(home), writer.path, 'fake')
    reader = FakeHarness()
    seat = SimpleNamespace(name='worker', home=home, trusted=trusted_files.of_home(home), harness=reader, session=SimpleNamespace(human=False),
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
    until = None if timeout is None else time.monotonic() + timeout
    while until is None or time.monotonic() < until:
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
        until = time.monotonic() + 60  # step until the child exists, the event this needs; a safety bound only
        while driver.process is None and time.monotonic() < until:
            driver.step(svc)
        assert driver.process is not None, 'the planning child never started'
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
    svc, seat, record, _ = world(tmp_path, monkeypatch, 300)
    steps = ('server_recovery', 'reconcile', 'groups', 'cascades', 'inboxes', 'recoveries', 'refusals',
             'answers', 'looks', 'refresh_usage', 'follow', 'requests', 'compactions', 'person_quiet',
             'fresh_after_order', 'rebrief', 'push_reminders', 'credentials', 'authentications', 'model_differences')
    for name in steps:
        monkeypatch.setattr(svc, name, lambda: None)
    svc.status_due = lambda: None
    moved, delivered = [], []
    delivery_seen, move_seen, released = (threading.Event() for _ in range(3))
    def delivery():
        assert not released.is_set() and svc.ledger.catching_up()
        delivered.append(True)
        delivery_seen.set()
    svc.deliveries = delivery
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
    def failover(target):
        assert delivery_seen.is_set() and not released.is_set()
        moved.append((target, svc.ledger.catching_up()))
        move_seen.set()
    seat.failover_to = failover
    monkeypatch.setattr(service.recovery, 'held', lambda seat: None)
    monkeypatch.setattr(quota, 'current_walled', lambda seat, book: True)
    monkeypatch.setattr(accounting_slices, 'BUDGET', 0.02)
    svc._accounting_tick = True
    start, blocked = Driver.start, []
    def stop_before_request(driver, service):
        result = start(driver, service)
        if not blocked:
            # start() has queued the request but pump() has not sent it yet.
            # The stop event therefore precedes any possible finished plan.
            pid = driver.process.pid
            os.kill(pid, signal.SIGSTOP)
            assert os.WIFSTOPPED(os.waitpid(pid, os.WUNTRACED)[1])
            blocked.append(pid)
        return result
    monkeypatch.setattr(Driver, 'start', stop_before_request)
    try:
        while not blocked:
            native_account()
            if not blocked:
                time.sleep(0.005)
        assert svc.account_driver.active
        assert svc.ledger.ingested(record) == 0
        svc._tick_steps()
        assert len(delivered) == len(moved) == 1
        target, backlog = moved[0]
        assert delivery_seen.is_set() and move_seen.is_set()
        assert not released.is_set()
        assert target == 'available' and backlog
        assert operational == ['push', 'signin', 'account']
        assert svc.ledger.catching_up()
        assert str(watch.tokens_today(seat)) == 'unknown'
        assert not svc.ledger.db.in_transaction
        released.set()
        os.kill(blocked[0], signal.SIGCONT)
        finish(svc.account_driver, svc, timeout=None)
        assert svc.ledger.ingested(record) == 300
        assert not svc.ledger.catching_up()
    finally:
        if blocked and not released.is_set():
            try:
                os.kill(blocked[0], signal.SIGCONT)
            except ProcessLookupError:
                pass
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
            with record_owner.registry(seat.trusted).open('a') as stream:
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
        # Step until the child exists, the event this test needs: a step bounded by the small budget may end
        # before Driver.start on a busy machine. The bound is a safety stop far above any stall, not a timing.
        until = time.monotonic() + 60
        while driver.process is None and time.monotonic() < until:
            driver.step(svc)
        assert driver.process is not None, 'the planning child never started'
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
    record_owner.register(trusted_files.of_home(home), writer.path, 'fake')
    other = SimpleNamespace(name='synthetic-other', home=home, trusted=trusted_files.of_home(home), harness=FakeHarness(),
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
            with record_owner.registry(changed.trusted).open('a') as stream:
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
                with record_owner.registry(other.trusted).open('a') as stream:
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


def test_planner_finishes_while_the_busy_parent_does_not_read_results(tmp_path, monkeypatch):
    svc, _, record, _ = world(tmp_path, monkeypatch, 1000)
    driver = Driver()
    monkeypatch.setattr(accounting_slices, 'BUDGET', 0.02)
    try:
        ingested = svc.ledger.ingested(record)
        pread = os.pread
        def unread_plan(fd, count, offset):
            if driver.plan is not None and fd == driver.plan.fileno():
                return b''
            return pread(fd, count, offset)
        # A budgeted step may end before sending even this small request. Let
        # the service send it, while withholding only its reads of the plan.
        with monkeypatch.context() as setup:
            setup.setattr(os, 'pread', unread_plan)
            while True:
                driver.step(svc, wait=False)
                if driver.process is not None and not driver.outgoing:
                    break
                time.sleep(0.005)
        assert driver.plan_offset == 0 and not driver.incoming and not driver.ready, 'setup reads no plan'
        assert svc.ledger.ingested(record) == ingested, 'setup applies no planned writes'
        done = accounting_slices.frame(('done',))
        # No driver pump, drain or accounting call: another duty owns the parent.
        while True:
            size = os.fstat(driver.plan.fileno()).st_size
            if size >= len(done) and os.pread(driver.plan.fileno(), len(done), size - len(done)) == done:
                break
            time.sleep(0.005)
        assert size > 65536, 'the plan must exceed a pipe-sized output buffer'
        assert svc.ledger.ingested(record) == ingested, 'only the service applies planned writes'
        finish(driver, svc, timeout=None)
        assert svc.ledger.ingested(record) == 1000
        assert not svc.ledger.catching_up()
        first_size = os.fstat(driver.plan.fileno()).st_size
        finish(driver, svc, timeout=None)
        assert os.fstat(driver.plan.fileno()).st_size < first_size, 'completed jobs reclaim their plan file'
        assert svc.ledger.ingested(record) == 1000
    finally:
        driver.close()
        svc.ledger.close()
    assert driver.plan is None
    assert not list(svc.runtime.glob('tmp*')), 'the anonymous plan has no retained path'


def test_progress_is_logged_after_committed_record_groups(tmp_path, monkeypatch):
    from collections import deque
    import tempfile
    svc, seat, _, _ = world(tmp_path, monkeypatch)
    driver = Driver()
    driver.active = True
    driver.started = time.monotonic()
    driver.job = 1
    driver.plan = tempfile.TemporaryFile(dir=svc.runtime)
    driver.identities = {seat.name: driver.identity(svc.runtime, seat.name)}
    driver.watched = {seat.name}
    driver.ready = deque([('context', str(tmp_path / f'synthetic-record-{n}'), [seat.name]) for n in range(205)]
                         + [('context', None, []), ('done',)])
    monkeypatch.setattr(driver, 'pump', lambda: False)
    logs = []
    def logged(event, **fields):
        assert not svc.ledger.db.in_transaction, 'progress must describe committed work'
        logs.append((event, fields))
    svc.log = logged
    try:
        driver.step(svc)
        progress = [data for event, data in logs if event == 'accounting-job-progress']
        assert len(progress) == 1
        assert progress[0]['records'] == progress[0]['unique_records'] == 205
        assert progress[0]['job'] == 1
        assert [event for event, _ in logs] == ['accounting-job-progress', 'accounting-job-end']
    finally:
        driver.close()
        svc.ledger.close()


def test_empty_interstep_drain_does_not_wait_or_start_a_new_job(tmp_path, monkeypatch):
    svc, _, _, _ = world(tmp_path, monkeypatch)
    driver = Driver()
    try:
        driver.drain(svc)
        assert driver.process is None
        driver.active = True
        driver.started = time.monotonic()
        monkeypatch.setattr(driver, 'pump', lambda: False)
        started = time.monotonic()
        driver.drain(svc)
        assert time.monotonic() - started < 0.2
        assert not svc.ledger.db.in_transaction
        assert driver.process is None and driver.active
    finally:
        driver.close()
        svc.ledger.close()
