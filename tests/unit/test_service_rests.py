"""Unchanged presentation rests; changed hints request fresh facts, never an action."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import service as servicemod, service_status, service_wait, ship as shipmod
from quarterdeck.service import Service


@pytest.fixture
def scheduled(tmp_path, monkeypatch):
    (tmp_path / 'ship.toml').write_text('[ship]\ncode="checkout"\n[sessions.crew]\n')
    clock = [0.0]
    monkeypatch.setattr(servicemod.time, 'monotonic', lambda: clock[0])
    service = Service(shipmod.load(tmp_path), SimpleNamespace(clients_attached=lambda sessions: True))
    service.heartbeat = lambda: None
    published = []
    service.publish_status = lambda: published.append(clock[0])
    monkeypatch.setattr(service_status, 'inputs', lambda ship: 'unchanged')
    try:
        yield service, clock, published
    finally:
        service.ledger.close()


def test_unchanged_checks_wait_thirty_seconds_before_recollecting(scheduled):
    service, clock, published = scheduled
    for index in range(310):
        clock[0] = index / 10
        service.status_due()
    assert len(published) == 2
    assert published[0] == 0.0
    assert 30 <= published[1] < 31


def test_a_slow_collection_does_not_return_with_an_overdue_deadline(scheduled):
    service, clock, published = scheduled
    def collect():
        published.append(clock[0])
        clock[0] += 3.0
    service.publish_status = collect
    service.status_due()
    assert service.next_status == pytest.approx(3.7)
    assert service.status_refresh == 33.0, 'the refresh starts when collection finishes'
    for _ in range(50):
        service.status_due()
    assert published == [0.0], 'duties/rest cannot begin back-to-back slow collections'
    clock[0] = 4.0
    service.status_due()
    assert published == [0.0], 'an unchanged hint does not require another full reading'
    clock[0] = 32.0
    service.status_due()
    assert published == [0.0], 'collection duration must not shorten the thirty-second rest'
    clock[0] = 33.0
    service.status_due()
    assert published == [0.0, 33.0]


def test_the_service_uses_the_ship_files_refresh_interval(scheduled):
    service, clock, published = scheduled
    service.ship.file.write_text('[ship]\ncode="checkout"\nstatus_refresh=45.5\n')
    service.ship = shipmod.load(service.ship.folder)
    service.status_due()
    assert service.status_refresh == 45.5
    clock[0] = 44.0
    service.status_due()
    assert published == [0.0]
    clock[0] = 45.5
    service.status_due()
    assert published == [0.0, 45.5]


@pytest.mark.parametrize('change', ['arrival', 'attempt', 'journal', 'seat', 'heartbeat'])
def test_a_changed_input_is_published_without_waiting_for_the_periodic_refresh(scheduled, monkeypatch, change):
    service, clock, published = scheduled
    service.status_due()
    monkeypatch.setattr(service_status, 'inputs', lambda ship: change)
    clock[0] = 0.8
    service.status_due()
    assert published == [0.0, 0.8]


def test_changes_during_collection_remain_owed(scheduled, monkeypatch):
    service, clock, published = scheduled
    hint = ['before']
    monkeypatch.setattr(service_status, 'inputs', lambda ship: hint[0])
    def collect():
        published.append(clock[0])
        hint[0] = 'after'
    service.publish_status = collect
    service.status_due()
    clock[0] = 0.8
    service.status_due()
    assert published == [0.0, 0.8]


def test_a_failed_collection_keeps_the_same_input_owed(scheduled):
    service, clock, published = scheduled
    def fail():
        raise OSError('synthetic collection unavailable')
    service.publish_status = fail
    service.status_due()
    service.publish_status = lambda: published.append(clock[0])
    clock[0] = 0.8
    service.status_due()
    assert published == [0.8]


def test_real_input_hints_observe_arrivals_attempts_journals_and_seat_state(tmp_path, monkeypatch):
    (tmp_path / 'ship.toml').write_text('[ship]\ncode="checkout"\n[pools.p]\nprovider="fake"\n[sessions.crew]\n[seats.worker]\nsession="crew"\nharness="fake"\npool="p"\n')
    ship = shipmod.load(tmp_path)
    monkeypatch.setattr(service_status.watch, 'service_state', lambda runtime: 'ok')
    monkeypatch.setattr(service_status.accounting_health, 'reading', lambda runtime: None)
    paths = [ship.runtime / name for name in ('post/store', 'post/attempts', 'post/pending/worker', 'seats/worker')]
    for folder in paths:
        folder.mkdir(parents=True)
    before = service_status.inputs(ship)
    for folder in paths:
        (folder / 'synthetic.json').write_text('{}')
        after = service_status.inputs(ship)
        assert after != before
        before = after
    # Same-path native updates have no directory change: the record/WAL stamp is checked.
    native = tmp_path / 'synthetic-record'
    native.write_text('first')
    (paths[-1] / 'record').write_text(str(native))
    before = service_status.inputs(ship)
    native.write_text('changed record bytes')
    assert service_status.inputs(ship) != before


def test_resting_wait_returns_for_a_real_arrival(tmp_path):
    import threading
    runtime = tmp_path / 'runtime'
    folder = runtime / 'post/store'
    folder.mkdir(parents=True)
    waiter = service_wait.Arrivals(runtime)
    waiter.refresh([])
    try:
        assert waiter.fd is not None and waiter.watched
        ready = threading.Event()
        def arrive():
            ready.wait()
            (folder / 'synthetic.json').write_text('{}')
        writer = threading.Thread(target=arrive)
        writer.start()
        ready.set()
        assert waiter.wait(2.0) is True
        writer.join(timeout=2.0)
        assert not writer.is_alive()
    finally:
        waiter.close()


def test_unavailable_notifications_sleep_until_the_tick(tmp_path, monkeypatch):
    waiter = service_wait.Arrivals(tmp_path)
    waiter.close()
    slept = []
    monkeypatch.setattr(service_wait.time, 'sleep', slept.append)
    assert waiter.wait(0.25) is False
    assert slept == [0.25]


def test_withheld_work_is_retried_on_each_tick_and_arrivals_can_wake_earlier(tmp_path, monkeypatch):
    (tmp_path / 'ship.toml').write_text('[ship]\ncode="checkout"\ntick=1\n')
    service = Service(shipmod.load(tmp_path), SimpleNamespace())
    clock = [0.0]
    monkeypatch.setattr(servicemod.time, 'monotonic', lambda: clock[0])
    attempts, waits = [], []
    class Finished(BaseException):
        pass
    def tick():
        attempts.append(clock[0])  # a withheld delivery is still due every pass
        if len(attempts) == 4:
            raise Finished
    def wait(delay):
        waits.append(delay)
        clock[0] += 0.2 if len(waits) == 2 else delay
    waiter = SimpleNamespace(refresh=lambda names: None, wait=wait, close=lambda: None)
    monkeypatch.setattr(service_wait, 'Arrivals', lambda runtime: waiter)
    monkeypatch.setattr(service, 'tick', tick)
    monkeypatch.setattr(service, 'drain_accounting', lambda: None)
    # The independent heartbeat threads are irrelevant to this deterministic actor clock.
    class Thread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
        def join(self, **kwargs):
            pass
    monkeypatch.setattr(servicemod.threading, 'Thread', Thread)
    try:
        with pytest.raises(Finished):
            service.run()
        assert attempts == pytest.approx([0, 1, 1.2, 2.2])
        assert waits == [1, 1, 1], 'no status deadline can shorten, busy-spin or lengthen a quiet tick'
    finally:
        service.lock.close()
        service.ledger.close()


def test_numeric_timing_summaries_are_bounded_to_one_per_minute(monkeypatch):
    from quarterdeck import service_timing
    clock, cpu = [0.0], [0.0]
    monkeypatch.setattr(service_timing.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(service_timing.time, 'process_time', lambda: cpu[0])
    timing = service_timing.Timings()
    lines = []
    def log(event, **data):
        lines.append((event, data))
    for _ in range(120):
        with timing.measure('pass'):
            with timing.measure('status'):
                clock[0] += 0.2
                cpu[0] += 0.1
            clock[0] += 0.8
        timing.publish(log)
    assert len(lines) == 2
    assert sum(row['passes'] for _, row in lines) == 120
    for event, row in lines:
        assert event == 'service-timing'
        assert set(row) == {'seconds', 'passes', 'status_checks', 'duties'}
        assert row['status_checks'] == 0
        assert set(row['duties']) == {'pass', 'status'}
        assert row['duties']['status']['max_cpu'] == pytest.approx(0.1)
        assert row['duties']['status']['max_wall'] == pytest.approx(0.2)
    timing.publish(log)
    assert len(lines) == 2, 'reading the summary must not emit another line'


def test_status_summaries_count_full_collections_separately_from_cheap_checks(scheduled, monkeypatch):
    from quarterdeck import service_timing
    service, clock, published = scheduled
    cpu = [0.0]
    monkeypatch.setattr(service_timing.time, 'process_time', lambda: cpu[0])
    def collect():
        published.append(clock[0])
        clock[0] += 2.0
        cpu[0] += 1.0
    service.publish_status = collect
    for _ in range(100):
        service.status_due()
    clock[0] = 3.0
    service.status_due()  # a real input check, unchanged; no collection
    timing = service.timings()
    assert timing.status_checks == 101
    assert timing.rows['status'] == dict(calls=1, wall=2.0, cpu=1.0, max_wall=2.0, max_cpu=1.0)
    lines = []
    clock[0] = 60.0
    timing.publish(lambda event, **row: lines.append(row))
    assert lines[0]['status_checks'] == 101
    assert lines[0]['duties']['status']['calls'] == 1
    assert timing.status_checks == 0 and timing.rows == {}


@pytest.mark.parametrize('raises', [False, True])
def test_failed_or_refused_collection_still_has_one_collection_measurement(scheduled, raises):
    service, clock, published = scheduled
    def failed():
        clock[0] += 2.0
        if raises:
            raise OSError('synthetic collection failure')
        return False
    service.publish_status = failed
    service.status_due()
    service.status_due()  # deadline still spares another collection
    assert service.timings().status_checks == 2
    assert service.timings().rows['status']['calls'] == 1
    assert service.timings().rows['status']['wall'] == 2.0
