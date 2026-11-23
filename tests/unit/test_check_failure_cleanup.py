"""Ordinary check outcomes and teardown on stand-ins without a provider."""

from contextlib import contextmanager, ExitStack
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import brief_change, check, check_outcome, cli, launch_opening, lifecycle, pending
from quarterdeck import ship_locks, trusted_files
from quarterdeck import seat as seats
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.limits import Usage
from quarterdeck.harness.muse_usage import decode
from quarterdeck.ship import Pool, Ship, ShipError


RUN = 'check-fake-c01d'
READABLE = '{"schemaVersion":1,"allowance":{"window":{"usedPercent":20,"resetsAtMs":1893459600000}}}'
UNAVAILABLE = [
    '{"schemaVersion":1,"allowance":null}',
    '{"schemaVersion":1,"allowance":null,"reason":"no_allowance"}',
]


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
    faults = SimpleNamespace(at=None, usage=decode(READABLE), after=None)
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
        after = 'usage-before' in calls
        reach('usage-after' if after else 'usage-before')
        return faults.after if after and faults.after is not None else faults.usage

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


@pytest.mark.parametrize(('reading', 'reason'), [
    (decode('synthetic unreadable private response'), 'usage-unreadable'),
    (decode('{"schemaVersion":1,"allowance":{"unmeasured":{}}}'), 'usage-unreadable'),
    (Usage((), decode(READABLE).read_at, 'Muse check settings changed; no usage call', called=False),
     'usage-unavailable'),
    (Usage((), decode(READABLE).read_at, 'Muse check usage command refused'), 'usage-unreadable'),
    (Usage((), decode(READABLE).read_at, 'rate limited', retry_after=900), 'usage-unreadable'),
])
def test_unusable_allowance_returns_the_ordinary_failure_without_launch(task_check, monkeypatch, reading, reason):
    ship, tmux, session, calls, faults = task_check
    faults.usage = reading
    words = check_outcome.REASONS[reason][1]
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


@pytest.mark.parametrize('before,after', [
    (UNAVAILABLE[0], READABLE), (READABLE, UNAVAILABLE[1]), tuple(UNAVAILABLE),
])
def test_explicitly_unavailable_allowance_continues_and_saves_unknown(task_check, monkeypatch, before, after):
    ship, tmux, session, calls, faults = task_check
    faults.usage, faults.after = decode(before), decode(after)
    monkeypatch.setattr(cli, 'load_ship', lambda _: ship)
    monkeypatch.setattr(cli.Tmux, 'for_ship', lambda _: tmux)
    args = SimpleNamespace(harness='fake', minimal=True)
    lines = cli.cmd_check(args)
    assert args.exit_code == 0
    assert calls.count('launch') == calls.count('opening') == 1
    assert 'PASS  native usage share spent · usage unknown' in lines
    evidence = ship.runtime / 'check' / RUN
    expected = {'share_spent': 'unknown'}
    for stage, native in [('before', before), ('after', after)]:
        saved = json.loads((evidence / f'usage-{stage}.json').read_text())
        line = next(line for line in lines if f'native usage read {stage} the turn' in line)
        if native in UNAVAILABLE:
            assert 'usage unknown' in line
            assert saved['status'] == 'unknown' and saved['windows'] == []
            expected[stage] = 'unknown'
        else:
            assert saved['windows'][0]['used'] == .2
    final = check_outcome.read(ship.runtime / 'minimal-checks', 'fake')['definitive']
    assert final['outcome'] == 'passed' and final['usage'] == expected
    check_scratch_is_retired(ship, session)


def test_a_refused_after_read_still_fails_when_before_usage_is_unknown(task_check):
    ship, tmux, session, calls, faults = task_check
    faults.usage = decode(UNAVAILABLE[0])
    faults.after = Usage((), faults.usage.read_at, 'rate limited', retry_after=900)
    checks = check.check(ship, 'fake', tmux, minimal=True)
    assert any(row.name == 'native usage read after the turn' and row.passed is False for row in checks)
    final = check_outcome.read(ship.runtime / 'minimal-checks', 'fake')['definitive']
    assert final['reason'] == 'usage-unreadable'
    assert final['usage'] == {'before': 'unknown', 'share_spent': 'unknown'}
    assert calls.count('launch') == calls.count('opening') == 1
    check_scratch_is_retired(ship, session)


@pytest.mark.parametrize(('outcome', 'held', 'brief'), [
    ('passed', False, 'Synthetic brief\n'),
    ('passed', True, ''),
    ('failed', False, ''),
    ('failed', True, 'Synthetic brief\n'),
    ('probe exception', True, 'Synthetic brief\n'),
    ('startup exception', True, 'Synthetic brief\n'),
])
def test_retired_check_removes_only_its_brief_and_empty_free_locks(
        task_check, monkeypatch, capsys, outcome, held, brief):
    ship, tmux, session, calls, faults = task_check
    runtime = ship.runtime
    other = RUN + '--other'
    brief_change.mark(runtime, other, 'Keep this brief')
    other_lock = ship_locks.for_seat_file(trusted_files.folder(runtime, other), trusted_files.READERS)
    with ship_locks.held(other_lock, 'synthetic'):
        pass
    check_folder = runtime / 'check' / RUN
    saved = check_folder / 'saved.txt'
    folder = trusted_files.folder(runtime, RUN)
    locks = [ship_locks.for_seat_file(folder, name) for name in
             ('move', trusted_files.READERS, trusted_files.LAUNCHES)]
    reader_lock = locks[1]
    status_line = tmux.status_line
    outcomes = runtime / 'minimal-checks'
    check_outcome.publish(outcomes, 'fake', check_outcome.event('passed', 'earlier', 'passed'))
    previous = check_outcome.read(outcomes, 'fake')['definitive']

    with ExitStack() as holders:
        def status(*args):
            brief_change.mark(runtime, RUN, brief)
            for lock in locks:
                with ship_locks.held(lock, 'synthetic'):
                    pass
            if held:
                holders.enter_context(ship_locks.held(reader_lock, 'synthetic'))
                locks[0].write_text('Keep this nonempty lock')
            saved.write_text('Keep the evidence')
            if outcome == 'startup exception':
                raise ShipError('synthetic startup abort')
            status_line(*args)

        monkeypatch.setattr(tmux, 'status_line', status)
        monkeypatch.setattr(cli, 'load_ship', lambda _: ship)
        monkeypatch.setattr(cli.Tmux, 'for_ship', lambda _: tmux)
        if outcome == 'failed':
            faults.after = Usage((), faults.usage.read_at, 'synthetic bad usage')
        if outcome == 'probe exception':
            faults.at = 'proof'
            with pytest.raises(Injected) as raised:
                check.check(ship, 'fake', tmux, minimal=True)
            assert str(raised.value) == 'proof'
            lines = '\n'.join(raised.value.__notes__)
        elif outcome == 'startup exception':
            monkeypatch.setattr(cli, 'handoff', lambda *_: None)
            monkeypatch.setattr(check.harnesses.get('fake'), 'synthetic', False)
            assert cli.main(['--ship', str(ship.folder), 'check', 'fake', '--minimal']) == 2
            lines = capsys.readouterr().err
            assert 'qd: synthetic startup abort' in lines
        else:
            args = SimpleNamespace(harness='fake', minimal=True)
            lines = '\n'.join(cli.cmd_check(args))
            assert args.exit_code == (1 if outcome == 'failed' else 0)

        assert (f'{reader_lock.name}: held seat-file lock retained' in lines) is held
        assert not locks[2].exists()
        if held:
            assert reader_lock.stat().st_size == 0
            assert locks[0].read_text() == 'Keep this nonempty lock'
        else:
            assert not reader_lock.exists() and not locks[0].exists()

    check_scratch_is_retired(ship, session)
    assert brief_change.told(runtime, RUN) is None
    assert brief_change.told(runtime, other) == 'Keep this brief'
    assert other_lock.is_file()
    assert saved.read_text() == 'Keep the evidence'
    history = check_outcome.read(outcomes, 'fake')
    if outcome.endswith('exception'):
        assert history['latest_attempt']['outcome'] == 'unknown'
        assert history['latest_attempt']['reason'] == 'unclassified'
        assert history['definitive'] == previous
    else:
        assert history['definitive']['outcome'] == outcome
        assert (check_folder / 'proof.txt').read_bytes() == b'OK\n'


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


@pytest.fixture
def abandoned_scratch(tmp_path, monkeypatch):
    name = 'check-muse-0c64'
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {}, {}, {})
    runtime = ship.runtime
    evidence = runtime / 'check' / name
    task = evidence / 'task'
    task.mkdir(parents=True)
    history = check_outcome.path(runtime / 'minimal-checks', 'muse')
    history.parent.mkdir(parents=True)
    saved = {
        evidence / 'usage-before.json': b'synthetic saved usage\n',
        evidence / 'proof.txt': b'synthetic saved proof\n',
        history: b'synthetic saved history\n',
    }
    for path, content in saved.items():
        path.write_bytes(content)
    transient, _ = pending._check_paths(runtime, name)
    with pending.transient_lock(runtime, name), lifecycle.claim(runtime, name):
        pass
    lifecycle_lock = lifecycle.lock_path(runtime, name)
    monkeypatch.setattr(cli, 'load_ship', lambda _: ship)
    monkeypatch.setattr(cli.Tmux, 'for_ship', lambda _: SimpleNamespace(has_session=lambda _: False))
    monkeypatch.setattr(seats.Seat, 'up', lambda *a, **k: pytest.fail('cleanup must not launch'))
    return ship, name, task, transient, lifecycle_lock, saved


def test_down_retires_only_the_exact_empty_scratch_and_never_calls_a_check(abandoned_scratch, monkeypatch):
    ship, name, task, transient, lifecycle_lock, saved = abandoned_scratch
    runtime = ship.runtime
    neighbor = runtime / 'check' / 'check-muse-0c65'
    neighbor.mkdir()
    monkeypatch.setattr(check.harnesses, 'get', lambda *a: pytest.fail('cleanup must not select a program'))
    args = SimpleNamespace(seat=name)
    with pending.transient_lock(runtime, name):
        with pytest.raises(ShipError, match='still running'):
            cli.cmd_down(args)
    assert task.is_dir() and transient.is_file() and lifecycle_lock.is_file()
    assert cli.cmd_down(args) == f'{name}: empty check scratch and coordination retired'
    assert not task.exists() and not transient.exists() and not lifecycle_lock.exists()
    for path, content in saved.items():
        assert path.read_bytes() == content
    assert neighbor.is_dir()


def test_down_refuses_nonempty_check_scratch_and_retains_its_artifacts(abandoned_scratch, capsys):
    ship, name, task, transient, lifecycle_lock, saved = abandoned_scratch
    leftover = task / 'leftover.txt'
    leftover.write_bytes(b'synthetic task contents\n')
    coordination = {path: path.stat().st_ino for path in (transient, lifecycle_lock)}

    assert cli.main(['--ship', str(ship.folder), 'down', name]) == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == f'qd: {task} still holds files\n'
    assert list(task.iterdir()) == [leftover]
    assert leftover.read_bytes() == b'synthetic task contents\n'
    for path, inode in coordination.items():
        assert path.stat().st_ino == inode and path.read_bytes() == b''
    for path, content in saved.items():
        assert path.read_bytes() == content
