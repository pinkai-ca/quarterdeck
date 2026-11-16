"""Only a complete, stable retirement sweep earns a bounded scheduling wait."""

from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest

from quarterdeck import post, post_retirement as retirement, retirement_gate as gate


@pytest.fixture
def clock(monkeypatch):
    import time
    wall = time.time_ns() + 100_000_000_000
    value = SimpleNamespace(wall=wall, mono=0.0)
    monkeypatch.setattr(gate.time, 'time_ns', lambda: value.wall)
    monkeypatch.setattr(gate.time, 'monotonic', lambda: value.mono)
    return value


def service(tmp_path, names=('builder',)):
    runtime = tmp_path / 'runtime'
    runtime.mkdir(parents=True)
    declaration = tmp_path / 'ship.toml'
    declaration.write_text('# Synthetic declaration\n')
    logs = []
    s = SimpleNamespace(runtime=runtime, ship=SimpleNamespace(file=declaration, seats=dict.fromkeys(names)),
        store=post.Store(runtime), attempts=post.Attempts(runtime),
        ledger=SimpleNamespace(catching_up=lambda: False),
        log=lambda event, **fields: logs.append((event, fields)), logs=logs)
    for name in names:
        (runtime / 'post' / 'pending' / name).mkdir(parents=True)
    (runtime / 'post' / 'attempts').mkdir()
    s.store.all()
    s.attempts.all()
    return s


def certify(s):
    retirement.due(s, idle=True)
    assert s.post_retirement_gate.certificate is not None


def test_quiet_complete_sweep_waits_until_fixed_backstop_and_new_service_cannot_reuse_it(tmp_path, clock):
    s = service(tmp_path)
    certify(s)
    deadline = s.post_retirement_gate.until
    for tick in (1, 50, 299):
        clock.mono = tick
        retirement.due(s, idle=True)
        assert len(s.logs) == 1
        assert s.post_retirement_gate.until == deadline
    clock.mono = 300
    retirement.due(s, idle=True)
    assert len(s.logs) == 2
    fresh = SimpleNamespace(**{key: value for key, value in vars(s).items()
                               if not key.startswith(('post_retirement', '_post_retirement'))})
    retirement.due(fresh, idle=True)
    assert len(s.logs) == 3


@pytest.mark.parametrize('relative', [
    'post/store', 'post/imports', 'post/forwarded', 'post/addressed',
    'post/attempts/synthetic.json', 'post/pending/builder/synthetic.json',
    'requests/synthetic.json', 'post/retired/index', 'post/retired/completed',
    'post/retired/attempts', 'post/retired/pending/builder',
    'trusted/builder/launch.json', 'service.delivery',
])
def test_each_input_change_wakes_the_next_scan(tmp_path, clock, monkeypatch, relative):
    s = service(tmp_path)
    path = s.runtime / relative
    directory = relative in ('post/store', 'post/imports', 'post/forwarded', 'post/addressed',
                            'post/retired/index', 'post/retired/completed',
                            'post/retired/attempts', 'post/retired/pending/builder')
    path.parent.mkdir(parents=True, exist_ok=True)
    if directory:
        path.mkdir(exist_ok=True)
    else:
        path.write_text('{}')
    g = gate.Gate()
    assert g.begin(s, 0)
    progress = SimpleNamespace(names=frozenset(), visited=set(), blocked=False)
    g.finish(s, dict(attempts=0, journals=0), progress, 300)
    assert not g.begin(s, 1)
    if directory:
        (path / 'new.json').write_text('{}')
    else:
        path.write_text('{"changed":true}')
    assert g.begin(s, 2)
    assert g.certificate is None


def test_declaration_and_ledger_gate_changes_wake_scan(tmp_path, clock):
    s = service(tmp_path)
    certify(s)
    s.ship.file.write_text('# Changed declaration\n')
    clock.mono = 1
    assert s.post_retirement_gate.begin(s, clock.mono)
    certify(s)
    s.ledger.catching_up = lambda: True
    retirement.due(s, idle=True)
    assert s.post_retirement_gate.certificate is None


def test_zero_move_slices_carry_cursor_but_certify_only_after_complete_sweep(tmp_path, clock, monkeypatch):
    s = service(tmp_path, ('alpha', 'beta', 'gamma', 'omega'))
    original = retirement._Progress.examining
    examined = []
    @contextmanager
    def examining(progress, name):
        with original(progress, name):
            yield
        examined.append(name)
        clock.mono += .6
    monkeypatch.setattr(retirement._Progress, 'examining', examining)
    retirement.due(s, idle=True)
    assert s.logs[-1][1]['paused']
    assert s.post_retirement_gate.certificate is None
    cursor = s.post_retirement_cursor
    assert len(examined) == 2
    retirement.due(s, idle=True)
    assert examined[2] == cursor
    assert len(examined) == len(set(examined)) == 4
    assert s.post_retirement_gate.certificate is not None


@pytest.mark.parametrize('problem', ['failed', 'blocked', 'partial', 'young', 'unreadable', 'clock-backward'])
def test_uncertain_sweeps_never_certify(tmp_path, clock, monkeypatch, problem):
    s = service(tmp_path)
    g = gate.Gate()
    assert g.begin(s, 10)
    result = dict(attempts=0, journals=0)
    progress = SimpleNamespace(names=frozenset(('builder',)), visited={'builder'}, blocked=False)
    if problem == 'failed':
        result['failed'] = True
    elif problem == 'blocked':
        progress.blocked = True
    elif problem == 'partial':
        result['paused'] = True
    elif problem == 'young':
        clock.wall = s.ship.file.stat().st_ctime_ns
    elif problem == 'unreadable':
        monkeypatch.setattr(gate, 'capture', lambda _: None)
    else:
        clock.wall -= 1
        assert g.begin(s, 9)
    g.finish(s, result, progress, 300)
    assert g.certificate is None


def test_concurrent_change_discards_completed_examinations(tmp_path, clock):
    s = service(tmp_path)
    g = gate.Gate()
    assert g.begin(s, 0)
    s.ship.file.write_text('changed during sweep\n')
    g.finish(s, dict(attempts=0, journals=0),
             SimpleNamespace(names=frozenset(), visited=set(), blocked=False), 300)
    assert g.certificate is None and g.sweep is None


def test_moving_batches_keep_draining_and_preserve_original_bytes(tmp_path, clock):
    s = service(tmp_path, ('alpha', 'beta', 'gamma'))
    originals = []
    for name in s.ship.seats:
        message = s.store.new('answer', 'lead', name, 'Synthetic completed task')
        attempt = s.attempts.open(message, name, '%1', 100, '2000-01-01T00:00:00+00:00')
        s.attempts.close(attempt, 'delivered')
        originals.append((attempt.path, attempt.path.read_bytes()))
    for count in range(3):
        retirement.due(s, idle=True)
        assert s.logs[-1][1]['attempts'] == 1
        assert s.post_retirement_gate.certificate is None
        assert sum(path.exists() for path, _ in originals) == 2 - count
    certify(s)
    for path, original in originals:
        assert retirement.retired(path, s.runtime).read_bytes() == original


@pytest.mark.parametrize('change', ['closure', 'receipt', 'journal', 'launch'])
def test_changed_eligibility_rechecks_and_moves_original_evidence(tmp_path, clock, change):
    s = service(tmp_path)
    message = s.store.new('order' if change == 'closure' else 'answer',
                          'lead', 'builder', 'Synthetic task')
    attempt = s.attempts.open(message, 'builder', '%1', 100, '2000-01-01T00:00:00+00:00')
    if change != 'receipt':
        s.attempts.close(attempt, 'delivered')
    journal = s.runtime / 'post' / 'pending' / 'builder' / attempt.path.name
    launch = s.runtime / 'trusted' / 'builder' / 'launch.json'
    if change == 'journal':
        data = dict(seat='builder', identity=dict(pane='%1', pid=100, harness='fake', launch=attempt.launched_at),
                    at=attempt.at, since=attempt.at, payload=post.typed([message]), phase='queued',
                    record=None, shown=None, attempts=[attempt.path.name])
        journal.write_text(json.dumps(data))
    elif change == 'launch':
        launch.parent.mkdir(parents=True)
        launch.write_text(json.dumps(dict(pane='%1', pid=100, at=attempt.launched_at)))
    certify(s)
    assert attempt.path.exists()
    if change == 'closure':
        s.store.new('report', 'builder', 'lead', 'Synthetic completion', re=message.id)
    elif change == 'receipt':
        s.attempts.close(attempt, 'delivered')
    elif change == 'journal':
        data['phase'] = 'delivered'
        journal.write_text(json.dumps(data))
    else:
        launch.unlink()
    original = attempt.path.read_bytes()
    journal_bytes = journal.read_bytes() if journal.exists() else None
    retirement.due(s, idle=True)
    assert s.logs[-1][1]['attempts'] == 1
    assert retirement.retired(attempt.path, s.runtime).read_bytes() == original
    if journal_bytes is not None:
        assert retirement.retired(journal, s.runtime).read_bytes() == journal_bytes


def test_failed_pass_retains_stall_deadline_and_does_not_wait(tmp_path, clock, monkeypatch):
    s = service(tmp_path)
    wall = [1000.0]
    monkeypatch.setattr(retirement.time, 'time', lambda: wall[0])
    def failed(s, **kwargs):
        s.post_retirement_counts = dict(attempts=0, journals=0)
        s.post_retirement_observation = dict(seconds=0, read_seconds=0, phase='store read',
            candidates=None, retirable=0, blocked=False)
        raise OSError('synthetic read failure')
    monkeypatch.setattr(retirement, 'run', failed)
    for now in (0, 300):
        clock.mono = now
        wall[0] = 1000 + now
        with pytest.raises(OSError):
            retirement.due(s, idle=True)
    assert retirement.reading(s.runtime) == 'retirement stalled: store read'
    assert len(s.logs) == 2
    assert json.loads((s.runtime / retirement.STATUS).read_text())['since'] == 1000
