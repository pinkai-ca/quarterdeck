"""Service inventories finish; progress and persistent waits stay visible."""
from types import SimpleNamespace
import json
import pytest

from quarterdeck import post, post_retirement as retirement, presentation
from quarterdeck.service import Service
from quarterdeck.watch import Facts


def service(runtime):
    logs = []
    return SimpleNamespace(runtime=runtime, store=post.Store(runtime), attempts=post.Attempts(runtime),
                           ledger=SimpleNamespace(catching_up=lambda: False),
                           log=lambda event, **fields: logs.append((event, fields)), logs=logs)


def test_service_moves_after_slow_mandatory_shared_reads(tmp_path, monkeypatch):
    s = service(tmp_path / 'runtime')
    message = s.store.new('answer', 'lead', 'builder', 'Synthetic completed delivery')
    attempt = s.attempts.open(message, 'builder', '%1', 1, '2000-01-01T00:00:00+00:00')
    s.attempts.close(attempt, 'delivered')
    original = attempt.path.read_bytes()
    clock, beats, readers = [0.0], [], []
    s.heartbeat = lambda: beats.append(clock[0])
    monkeypatch.setattr(retirement.time, 'monotonic', lambda: clock[0])
    for reader, seconds in ((s.attempts, 2), (s.store, 3)):
        call = reader.all
        def slow(*args, _call=call, _reader=reader, _seconds=seconds, **kwargs):
            readers.append(_reader)
            value = _call(*args, **kwargs)
            clock[0] += _seconds
            if progress := kwargs.get('progress'):
                progress()
            return value
        monkeypatch.setattr(reader, 'all', slow)
    retirement.due(s)
    assert s.logs[-1][1]['attempts'] == 1
    assert s.logs[-1][1]['read_seconds'] >= 10
    assert s.logs[-1][1]['seconds'] >= 10
    assert set(map(id, readers)) == {id(s.attempts), id(s.store)}
    assert len(beats) >= 2
    assert not attempt.path.exists()
    assert retirement.retired(attempt.path, s.runtime).read_bytes() == original


@pytest.fixture
def busy_service(tmp_path, monkeypatch):
    s = service(tmp_path / 'runtime')
    s.store.dir.mkdir(parents=True)
    ship_file = tmp_path / 'ship.toml'
    ship_file.write_text('[ship]\n')
    s.ship = SimpleNamespace(file=ship_file, seats={})
    s.pass_had_work = True
    clock, beats = [0.0], []
    s.heartbeat = lambda: beats.append(clock[0])
    monkeypatch.setattr(retirement.time, 'monotonic', lambda: clock[0])
    checked = retirement._Progress.__call__

    def measured(progress):
        if progress.phase == 'eligibility':
            clock[0] += 0.25  # represent classification cost independently of host load
        checked(progress)
    monkeypatch.setattr(retirement._Progress, '__call__', measured)
    at = post.now()
    prefix = at.strftime('%Y%m%d-%H%M')
    messages = [post.Message(f'ans-{prefix}-lead-{n:06x}', 'answer', 'lead', 'builder', at, 'Generated post')
                for n in range(1201)]
    for message in messages:
        (s.store.dir / f'{message.id}.json').write_text(message.to_json())
    return s, iter(messages), clock, beats


def completed_attempts(s, messages, count):
    return [s.attempts.close(s.attempts.open(next(messages), 'builder', None, None, None), 'delivered')
            for _ in range(count)]


def test_busy_service_finishes_large_eligibility_read_and_moves_a_batch(busy_service):
    s, messages, _, beats = busy_service
    attempts = completed_attempts(s, messages, 4)
    originals = {attempt.path: attempt.path.read_bytes() for attempt in attempts}
    Service.retire_post(s)
    event, result = s.logs[-1]
    assert event == 'post-retired' and result['attempts'] == result['retirable'] == 4
    assert not result.get('paused') and result['read_seconds'] > 2 * retirement.SECONDS
    assert len(beats) > 2 and retirement.reading(s.runtime) is None
    for path, raw in originals.items():
        assert not path.exists() and retirement.retired(path, s.runtime).read_bytes() == raw


def test_steady_busy_activity_shrinks_backlog_with_a_bounded_move_slice(busy_service, monkeypatch):
    s, messages, clock, _ = busy_service
    completed_attempts(s, messages, 30)
    real_move = retirement.move

    def measured_move(source, destination):
        moved = real_move(source, destination)
        clock[0] += 0.11
        return moved
    monkeypatch.setattr(retirement, 'move', measured_move)

    remaining = []
    for _ in range(3):
        completed_attempts(s, messages, 2)  # steady new deliveries keep each pass busy
        Service.retire_post(s)
        _, result = s.logs[-1]
        assert result['attempts'] == 10 and result['paused'] and result['phase'] == 'moves'
        remaining.append(len(list(s.attempts.dir.glob('*.json'))))
    assert remaining == [22, 14, 6]
    assert retirement.reading(s.runtime) is None


def paused(s, **kwargs):
    s.post_retirement_observation = dict(seconds=1, read_seconds=1, phase='attempts read',
                                        candidates=None, retirable=0, blocked=False)
    return dict(attempts=0, journals=0, paused=True)


def test_each_wait_logs_and_persistent_wait_survives_status_overflow(tmp_path, monkeypatch):
    s = service(tmp_path / 'runtime');s.runtime.mkdir()
    wall = [1000.0]
    monkeypatch.setattr(retirement.time, 'time', lambda: wall[0])
    monkeypatch.setattr(retirement, 'run', paused)
    for age in (0, 120, retirement.STALLED_AFTER):
        wall[0] = 1000 + age
        s.post_retirement_due = 0
        retirement.due(s)
    assert len(s.logs) == 3
    assert all(event == 'post-retired' and fields['paused'] for event, fields in s.logs)
    assert json.loads((s.runtime / retirement.STATUS).read_text())['since'] == 1000
    text = retirement.reading(s.runtime)
    assert text == 'retirement stalled: attempts read'
    facts = Facts('ok', 0, 0, 0, retirement=text, needs_human=['synthetic-seat'] * 50)
    assert text in facts.line()
    def held(s, **kwargs):
        paused(s)
        return dict(attempts=0, journals=0, held='ledger catch-up')
    monkeypatch.setattr(retirement, 'run', held)
    s.post_retirement_due = 0
    retirement.due(s)
    assert retirement.reading(s.runtime) == text
    def finished(s, **kwargs):
        paused(s)
        s.post_retirement_observation.update(candidates=0)
        return dict(attempts=0, journals=0)
    monkeypatch.setattr(retirement, 'run', finished)
    s.post_retirement_due = 0
    retirement.due(s)
    assert retirement.reading(s.runtime) is None
    assert not (s.runtime / retirement.STATUS).exists()


def test_changed_retirement_wait_invalidates_cached_healthy_line(tmp_path, monkeypatch):
    folder = tmp_path / 'ship';runtime = folder / 'runtime';runtime.mkdir(parents=True)
    monkeypatch.setattr(presentation.copy_state, 'select', lambda _: SimpleNamespace(warning=None, qd=None))
    monkeypatch.setattr(presentation, 'fingerprint', lambda _: [])
    monkeypatch.setattr(presentation, 'framework_stamp', lambda: [])
    monkeypatch.setattr(presentation, 'process_start', lambda _: '1')
    monkeypatch.setattr(presentation, 'fresh', lambda *_: True)
    monkeypatch.setattr(presentation.servicectl, 'read_heartbeat', lambda _: (1, 0))
    monkeypatch.setattr(presentation.servicectl, 'alive', lambda _: True)
    from quarterdeck import accounting_health, ledger_recovery
    monkeypatch.setattr(accounting_health, 'reading', lambda _: None)
    monkeypatch.setattr(ledger_recovery, 'alarms', lambda _: [])
    line = 'service ok · 0 waiting · qd status'
    (runtime / presentation.NAME).write_text(json.dumps(dict(format=1, line=line, wall=1, monotonic=1,
        ship=[], framework=[], pid=1, process_start='1', accounting=None, ledger_recovery=[], retirement=None)))
    assert presentation.cached_line(folder) == line
    (runtime / retirement.STATUS).write_text(json.dumps(dict(format=2, misses=2,
        since=retirement.time.time() - retirement.STALLED_AFTER - 1, phase='store read')))
    assert presentation.cached_line(folder) is None


def test_failed_pass_is_logged_and_cannot_clear_a_wait(tmp_path, monkeypatch):
    s = service(tmp_path / 'runtime');s.runtime.mkdir()
    wall = [1000.0]
    monkeypatch.setattr(retirement.time, 'time', lambda: wall[0])
    def failed(s, **kwargs):
        paused(s)
        raise OSError('synthetic read failure')
    monkeypatch.setattr(retirement, 'run', failed)
    for age in (0, retirement.STALLED_AFTER):
        wall[0] = 1000 + age
        s.post_retirement_due = 0
        with pytest.raises(OSError):
            retirement.due(s)
    assert len(s.logs) == 2
    assert s.logs[-1][1]['failed'] is True
    assert retirement.reading(s.runtime) == 'retirement stalled: attempts read'


def test_accounting_gate_does_not_emit_a_delivery_event(tmp_path):
    s = service(tmp_path / 'runtime');s.runtime.mkdir()
    s.ledger.catching_up = lambda: True
    retirement.due(s)
    assert s.logs == []
    assert s.post_retirement_observation['phase'] == 'ledger catch-up'
