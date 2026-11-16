"""Unchanged presentation rests; changed hints request fresh facts, never an action."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import accounting_health, service as servicemod, service_status, service_wait, ship as shipmod
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


def hints(service, post_stamp, other_stamp):
    """Inputs as service_status gives them: one under the post, one elsewhere."""
    runtime = service.ship.runtime
    return ((str(runtime / 'post' / 'store'), post_stamp), (str(runtime / 'seats' / 'worker'), other_stamp))


@pytest.mark.parametrize('change, post_traffic', [('arrival', True), ('attempt', True), ('journal', True),
                                                  ('seat', False), ('heartbeat', False)])
def test_a_changed_input_is_published_without_waiting_for_the_periodic_refresh(scheduled, monkeypatch, change,
                                                                               post_traffic):
    service, clock, published = scheduled
    monkeypatch.setattr(service_status, 'inputs', lambda ship: hints(service, 'before', 'before'))
    service.status_due()
    changed = hints(service, change, 'before') if post_traffic else hints(service, 'before', change)
    monkeypatch.setattr(service_status, 'inputs', lambda ship: changed)
    clock[0] = 0.8
    service.status_due()
    if post_traffic:
        assert published == [0.0, 0.8] and service.status_request[0] == 'post', 'post traffic: a post refresh at once'
        return
    assert published == [0.0], "another change waits for the full collection's interval"
    clock[0] = servicemod.FULL_STATUS_INTERVAL - 1.0
    service.status_due()
    assert published == [0.0]
    clock[0] = servicemod.FULL_STATUS_INTERVAL
    service.status_due()
    assert published == [0.0, servicemod.FULL_STATUS_INTERVAL] and service.status_request[0] == 'full'


def test_an_accounting_transition_forces_exactly_one_collection(scheduled):
    # The service's in-memory accounting state is compared beside the file inputs: each transition is
    # published at once, and the same state across many checks forces nothing (the 30 s refresh aside).
    service, clock, published = scheduled
    health = service.accounting_health
    assert health.shown() == accounting_health.UNKNOWN
    service.status_due()
    assert published == [0.0]
    moment = [0.0]

    def checks(count):
        for _ in range(count):
            moment[0] += 0.8
            clock[0] = moment[0]
            service.status_due()

    checks(5)
    assert published == [0.0], 'unknown, unchanged'
    health.committed()  # unknown -> healthy
    checks(5)
    assert len(published) == 2 and published[-1] == pytest.approx(moment[0] - 3.2), \
        'one collection, at the first check after the transition, and none after'
    for _ in range(accounting_health.BOUND - 1):
        health.failed('database error', servicemod.post.now())
    checks(5)
    assert len(published) == 2, 'failures below the bound show nothing new'
    health.failed('database error', servicemod.post.now())  # healthy -> failing since a time, with its cause
    checks(5)
    assert len(published) == 3 and published[-1] == pytest.approx(moment[0] - 3.2)
    health.committed()  # failing -> healthy
    checks(5)
    assert len(published) == 4 and published[-1] == pytest.approx(moment[0] - 3.2)
    assert moment[0] < 30, 'every collection here was forced by a transition, never by the refresh'


def test_an_unchanged_accounting_state_forces_nothing_across_many_checks(scheduled):
    service, clock, published = scheduled
    service.accounting_health.committed()
    service.status_due()
    for index in range(1, 37):
        clock[0] = index * 0.8
        service.status_due()
    assert published == [0.0], 'thirty-six unchanged checks within the refresh collect nothing'


@pytest.mark.parametrize('post_traffic', [True, False])
def test_changes_during_collection_remain_owed(scheduled, monkeypatch, post_traffic):
    service, clock, published = scheduled
    hint = ['before']
    monkeypatch.setattr(service_status, 'inputs', lambda ship: (
        hints(service, hint[0], 'before') if post_traffic else hints(service, 'before', hint[0])))
    def collect():
        published.append(clock[0])
        hint[0] = 'after'
    service.publish_status = collect
    service.status_due()
    clock[0] = 0.8
    service.status_due()
    if not post_traffic:
        assert published == [0.0], 'owed, until the full interval comes round'
        clock[0] = servicemod.FULL_STATUS_INTERVAL
        service.status_due()
    assert published == [0.0, clock[0]]


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


def hinted_ship(tmp_path):
    (tmp_path / 'ship.toml').write_text('[ship]\ncode="checkout"\n[pools.p]\nprovider="fake"\n[sessions.crew]\n[seats.worker]\nsession="crew"\nharness="fake"\npool="p"\n')
    ship = shipmod.load(tmp_path)
    for name in ('post/store', 'post/attempts', 'post/pending/worker', 'post/drop/worker', 'seats/worker', 'requests'):
        (ship.runtime / name).mkdir(parents=True)
    return ship


def replace(path, text):
    """The way the framework writes: a whole new file renamed into place."""
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text)
    temporary.replace(path)


def test_each_kept_input_still_requests_a_reading(tmp_path):
    ship = hinted_ship(tmp_path)
    seat = ship.runtime / 'seats/worker'
    native = tmp_path / 'synthetic-record'
    native.write_text('first')
    replace(seat / 'human-alert.json', '{}')
    before = service_status.inputs(ship)
    changes = [
        lambda: (ship.runtime / 'post/store/synthetic.json').write_text('{}'),  # an arrival
        lambda: (ship.runtime / 'post/attempts/synthetic.json').write_text('{}'),  # a delivery attempt
        lambda: (ship.runtime / 'post/pending/worker/synthetic.json').write_text('{}'),  # a delivery journal
        lambda: (ship.runtime / 'post/drop/worker/synthetic.json').write_text('{}'),  # a seat's post
        lambda: (ship.runtime / 'requests/synthetic.json').write_text('{}'),  # a request
        lambda: (seat / 'launch.json').write_text('{}'),  # a new seat file
        lambda: replace(seat / 'human-alert.json', '{"alert": true}'),  # a seat file rewritten in place
        lambda: (seat / 'record').write_text(str(native)),  # a launch points the seat at a record
        lambda: (seat / 'launch.json').unlink(),  # a seat file removed
        lambda: ship.file.write_text(ship.file.read_text() + '# changed\n'),  # the ship file
    ]
    for change in changes:
        change()
        after = service_status.inputs(ship)
        assert after != before
        before = after


def test_what_the_service_writes_itself_never_requests_a_reading(tmp_path):
    ship = hinted_ship(tmp_path)
    seat = ship.runtime / 'seats/worker'
    replace(seat / 'move-hold.json', '{"wall": 1}')
    replace(seat / 'compaction.json', '{"extent": 1}')
    before = service_status.inputs(ship)
    replace(seat / 'move-hold.json', '{"wall": 2}')  # its idle reading's bounded refresh, on its own pass
    replace(seat / 'compaction.json', '{"extent": 2}')  # its observation of a growing record
    servicemod.write_heartbeat(ship.runtime)  # before every step and with each collection
    (ship.runtime / 'accounting-health.json').write_text('{}')  # beside each heartbeat
    assert service_status.inputs(ship) == before


def test_a_working_seats_record_growing_never_requests_a_reading(tmp_path):
    # The periodic refresh reads it: every alert the line carries is in a kept file or as slow as a screen.
    ship = hinted_ship(tmp_path)
    native = tmp_path / 'synthetic-record'
    native.write_text('first')
    (ship.runtime / 'trusted/worker').mkdir(parents=True, exist_ok=True)
    (ship.runtime / 'trusted/worker/record').write_text(str(native))
    before = service_status.inputs(ship)
    with native.open('a') as record:
        record.write('a turn of the working seat\n')
    Path(str(native) + '-wal').write_text('its write-ahead log')
    assert service_status.inputs(ship) == before


def test_the_service_collects_again_only_for_a_kept_input(tmp_path, monkeypatch):
    ship = hinted_ship(tmp_path)
    seat = ship.runtime / 'seats/worker'
    native = tmp_path / 'synthetic-record'
    native.write_text('first')
    (seat / 'record').write_text(str(native))
    clock = [0.0]
    monkeypatch.setattr(servicemod.time, 'monotonic', lambda: clock[0])
    service = Service(ship, SimpleNamespace(clients_attached=lambda sessions: True))
    try:
        service.heartbeat = lambda: servicemod.write_heartbeat(ship.runtime)
        published = []
        service.publish_status = lambda: published.append(clock[0])
        service.status_due()
        for step in range(1, 20):
            clock[0] = step * 0.8
            replace(seat / 'move-hold.json', f'{{"wall": {step}}}')
            replace(seat / 'compaction.json', f'{{"extent": {step}}}')
            with native.open('a') as record:
                record.write('a turn\n')
            service.status_due()
        assert published == [0.0], 'neither its own writes nor a working record forced a collection'
        clock[0] = 16.0
        (ship.runtime / 'post/store/synthetic.json').write_text('{}')
        service.status_due()
        assert published == [0.0, 16.0], 'an arrival is still published at once'
    finally:
        service.ledger.close()


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
        assert service.timings().rows['drain_accounting']['calls'] == 3
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


@pytest.mark.parametrize('new_step', [False, True])
def test_every_pass_step_and_between_duty_drain_reaches_the_numeric_summary(monkeypatch, new_step):
    from quarterdeck import service_timing
    clock, cpu = [0.0], [0.0]
    monkeypatch.setattr(service_timing.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(service_timing.time, 'process_time', lambda: cpu[0])
    service = Service.__new__(Service)
    lines, order = [], []
    service.log = lambda event, **data: lines.append((event, data))
    service.heartbeat = lambda: None
    service.status_due = lambda: None
    service._tick = service._tick_steps
    steps = ('server_recovery', 'relaunch_intents', 'reconcile', 'file_drops', 'groups', 'cascades', 'fleet_post',
             'inboxes', 'views', 'recoveries', 'refusals', 'answers', 'looks', 'jailed_watches',
             'write_bound_floor', 'deliveries', 'follow', 'requests', 'compactions', 'output_limits', 'person_quiet',
             'fresh_after_order',
             'rebrief', 'push_reminders', 'credentials', 'authentications', 'model_differences',
             'failover', 'retire_hand_transcripts', 'account', 'refresh_usage', 'pool_forecasts', 'retire_post')
    drains = ('drain_drops', 'drain_accounting', 'drain_requests')
    def stand_in(name, wall):
        def duty():
            order.append(name)
            clock[0] += wall
            cpu[0] += wall / 2
        duty.__name__ = name
        return duty
    names = list(steps)
    if new_step:
        names[0] = 'new_housekeeping_step'
        monkeypatch.setattr(Service, names[0], stand_in(names[0], 0.25), raising=False)
    for attribute, name in zip(steps, names):
        setattr(service, attribute, stand_in(name, 0.25))
    for name in drains:
        setattr(service, name, stand_in(name, 0.125))
    # These runtime values must not enter the numeric observation.
    service.seat = 'synthetic-seat'
    service.message = 'synthetic-message'
    service.record = 'synthetic-record'
    timing = service.timings()
    for _ in range(2):
        service.tick()
    assert lines == [], 'finishing duties does not add log lines before the minute'
    expected_order = list(drains)
    for name in names:
        expected_order.extend((name, *drains))
    assert order == expected_order * 2, 'timing preserves pass and drain order'
    clock[0] = 60.0
    timing.publish(service.log)
    timing.publish(service.log)
    assert len(lines) == 1
    event, summary = lines[0]
    assert event == 'service-timing'
    assert set(summary) == {'seconds', 'passes', 'status_checks', 'duties'}
    assert summary['seconds'] == 60.0 and summary['passes'] == 2
    assert summary['status_checks'] == 0
    assert set(summary['duties']) == {*names, *drains, 'pass'}
    for name in names:
        assert summary['duties'][name] == dict(calls=2, wall=0.5, cpu=0.25, max_wall=0.25, max_cpu=0.125)
    drain_calls = 2 * (len(steps) + 1)
    for name in drains:
        assert summary['duties'][name] == dict(calls=drain_calls, wall=drain_calls * 0.125,
                                             cpu=drain_calls * 0.0625, max_wall=0.125, max_cpu=0.0625)
    pass_wall = len(steps) * 0.25 + (len(steps) + 1) * 3 * 0.125
    assert summary['duties']['pass'] == dict(calls=2, wall=pass_wall * 2, cpu=pass_wall,
                                            max_wall=pass_wall, max_cpu=pass_wall / 2)
    for row in summary['duties'].values():
        assert set(row) == {'calls', 'wall', 'cpu', 'max_wall', 'max_cpu'}
        assert all(type(value) in (int, float) for value in row.values())
    assert all(type(summary[name]) in (int, float) for name in ('seconds', 'passes', 'status_checks'))
    assert timing.rows == {} and timing.status_checks == 0
    clock[0] = 119.0
    timing.publish(service.log)
    assert len(lines) == 1
    clock[0] = 120.0
    timing.publish(service.log)
    assert len(lines) == 2 and lines[1][1]['duties'] == {}


def test_unknown_timing_labels_are_counted_without_publishing_their_names(monkeypatch):
    from quarterdeck import service_timing
    clock = [0.0]
    monkeypatch.setattr(service_timing.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(service_timing.time, 'process_time', lambda: clock[0] / 2)
    timing = service_timing.Timings()
    for name in ('synthetic-message:example', 'synthetic_record', 'Service.synthetic_seat',
                 'since', '__class__', 'reconcile', 'sleep', 'status_checks'):
        with timing.measure(name):
            clock[0] += 0.25
    lines = []
    clock[0] = 60.0
    timing.publish(lambda event, **data: lines.append((event, data)))
    assert len(lines) == 1 and lines[0][0] == 'service-timing'
    duties = lines[0][1]['duties']
    assert set(duties) == {'other', 'reconcile', 'sleep', 'status_checks'}
    assert duties['other'] == dict(calls=5, wall=1.25, cpu=0.625, max_wall=0.25, max_cpu=0.125)
    assert all(duties[name]['calls'] == 1 for name in ('reconcile', 'sleep', 'status_checks'))


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
    assert timing.rows['status'] == dict(calls=1, wall=2.0, cpu=1.0, max_wall=2.0, max_cpu=1.0, ledger_writes=0)
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


def test_a_declined_post_refresh_collects_in_full(scheduled, monkeypatch):
    service, clock, published = scheduled
    del service.publish_status  # the service's own, not the fixture's stand-in
    calls = []
    monkeypatch.setattr(servicemod.presentation, 'publish_collected',
                        lambda *args, **kwargs: calls.append('post') or (None, kwargs['kept']))
    monkeypatch.setattr(servicemod.presentation, 'publish',
                        lambda *args: calls.append('full') or args[5].append('a new collection') or True)
    service.status_kept, service.status_request = 'a kept collection', ('post', frozenset())
    assert service.publish_status() is True
    assert calls == ['post', 'full'] and service.status_done == 'full'
    assert service.status_kept == 'a new collection' and service.status_request[0] == 'full', 'a request is used once'


def test_the_door_is_read_again_only_for_seats_whose_own_pending_or_drop_folder_changed(tmp_path):
    ship = hinted_ship(tmp_path)
    post = str(ship.runtime / 'post')
    before = ((post + '/store', 1), (post + '/pending/worker', 1), (post + '/drop/worker', 1),
              (post + '/pending/other', 1), (post + '/drop/other', 1))
    after = ((post + '/store', 2), (post + '/pending/worker', 2), (post + '/drop/worker', 1),
             (post + '/pending/other', 1), (post + '/drop/other', 2))
    assert service_status.door_seats(ship, before, after) == {'worker', 'other'}
    assert service_status.door_seats(ship, before, before) == frozenset()
    posted, other = service_status.split(ship, before + ((str(ship.runtime / 'seats/worker'), 1),))
    assert posted == before and other == ((str(ship.runtime / 'seats/worker'), 1),)


def test_each_duty_counts_the_ledger_rows_it_writes_and_nothing_else():
    """The duty counter: a duty's row counts the ledger rows written while it ran, a number under its fixed name, and
    a duty that writes nothing counts none; a counter that cannot be read leaves the count out."""
    import sqlite3
    from quarterdeck import service_timing
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE ordinary(value TEXT)')
    timing = service_timing.Timings(changes=lambda: db.total_changes)
    with timing.measure('refresh_usage'):
        db.executemany('INSERT INTO ordinary VALUES (?)', [('a',), ('b',), ('c',)])
    with timing.measure('views'):
        pass
    logged = []
    timing.since -= service_timing.INTERVAL
    timing.publish(lambda event, **fields: logged.append(fields))
    duties = logged[0]['duties']
    assert duties['refresh_usage']['ledger_writes'] == 3 and duties['views']['ledger_writes'] == 0
    def unreadable():
        raise sqlite3.ProgrammingError('closed')
    timing = service_timing.Timings(changes=unreadable)
    with timing.measure('views'):
        pass
    assert 'ledger_writes' not in timing.rows['views']
