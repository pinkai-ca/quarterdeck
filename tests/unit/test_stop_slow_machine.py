"""A service that answers SIGTERM is not read as unanswered because the stop's own clock moved on while it waited.

A stand-in service frees its lock 0.3 s (real time) after SIGTERM: an honest, quick answer. The stop's monotonic clock
stalls for its first few reads, as a busy machine's does (tests/stalled_clock.py), never load. A stand-in that never
answers is still killed, at the safety bound, by the same stalled clock with no real waiting.
"""
import signal
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from quarterdeck import service_processes, servicectl, shutdown, write_bound
from tests.stalled_clock import stall

ANSWER = 0.3  # seconds of real time the stand-in takes to answer SIGTERM


def stand_in(tmp_path, monkeypatch):
    monkeypatch.setattr(service_processes, "capture", lambda *args, **kwargs:
        SimpleNamespace(read=lambda: {}, proof="processes re-read: synthetic private scope", servers={}))
    pid = 999999996
    servicectl.heartbeat_path(tmp_path).parent.mkdir()
    servicectl.heartbeat_path(tmp_path).write_text(f"{pid} {datetime.now(timezone.utc).isoformat()}\n")
    expected = {"pid": pid, "start": "synthetic-start-zzzz"}
    state = {"term": None, "killed": False}

    def released():
        return state["killed"] or (state["term"] is not None and time.monotonic() - state["term"] >= ANSWER)

    def kill(target, sig):
        if sig == signal.SIGTERM:
            state["term"] = time.monotonic()  # the test's real clock: only servicectl's stalls

    monkeypatch.setattr(servicectl, "alive", lambda _: True)
    monkeypatch.setattr(servicectl, "holder", lambda _: pid)
    monkeypatch.setattr(servicectl, "locked", lambda _: not released())
    monkeypatch.setattr(servicectl.os, "kill", kill)
    monkeypatch.setattr(write_bound, "tree", lambda *a, **k: {})
    monkeypatch.setattr(write_bound, "kill", lambda known, **k: state.update(killed=True))
    monkeypatch.setattr(shutdown, "owner", lambda runtime, found=None: expected)
    monkeypatch.setattr(shutdown, "process_start", lambda found: expected["start"])
    monkeypatch.setattr(shutdown, "inflight", lambda runtime, owner: None)
    monkeypatch.setattr(shutdown, "process_gone", lambda found: released())
    monkeypatch.setattr(shutdown, "annotate", lambda runtime, owner, forced: "")
    return state


def test_a_quick_answer_reads_stopped_on_a_free_machine(tmp_path, monkeypatch):
    state = stand_in(tmp_path, monkeypatch)
    result = servicectl.stop(tmp_path)
    assert result.startswith("service stopped") and not state["killed"], result


def test_a_quick_answer_is_not_killed_because_the_stops_clock_stalled(tmp_path, monkeypatch):
    state = stand_in(tmp_path, monkeypatch)
    clock = stall(monkeypatch, servicectl, reads=4, seconds=2.0)  # the stop is not scheduled for a moment: its clock jumps
    began = time.monotonic()
    result = servicectl.stop(tmp_path)
    assert time.monotonic() - began < ANSWER * 3, 'the whole stop took well under a second of real time'
    assert result.startswith("service stopped") and not state["killed"], result
    assert "s for the service to end" in result, 'a slow stop stays visible: ' + result


def test_a_service_that_never_answers_is_killed_at_the_safety_bound(tmp_path, monkeypatch):
    state = stand_in(tmp_path, monkeypatch)
    monkeypatch.setattr(servicectl.os, "kill", lambda target, sig: None)  # SIGTERM lands nowhere: no answer
    stall(monkeypatch, servicectl, reads=12, seconds=4.0)  # the stop's clock passes the bound with no real waiting
    began = time.monotonic()
    result = servicectl.stop(tmp_path)
    assert time.monotonic() - began < 2.0
    assert state["killed"] and result.startswith("service killed"), result
    assert f"did not answer SIGTERM within {shutdown.ORDINARY_GRACE:g}s" in result, result


def test_the_safety_bound_is_far_above_any_stall_and_one_with_a_deliverys():
    assert shutdown.ORDINARY_GRACE == shutdown.DELIVERY_GRACE == 30.0
