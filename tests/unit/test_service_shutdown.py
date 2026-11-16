"""Ordinary durable shutdown phases and service-local signal deferral."""

from datetime import datetime, timezone
import json

import pytest

from quarterdeck import pending, shutdown


def reservation(tmp_path, phase="prepared", *, marked=True):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    lock = runtime / "locks" / "service.lock"
    lock.parent.mkdir()
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


def test_unreadable_process_start_is_unknown(monkeypatch):
    from pathlib import Path
    def unreadable(_):
        raise OSError("synthetic identity temporarily unavailable zzzz")
    monkeypatch.setattr(Path, "read_text", unreadable)
    assert not shutdown.process_gone({"pid": 999999999, "start": "synthetic-start-zzzz"})


def test_stop_keeps_a_heartbeat_that_now_names_another_pid(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from quarterdeck import service_processes, servicectl

    # This case models process lifetime, including the private scope's empty re-read.
    monkeypatch.setattr(service_processes, "capture", lambda *args, **kwargs:
        SimpleNamespace(read=lambda: {}, proof="processes re-read: synthetic private scope"))

    stopped_pid, next_pid = 999999998, 999999999
    heartbeat = servicectl.heartbeat_path(tmp_path)
    heartbeat.parent.mkdir()
    stamp = datetime.now(timezone.utc).isoformat()
    heartbeat.write_text(f"{stopped_pid} {stamp}\n")
    monkeypatch.setattr(servicectl, "alive", lambda _: True)
    monkeypatch.setattr(servicectl, "holder", lambda _: stopped_pid)  # the service is the lock's holder
    monkeypatch.setattr(servicectl, "locked", lambda _: False)
    ended = []
    def ending(*_):
        ended.append(True)
        heartbeat.write_text(f"{next_pid} {stamp}\n")
    monkeypatch.setattr(shutdown, "process_start", lambda _: "synthetic-start-zzzz")
    monkeypatch.setattr(shutdown, "process_gone", lambda _: bool(ended))
    monkeypatch.setattr(servicectl.os, "kill", ending)
    assert "service stopped" in servicectl.stop(tmp_path)
    assert servicectl.read_heartbeat(tmp_path)[0] == next_pid


def test_stop_waits_for_the_process_to_be_gone_after_its_lock_frees(tmp_path, monkeypatch):
    """The lock frees as the process closes its files, before the kernel marks it gone; stop waits for that event."""
    from types import SimpleNamespace
    from quarterdeck import service_processes, servicectl

    # This case models process lifetime, including the private scope's empty re-read.
    monkeypatch.setattr(service_processes, "capture", lambda *args, **kwargs:
        SimpleNamespace(read=lambda: {}, proof="processes re-read: synthetic private scope"))

    pid, stamp = 999999997, datetime.now(timezone.utc).isoformat()
    servicectl.heartbeat_path(tmp_path).parent.mkdir()
    servicectl.heartbeat_path(tmp_path).write_text(f"{pid} {stamp}\n")
    expected = {"pid": pid, "start": "synthetic-start-zzzz"}
    gone = iter([False, False, True])
    seen = []
    monkeypatch.setattr(servicectl, "alive", lambda _: True)
    monkeypatch.setattr(servicectl, "holder", lambda _: pid)  # the service is the lock's holder
    monkeypatch.setattr(servicectl, "locked", lambda _: False)
    monkeypatch.setattr(servicectl.os, "kill", lambda *_: None)
    monkeypatch.setattr(shutdown, "owner", lambda runtime, found=None: expected)
    monkeypatch.setattr(shutdown, "process_start", lambda found: expected["start"])
    monkeypatch.setattr(shutdown, "inflight", lambda runtime, owner: None)
    def look(owner):
        result = next(gone, True)
        seen.append("gone" if result else "alive")
        return result
    monkeypatch.setattr(shutdown, "process_gone", look)
    monkeypatch.setattr(shutdown, "annotate", lambda runtime, owner, forced: seen.append("annotate") or "synthetic annotation")
    assert "synthetic annotation" in servicectl.stop(tmp_path)
    assert seen[:3] == ["alive", "alive", "gone"]
    assert seen[-1] == "annotate" and seen.count("annotate") == 1
    assert "alive" not in seen[seen.index("gone"):], "annotated only once the process reads gone"


@pytest.mark.parametrize('prior', [False, True])
def test_preflight_history_keeps_the_earliest_attempt_without_a_second_walk(tmp_path, monkeypatch, prior):
    from types import SimpleNamespace
    from quarterdeck import post

    runtime = tmp_path / 'runtime'
    at = datetime(2000, 1, 2, tzinfo=timezone.utc).isoformat()
    earlier = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
    bound = dict(pane='synthetic-pane-zzzz', pid=101, launch=at, harness='fake')
    seat = SimpleNamespace(name='synthetic-history-zzzz', ship=SimpleNamespace(runtime=runtime), record=lambda: None)
    attempt = post.Attempt('synthetic-message-history-zzzz', seat.name, at, bound['pane'], bound['pid'], at,
                           path=runtime / 'post/attempts/synthetic-history-zzzz.json')
    previous = post.Attempt(attempt.message, seat.name, earlier)
    unrelated = post.Attempt('synthetic-other-message-zzzz', seat.name, earlier)
    monkeypatch.setattr(pending, 'current_identity', lambda _: bound)
    def unexpected_walk(*args, **kwargs):
        pytest.fail('opening a journal repeated the preflight history scan')
    monkeypatch.setattr(post.Attempts, 'all', unexpected_walk)
    proof = pending.Proof.begin(seat, attempt, 'synthetic history payload',
                                attempt_history=[unrelated, previous] if prior else [])
    assert proof.data['at'] == (earlier if prior else at)
    assert proof.data['attempts'] == [attempt.path.name]


def test_stop_already_requested_cannot_enter_a_new_write_unit(tmp_path):
    from types import SimpleNamespace
    with shutdown.Stop(tmp_path).installed():
        shutdown._controller.requested = True
        with pytest.raises(SystemExit):
            with shutdown.transaction_scope(SimpleNamespace(runtime=tmp_path)):
                pytest.fail('a new write began after the requested stop')


def test_open_attempt_publishes_grace_before_a_journal_exists(tmp_path):
    from types import SimpleNamespace
    from quarterdeck import post

    runtime = tmp_path / 'runtime'
    store = post.Store(runtime)
    message = store.new('order', 'ship', 'synthetic-open-seat-zzzz', 'synthetic opening exercise')
    with shutdown.Stop(runtime).installed():
        with shutdown.transaction_scope(SimpleNamespace(runtime=runtime)):
            attempt = post.Attempts(runtime).open(message, message.to, None, None, None)
            original = attempt.path.read_bytes()
            active = shutdown.inflight(runtime, shutdown.owner(runtime))
            assert active['kind'] == 'attempt' and active['before_paste']
            assert active['journal'] == attempt.path.relative_to(runtime).as_posix()
            assert not (runtime / 'post/pending' / message.to / attempt.path.name).exists()
        assert not shutdown.pointer_path(runtime).exists()
    assert attempt.path.read_bytes() == original


def test_prejournal_stop_retains_the_attempt_for_receipt_reconciliation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from quarterdeck import post

    runtime = tmp_path / 'runtime'
    message = post.Store(runtime).new('order', 'ship', 'synthetic-open-seat-zzzz', 'synthetic opening exercise')
    with shutdown.Stop(runtime).installed():
        expected = shutdown.owner(runtime)
        with pytest.raises(SystemExit):
            with shutdown.transaction_scope(SimpleNamespace(runtime=runtime)):
                attempt = post.Attempts(runtime).open(message, message.to, None, None, None)
                original = attempt.path.read_bytes()
                shutdown._controller.signal()
                assert shutdown.requested()
        assert shutdown.inflight(runtime, expected)['kind'] == 'attempt'
    monkeypatch.setattr(shutdown, 'process_gone', lambda _: True)
    assert 'retained for receipt reconciliation' in shutdown.annotate(runtime, expected, forced=False)
    assert not shutdown.pointer_path(runtime).exists()
    assert attempt.path.read_bytes() == original
