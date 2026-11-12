"""Ordinary synthetic cascade bookkeeping; no provider or protection fixtures."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import cascade, cli, post, ship as shipmod
from quarterdeck.harness.base import Control
from quarterdeck.ship import Cascade, Pool, SeatSpec, Session, Ship, ShipError


@pytest.fixture
def ring(tmp_path):
    members = ("alpha", "beta", "gamma")
    specs = {name: SeatSpec(name, "crew", "ring", "fake", ("fake",), effort="low")
             for name in (*members, "conductor")}
    ship = Ship(tmp_path, "example", "operator", tmp_path, {"fake": Pool("fake", "synthetic")},
                {"crew": Session("crew", "Example")}, specs, Cascade(members, "conductor"))
    store = post.Store(ship.runtime)
    svc = SimpleNamespace(ship=ship, store=store)
    question = store.new("question", "operator", "conductor", "Invent a garden plan.")
    changes = []

    def effort(name, wanted):
        changes.append((name, wanted))
        return dict(requested=wanted, actual=wanted, detail="Synthetic control confirmed.")

    runner = cascade.Runner(ship, store, lambda name: None, effort)
    cascade.start(svc, question.short, "conductor")
    path = cascade.folder(ship.runtime, question.id)
    return SimpleNamespace(ship=ship, store=store, service=svc, runner=runner, question=question,
                           path=path, changes=changes)


def current(ring):
    data = cascade.load(ring.path)
    return ring.store.get(data["steps"][-1]["order"])


def report(ring, text="A small improvement.", answer="The complete garden plan."):
    order = current(ring)
    return ring.store.new("report", order.to, "ship", f"{cascade.NOTE}\n{text}\n{cascade.ANSWER}\n{answer}", re=order.id)


def test_three_laps_relay_whole_answers_and_one_final_without_intermediate_orchestrator_posts(ring):
    root = ring.question
    prior = root.body
    notes = []
    sizes = []
    for index in range(9):
        ring.runner.advance(ring.path)
        order = current(ring)
        assert order.to == ("alpha", "beta", "gamma")[index % 3]
        assert order.sender == "ship" and order.re == root.id
        assert order.body.endswith(prior)
        assert ring.store.is_open(root)
        assert not [m for m in ring.store.for_seat("conductor") if m.sender == "ship"]
        answer = report(ring, text=f"Improvement {index + 1}.", answer=f"Full answer {index + 1}: 🌱")
        prior = answer.body
        notes.append(f"Improvement {index + 1}.")
        ring.runner.advance(ring.path)
        sizes.append((ring.path / "responses.md").stat().st_size)
        if index < 8:
            assert current(ring).body.endswith(answer.body)
    assert sizes == sorted(set(sizes)), "the same file grows at every accepted stop"
    text = (ring.path / "responses.md").read_text()
    assert all(text.count(note) == 1 for note in notes)
    assert all(text.count(f"## {name} · lap {lap}") == 1
               for name in ("alpha", "beta", "gamma") for lap in (1, 2, 3))
    assert ring.changes == [(name, level) for level in ("low", "medium", "high")
                            for name in ("alpha", "beta", "gamma")]
    finals = [m for m in ring.store.for_seat("conductor") if m.sender == "ship"]
    assert len(finals) == 1 and finals[0].kind == "report" and finals[0].re == root.id
    assert str(ring.path / "responses.md") in finals[0].body and finals[0].body.endswith(prior)
    assert ring.store.is_open(root), "the service notice cannot answer the original question"
    for _ in range(3):
        cascade.Runner(ring.ship, post.Store(ring.ship.runtime), lambda name: None,
                       ring.runner.effort).advance(ring.path)
        cascade.start(ring.service, root.id, "conductor")
    assert len(ring.store.all()) == 20  # root + nine orders/reports + final
    ring.store.new("answer", "conductor", "operator", "Concluded.", re=root.id)
    assert not ring.store.is_open(root)


def test_start_keeps_one_active_question_and_reuses_its_canonical_identity(ring):
    assert "running" in cascade.start(ring.service, ring.question.id, "conductor")
    other = ring.store.new("question", "operator", "conductor", "Another garden.")
    with pytest.raises(ShipError, match="another question"):
        cascade.start(ring.service, other.id, "operator")
    ring.runner.health = lambda name: "dead or missing owned pane"
    ring.runner.advance(ring.path)
    assert "running" in cascade.start(ring.service, other.id, "operator", ["high"])
    assert cascade.folder(ring.ship.runtime, other.id) != ring.path
    ring.runner.advance(cascade.folder(ring.ship.runtime, other.id))
    assert (ring.path / "responses.md").read_text() != (cascade.folder(ring.ship.runtime, other.id) / "responses.md").read_text()


def test_named_fewer_laps_run_in_the_named_order(tmp_path, ring):
    # A separate ordinary question starts after the first run ends.
    ring.runner.health = lambda name: "pool wall"
    ring.runner.advance(ring.path)
    ring.runner.health = lambda name: None
    question = ring.store.new("question", "operator", "conductor", "A second plan.")
    cascade.start(ring.service, question.id, "operator", ["high", "low"])
    ring.path = cascade.folder(ring.ship.runtime, question.id)
    for _ in range(6):
        ring.runner.advance(ring.path)
        report(ring)
        ring.runner.advance(ring.path)
    assert [level for _, level in ring.changes] == ["high"] * 3 + ["low"] * 3
    assert cascade.load(ring.path)["status"] == "done"


@pytest.mark.parametrize("reason", ["dead or missing owned pane", "pool wall"])
def test_early_stop_names_current_seat_preserves_last_answer_and_posts_final_once(ring, reason):
    ring.runner.advance(ring.path)
    answer = report(ring)
    ring.runner.advance(ring.path)
    outstanding = current(ring)
    ring.runner.health = lambda name: reason
    ring.runner.advance(ring.path)
    before = ring.store.all()
    ring.runner.advance(ring.path)
    assert ring.store.all() == before
    final = before[-1]
    assert f"beta: {reason}" in final.body and final.body.endswith(answer.body)
    assert cascade.load(ring.path)["status"] == "stopped"
    assert ring.store.is_open(outstanding), "early stop cannot forge the addressee's closing report"
    assert outstanding.id in final.body
    assert not [m for m in before if m.kind == "order" and m.to == "gamma"]
    late = ring.store.new("report", outstanding.to, "ship", "## Cascade note\nLate.\n## Answer\nLate answer.", re=outstanding.id)
    ring.runner.advance(ring.path)
    assert ring.store.all() == before + [late]
    assert "Late." not in (ring.path / "responses.md").read_text()


@pytest.mark.parametrize("body", ["No heading", "## Cascade note\n\n## Answer\nAnswer",
                                      "## Cascade note\n" + "x" * 801 + "\n## Answer\nAnswer",
                                      "## Cascade note\nBrief\n## Answer\n"])
def test_invalid_note_ends_early_without_truncating_or_relaying(ring, body):
    ring.runner.advance(ring.path)
    order = current(ring)
    ring.store.new("report", order.to, "ship", body, re=order.id)
    ring.runner.advance(ring.path)
    assert cascade.load(ring.path)["status"] == "stopped"
    assert not [m for m in ring.store.all() if m.kind == "order" and m.to == "beta"]
    assert "## alpha" not in (ring.path / "responses.md").read_text()


def test_duplicate_reports_only_accept_the_first_closing_report(ring):
    ring.runner.advance(ring.path)
    first = report(ring, text="First note.")
    report(ring, text="Duplicate note.")
    ring.runner.advance(ring.path)
    assert current(ring).body.endswith(first.body)
    assert "Duplicate note." not in (ring.path / "responses.md").read_text()


@pytest.mark.parametrize("cut", ["intent", "partial", "whole"])
def test_interrupted_append_recovers_remaining_bytes_exactly_once(ring, monkeypatch, cut):
    ring.runner.advance(ring.path)
    answer = report(ring, text="A fresh 🌿 note.")
    append = ring.runner.append
    original = (ring.path / "responses.md").read_bytes()

    def interrupted(path, data):
        pending = data["notes"]["pending"]
        if pending is not None and "A fresh" in pending:
            encoded = pending.encode("utf-8")
            amount = {"intent": 0, "partial": len(encoded) - 5, "whole": len(encoded)}[cut]
            with (path / "responses.md").open("ab") as output:
                output.write(encoded[:amount])
            raise OSError("synthetic interrupted append")
        return append(path, data)

    monkeypatch.setattr(ring.runner, "append", interrupted)
    with pytest.raises(OSError, match="synthetic interrupted append"):
        ring.runner.advance(ring.path)
    recovered = cascade.Runner(ring.ship, post.Store(ring.ship.runtime), lambda name: None, ring.runner.effort)
    recovered.advance(ring.path)
    recovered.advance(ring.path)
    text = (ring.path / "responses.md").read_text()
    assert text.count("A fresh 🌿 note.") == 1
    assert (ring.path / "responses.md").read_bytes().startswith(original)
    assert current(ring).body.endswith(answer.body)
    assert len([m for m in ring.store.all() if m.kind == "order" and m.to == "beta"]) == 1


@pytest.mark.parametrize("kind", ["order", "report"])
def test_publication_cut_after_store_write_recovers_same_immutable_intent(ring, monkeypatch, kind):
    if kind == "report":
        ring.runner.health = lambda name: "pool wall"
    write = ring.store.write

    def cut(message):
        write(message)
        raise OSError("synthetic publication cut")

    monkeypatch.setattr(ring.store, "write", cut)
    with pytest.raises(OSError, match="synthetic publication cut"):
        ring.runner.advance(ring.path)
    before = ring.store.all()
    cascade.Runner(ring.ship, post.Store(ring.ship.runtime), ring.runner.health,
                   ring.runner.effort).advance(ring.path)
    assert ring.store.all() == before
    assert before[-1].kind == kind


def test_interrupted_effort_control_is_not_retried_or_claimed_as_applied(ring):
    def cut(name, wanted):
        raise OSError("synthetic control cut")

    ring.runner.effort = cut
    with pytest.raises(OSError, match="synthetic control cut"):
        ring.runner.advance(ring.path)
    data = cascade.load(ring.path)
    assert data["steps"][0]["effort"]["actual"] == "unknown"
    ring.runner.advance(ring.path)  # no second call to the cutting control
    assert "actual unknown" in current(ring).body
    report(ring)
    ring.runner.effort = lambda name, wanted: dict(requested=wanted, actual="low", detail="Fallback.")
    ring.runner.advance(ring.path)
    assert "actual: unknown" in (ring.path / "responses.md").read_text()


@pytest.mark.parametrize("mode,actual,marker", [
    ("same", "high", "Already"), ("launch", "low", "No proven"),
    ("working", "low", "still open"), ("applied", "high", "confirmed by the program"),
    ("unconfirmed", "low", "not confirmed")])
def test_effort_control_uses_existing_confirmation_or_names_observed_fallback(monkeypatch, mode, actual, marker):
    from quarterdeck import watch
    controls = []
    seat = SimpleNamespace(spec=SimpleNamespace(pool_choices={}),
                           harness=SimpleNamespace(control=lambda key: Control("effort", "--effort", None if mode == "launch" else "/effort {value}", "screen")),
                           is_working=lambda: mode == "working")
    monkeypatch.setattr(watch, "running_settings", lambda seat: {"effort": "high" if mode == "same" else "low"})

    def control(*args):
        controls.append(args)
        return "effort = high · confirmed by screen" if mode == "applied" else "effort not confirmed"

    service = SimpleNamespace(seat=lambda name: seat, control=control)
    result = cascade.effort(service, "alpha", "high")
    assert result["actual"] == actual and marker in result["detail"]
    assert len(controls) == (1 if mode in ("applied", "unconfirmed") else 0)


def test_optional_defaulted_declaration_and_template_are_loadable(ring):
    assert replace(ring.ship, cascade=None).cascade is None
    root = Path(__file__).resolve().parents[2]
    assert shipmod.load(root / "ships" / "drydock").cascade is None
    template = shipmod.load(root / "ships" / "cascade")
    assert template.cascade.seats == ("first", "second", "third")
    assert template.cascade.efforts == ("low", "medium", "high")
    assert template.cascade.orchestrator == "orchestrator" and template.cascade.notes == "ring"
    assert {template.pool(s.pools[0]).provider for s in template.seats.values()} == {"anthropic", "openai", "xai"}


def test_declaration_preserves_optional_defaults_and_explicit_fewer_laps():
    assert cascade.declaration(None) is None
    table = dict(seats=["one", "two", "three"], orchestrator="conductor")
    assert cascade.declaration(table) == Cascade(("one", "two", "three"), "conductor")
    table.update(efforts=["low", "high"], note_chars=400)
    assert cascade.declaration(table).efforts == ("low", "high")
    assert cascade.declaration(table).note_chars == 400


def test_cli_start_passes_canonical_question_and_named_laps_without_touching_message_format(ring, monkeypatch):
    monkeypatch.setattr(cli, "load_ship", lambda args: ring.ship)
    monkeypatch.setenv("QD_SEAT", "conductor")
    sent = []
    monkeypatch.setattr(cli.service, "request", lambda *args, **kwargs: sent.append((args, kwargs)) or "accepted")
    args = cli.build_parser().parse_args(["cascade", "--re", ring.question.short, "--efforts", "medium,high"])
    assert args.run(args) == "accepted"
    assert sent == [((ring.ship.runtime, "cascade"), dict(question=ring.question.id, caller="conductor", efforts=["medium", "high"]))]
    assert post.Message.from_json(ring.question.to_json()) == ring.question
    assert "cascade" not in json.loads(ring.question.to_json())
