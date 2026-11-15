"""Synthetic ordinary journals settle without granting transport or closing posts."""

from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from quarterdeck import harness, pending, post, thread_context
from quarterdeck.harness import closed_record
from quarterdeck.service import Service
from .test_closed_record import world, T0, append, native_input, stamp  # noqa: F401
from .test_pending_delivery import setup, send  # noqa: F401


def journal(world, tmp_path, monkeypatch):
    w = world
    runtime = tmp_path / 'runtime'
    monkeypatch.setattr(post, 'now', lambda: T0 + timedelta(seconds=10))
    store, attempts = post.Store(runtime), post.Attempts(runtime)
    message = store.new('order', 'issuer', 'worker', 'Synthetic open work.')
    attempt = attempts.open(message, 'worker', '%1', 100, stamp(1))
    attempts.close(attempt, 'pending', 'Synthetic original submission.')
    w.proof.path = runtime / 'post' / 'pending' / 'worker' / attempt.path.name
    w.proof.data.update(payload=post.typed([message]), shown=post.typed([message]).rstrip(),
                        attempts=[attempt.path.name])
    w.proof.save()
    w.seat.name = 'worker'
    w.seat.ship = SimpleNamespace(runtime=runtime)
    w.seat.pane = lambda: w.live['pane']
    w.seat.harness = harness.get(w.name)
    service = SimpleNamespace(runtime=runtime, attempts=attempts, store=store,
                              ship=SimpleNamespace(seats={'worker': None}),
                              seat=lambda name: w.seat, log=lambda *a, **kw: None)
    return service, store, message, attempt


@pytest.mark.parametrize('closure', ['gone', 'later-launch', 'earlier-server'])
def test_closed_old_shape_settles_once_with_no_transport_or_openness_change(world, tmp_path, monkeypatch, closure):
    w = world
    svc, store, message, attempt = journal(w, tmp_path, monkeypatch)
    if closure == 'gone':
        w.live['pane'] = None
    elif closure == 'later-launch':
        w.live['launched'] = replace(w.live['launched'], pane='%2', pid=200, at=stamp(30))
    else:
        w.live['earlier'] = True
    w.seat.tmux.paste = w.seat.tmux.keys = lambda *a, **kw: pytest.fail('settlement attempted transport')
    original = dict(w.proof.data)
    record = w.record.read_bytes()
    assert attempt.path in pending.reconcile(svc)
    settled = pending.active(w.seat, include_terminal=True)[0]
    assert settled.data['phase'] == closed_record.PHASE
    assert settled.data['resolution'] == closed_record.DETAIL
    assert {key: settled.data[key] for key in original if key != 'phase'} == {
        key: value for key, value in original.items() if key != 'phase'}
    assert svc.attempts.latest() == {message.id: closed_record.PHASE}
    assert not pending.active(w.seat) and not pending.detached(w.seat)
    assert not svc.attempts.undelivered(store, 'worker')
    assert not svc.attempts.undelivered(store, 'worker', for_launch=True)
    assert store.is_open(message) and thread_context.select(store, 'worker').incoming == [message]
    before = (w.proof.path.read_bytes(), w.proof.path.stat().st_mtime_ns,
              attempt.path.read_bytes(), attempt.path.stat().st_mtime_ns)
    pending.reconcile(svc)
    pending.reconcile(svc)
    assert before == (w.proof.path.read_bytes(), w.proof.path.stat().st_mtime_ns,
                      attempt.path.read_bytes(), attempt.path.stat().st_mtime_ns)
    assert w.record.read_bytes() == record
    assert len(svc.attempts.for_message(message.id)) == 1
    svc.attempts.close(attempt, 'delivered', 'A late finisher cannot overwrite the terminal reading.')
    assert svc.attempts.latest()[message.id] == closed_record.PHASE
    assert svc.attempts.abandon('worker', 'An unrelated later cleanup.') == []


@pytest.mark.parametrize('hold', ['live', 'match', 'missing-record', 'incomplete', 'skipped'])
def test_unresolved_evidence_stays_held_by_reconciliation(world, tmp_path, monkeypatch, hold):
    w = world
    svc, store, message, attempt = journal(w, tmp_path, monkeypatch)
    if hold != 'live':
        w.live['launched'] = replace(w.live['launched'], at=stamp(30))
    if hold == 'match':
        # This is after original at=10, before since=20: confirmed()==None cannot prove absence.
        append(w.record, native_input(w.name, w.proof.data['payload'], stamp(11)))
    elif hold == 'missing-record':
        w.record.unlink()
    elif hold == 'incomplete':
        with w.record.open('a') as stream:
            stream.write('{"type":')
    elif hold == 'skipped':
        append(w.record, dict(type='unknown-input-kind', timestamp=stamp(11)))
    pending.reconcile(svc)
    assert svc.attempts.latest()[message.id] == 'pending'
    assert pending.active(w.seat)[0].data['phase'] == 'submitted'
    assert store.is_open(message) and not svc.attempts.undelivered(store, 'worker')
    assert len(svc.attempts.for_message(message.id)) == 1


def test_interrupted_settlement_finishes_attempt_without_rewriting_proof(world, tmp_path, monkeypatch):
    w = world
    svc, store, message, attempt = journal(w, tmp_path, monkeypatch)
    w.live['pane'] = None
    w.proof.save(phase=closed_record.PHASE, resolution=closed_record.DETAIL)
    before = w.proof.path.stat().st_mtime_ns
    pending.reconcile(svc)
    assert svc.attempts.latest()[message.id] == closed_record.PHASE
    assert w.proof.path.stat().st_mtime_ns == before
    assert store.is_open(message)


def test_service_restart_settles_detached_alarm_without_sending(setup):
    svc, seat, live, record = setup
    message, proof = send(svc, seat)
    launch = seat.folder / 'launch.json'
    data = json.loads(launch.read_text())
    data['at'] = post.now().isoformat()
    launch.write_text(json.dumps(data))
    assert pending.detached(seat)
    sends = live['pasted'], live['submitted']
    restarted = Service(svc.ship, svc.tmux)
    try:
        restarted.reconcile()
        restarted.deliveries()
        restarted.reconcile()
        assert not pending.detached(seat)
        assert restarted.attempts.latest()[message.id] == closed_record.PHASE
        assert restarted.store.is_open(message)
        assert (live['pasted'], live['submitted']) == sends
        assert len(restarted.attempts.for_message(message.id)) == 1
    finally:
        restarted.ledger.close()
