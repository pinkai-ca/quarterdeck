"""Each service request is answered alone: a poison before a stop never keeps the stop waiting.

A synthetic ship with one fake seat; the stop's interrupt is synthetic, so no program is touched.
"""

import json

import pytest

from quarterdeck import door as doormod
from quarterdeck.service import Service
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


def test_a_failing_rebrief_stays_to_be_tried_again_and_the_stop_after_it_is_answered(svc, monkeypatch):
    service, folder = svc
    monkeypatch.setattr(Service, "answer", lambda self, data, original=Service.answer:
                        (_ for _ in ()).throw(OSError("synthetic interrupted post")) if data["kind"] == "rebrief"
                        else original(self, data))
    paths = requests(service, folder, **{"110000-rebrief": {"kind": "rebrief", "commit": "synthetic", "token": "t"},
                                         "110001-stop-2": {"kind": "stop", "seat": "survivor"}})
    assert paths["110001-stop-2"].with_suffix(".done").read_text() == "survivor interrupted (synthetic)\n"
    assert paths["110000-rebrief"].exists() and not (folder / "failed").exists(), "kept in place for the next tick"
    error, = logged(service, "error")
    assert "synthetic interrupted post" in error["error"] and "Traceback" in error["trace"]
