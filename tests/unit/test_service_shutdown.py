"""Ordinary durable shutdown phases and service-local signal deferral."""

from datetime import datetime, timezone
import json

import pytest

from quarterdeck import pending, shutdown


def reservation(tmp_path, phase="prepared", *, marked=True):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    lock = runtime / "service.lock"
    lock.touch()
    info = lock.stat()
    owner = dict(version=1, pid=999999999, start="synthetic-start-zzzz", token="synthetic-owner-zzzz",
                 device=info.st_dev, inode=info.st_ino)
    lock.write_text(json.dumps(owner))
    at = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
    path = runtime / "post/pending/synthetic-seat-zzzz/synthetic-attempt-zzzz.json"
    proof = pending.Proof(path, {})
    proof.save(seat="synthetic-seat-zzzz", identity=dict(pane="synthetic-pane-zzzz", pid=101,
               launch=at, harness="fake"), at=at, since=at, payload="synthetic payload zzzz", phase=phase,
               record=None, shown=None, attempts=["synthetic-attempt-zzzz.json"], service_owner=owner,
               delivery_token="synthetic-delivery-zzzz", **({"paste_boundary": shutdown.BEFORE_PASTE} if marked else {}))
    shutdown.pointer_path(runtime).write_text(json.dumps(dict(version=1, owner=owner,
        token="synthetic-delivery-zzzz", before_paste=marked, journal=path.relative_to(runtime).as_posix())))
    return runtime, owner, proof


def test_only_new_prepared_marker_proves_no_paste(tmp_path):
    _, _, proof = reservation(tmp_path)
    assert shutdown.never_pasted(proof.data)
    for phase in ("pasting", "submitting", "submitted"):
        assert not shutdown.never_pasted({**proof.data, "phase": phase})
    legacy = dict(proof.data)
    legacy.pop("paste_boundary")
    assert not shutdown.never_pasted(legacy)
    assert not shutdown.never_pasted({**proof.data, "recovery": {"mode": "pending"}})


@pytest.mark.parametrize("phase,expected", [("prepared", "cancelled"), ("pasting", "pasting"),
                                           ("submitting", "submitting"), ("submitted", "submitted")])
def test_stopped_journal_retains_the_actual_durable_boundary(tmp_path, phase, expected):
    runtime, owner, proof = reservation(tmp_path, phase)
    detail = shutdown.annotate(runtime, owner, forced=True)
    saved = json.loads(proof.path.read_text())
    assert saved["phase"] == expected
    assert saved["service_stop"]["phase"] == phase
    assert saved["service_stop"]["kind"] == "forced"
    assert not shutdown.pointer_path(runtime).exists()
    if phase in ("pasting", "submitting"):
        assert "outcome uncertain; never resent" in detail


def test_legacy_prepared_phase_is_not_cancelled(tmp_path):
    runtime, owner, proof = reservation(tmp_path, marked=False)
    shutdown.annotate(runtime, owner, forced=True)
    assert json.loads(proof.path.read_text())["phase"] == "prepared"


def test_signal_outside_a_delivery_exits_immediately(tmp_path):
    with pytest.raises(SystemExit) as exit:
        shutdown.Stop(tmp_path).signal()
    assert exit.value.code == 0


def test_signal_inside_delivery_waits_for_submitted_journal(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    proof = pending.Proof(runtime / "post/pending/synthetic-seat-zzzz/synthetic-attempt-zzzz.json", {})
    with shutdown.Stop(runtime).installed():
        proof.save(phase="prepared", shown=None, paste_boundary=shutdown.BEFORE_PASTE, **shutdown.prepare(proof))
        shutdown.prepared(proof)
        with pytest.raises(SystemExit):
            with shutdown.delivery(proof):
                proof.save(phase="pasting")
                # The installed handler defers; this is not a global clock replacement.
                from signal import SIGTERM, getsignal
                getsignal(SIGTERM)(SIGTERM, None)
                assert shutdown.requested()
                proof.save(phase="submitted")
        assert proof.data["phase"] == "submitted"
        assert shutdown.pointer_path(runtime).exists()
