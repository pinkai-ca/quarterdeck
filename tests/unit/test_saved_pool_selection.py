"""A missing selection survives lifecycle changes; unresolved seats stay named holds."""

from datetime import timedelta
from dataclasses import replace
import json
import os
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import cli, pending, post, quota, record_owner, seat as seatmod, service, ship as shipmod, watch
from quarterdeck.harness import codex
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.synthetic_record import CodexShape
from quarterdeck.harness.types import Screen
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.tmux import Pane
from .test_pool_choices import declaration

CHOICES = '["first", {pool="second",harness="claude",model="synthetic-target"}]'


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    plain = declaration(tmp_path, '"first"')
    live, launches = {}, []
    tmux = SimpleNamespace(pane=lambda ident: live.get(ident), capture=lambda *a, **k: '',
                           kill_window=lambda ident, *, expected_pid=None: live.pop(ident, None))
    original = Seat(plain, plain.seat('worker'), tmux)

    def own(seat, ident):
        seat.home.mkdir(parents=True, exist_ok=True)
        record = CodexShape(seat.home, tmp_path, f'synthetic-{seat.name}', 'synthetic-source')
        record.begin('Synthetic previous input', 'synthetic-source', 'high')
        record.end('Synthetic previous reply')
        pane = Pane(ident, 'crew', seat.name, 100 + len(live), 80, 24, False, False, None)
        live[ident] = pane
        Launched(ident, pane.pid, (post.now()-timedelta(minutes=1)).isoformat(), 'first', None,
                 ['codex'], 'synthetic', 'codex').write(seat.folder/'launch.json')
        (seat.folder/'record').write_text(str(record.path))
        assert seat.record() == record.path
        return record

    source = own(original, '%1')
    assert not (original.folder/'pool').exists()
    changed = declaration(tmp_path, CHOICES)
    with changed.file.open('a') as out:
        out.write('\n[seats.healthy]\nsession="crew"\nharness="codex"\npool="first"\nmodel="synthetic-source"\n')
    changed = shipmod.load(tmp_path)
    healthy = Seat(changed, changed.seat('healthy'), tmux)
    healthy_source = own(healthy, '%2')
    worker = Seat(changed, changed.seat('worker'), tmux)

    def open_window(seat, argv, env, path):
        launches.append((seat.spec.harness, seat.pool.name, argv))
        if 'resume' not in argv:
            fresh = CodexShape(seat.home, tmp_path, 'synthetic-fresh', seat.spec.model)
            fresh.begin('Synthetic fresh input', seat.spec.model, 'high')
            fresh.end('Synthetic fresh reply')
        pane = Pane('%3', 'crew', seat.name, 303, 80, 24, False, False, None)
        live[pane.id] = pane
        return pane

    monkeypatch.setattr(Seat, '_open_window', open_window)
    monkeypatch.setattr(Seat, '_survives', lambda *a: None)
    monkeypatch.setattr(seatmod.shutil, 'which', lambda *a, **k: '/usr/bin/sh')
    monkeypatch.setattr(cli.Tmux, 'for_ship', lambda ship: tmux)
    for adapter in (CodexHarness, ClaudeHarness):
        monkeypatch.setattr(adapter, 'version', lambda self: 'synthetic')
        monkeypatch.setattr(adapter, 'screen', lambda *a: Screen('', ''))
        monkeypatch.setattr(adapter, 'usage', lambda *a: pytest.fail('no provider call is authorized'))
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    monkeypatch.setattr(quota, 'refresh', lambda *a: None)
    monkeypatch.setattr(quota, 'refresh_declared', lambda *a: None)
    return SimpleNamespace(ship=changed, worker=worker, healthy=healthy, source=source,
                           healthy_source=healthy_source, tmux=tmux, live=live, launches=launches)


@pytest.mark.parametrize('action', ['down-up', 'restart', 'fresh'])
def test_plain_seat_gaining_choices_preserves_its_pool_before_stop(migrated, action):
    r = migrated
    store, attempts = post.Store(r.ship.runtime), post.Attempts(r.ship.runtime)
    message = store.new('order', 'issuer', 'worker', 'Synthetic open work remains open')
    old = attempts.open(message, 'worker', '%1', r.worker.launched().pid, r.worker.launched().at)
    attempts.close(old, 'blind', 'Synthetic old submission is unconfirmed')
    original = r.source.path.read_bytes()
    if action == 'down-up':
        r.worker.down()
        r.worker = Seat(r.ship, r.ship.seat('worker'), r.tmux)
        r.worker.up()
    else:
        getattr(r.worker, action)()
    restored = Seat(r.ship, r.ship.seat('worker'), r.tmux)
    assert (restored.folder/'pool').read_text() == 'first\n'
    assert restored.harness.name == 'codex' and restored.pool.name == 'first'
    assert restored.launched().pool == 'first' and restored.launched().harness == 'codex'
    assert bool(restored.launched().resume) is (action != 'fresh')
    assert message.id in r.launches[0][2][-1] and f'qd thread {message.id}' in r.launches[0][2][-1]
    assert message.body not in r.launches[0][2][-1]
    assert store.is_open(message) and attempts.for_message(message.id)[0].result == 'blind'
    assert attempts.latest()[message.id] == 'launch'
    assert r.source.path.read_bytes() == original
    assert record_owner.pinned_reader(restored, r.source.path) == 'codex'
    events = record_owner.events(restored.folder/'record-launches.jsonl')
    assert any(event['record'] == str(r.source.path) and event['pool'] == 'first' for event in events)


def unresolved(r):
    (r.worker.folder/'launch.json').unlink()
    assert not (r.worker.folder/'pool').exists()


@pytest.mark.usefixtures('synthetic_disk_room')  # the machine's disk is not this test's subject
def test_unresolved_selection_reads_unknown_without_reading_a_guessed_record(migrated, monkeypatch, capsys):
    r = migrated
    unresolved(r)
    original = codex.read

    def owned(path):
        assert path != r.source.path, 'unresolved current selection cannot choose a record adapter'
        return original(path)

    monkeypatch.setattr(CodexHarness, 'reader', staticmethod(owned))
    for args in (['status'], ['watch'], ['watch', 'worker'], ['status', '--line']):
        assert cli.main(['--ship', str(r.ship.folder), *args]) == 0
        text = capsys.readouterr().out
        assert 'worker' in text and 'unknown' in text and 'saved pool missing' in text
        if args in (['status'], ['watch']):
            assert 'healthy' in text
    assert cli.main(['--json', '--ship', str(r.ship.folder), 'status']) == 0
    rows = {row['name']: row for row in json.loads(capsys.readouterr().out)['seats']}
    assert rows['worker']['activity']['state'] == 'unknown'
    assert 'saved pool missing' in rows['worker']['activity']['detail']
    assert rows['worker']['pool'] == rows['worker']['harness'] == 'unknown'
    assert rows['healthy']['pool'] == 'first' and rows['healthy']['activity']['state'] == 'idle'
    assert cli.main(['--json', '--ship', str(r.ship.folder), 'watch', 'worker']) == 0
    full = json.loads(capsys.readouterr().out)
    assert full['record'] is full['state'] is full['screen'] is None
    assert 'saved pool missing' in full['record_error']
    facts = watch.facts(r.ship, r.tmux)
    assert 'saved pool missing' in facts.unknown[0]
    overflow = replace(facts, needs_human=['synthetic-name' * 30]).line()
    assert '1 selection unknown' in overflow and 'qd status' in overflow
    assert not (r.worker.folder/'pool').exists(), 'a reading must not persist a guessed selection'


@pytest.mark.parametrize('damage', ['undeclared', 'conflicting', 'unreadable'])
def test_invalid_selection_is_also_isolated_without_fallback(migrated, monkeypatch, damage):
    r = migrated
    path = r.worker.folder/'pool'
    path.write_text({'undeclared': 'unlisted', 'conflicting': 'second', 'unreadable': 'first'}[damage])
    expected = {'undeclared': 'no declared choice', 'conflicting': 'disagree', 'unreadable': 'PermissionError'}[damage]
    if damage == 'unreadable':
        read = Path.read_text
        def unavailable(file, *args, **kwargs):
            if file == path:
                raise PermissionError('PermissionError: synthetic unreadable selection')
            return read(file, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', unavailable)
    rows = {row.name: row for row in watch.status_reading(r.ship, r.tmux)['seats']}
    assert rows['worker'].activity.state == 'unknown' and expected in rows['worker'].activity.detail
    assert rows['worker'].pool == rows['worker'].harness == 'unknown'
    assert rows['healthy'].activity.state == 'idle' and rows['healthy'].pool == 'first'


def test_selection_write_failure_cannot_destroy_the_only_current_launch(migrated, monkeypatch):
    r = migrated
    path = r.worker.folder/'launch.json'
    before = path.read_bytes()
    def unavailable(*args):
        raise PermissionError('synthetic selection write refused')
    monkeypatch.setattr(r.worker, 'move_to', unavailable)
    with pytest.raises(PermissionError, match='selection write refused'):
        r.worker.down()
    assert path.read_bytes() == before and r.worker.is_up
    assert not (r.worker.folder/'pool').exists()


@pytest.fixture
def selection_service(migrated, monkeypatch, request):
    """Own the accounting child, optionally stopped before its first planning job."""
    from quarterdeck import accounting_slices

    svc = Service(migrated.ship, migrated.tmux)
    start = accounting_slices.Driver.start
    if request.param:
        def paused(driver, service):
            start(driver, service)
            os.kill(driver.process.pid, signal.SIGSTOP)
            assert os.WIFSTOPPED(os.waitpid(driver.process.pid, os.WUNTRACED)[1])
        monkeypatch.setattr(accounting_slices.Driver, 'start', paused)
        monkeypatch.setattr(accounting_slices, 'BUDGET', 0.03)

    def resume():
        driver = getattr(svc, 'account_driver', None)
        if driver is not None and driver.process is not None:
            os.kill(driver.process.pid, signal.SIGCONT)
        monkeypatch.setattr(accounting_slices.Driver, 'start', start)

    try:
        yield svc, resume, bool(request.param)
    finally:
        resume()
        if driver := getattr(svc, 'account_driver', None):
            driver.close()
        svc.ledger.close()


@pytest.mark.parametrize('selection_service', [False, True], indirect=True, ids=['ordinary', 'paused-child'])
def test_service_holds_unresolved_selection_and_continues_healthy_receipts_and_requests(migrated, selection_service):
    r = migrated
    svc, resume, delayed = selection_service
    bad = svc.store.new('order', 'issuer', 'worker', 'Synthetic uncertain submission')
    attempt = svc.attempts.open(bad, 'worker', '%1', r.worker.launched().pid, r.worker.launched().at)
    proof = pending.Proof.begin(r.worker, attempt, post.typed([bad]))
    proof.save(phase='submitted', shown=post.typed([bad]))
    svc.attempts.close(attempt, 'pending')
    before = (attempt.path.read_bytes(), proof.path.read_bytes(), r.source.path.read_bytes())
    good = svc.store.new('order', 'issuer', 'healthy', 'Synthetic healthy acceptance')
    accepted = svc.attempts.open(good, 'healthy', '%2', r.healthy.launched().pid, r.healthy.launched().at)
    svc.attempts.close(accepted, 'queued')
    r.healthy_source.begin(post.typed([good]), 'synthetic-source', 'high')
    r.healthy_source.end('Synthetic completed acceptance')
    unresolved(r)
    requests = r.ship.runtime/'requests'
    requests.mkdir()
    held = requests/'a-unresolved.json'
    held.write_text(json.dumps(dict(kind='relaunch', seat='worker', resume=False)))
    done = requests/'b-healthy.json'
    done.write_text(json.dumps(dict(kind='control', seat='healthy', name='fresh', value='never')))
    svc.tick()
    assert svc.attempts.latest()[good.id] == 'delivered'
    assert before == (attempt.path.read_bytes(), proof.path.read_bytes(), r.source.path.read_bytes())
    assert held.exists() and not held.with_suffix('.done').exists()
    assert not done.exists() and done.with_suffix('.done').exists()
    assert svc.store.is_open(bad) and not r.launches
    if delayed:
        assert svc.account_driver.active
        assert svc.ledger.db.execute('SELECT count(*) FROM turns WHERE seat=?', ('healthy',)).fetchone()[0] == 0
    resume()
    from tests.integration.waiting import wait_for

    def accounted():
        svc.tick()
        return svc.ledger.db.execute('SELECT count(*) FROM turns WHERE seat=?', ('healthy',)).fetchone()[0] == 2

    wait_for(accounted, seconds=60)
    assert svc.ledger.db.execute('SELECT count(*) FROM turns WHERE seat=?', ('healthy',)).fetchone()[0] == 2
    holds = [json.loads(line) for line in svc.log_path.read_text().splitlines() if json.loads(line)['event'] == 'held']
    assert holds and all(row['seat'] == 'worker' and 'saved pool missing' in row['error'] for row in holds)
    assert 'worker' in watch.line(r.ship, r.tmux) and 'saved pool missing' in watch.line(r.ship, r.tmux)
