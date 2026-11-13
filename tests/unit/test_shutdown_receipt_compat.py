"""Ordinary composition of receipt-refresh eligibility with stop's writer attestation."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import pending, receipt_refresh, shutdown


@pytest.mark.parametrize('phase,marked,expected', [
    ('prepared', False, True),
    ('prepared', True, False),
    ('pasting', True, True),
    ('submitting', True, True),
    ('submitted', True, True),
])
def test_receipt_pass_preserves_legacy_prepared_but_excludes_attested_no_paste(
        tmp_path, monkeypatch, phase, marked, expected):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    seat = SimpleNamespace(name='synthetic-compat-seat-zzzz', ship=SimpleNamespace(runtime=runtime))
    attempt = SimpleNamespace(path=runtime / 'post/attempts/synthetic-compat-attempt-zzzz.json')
    attempt.path.parent.mkdir(parents=True)
    attempt.path.touch()  # journals verifies the corresponding receipt exists
    proof = pending.Proof(runtime / 'post/pending' / seat.name / attempt.path.name, {})
    at = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
    proof.save(seat=seat.name, identity=dict(pane='synthetic-pane-zzzz', pid=101, launch=at, harness='fake'),
               at=at, since=at, payload='synthetic receipt compatibility zzzz', phase=phase,
               record=None, shown=None, attempts=[attempt.path.name],
               **({'paste_boundary': shutdown.BEFORE_PASTE} if marked else {}))
    before = proof.path.read_bytes()
    observed = []

    def observe(service, actual_seat, actual_proof, attempts, commit):
        # This gate covers dispatch only: the existing matcher and settlement are unchanged.
        observed.append((actual_seat.name, actual_proof.data['phase']))
        return True

    monkeypatch.setattr(receipt_refresh, '_observe', observe)
    service = SimpleNamespace(runtime=runtime, ship=SimpleNamespace(seats={seat.name: None}),
                              seat=lambda name: seat, attempts=SimpleNamespace(all=lambda: [attempt]),
                              log=lambda *args, **kwargs: pytest.fail('ordinary receipt dispatch logged an error'))
    commit = '0' * 40  # synthetic revision, not a captured runtime identifier
    receipt_refresh.run(service, commit)
    assert observed == ([(seat.name, phase)] if expected else [])
    assert 'prepared' in receipt_refresh.ELIGIBLE
    assert receipt_refresh.Checkpoint(runtime, commit).complete
    assert proof.path.read_bytes() == before
    receipt_refresh.run(service, commit)
    assert observed == ([(seat.name, phase)] if expected else [])
