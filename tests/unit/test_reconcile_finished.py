"""Reconciliation reads per pass; only settled terminals bypass action work."""

from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from quarterdeck import durable, pending, post
from quarterdeck.harness import closed_record
from quarterdeck.service import Service
from .test_pending_delivery import setup as transport_setup, send, input_row


OUTCOMES = {'delivered': 'delivered', 'cancelled': 'withheld', 'carried': 'launch',
            'abandoned': 'abandoned', 'withheld': 'withheld', 'unacted': 'unacted',
            'uncertain': 'uncertain', closed_record.PHASE: closed_record.PHASE}


def sample(tmp_path, *, phase='delivered', result='delivered'):
    runtime = tmp_path / 'runtime'
    store, attempts = post.Store(runtime), post.Attempts(runtime)
    message = store.new('order', 'operator', 'worker', 'Synthetic work')
    launch = (post.now() - timedelta(minutes=1)).isoformat()
    attempt = attempts.open(message, 'worker', '%1', 123, launch)
    if result is not None:
        attempt = attempts.close(attempt, result)
    seat = SimpleNamespace(name='worker', ship=SimpleNamespace(runtime=runtime),
                           session=SimpleNamespace(human=False), record=lambda: None)
    proof = pending.Proof(runtime / 'post/pending/worker' / attempt.path.name, {})
    proof.save(seat='worker', phase=phase, payload='Synthetic payload', shown=None, record=None,
               identity=dict(pane='%1', pid=123, launch=launch, harness='codex'),
               attempts=[attempt.path.name], at=post.now().isoformat(), since=post.now().isoformat())
    svc = object.__new__(Service)
    svc.runtime = runtime
    svc.ship = SimpleNamespace(runtime=runtime, seats={'worker': object()})
    svc.store, svc.attempts = store, attempts
    svc.seat = lambda name: seat
    svc.resolved_seats = lambda: [seat]
    svc.heartbeat = lambda: None
    svc.log = lambda *args, **kwargs: None
    svc.blind_checked = time.monotonic()
    return SimpleNamespace(service=svc, seat=seat, proof=proof, attempt=attempt, message=message)


def count_inventory(monkeypatch, attempts):
    reads = []
    original = attempts.all
    def read(*args, **kwargs):
        reads.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(attempts, 'all', read)
    return reads


@pytest.mark.parametrize('phase', sorted(OUTCOMES))
def test_each_settled_terminal_phase_needs_one_journal_read_and_no_settlement(tmp_path, monkeypatch, phase):
    assert set(OUTCOMES) == pending.TERMINAL
    w = sample(tmp_path, phase=phase, result=OUTCOMES[phase])
    reads = []
    # The reader has already validated this proof. Isolate the phase/action
    # boundary, including withheld's independently validated refusal metadata.
    def journals(seat):
        reads.append(True)
        return [pending.Proof(w.proof.path, json.loads(w.proof.path.read_text()))], []
    monkeypatch.setattr(pending, 'journals', journals)
    monkeypatch.setattr(pending.Proof, 'finish', lambda *a: pytest.fail('finished journal settled again'))
    monkeypatch.setattr(pending.Proof, 'confirmed', lambda *a: pytest.fail('terminal receipt read again'))
    monkeypatch.setattr(w.service.attempts, 'close', lambda *a: pytest.fail('settled attempt changed'))
    before = w.attempt.path.read_bytes()
    tracked = pending._reconcile_claimed(w.service, w.seat, {w.attempt.path.name: w.attempt})
    assert tracked == {w.attempt.path} and reads == [True]
    assert w.attempt.path.read_bytes() == before


def test_real_delivered_journal_and_inventory_are_read_once_per_pass(tmp_path, monkeypatch):
    w = sample(tmp_path)
    reads = count_inventory(monkeypatch, w.service.attempts)
    journal_reads = []
    original = pending.post_dir.read_text
    def read(path, *args, **kwargs):
        if path == w.proof.path:
            journal_reads.append(True)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(pending.post_dir, 'read_text', read)
    monkeypatch.setattr(pending.Proof, 'finish', lambda *a: pytest.fail('duplicate terminal work'))
    for _ in range(2):
        w.service.reconcile()
    assert len(reads) == len(journal_reads) == 2
    assert w.service.loaded_attempts[0].result == 'delivered'
    assert w.service._reconcile_attempt_inventory is None


def test_delivered_journal_with_pending_attempt_finishes_interrupted_settlement(transport_setup):
    svc, seat, live, record = transport_setup
    message, proof = send(svc, seat)
    input_row(record, proof.data['payload'], post.now())
    proof.finish('delivered')  # crash before the attempt's matching write
    assert svc.attempts.latest()[message.id] == 'pending'
    svc.reconcile()
    assert svc.attempts.latest()[message.id] == 'delivered'
    assert live['pasted'] == live['submitted'] == 1
    before = svc.attempts.for_message(message.id)[0].path.read_bytes()
    svc.reconcile()
    assert svc.attempts.for_message(message.id)[0].path.read_bytes() == before


def test_nonterminal_receipt_is_read_before_fresh_attempt_settlement(transport_setup, monkeypatch):
    svc, seat, live, record = transport_setup
    message, proof = send(svc, seat)
    input_row(record, proof.data['payload'], post.now())
    events = []
    confirm = pending.Proof.confirmed
    write = svc.attempts._write
    def confirmed(proof, subject):
        assert post._READING.get() is None
        result = confirm(proof, subject)
        events.append(('record', result))
        return result
    def written(attempt):
        assert post._READING.get() is None
        assert ('record', 'delivered') in events
        events.append(('settle', attempt.result))
        return write(attempt)
    monkeypatch.setattr(pending.Proof, 'confirmed', confirmed)
    monkeypatch.setattr(svc.attempts, '_write', written)
    svc.reconcile()
    assert svc.attempts.latest()[message.id] == 'delivered'
    assert events.index(('record', 'delivered')) < events.index(('settle', 'delivered'))
    assert live['pasted'] == live['submitted'] == 1


def test_journal_turning_terminal_during_planning_is_reread(transport_setup, monkeypatch):
    svc, seat, live, record = transport_setup
    message, proof = send(svc, seat)
    input_row(record, proof.data['payload'], post.now())
    reader = pending.journals
    def journals(subject):
        proofs, errors = reader(subject)
        if proofs:
            pending.Proof(proofs[0].path, dict(proofs[0].data)).finish('delivered')
        return proofs, errors  # the earlier nonterminal observation is now stale
    monkeypatch.setattr(pending, 'journals', journals)
    monkeypatch.setattr(pending.Proof, 'confirmed', lambda *a: pytest.fail('fresh terminal proof ignored'))
    svc.reconcile()
    assert svc.attempts.latest()[message.id] == 'delivered'
    assert live['pasted'] == live['submitted'] == 1


def test_unfinished_refresh_keeps_its_existing_recovery_path(tmp_path, monkeypatch):
    from quarterdeck import receipt_refresh
    w = sample(tmp_path, phase='carried', result='launch')
    monkeypatch.setattr(pending, 'journals', lambda seat: ([w.proof], []))
    monkeypatch.setattr(receipt_refresh, 'unfinished', lambda data: True)
    recovered = []
    monkeypatch.setattr(receipt_refresh, 'recover', lambda service, proof: recovered.append(proof.path))
    pending._reconcile_claimed(w.service, w.seat, {w.attempt.path.name: w.attempt})
    assert recovered == [w.proof.path]


@pytest.mark.parametrize('writer', ['other-instance', 'atomic-publisher', 'in-place', 'restored-mtime'])
def test_intervening_attempt_change_forces_fresh_legacy_inventory(tmp_path, monkeypatch, writer):
    w = sample(tmp_path)
    reads = count_inventory(monkeypatch, w.service.attempts)
    original = pending._reconcile_inventory
    def reconcile(service, records):
        tracked = original(service, records)
        if writer == 'other-instance':
            post.Attempts(service.runtime).close(w.attempt, 'watch', 'new owner')
        else:
            data = json.loads(w.attempt.path.read_text())
            data.update(result='watch', detail='new owner')
            if writer == 'atomic-publisher':
                durable.replace_file(w.attempt.path, json.dumps(data))
            else:
                info = w.attempt.path.stat()
                contents = json.dumps(data)
                if writer == 'restored-mtime':
                    contents = contents.ljust(info.st_size)
                w.attempt.path.write_text(contents)
                if writer == 'restored-mtime':
                    os.utime(w.attempt.path, ns=(info.st_atime_ns, info.st_mtime_ns))
                    after = w.attempt.path.stat()
                    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (info.st_ino, info.st_size, info.st_mtime_ns)
        return tracked
    monkeypatch.setattr(pending, '_reconcile_inventory', reconcile)
    w.service.reconcile()
    assert len(reads) == 1 and w.service.loaded_attempts[0].result == 'watch'
    assert w.service._reconcile_attempt_inventory is None


def test_unchanged_recheck_reads_no_attempt_or_index_bytes(tmp_path, monkeypatch):
    w = sample(tmp_path)
    records = w.service.attempts.all()
    snapshot = w.service.attempts.inventory_snapshot(records)
    monkeypatch.setattr(post.post_dir, 'read_text', lambda *a, **k: pytest.fail('unchanged bytes read'))
    assert w.service.attempts.recheck_inventory(snapshot) is records


def test_changed_recheck_reads_only_changed_attempt_and_keeps_sort_order(tmp_path, monkeypatch):
    w = sample(tmp_path)
    second = w.service.attempts.open(w.message, 'worker', '%1', 123, post.now().isoformat())
    records = w.service.attempts.all()
    snapshot = w.service.attempts.inventory_snapshot(records)
    w.service.attempts.close(second, 'watch')
    reads = []
    reader = post.post_dir.read_text
    def read(path, **kwargs):
        reads.append(path)
        return reader(path, **kwargs)
    monkeypatch.setattr(post.post_dir, 'read_text', read)
    result = w.service.attempts.recheck_inventory(snapshot)
    assert reads == [second.path]
    assert [a.result for a in result] == ['delivered', 'watch']


def test_changed_retired_batch_is_seen_by_recheck(tmp_path):
    w = sample(tmp_path)
    index = w.service.runtime / 'post/retired/index/batch.json'
    index.parent.mkdir(parents=True)
    fields = json.loads(w.attempt.path.read_text())
    index.write_text(json.dumps({'format': 1, 'attempts': {'old-1.json': fields}}))
    records = w.service.attempts.all()
    snapshot = w.service.attempts.inventory_snapshot(records)
    fields['result'] = 'watch'
    index.write_text(json.dumps({'format': 1, 'attempts': {'old-1.json': fields}}))
    result = w.service.attempts.recheck_inventory(snapshot)
    assert next(a for a in result if a.path.name == 'old-1.json').result == 'watch'


def test_changed_listing_is_seen_by_recheck(tmp_path):
    w = sample(tmp_path)
    records = w.service.attempts.all()
    snapshot = w.service.attempts.inventory_snapshot(records)
    second = w.service.attempts.open(w.message, 'worker', '%1', 123, post.now().isoformat())
    assert w.service.attempts.recheck_inventory(snapshot)[-1] == second
    second.path.unlink()
    assert w.service.attempts.recheck_inventory(snapshot) == records


def test_failed_pass_cannot_leave_an_old_inventory(tmp_path, monkeypatch):
    w = sample(tmp_path)
    w.service.loaded_attempts = [w.attempt]
    def failed(*args):
        raise OSError('synthetic read failure')
    monkeypatch.setattr(pending, '_reconcile_inventory', failed)
    with pytest.raises(OSError):
        w.service.reconcile()
    assert w.service.loaded_attempts is None and w.service._reconcile_attempt_inventory is None


@pytest.mark.parametrize('result,expected_reads', [('delivered', 1), ('pending', 1)])
def test_removed_seat_uses_the_first_inventory_and_refreshes_after_actual_settlement(tmp_path, monkeypatch, result, expected_reads):
    w = sample(tmp_path, result=result)
    w.proof.path.unlink()
    w.service.ship.seats = {}
    w.service.resolved_seats = lambda: []
    reads = count_inventory(monkeypatch, w.service.attempts)
    w.service.reconcile()
    assert len(reads) == expected_reads
    assert w.service.loaded_attempts[0].result == ('delivered' if result == 'delivered' else 'abandoned')


def test_removed_seat_settlement_rereads_the_candidate_under_the_lock(tmp_path):
    w = sample(tmp_path, result='pending')
    stale = w.attempt
    w.service.attempts.close(stale, 'delivered', 'fresh receipt')
    assert w.service.attempts.abandon('worker', 'old seat', attempts=[stale]) == []
    assert w.service.attempts.latest()[w.message.id] == 'delivered'
