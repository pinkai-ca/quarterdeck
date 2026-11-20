"""Ordinary check outcomes and teardown on stand-ins without a provider."""

from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import check, check_outcome, cli, launch_opening, lifecycle, pending
from quarterdeck import seat as seats
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.muse_usage import decode
from quarterdeck.ship import Pool, Ship, ShipError


RUN = 'check-fake-c01d'
READABLE = '{"schemaVersion":1,"allowance":{"window":{"usedPercent":20,"resetsAtMs":1893459600000}}}'


class Injected(RuntimeError):
    pass


@pytest.fixture
def task_check(tmp_path, monkeypatch):
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {'fake': Pool('fake', 'synthetic')}, {}, {})
    adapter = FakeHarness()
    monkeypatch.setattr(adapter, 'minimal_check_task', 'Write a synthetic proof.')
    monkeypatch.setattr(adapter, 'requires_deferred_opening', True)
    monkeypatch.setattr(seats.harnesses, 'get', lambda name: adapter)
    monkeypatch.setattr(check, '_version_in_runtime', lambda *args: '1.0')
    monkeypatch.setattr(check, 'program_where', lambda *args: None)
    monkeypatch.setattr(check.secrets, 'token_hex', lambda size: 'c01d' if size == 2 else 'a' * (size * 2))
    session = set()
    calls = []
    faults = SimpleNamespace(at=None, usage=decode(READABLE))
    real_make = seats.trusted_files.make
    real_replace = check.durable.replace_file
    real_registration = pending.check_inbox_registration
    real_proof = check.proof_verdict
    real_publish = check_outcome.publish

    def reach(stage):
        calls.append(stage)
        if stage == faults.at:
            raise Injected(stage)

    def make(*args, **kwargs):
        real_make(*args, **kwargs)
        reach('trusted')

    def write(path, body):
        if path.name == seats.TEMPORARY:
            reach('temporary')
        return real_replace(path, body)

    @contextmanager
    def registration(*args, **kwargs):
        with real_registration(*args, **kwargs):
            reach('registration')
            yield

    def new_session(name, **kwargs):
        reach('session')
        session.add(name)
        return name

    def kill_session(name):
        session.remove(name)
        reach('session-end')

    tmux = SimpleNamespace(has_session=session.__contains__, new_session=new_session,
                           kill_session_id=kill_session, status_line=lambda *_: reach('status'))

    def build_home(home, *_):
        home.mkdir(parents=True, exist_ok=True)
        reach('home')

    def usage(*_):
        reach('usage-after' if 'usage-before' in calls else 'usage-before')
        return faults.usage

    def up(seat, **kwargs):
        with seat._lifecycle():
            reach('launch')
            (seat.spec.cwd / 'proof.txt').write_text('OK\n')
            return 'synthetic launch'

    def down(seat):
        with seat._lifecycle(wait=True):
            reach('down')
            return 'synthetic down'

    def delivered(*_):
        reach('opening')
        return True

    def record(_):
        reach('record')
        return Path('synthetic.record')

    def proof(*args):
        reach('proof')
        return real_proof(*args)

    def publish(runtime, name, observed, **kwargs):
        reach('outcome-begin' if observed['reason'] == 'incomplete' else
              'outcome-end' if observed['outcome'] == 'passed' else 'outcome-other')
        return real_publish(runtime, name, observed, **kwargs)

    monkeypatch.setattr(seats.trusted_files, 'make', make)
    monkeypatch.setattr(check.durable, 'replace_file', write)
    monkeypatch.setattr(pending, 'check_inbox_registration', registration)
    monkeypatch.setattr(adapter, 'build_home', build_home)
    monkeypatch.setattr(adapter, 'check_usage_in_home', usage)
    monkeypatch.setattr(adapter, 'state', lambda record: SimpleNamespace(turns=1, turn_open=False, queued=False, model=None))
    monkeypatch.setattr(seats.Seat, 'up', up)
    monkeypatch.setattr(seats.Seat, 'down', down)
    monkeypatch.setattr(seats.Seat, 'is_up', property(lambda _: True))
    monkeypatch.setattr(seats.Seat, 'record', record)
    monkeypatch.setattr(launch_opening, 'delivered', delivered)
    monkeypatch.setattr(check, 'proof_verdict', proof)
    monkeypatch.setattr(check_outcome, 'publish', publish)
    return ship, tmux, session, calls, faults


def check_scratch_is_retired(ship, session):
    runtime = ship.runtime
    transient, registration = pending._check_paths(runtime, RUN)
    assert not session
    assert not (runtime / 'seats' / RUN).exists()
    assert not (runtime / 'trusted' / RUN).exists()
    assert not (runtime / 'check' / RUN / 'task').exists()
    assert not registration.exists() and not transient.exists()
    assert not lifecycle.lock_path(runtime, RUN).exists()


@pytest.mark.parametrize(('native', 'reason', 'words'), [
    ('{"schemaVersion":1,"allowance":null}', 'usage-unavailable', 'allowance unavailable'),
    ('{"schemaVersion":1,"allowance":null,"reason":"no_allowance"}', 'usage-unavailable', 'allowance unavailable'),
    ('synthetic unreadable private response', 'usage-unreadable', 'allowance unreadable'),
    ('{"schemaVersion":1,"allowance":{"unmeasured":{}}}', 'usage-unreadable', 'allowance unreadable'),
])
def test_unusable_allowance_returns_the_ordinary_failure_without_launch(task_check, monkeypatch, native, reason, words):
    ship, tmux, session, calls, faults = task_check
    faults.usage = decode(native)
    monkeypatch.setattr(cli, 'load_ship', lambda _: ship)
    monkeypatch.setattr(cli.Tmux, 'for_ship', lambda _: tmux)
    args = SimpleNamespace(harness='fake', minimal=True)
    lines = cli.cmd_check(args)
    assert args.exit_code == 1
    assert any(line.startswith('FAIL  native usage read before the turn') and words in line for line in lines)
    assert 'launch' not in calls and 'usage-after' not in calls
    evidence = ship.runtime / 'check' / RUN
    assert json.loads((evidence / 'usage-before.json').read_text())['windows'] == []
    outcomes = ship.runtime / 'minimal-checks'
    data = check_outcome.read(outcomes, 'fake')
    assert data['definitive']['reason'] == reason and data['definitive']['step'] == 'usage'
    assert words in check_outcome.alarms([data])[0]
    assert 'synthetic unreadable private response' not in str(lines) + str(data)
    check_scratch_is_retired(ship, session)


@pytest.mark.parametrize('stage', [
    'outcome-begin', 'trusted', 'temporary', 'registration', 'session', 'status',
    'home', 'usage-before', 'launch', 'opening', 'record', 'proof', 'usage-after',
    'down', 'session-end', 'outcome-end',
])
def test_an_exception_at_each_stage_retires_scratch_and_retains_saved_evidence(task_check, stage):
    ship, tmux, session, calls, faults = task_check
    faults.at = stage
    with pytest.raises(Injected, match=stage):
        check.check(ship, 'fake', tmux, minimal=True)
    check_scratch_is_retired(ship, session)
    evidence = ship.runtime / 'check' / RUN
    if 'usage-before' in calls and stage != 'usage-before':
        assert json.loads((evidence / 'usage-before.json').read_text())['windows'][0]['used'] == .2
    if 'usage-after' in calls and stage != 'usage-after':
        assert json.loads((evidence / 'usage-after.json').read_text())['windows'][0]['used'] == .2
    if stage in ('usage-after', 'down', 'session-end', 'outcome-end'):
        assert (evidence / 'proof.txt').read_bytes() == b'OK\n'


def test_credential_hold_keeps_its_home_while_check_scratch_retires(task_check, monkeypatch):
    ship, tmux, session, calls, faults = task_check
    from quarterdeck import credential

    monkeypatch.setattr(credential, 'hold_home', lambda *a, **k: True)
    assert all(row.passed for row in check.check(ship, 'fake', tmux, minimal=True))
    runtime = ship.runtime
    assert (runtime / 'seats' / RUN / 'home').is_dir()
    assert not session and not (runtime / 'check' / RUN / 'task').exists()
    assert not lifecycle.lock_path(runtime, RUN).exists()
    assert not any(path.exists() for path in pending._check_paths(runtime, RUN))


def test_down_retires_only_the_exact_empty_scratch_and_never_calls_a_check(tmp_path, monkeypatch):
    name = 'check-muse-0c64'
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {}, {}, {})
    runtime = ship.runtime
    evidence = runtime / 'check' / name
    task = evidence / 'task'
    task.mkdir(parents=True)
    (evidence / 'usage-before.json').write_bytes(b'synthetic saved usage\n')
    (evidence / 'proof.txt').write_bytes(b'synthetic saved proof\n')
    history = check_outcome.path(runtime / 'minimal-checks', 'muse')
    history.parent.mkdir(parents=True)
    history.write_bytes(b'synthetic saved history\n')
    transient, _ = pending._check_paths(runtime, name)
    with pending.transient_lock(runtime, name), lifecycle.claim(runtime, name):
        pass
    lifecycle_lock = lifecycle.lock_path(runtime, name)
    neighbor = runtime / 'check' / 'check-muse-0c65'
    neighbor.mkdir()
    monkeypatch.setattr(cli, 'load_ship', lambda _: ship)
    monkeypatch.setattr(cli.Tmux, 'for_ship', lambda _: SimpleNamespace(has_session=lambda _: False))
    monkeypatch.setattr(check.harnesses, 'get', lambda *a: pytest.fail('cleanup must not select a program'))
    monkeypatch.setattr(seats.Seat, 'up', lambda *a, **k: pytest.fail('cleanup must not launch'))
    args = SimpleNamespace(seat=name)
    with pending.transient_lock(runtime, name):
        with pytest.raises(ShipError, match='still running'):
            cli.cmd_down(args)
    assert task.is_dir() and transient.is_file() and lifecycle_lock.is_file()
    assert cli.cmd_down(args) == f'{name}: empty check scratch and coordination retired'
    assert not task.exists() and not transient.exists() and not lifecycle_lock.exists()
    assert (evidence / 'usage-before.json').read_bytes() == b'synthetic saved usage\n'
    assert (evidence / 'proof.txt').read_bytes() == b'synthetic saved proof\n'
    assert history.read_bytes() == b'synthetic saved history\n'
    assert neighbor.is_dir()
