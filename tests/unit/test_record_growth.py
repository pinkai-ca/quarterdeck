"""Size-only warning episodes for made records and abbreviated headlines."""

from dataclasses import replace
import os
from types import SimpleNamespace

import pytest

from quarterdeck import fresh_handoff, headlines, human_wake, post, record_growth, record_owner, server_recovery, trusted_files, watch
from quarterdeck.harness import get, record_file

from .test_cross_program_failover import running  # noqa: F401


AT = 10_000


def sized(path, mib):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Forecasts need the apparent size; none of the fixture's body is allocated.
    with path.open('wb') as stream:
        stream.truncate(mib * record_file.MiB)
        assert os.fstat(stream.fileno()).st_blocks == 0


@pytest.fixture
def world(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    specs = {name: SimpleNamespace(name=name, role=role)
             for name, role in [('operator-desk', 'desk'), ('operator-media', 'office'),
                                ('builder', 'worker'), ('systems-seat', 'systems')]}
    ship = SimpleNamespace(runtime=runtime, seats=specs)
    logs, calls = [], []

    def request(seat, *, reason, expected_launch):
        assert not seat.session.human
        calls.append((seat.name, reason, expected_launch))
        return SimpleNamespace(disposition='staged', proof='synthetic handoff accepted')

    def service():
        return SimpleNamespace(ship=ship, runtime=runtime, store=post.Store(runtime),
                               request_fresh_handoff=request,
                               log=lambda event, **data: logs.append((event, data)))

    def make(name, human):
        seat = SimpleNamespace(name=name, home=runtime / 'seats' / name / 'home',
                               trusted=trusted_files.make(runtime, name),
                               session=SimpleNamespace(human=human),
                               harness=SimpleNamespace(home_variable='SYNTHETIC_HOME'))
        pointer = seat.trusted / trusted_files.RECORD
        seat.lookup_record = (lambda: pytest.fail('human uses only the owned pointer')) if human else (
            lambda: human_wake.owned_record(seat))
        seat.launch = object()
        seat.launched = (lambda: pytest.fail('human forecast uses only record metadata')) if human else (
            lambda: seat.launch)
        seat.record = lambda: pytest.fail('the forecast must not discover conversations')
        return seat, pointer

    monkeypatch.setattr(record_file, 'open_record', lambda *a, **k: pytest.fail('metadata only'))
    return SimpleNamespace(make=make, service=service, logs=logs, calls=calls)


@pytest.mark.parametrize(('name', 'human', 'recipient'), [
    ('operator-desk', True, 'operator-desk'),
    ('operator-media', True, 'operator-desk'),
    ('builder', False, 'systems-seat'),
])
def test_growing_current_record_warns_before_threshold_and_routes_once(world, name, human, recipient):
    seat, pointer = world.make(name, human)
    record = seat.home / 'synthetic-private-record.jsonl'
    pointer.write_text(str(record))
    service = world.service()
    sized(record, 24)
    record_growth.observe(service, seat, AT)
    sized(record, 48)
    record_growth.observe(service, seat, AT + 3600)
    [report] = service.store.all()
    assert (report.to, report.sender, report.kind) == (recipient, post.SHIP, 'report')
    assert 'record 48.0 MiB of its 128.0 MiB read bound' in report.body
    assert '24.0 MiB/h over 1.00 h' in report.body and 'in 3.3 h' in report.body
    assert record.name not in report.body and str(seat.home) not in report.body
    assert 'natural break' in report.body
    if human:
        assert 'Ask the Admiral for his yes' in report.body
        assert world.calls == []
    else:
        assert world.calls == [(name, report.body.splitlines()[0], seat.launch)]

    restarted = world.service()
    sized(record, 55)
    record_growth.observe(restarted, seat, AT + 7200)
    assert [report.id] == [message.id for message in restarted.store.all()]
    assert len(world.calls) == (0 if human else 1)

    second = record.with_name('synthetic-next.jsonl')
    sized(second, 54)
    pointer.write_text(str(second))
    seat.launch = object()
    record_growth.observe(restarted, seat, AT + 7201)
    sized(second, 61)  # 7 MiB/h, in the range of the observed growth episode
    record_growth.observe(restarted, seat, AT + 10_801)
    assert len(restarted.store.all()) == 2
    assert len(world.calls) == (0 if human else 2)
    pointer.write_text(str(record))  # a resumed older record retains its original notification
    seat.launch = object()
    record_growth.observe(restarted, seat, AT + 10_802)
    assert len(restarted.store.all()) == 2
    assert len(world.calls) == (0 if human else 2)


def test_crew_rechecks_launch_and_retries_temporary_holds_without_reposting(world):
    seat, pointer = world.make('builder', False)
    record = seat.home / 'synthetic.jsonl'
    pointer.write_text(str(record))
    service = world.service()
    sized(record, 48)
    record_growth.observe(service, seat, AT)
    sized(record, 56)
    old_launch = seat.launch
    replacement = object()
    outcomes = iter(('changed-launch', 'held', 'already-staged'))
    calls = []

    def request(target, *, reason, expected_launch):
        assert target is seat
        calls.append((reason, expected_launch))
        outcome = next(outcomes)
        if outcome == 'changed-launch':
            seat.launch = replacement
        return SimpleNamespace(disposition=outcome, proof='synthetic crew handoff disposition')

    service.request_fresh_handoff = request
    record_growth.observe(service, seat, AT + 3600)
    [report] = service.store.all()
    restarted = world.service()
    restarted.request_fresh_handoff = request
    for now in (AT + 3900, AT + 4200, AT + 4500):
        record_growth.observe(restarted, seat, now)
    assert restarted.store.all() == [report]
    assert calls == [(report.body.splitlines()[0], launch) for launch in (old_launch, replacement, replacement)]


def test_pending_forecast_cannot_stage_after_the_record_crosses_its_bound(world):
    seat, pointer = world.make('builder', False)
    record = seat.home / 'synthetic.jsonl'
    pointer.write_text(str(record))
    service = world.service()
    sized(record, 48)
    record_growth.observe(service, seat, AT)
    sized(record, 56)
    service.request_fresh_handoff = lambda *a, **k: SimpleNamespace(disposition='held', proof='synthetic hold')
    record_growth.observe(service, seat, AT + 3600)
    [report] = service.store.all()
    restarted = world.service()
    sized(record, 129)
    record_growth.observe(restarted, seat, AT + 3900)
    assert restarted.store.all() == [report]
    assert world.calls == []


def test_retries_a_shared_handoff_if_its_local_acceptance_was_interrupted(world, monkeypatch):
    seat, pointer = world.make('builder', False)
    record = seat.home / 'synthetic.jsonl'
    pointer.write_text(str(record))
    service = world.service()
    sized(record, 48)
    record_growth.observe(service, seat, AT)
    sized(record, 56)
    prompts = set()
    attempts = []

    def request(target, *, reason, expected_launch):
        assert target is seat
        attempts.append(expected_launch)
        disposition = 'already-staged' if expected_launch in prompts else 'staged'
        prompts.add(expected_launch)
        return SimpleNamespace(disposition=disposition, proof='synthetic durable handoff')

    save = record_growth._save

    def interrupted(path, data):
        if data['crew_accepted']:
            raise OSError('synthetic interruption after shared acceptance')
        save(path, data)

    service.request_fresh_handoff = request
    with monkeypatch.context() as cut:
        cut.setattr(record_growth, '_save', interrupted)
        with pytest.raises(OSError, match='after shared acceptance'):
            record_growth.observe(service, seat, AT + 3600)
    [report] = service.store.all()
    restarted = world.service()
    restarted.request_fresh_handoff = request
    record_growth.observe(restarted, seat, AT + 3900)
    record_growth.observe(restarted, seat, AT + 4200)
    assert restarted.store.all() == [report]
    assert attempts == [seat.launch, seat.launch]
    assert prompts == {seat.launch}


def test_published_notice_is_not_repeated_if_its_final_save_was_interrupted(world, monkeypatch):
    seat, pointer = world.make('builder', False)
    record = seat.home / 'synthetic.jsonl'
    pointer.write_text(str(record))
    service = world.service()
    sized(record, 48)
    record_growth.observe(service, seat, AT)
    sized(record, 56)
    save = record_growth._save

    def interrupted(path, data):
        if data['published']:
            raise OSError('synthetic interruption after the immutable post')
        save(path, data)

    with monkeypatch.context() as cut:
        cut.setattr(record_growth, '_save', interrupted)
        with pytest.raises(OSError, match='synthetic interruption'):
            record_growth.observe(service, seat, AT + 3600)
    [report] = service.store.all()
    restarted = world.service()
    record_growth.observe(restarted, seat, AT + 3601)
    assert [report.id] == [message.id for message in restarted.store.all()]
    record_growth.observe(restarted, seat, AT + 3602)
    assert [report.id] == [message.id for message in restarted.store.all()]


def test_sparse_oversize_record_is_visible_without_a_forecast_or_move_hold(world):
    seat, pointer = world.make('operator-media', True)
    record = seat.home / 'synthetic.jsonl'
    pointer.write_text(str(record))
    sized(record, 295)
    service = world.service()
    record_growth.observe(service, seat, AT)
    assert service.store.all() == []
    [alarm] = record_file.alarms([seat], owned=human_wake.owned_record)
    facts = watch.Facts('ok', 0, 0, 0, record_bounds=[alarm],
                        needs_human=[f'synthetic-long-seat-{i}' for i in range(15)])
    assert 'qd status' in facts.line()  # the abbreviated route
    assert 'operator-media: record past the read bound (295.0 MiB, bound 128.0 MiB)' in facts.line()
    facts = replace(facts, move_holds=['builder: move held: its record is unavailable'])
    line = facts.line()
    move = next(item for item in (facts.move_holds[0], '1 move alert', '+1 more') if item in line)
    assert line.index(alarm) < line.index(move)


def claude_seat(world, human):
    seat, pointer = world.make('operator-media' if human else 'builder', human)
    seat.harness = get('claude')
    path = seat.home / 'synthetic-claude.jsonl'
    pointer.write_text(str(path))
    record_owner.register(seat.trusted, path, 'claude')
    if not human:
        seat.record = seat.lookup_record
    return seat, path


def projection(path, **usage):
    info = path.stat()
    stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return record_file.ProjectionUsage(stamp, **usage)


@pytest.mark.parametrize('human', [False, True], ids=['crew', 'operator'])
@pytest.mark.parametrize('resource', ['physical lines', 'retained bytes'])
def test_forecast_and_status_warn_for_the_first_claude_resource(world, monkeypatch, human, resource):
    seat, path = claude_seat(world, human)
    service = world.service()
    if resource == 'retained bytes':
        monkeypatch.setattr(record_file, 'CLAUDE_STREAM_BYTES', 160 * record_file.MiB)
        evidence = lambda size: projection(path, lines=100, retained=size * record_file.MiB // 2, memory=1000)
    else:
        evidence = lambda size: projection(path, lines=size * record_file.CLAUDE_STREAM_ROWS // 320, retained=100, memory=1000)

    def update(size):
        sized(path, size)
        service.ledger = SimpleNamespace(projection_usage=lambda _: evidence(size))
        return service.ledger.projection_usage

    first = update(224)
    assert record_file.alarms([seat], owned=human_wake.owned_record, observation=first) == []
    record_growth.observe(service, seat, AT)
    later = update(250)
    [alarm] = record_file.alarms([seat], owned=human_wake.owned_record, observation=later)
    assert resource in alarm and '320.0 MiB' in alarm
    record_growth.observe(service, seat, AT + 3600)
    record_growth.observe(service, seat, AT + 3900)
    [report] = service.store.all()
    assert report.to == ('operator-desk' if human else 'systems-seat')
    assert resource in report.body and '320.0 MiB' in report.body and '2.7 h' in report.body
    assert len(world.calls) == (0 if human else 1)


@pytest.mark.parametrize('human', [False, True], ids=['crew', 'operator'])
def test_past_projection_limit_reports_once_from_accounted_scalars(world, human):
    seat, path = claude_seat(world, human)
    sized(path, 250)
    usage = projection(path, exhausted='physical lines')
    service = world.service()
    service.ledger = SimpleNamespace(projection_usage=lambda _: usage)
    [alarm] = record_file.alarms([seat], owned=human_wake.owned_record, observation=service.ledger.projection_usage)
    assert 'Claude physical lines limit exceeded' in alarm and 'Reading unknown' in alarm
    near = 'synthetic-other: Claude retained bytes may reach its limit at about 301.4 MiB'
    facts = watch.Facts('down', 3, 2, 1, needs_human=['synthetic-human'], held=['synthetic-limited'],
                        sign_in_failures=['synthetic-account: sign-in failed'], disks=[('synthetic-disk', 97)],
                        login=[('synthetic-login', 'tomorrow')], record_bounds=[near, alarm])
    assert '1 past read bound' in facts.line()
    assert alarm in headlines.details(facts)
    assert any(near in group.details for group in headlines.sections(facts).lower)
    record_growth.observe(service, seat, AT)
    record_growth.observe(service, seat, AT + 300)
    [report] = service.store.all()
    assert report.to == ('operator-desk' if human else 'systems-seat')
    assert 'exceeded its physical lines limit' in report.body
    assert world.calls == []


@pytest.mark.parametrize('human', [False, True], ids=['crew', 'operator'])
def test_compatibility_forecast_and_96_mib_warning_follow_appends(world, human):
    seat, path = claude_seat(world, human)
    service = world.service()
    sized(path, 92)
    usage = projection(path, compatibility=True)
    service.ledger = SimpleNamespace(projection_usage=lambda _: usage)

    def alarms():
        return record_file.alarms([seat], owned=human_wake.owned_record, observation=service.ledger.projection_usage)

    def extend(mib):
        with path.open('ab') as stream:
            stream.truncate(mib * record_file.MiB)

    record_growth.observe(service, seat, AT)
    extend(95)
    assert alarms() == []
    extend(96)
    [alarm] = alarms()
    assert '96.0 MiB of its 128.0 MiB Claude compatibility read bound' in alarm
    record_growth.observe(service, seat, AT + 3600)
    record_growth.observe(service, seat, AT + 3900)
    [report] = service.store.all()
    assert report.to == ('operator-desk' if human else 'systems-seat')
    assert '96.0 MiB of its 128.0 MiB Claude compatibility read bound' in report.body
    assert 'in 8.0 h' in report.body
    assert len(world.calls) == (0 if human else 1)

    extend(128)
    assert '128.0 MiB of its 128.0 MiB' in alarms()[0]
    with path.open('ab') as stream:
        stream.write(b'\n')
    [alarm] = alarms()
    assert 'record past the read bound' in alarm and 'Reading unknown' in alarm
    assert '128.0 MiB' in alarm
    record_growth.observe(service, seat, AT + 4200)
    assert len(world.calls) == (0 if human else 1)

    usage = projection(path, lines=1000, retained=1000, memory=1000)
    assert alarms() == []  # a later guarded stream supersedes the fallback
    usage = projection(path, compatibility=True)
    replacement = path.with_suffix('.replacement')
    sized(replacement, 130)
    replacement.replace(path)
    assert alarms() == []  # the old inode can no longer define the first bound


@pytest.mark.parametrize('human', [False, True], ids=['crew', 'operator'])
@pytest.mark.parametrize('resource', ['native projection', 'retained bytes per row'])
def test_row_exhaustion_has_an_immediate_compatibility_warning(world, human, resource):
    seat, path = claude_seat(world, human)
    service = world.service()
    sized(path, 295)
    usage = projection(path, exhausted=resource)
    service.ledger = SimpleNamespace(projection_usage=lambda _: usage)
    record_growth.observe(service, seat, AT)
    record_growth.observe(service, seat, AT + 300)
    [report] = service.store.all()
    assert report.to == ('operator-desk' if human else 'systems-seat')
    assert '295.0 MiB exceeds its 128.0 MiB Claude compatibility read bound. Reading is unknown.' in report.body
    assert world.calls == []


@pytest.mark.parametrize('human', [False, True], ids=['crew', 'operator'])
def test_claude_raw_alarm_uses_768_mib_floor_and_actual_stream_bound(world, human):
    seat, path = claude_seat(world, human)

    def alarms():
        return record_file.alarms([seat], owned=human_wake.owned_record,
                                  observation=lambda _: projection(path, lines=1000, retained=1000, memory=1000))

    sized(path, 767)
    assert alarms() == []
    sized(path, 768)
    assert '768.0 MiB of its 1024.0 MiB' in alarms()[0]
    sized(path, 1024)
    assert '1024.0 MiB of its 1024.0 MiB' in alarms()[0]
    with path.open('ab') as stream:
        stream.write(b'\n')
    assert 'past the read bound' in alarms()[0]


def test_unmeasured_claude_shape_assumes_dense_evidence(world):
    seat, path = claude_seat(world, True)
    sized(path, 250)
    pressure = record_file.pressure(seat, path)
    assert pressure.raw_bound == record_file.CLAUDE_STREAM_BOUND
    assert pressure.resource == 'retained bytes' and 301 * record_file.MiB < pressure.first_bound < 302 * record_file.MiB
    [alarm] = record_file.alarms([seat], owned=human_wake.owned_record)
    assert 'Claude retained bytes may reach its limit' in alarm


def test_forecast_requires_a_recent_positive_rate_for_the_same_file(world):
    seat, pointer = world.make('builder', False)
    record = seat.home / 'synthetic.jsonl'
    pointer.write_text(str(record))
    service = world.service()
    sized(record, 48)
    record_growth.observe(service, seat, AT)
    sized(record, 50)
    record_growth.observe(service, seat, AT + 300)  # short-lived growth is not yet a measured trend
    sized(record, 45)
    record_growth.observe(service, seat, AT + 900)  # a reduction starts a new sample baseline
    sized(record, 55)
    record_growth.observe(service, seat, AT + 12_000)  # the reading gap is too long to project
    assert service.store.all() == []


@pytest.mark.parametrize('live', [True, False], ids=['live-crew', 'stopped-crew'])
def test_forecast_uses_the_real_service_handoff_entry(running, monkeypatch, live):
    r = running
    seat, service = r.seat, r.svc
    systems = 'synthetic-systems'
    seat.ship.seats[systems] = replace(seat.spec, name=systems, role='systems')
    launch = seat.launched()
    record = seat.lookup_record()
    assert record == r.source.path
    clock = [AT]
    monkeypatch.setattr(record_growth, 'time', SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))
    monkeypatch.setattr(seat.harness, 'state', lambda *a: pytest.fail('the forecast and staging only need metadata'))

    sized(record, 48)
    service.record_growth()
    sized(record, 56)
    if not live:
        r.live['pane'] = None
        monkeypatch.setattr(server_recovery, 'pid_dead', lambda pid: True)
    for now in (AT + 3600, AT + 3900):
        clock[0] = now
        service.record_growth()

    [notice] = service.store.for_seat(systems)
    assert notice.kind == 'report' and '8.0 MiB/h' in notice.body
    orders = service.store.for_seat(seat.name)
    episode = fresh_handoff.load(service.runtime, seat.name)
    if live:
        [order] = orders
        assert order.kind == 'order' and order.subject == fresh_handoff.SUBJECT
        assert 'Update the notes' in order.body and '8.0 MiB/h' in order.body
        assert episode['phase'] == 'waiting' and episode['launch']['at'] == launch.at
    else:
        assert orders == [] and episode is None
    assert seat.launched() == launch and r.launches == []
