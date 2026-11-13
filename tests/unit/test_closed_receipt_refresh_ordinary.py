"""Generic journal/attempt ownership and interrupted writes; no native record shapes."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json

import pytest

from quarterdeck import input_receipt, pending, post, receipt_refresh, record_owner
from quarterdeck.service import Service


AT = datetime(2032, 6, 7, 8, 9, tzinfo=timezone.utc)
OLD = '3' * 40
NEW = '4' * 40


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(post, 'now', lambda: AT)
    runtime = tmp_path / 'runtime'
    attempts = post.Attempts(runtime)
    message = post.Store(runtime).new('order', 'issuer', 'seat-orchid', 'synthetic work')
    attempt = attempts.open(message, 'seat-orchid', '%synthetic', 123456789, AT.isoformat())
    attempts.close(attempt, 'not-in-closed-record', 'synthetic previous matcher negative')
    record = tmp_path / 'original-record.txt'
    record.write_text('Synthetic record: matcher is a stand-in, not a native parser.\n')
    folder = runtime / 'seats' / 'seat-orchid'
    record_owner.register(folder, record, 'fake')
    proof = pending.Proof(runtime / 'post' / 'pending' / 'seat-orchid' / attempt.path.name, {})
    proof.save(seat='seat-orchid', identity=dict(pane='%synthetic', pid=123456789,
               launch=AT.isoformat(), harness='fake'), at=AT.isoformat(), since=AT.isoformat(),
               payload='synthetic original payload', phase='not-in-closed-record', record=str(record),
               shown=None, attempts=[attempt.path.name])
    seat = SimpleNamespace(name='seat-orchid', folder=folder, ship=SimpleNamespace(runtime=runtime),
                           launched=lambda: SimpleNamespace(when=AT + timedelta(days=1)),
                           record=lambda: pytest.fail('read current record'),
                           tmux=SimpleNamespace(capture=lambda *args: pytest.fail('read live screen')))
    service = SimpleNamespace(runtime=runtime, attempts=attempts,
                              ship=SimpleNamespace(seats={'seat-orchid': None}),
                              seat=lambda name: seat, log=lambda *args, **kwargs: None)
    calls = []

    def match(reader, path, text, since):
        calls.append((reader, path, text, since))
        return 'delivered'

    monkeypatch.setattr(input_receipt, 'confirmed', match)
    return SimpleNamespace(runtime=runtime, attempts=attempts, proof=proof, record=record,
                           seat=seat, service=service, calls=calls)


def result(world):
    return json.loads(world.proof.path.read_text()), world.attempts.all()[0]


def test_old_negative_becomes_delivered_once_under_new_revision(world, monkeypatch):
    original_journal = world.proof.path.read_bytes()
    original_attempt = world.attempts.all()[0].path.read_bytes()
    matcher = input_receipt.confirmed
    monkeypatch.setattr(input_receipt, 'confirmed', lambda *args: None)
    receipt_refresh.run(world.service, OLD)
    assert world.proof.path.read_bytes() == original_journal
    assert world.attempts.all()[0].path.read_bytes() == original_attempt
    monkeypatch.setattr(input_receipt, 'confirmed', matcher)
    receipt_refresh.run(world.service, NEW)
    journal, attempt = result(world)
    assert journal['phase'] == attempt.result == 'delivered'
    assert journal['receipt_refresh']['commit'] == NEW
    assert world.calls == [('fake', world.record, 'synthetic original payload', AT)]
    assert world.attempts.accepted_ids() == {attempt.message}
    del world.service.receipt_refresh_commit  # a fresh service reads the durable checkpoint
    receipt_refresh.run(world.service, NEW)
    assert len(world.calls) == 1


@pytest.mark.parametrize('reading', [None, 'queued', 'unreadable'])
def test_nonpositive_reading_leaves_both_evidence_owners_byte_identical(world, monkeypatch, reading):
    before = world.proof.path.read_bytes(), world.attempts.all()[0].path.read_bytes()
    calls = []

    def match(*args):
        calls.append(True)
        if reading == 'unreadable':
            raise OSError('synthetic unavailable record')
        return reading

    monkeypatch.setattr(input_receipt, 'confirmed', match)
    receipt_refresh.run(world.service, NEW)
    del world.service.receipt_refresh_commit
    receipt_refresh.run(world.service, NEW)
    assert (world.proof.path.read_bytes(), world.attempts.all()[0].path.read_bytes()) == before
    assert calls == [True]


@pytest.mark.parametrize('previous', ['uncertain', 'unsettled', 'launch'])
def test_standing_uncertain_journal_can_gain_only_a_positive_receipt(world, previous):
    data = json.loads(world.proof.path.read_text())
    data['phase'] = 'uncertain'
    world.proof.save(**data)
    original = world.attempts.all()[0]
    # Establish the preexisting outcome as fixture metadata, without weakening close().
    from dataclasses import replace
    world.attempts._write(replace(original, result=previous, detail='synthetic old uncertainty'))
    receipt_refresh.run(world.service, NEW)
    journal, attempt = result(world)
    assert journal['phase'] == attempt.result == 'delivered'


def test_carried_submission_can_gain_receipt_without_changing_later_launch(world):
    from dataclasses import replace
    world.proof.save(phase='carried')
    original = world.attempts.all()[0]
    world.attempts._write(replace(original, result='pending'))
    message = post.Store(world.runtime).get(original.message)
    successor = world.attempts.open(message, 'seat-orchid', '%successor', 987654321,
                                    (AT + timedelta(days=1)).isoformat())
    world.attempts.close(successor, 'launch', 'synthetic carriage; not confirmed read')
    successor_bytes = successor.path.read_bytes()
    receipt_refresh.run(world.service, NEW)
    journal, attempt = result(world)
    assert journal['phase'] == attempt.result == 'delivered'
    assert successor.path.read_bytes() == successor_bytes
    assert world.attempts.latest()[original.message] == 'launch'


def test_positive_intent_recovers_after_attempt_write_failure(world, monkeypatch):
    write = world.attempts._write

    def fail(attempt):
        raise OSError('synthetic interrupted attempt write')

    monkeypatch.setattr(world.attempts, '_write', fail)
    receipt_refresh.run(world.service, NEW)
    journal, attempt = result(world)
    assert 'receipt_refresh' in journal and journal['phase'] == 'not-in-closed-record'
    assert attempt.result == 'not-in-closed-record'
    assert not receipt_refresh.Checkpoint(world.runtime, NEW).complete
    monkeypatch.setattr(world.attempts, '_write', write)
    monkeypatch.setattr(input_receipt, 'confirmed', lambda *args: pytest.fail('reread unfinished positive intent'))
    receipt_refresh.run(world.service, NEW)
    journal, attempt = result(world)
    assert journal['phase'] == attempt.result == 'delivered'
    assert receipt_refresh.Checkpoint(world.runtime, NEW).complete


def test_competing_attempt_outcome_is_not_replaced(world, monkeypatch):
    from dataclasses import replace
    matcher = input_receipt.confirmed

    def match(*args):
        original = world.attempts.all()[0]
        world.attempts._write(replace(original, result='abandoned', detail='synthetic competing owner'))
        return matcher(*args)

    monkeypatch.setattr(input_receipt, 'confirmed', match)
    before = world.proof.path.read_bytes()
    receipt_refresh.run(world.service, NEW)
    assert world.proof.path.read_bytes() == before
    assert world.attempts.all()[0].result == 'abandoned'
    assert receipt_refresh.Checkpoint(world.runtime, NEW).complete
    assert len(world.calls) == 1
    receipt_refresh.run(world.service, NEW)
    assert len(world.calls) == 1


def test_reconcile_completes_intent_after_attempt_settlement_before_phase_write(world, monkeypatch):
    save = pending.Proof.save

    def interrupted(proof, **fields):
        if fields.get('phase') == 'delivered':
            raise OSError('synthetic cut before journal completion')
        return save(proof, **fields)

    monkeypatch.setattr(pending.Proof, 'save', interrupted)
    receipt_refresh.run(world.service, NEW)
    journal, attempt = result(world)
    assert journal['phase'] == 'not-in-closed-record' and attempt.result == 'delivered'
    assert not receipt_refresh.Checkpoint(world.runtime, NEW).complete
    monkeypatch.setattr(pending.Proof, 'save', save)
    monkeypatch.setattr(input_receipt, 'confirmed', lambda *args: pytest.fail('reread settled decision'))
    assert pending.reconcile(world.service) == {attempt.path}
    journal, attempt = result(world)
    assert journal['phase'] == attempt.result == 'delivered'
    receipt_refresh.run(world.service, NEW)
    assert receipt_refresh.Checkpoint(world.runtime, NEW).complete


def test_generic_close_still_refuses_terminal_negative(world):
    original = world.attempts.all()[0]
    assert world.attempts.close(original, 'delivered').result == 'not-in-closed-record'


def test_competing_recovery_outcome_retires_intent_instead_of_wedging_it(world, monkeypatch):
    from dataclasses import replace
    write = world.attempts._write
    def interrupted(attempt):
        raise OSError('synthetic cut')

    monkeypatch.setattr(world.attempts, '_write', interrupted)
    receipt_refresh.run(world.service, NEW)
    monkeypatch.setattr(world.attempts, '_write', write)
    original = world.attempts.all()[0]
    write(replace(original, result='abandoned', detail='synthetic competing retirement'))
    monkeypatch.setattr(input_receipt, 'confirmed', lambda *args: pytest.fail('reread durable positive intent'))
    receipt_refresh.run(world.service, NEW)
    journal, attempt = result(world)
    assert journal['receipt_refresh']['outcome'] == 'superseded'
    assert not receipt_refresh.unfinished(journal)
    assert attempt.result == 'abandoned'
    assert receipt_refresh.Checkpoint(world.runtime, NEW).complete
    monkeypatch.setattr(receipt_refresh, 'recover', lambda *args: pytest.fail('recovered superseded intent'))
    pending.reconcile(world.service)
    assert world.attempts.all()[0].result == 'abandoned'


def test_delivered_refresh_uses_ordinary_reconcile_without_refresh_locking(world, monkeypatch):
    receipt_refresh.run(world.service, NEW)
    monkeypatch.setattr(receipt_refresh, 'recover', lambda *args: pytest.fail('recovered already delivered journal'))
    pending.reconcile(world.service)
    assert result(world)[1].result == 'delivered'


def test_refresh_error_logs_once_and_does_not_suppress_rebrief(world):
    runner = Service.__new__(Service)
    runner.runtime = world.runtime
    runner.ship = SimpleNamespace(seats={})
    runner.framework_commit = NEW
    runner.brief_stamp = 'before'
    runner.brief_texts = {}
    stamps = iter(['after', 'later'])
    runner._brief_stamp = lambda: next(stamps)
    runner.resolved_seats = lambda: []
    logs = []
    runner.log = lambda event, **fields: logs.append((event, fields))

    (world.runtime / 'post' / 'receipt-refresh.json').write_text('{unsupported checkpoint')
    runner.rebrief()
    runner.rebrief()
    assert runner.brief_stamp == 'later'
    assert len([event for event, fields in logs if event == 'rebrief']) == 2
    assert len([fields for event, fields in logs if fields.get('step') == 'receipt-refresh']) == 1


def test_stale_unresolved_snapshot_retries_instead_of_becoming_terminal(world, monkeypatch):
    from dataclasses import replace
    matcher = input_receipt.confirmed

    def progress(*args):
        original = world.attempts.all()[0]
        world.attempts._write(replace(original, result='queued', detail='synthetic unresolved progress'))
        return matcher(*args)

    monkeypatch.setattr(input_receipt, 'confirmed', progress)
    receipt_refresh.run(world.service, NEW)
    assert not receipt_refresh.Checkpoint(world.runtime, NEW).complete
    assert 'receipt_refresh' not in result(world)[0]
    receipt_refresh.run(world.service, NEW)
    assert receipt_refresh.Checkpoint(world.runtime, NEW).complete
    assert result(world)[1].result == 'delivered'
