"""Checkpoint IO bounds with synthetic observations, no native receipt readers."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from quarterdeck import durable, pending, receipt_refresh


REVISION = '5' * 40
NEXT = '6' * 40


@pytest.fixture
def refresh(tmp_path, monkeypatch):
    proofs = []
    calls = []
    writes = []
    retry = set()
    runtime = tmp_path / 'runtime'
    replace = durable.replace_file

    def publish(path, text):
        writes.append(len(text.encode()))
        replace(path, text)

    def observe(service, seat, proof, attempts, commit):
        calls.append(proof.key)
        return proof.key not in retry

    def add(count):
        for number in range(len(proofs), len(proofs) + count):
            key = hashlib.sha256(f'synthetic-refresh-{number}'.encode()).hexdigest()
            proofs.append(SimpleNamespace(key=key, data={'phase': 'not-in-closed-record'}))

    def service():
        return SimpleNamespace(runtime=runtime, attempts=SimpleNamespace(all=lambda: []),
                               ship=SimpleNamespace(seats=['synthetic-seat']),
                               seat=lambda name: SimpleNamespace(name=name),
                               log=lambda *args, **kwargs: None)

    monkeypatch.setattr(durable, 'replace_file', publish)
    monkeypatch.setattr(pending, 'journals', lambda seat: (proofs, []))
    monkeypatch.setattr(receipt_refresh, 'journal_key', lambda proof: proof.key)
    monkeypatch.setattr(receipt_refresh, '_observe', observe)
    return SimpleNamespace(runtime=runtime, proofs=proofs, calls=calls, writes=writes,
                           retry=retry, add=add, service=service, observe=observe)


def test_twelve_thousand_observations_publish_one_bounded_checkpoint(refresh):
    refresh.add(12000)
    receipt_refresh.run(refresh.service(), REVISION)
    assert len(refresh.calls) == 12000
    assert len(refresh.writes) == 1
    assert sum(refresh.writes) < 1_000_000
    saved = json.loads((refresh.runtime / 'post' / 'receipt-refresh.json').read_text())
    assert saved['complete'] and len(saved['observed']) == 12000
    assert set(saved['observed']) == {proof.key for proof in refresh.proofs}
    receipt_refresh.run(refresh.service(), REVISION)
    assert len(refresh.calls) == 12000 and len(refresh.writes) == 1


def test_partial_pass_publishes_once_and_restart_observes_only_unfinished_keys(refresh):
    refresh.add(20)
    unfinished = refresh.proofs[7].key
    refresh.retry.add(unfinished)
    receipt_refresh.run(refresh.service(), REVISION)
    marker = receipt_refresh.Checkpoint(refresh.runtime, REVISION)
    assert not marker.complete and not marker.seen(unfinished)
    assert all(marker.seen(proof.key) for proof in refresh.proofs if proof.key != unfinished)
    assert len(refresh.writes) == 1
    refresh.retry.clear()
    receipt_refresh.run(refresh.service(), REVISION)
    assert refresh.calls == [proof.key for proof in refresh.proofs] + [unfinished]
    assert receipt_refresh.Checkpoint(refresh.runtime, REVISION).complete
    assert len(refresh.writes) == 2


def test_new_revision_rechecks_negative_observations_and_checks_new_keys(refresh):
    refresh.add(3)
    receipt_refresh.run(refresh.service(), REVISION)
    refresh.add(1)
    receipt_refresh.run(refresh.service(), NEXT)
    assert refresh.calls == [proof.key for proof in refresh.proofs[:3]] + [
        proof.key for proof in refresh.proofs]
    assert len(refresh.writes) == 2
    assert receipt_refresh.Checkpoint(refresh.runtime, NEXT).complete


def test_interrupted_pass_repeats_unpublished_observations_on_restart(refresh, monkeypatch):
    refresh.add(12)

    def interrupted(*args):
        if len(refresh.calls) == 5:
            raise KeyboardInterrupt('synthetic stop before checkpoint publication')
        return refresh.observe(*args)

    monkeypatch.setattr(receipt_refresh, '_observe', interrupted)
    with pytest.raises(KeyboardInterrupt):
        receipt_refresh.run(refresh.service(), REVISION)
    assert refresh.calls == [proof.key for proof in refresh.proofs[:5]]
    assert not (refresh.runtime / 'post' / 'receipt-refresh.json').exists()
    monkeypatch.setattr(receipt_refresh, '_observe', refresh.observe)
    receipt_refresh.run(refresh.service(), REVISION)
    assert refresh.calls[5:] == [proof.key for proof in refresh.proofs]
    assert len(refresh.writes) == 1
    assert receipt_refresh.Checkpoint(refresh.runtime, REVISION).complete


def test_failed_batch_publication_does_not_claim_durable_progress(refresh, monkeypatch):
    refresh.add(8)
    publish = durable.replace_file
    service = refresh.service()

    def fail(*args):
        raise OSError('synthetic publication failure')

    monkeypatch.setattr(durable, 'replace_file', fail)
    with pytest.raises(OSError, match='publication failure'):
        receipt_refresh.run(service, REVISION)
    marker = receipt_refresh.Checkpoint(refresh.runtime, REVISION)
    assert not marker.complete and not any(marker.seen(proof.key) for proof in refresh.proofs)
    assert not hasattr(service, 'receipt_refresh_commit')
    monkeypatch.setattr(durable, 'replace_file', publish)
    receipt_refresh.run(service, REVISION)
    assert refresh.calls == [proof.key for proof in refresh.proofs] * 2
    assert receipt_refresh.Checkpoint(refresh.runtime, REVISION).complete
