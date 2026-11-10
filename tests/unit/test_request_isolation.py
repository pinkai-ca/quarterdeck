"""Each service request is answered alone: a poison before a stop never keeps the stop waiting.

A synthetic ship with one fake seat; the stop's interrupt is synthetic, so no program is touched.
"""

import json

import pytest

from quarterdeck import door as doormod, durable, watch
from quarterdeck.service import Service
from quarterdeck.ship import ShipError
from .test_check_lifetime import synthetic_ship


@pytest.fixture
def svc(tmp_path, monkeypatch):
    ship = synthetic_ship(tmp_path, survivor=True)
    monkeypatch.setattr(doormod.Door, "interrupt", lambda self: f"{self.seat.name} interrupted (synthetic)")
    service = Service(ship, None)
    folder = ship.runtime / "requests"
    folder.mkdir(parents=True)
    yield service, folder
    service.ledger.close()


def requests(service, folder, **named):
    paths = {}
    for name, body in named.items():
        paths[name] = folder / f"{name}.json"
        paths[name].write_text(body if isinstance(body, str) else json.dumps(body))
    service.tick()  # every duty, each failing alone, as the service runs them
    return paths


def logged(service, event):
    rows = [row for row in map(json.loads, service.log_path.read_text().splitlines()) if row["event"] == event]
    return [row for row in rows if event != "error" or row.get("step") == "requests"]


@pytest.mark.parametrize("poison", ["{not json", {"kind": "stop"}], ids=["unreadable", "missing-seat"])
def test_a_poison_request_sorted_before_a_stop_leaves_the_stop_answered_that_same_tick(svc, poison):
    service, folder = svc
    paths = requests(service, folder, **{"110000-stop-1": poison, "110001-stop-2": {"kind": "stop", "seat": "survivor"}})
    stop = paths["110001-stop-2"]
    assert not stop.exists() and stop.with_suffix(".done").read_text() == "survivor interrupted (synthetic)\n"
    bad = paths["110000-stop-1"]
    assert not bad.exists(), "the poison no longer sits in the queue"
    kept = folder / "failed" / bad.name
    assert kept.read_text() == (poison if isinstance(poison, str) else json.dumps(poison)), "the request is kept as it was"
    answer = bad.with_suffix(".done").read_text()
    assert answer.startswith("failed: ") and "requests/failed/110000-stop-1.json" in answer, answer
    error, = logged(service, "error")
    assert error["request"] == bad.name and "Traceback" in error["trace"], "the log explains the failure"
    service.tick()
    assert not bad.exists() and len(logged(service, "error")) == 1, "a failed request is never retried"


def test_a_refused_request_is_answered_in_its_done_and_removed(svc):
    service, folder = svc
    paths = requests(service, folder, **{"110000-stop-1": {"kind": "stop", "seat": "nobody"},
                                         "110001-stop-2": {"kind": "stop", "seat": "survivor"}})
    refused = paths["110000-stop-1"]
    assert not refused.exists() and not (folder / "failed").exists()
    answer = refused.with_suffix(".done").read_text()
    assert answer.startswith("refused: ") and "nobody" in answer, answer
    assert paths["110001-stop-2"].with_suffix(".done").read_text() == "survivor interrupted (synthetic)\n"
    assert [row["answer"][:8] for row in logged(service, "request")] == ["refused:", "survivor"]
    assert not logged(service, "error"), "a refusal is an answer, not an error"


def test_an_unwritable_reply_for_one_request_does_not_block_the_next_stop_or_repeat_the_action(svc, monkeypatch):
    service, folder = svc
    called = []
    monkeypatch.setattr(doormod.Door, "interrupt", lambda self: called.append(self.seat.name) or "synthetic stop")
    (folder / "110000-stop-1.done").mkdir()  # this request alone cannot publish its answer
    paths = requests(service, folder, **{"110000-stop-1": {"kind": "stop", "seat": "survivor"},
                                         "110001-stop-2": {"kind": "stop", "seat": "survivor"}})
    assert paths["110001-stop-2"].with_suffix(".done").read_text() == "synthetic stop\n"
    assert (folder / "failed" / paths["110000-stop-1"].name).is_file()
    assert not paths["110000-stop-1"].exists()
    errors = logged(service, "error")
    assert any(row.get("request") == "110000-stop-1.json" and "Traceback" in row["trace"] for row in errors)
    service.tick()
    assert called == ["survivor", "survivor"], "the first action may have run: neither is repeated"


def test_failed_archival_after_an_action_keeps_its_claim_and_later_requests_run_across_restart(svc, monkeypatch):
    service, folder = svc
    called = []

    def interrupt(self):
        called.append(self.seat.name)
        if len(called) == 1:
            raise RuntimeError("synthetic error after the action ran")
        return "synthetic stop"

    monkeypatch.setattr(doormod.Door, "interrupt", interrupt)
    (folder / "failed").write_text("synthetic obstruction to archival")
    paths = requests(service, folder, **{"110000-stop-1": {"kind": "stop", "seat": "survivor"},
                                         "110001-stop-2": {"kind": "stop", "seat": "survivor"}})
    assert paths["110001-stop-2"].with_suffix(".done").read_text() == "synthetic stop\n"
    held = paths["110000-stop-1"].with_suffix(".claimed")
    assert json.loads(held.read_text()) == {"kind": "stop", "seat": "survivor"}
    assert "claimed" in paths["110000-stop-1"].with_suffix(".done").read_text()
    service.requests()
    restarted = Service(service.ship, None)
    try:
        restarted.requests()
    finally:
        restarted.ledger.close()
    assert called == ["survivor", "survivor"], "a claimed action is never replayed, even if archival failed"
    monkeypatch.setattr(watch, "seats", lambda *a: [])
    assert "request claimed: 110000-stop-1.claimed" in watch.facts(service.ship, None, walls=[], login=[]).line()


def test_a_claim_write_failure_happens_before_dispatch_and_does_not_block_later_requests(svc, monkeypatch):
    service, folder = svc
    original = durable.replace_file
    called = []

    def write(path, text):
        if path.name == "110000-stop-1.claimed":
            raise OSError("synthetic claim write failure")
        return original(path, text)

    monkeypatch.setattr(durable, "replace_file", write)
    monkeypatch.setattr(doormod.Door, "interrupt", lambda self: called.append(self.seat.name) or "synthetic stop")
    paths = requests(service, folder, **{"110000-stop-1": {"kind": "stop", "seat": "survivor"},
                                         "110001-stop-2": {"kind": "stop", "seat": "survivor"}})
    assert paths["110001-stop-2"].with_suffix(".done").read_text() == "synthetic stop\n"
    assert called == ["survivor"], "no action runs without a durable claim"
    assert paths["110000-stop-1"].exists(), "the unexecuted request remains for a later claim attempt"


def test_a_preexisting_claim_is_never_dispatched_and_deferred_requests_release_their_claim(svc, monkeypatch):
    service, folder = svc
    held = folder / "110000-stop-1.json"
    held.write_text(json.dumps(dict(kind="stop", seat="survivor")))
    held.with_suffix(".claimed").write_text(held.read_text())
    original = Service.answer
    monkeypatch.setattr(Service, "answer", lambda self, data: None if data["kind"] == "synthetic-deferred" else original(self, data))
    paths = requests(service, folder, **{"110001-deferred": {"kind": "synthetic-deferred"},
                                         "110002-stop-2": {"kind": "stop", "seat": "survivor"}})
    assert held.exists() and held.with_suffix(".claimed").exists() and not held.with_suffix(".done").exists()
    assert paths["110001-deferred"].exists() and not paths["110001-deferred"].with_suffix(".claimed").exists()
    assert paths["110002-stop-2"].with_suffix(".done").read_text() == "survivor interrupted (synthetic)\n"


@pytest.mark.parametrize("error_type", [OSError, ShipError])
def test_a_failing_rebrief_stays_to_be_tried_again_and_the_stop_after_it_is_answered(svc, monkeypatch, error_type):
    service, folder = svc
    monkeypatch.setattr(Service, "answer", lambda self, data, original=Service.answer:
                        (_ for _ in ()).throw(error_type("synthetic interrupted post")) if data["kind"] == "rebrief"
                        else original(self, data))
    paths = requests(service, folder, **{"110000-rebrief": {"kind": "rebrief", "commit": "synthetic", "token": "t"},
                                         "110001-stop-2": {"kind": "stop", "seat": "survivor"}})
    assert paths["110001-stop-2"].with_suffix(".done").read_text() == "survivor interrupted (synthetic)\n"
    assert paths["110000-rebrief"].exists() and not (folder / "failed").exists(), "kept in place for the next tick"
    assert not paths["110000-rebrief"].with_suffix(".claimed").exists()
    error, = logged(service, "error")
    assert "synthetic interrupted post" in error["error"] and "Traceback" in error["trace"]
