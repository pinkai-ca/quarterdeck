"""Ordinary journal identity metadata and diagnostic labels; no program or transport."""

from dataclasses import asdict
import json
from types import SimpleNamespace

from quarterdeck import pending, post


def journal(tmp_path):
    stamp = '2001-01-01T00:00:00+00:00'
    name = 'synthetic'
    message = 'rep-upgrade-synthetic-notice'
    path = tmp_path / 'post/attempts' / f'{message}-1.json'
    path.parent.mkdir(parents=True)
    attempt = post.Attempt(message, name, stamp, '%synthetic', 123, stamp, path=path)
    data = asdict(attempt)
    data.pop('path')
    path.write_text(json.dumps(data))
    seat = SimpleNamespace(name=name, ship=SimpleNamespace(runtime=tmp_path),
                           harness=SimpleNamespace(name='fake'),
                           launched=lambda: SimpleNamespace(at=stamp),
                           pane=lambda: SimpleNamespace(id='%synthetic', pid=123),
                           record=lambda: None)
    proof = pending.Proof.begin(seat, attempt, 'Synthetic upgrade notice.', attempt_history=[])
    return seat, attempt, proof


def test_new_journal_keeps_the_attempts_actual_message_id(tmp_path):
    seat, attempt, proof = journal(tmp_path)
    assert proof.data['message'] == attempt.message
    assert json.loads(proof.path.read_text())['message'] == attempt.message
    reason = pending.inspection(seat, 'Synthetic diagnostic', proof.data['attempts'],
                                message=proof.data['message'])
    assert post.short_of(attempt.message) in reason
    assert 'message ID unknown' not in reason


def test_explicit_identity_needs_no_attempt_file_lookup(tmp_path, monkeypatch):
    seat, attempt, proof = journal(tmp_path)
    def unexpected(*args):
        raise AssertionError('explicit journal identity should not open an attempt')
    monkeypatch.setattr(pending, '_legacy_message', unexpected)
    reason = pending.inspection(seat, 'Synthetic diagnostic', proof.data['attempts'],
                                message=proof.data['message'])
    assert post.short_of(attempt.message) in reason


def test_old_journal_uses_only_its_named_attempt_without_backfill(tmp_path):
    seat, attempt, proof = journal(tmp_path)
    proof.data.pop('message')
    proof.path.write_text(json.dumps(proof.data))
    before = proof.path.read_bytes()
    reason = pending.inspection(seat, 'Synthetic diagnostic', proof.data['attempts'])
    assert post.short_of(attempt.message) in reason
    assert 'message ID unknown' not in reason
    assert proof.path.read_bytes() == before


def test_old_missing_upgrade_attempt_retains_the_unknown_label(tmp_path):
    seat, attempt, proof = journal(tmp_path)
    attempt.path.unlink()
    reason = pending.inspection(seat, 'Synthetic diagnostic', proof.data['attempts'])
    assert 'message ID unknown' in reason


def test_body_identity_changes_only_the_diagnostic_reference(tmp_path):
    seat, attempt, proof = journal(tmp_path)
    proof.data['phase'] = 'submitted'
    reason, remedy, quiet = proof.reading(seat, None)
    assert 'pending delivery; box cannot be verified' in reason
    assert post.short_of(attempt.message) in reason and 'message ID unknown' not in reason
    assert remedy == 'needs a human' and quiet is False
