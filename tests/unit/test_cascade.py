"""Ordinary synthetic cascade bookkeeping; no provider or protection fixtures."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import cascade, cli, post, post_threads, ship as shipmod
from quarterdeck.harness.base import Control, Input, State, Tokens, Turn
from quarterdeck.harness.types import TurnFailure
from quarterdeck.seat import Seat
from quarterdeck.ship import Cascade, Pool, SeatSpec, Session, Ship, ShipError


@pytest.fixture
def ring(tmp_path):
    members = ("alpha", "beta", "gamma")
    specs = {name: SeatSpec(name, "crew", "ring", "fake", ("fake",), effort="low")
             for name in (*members, "conductor")}
    ship = Ship(tmp_path, "example", "operator", tmp_path, {"fake": Pool("fake", "synthetic")},
                {"crew": Session("crew", "Example")}, specs, cascade=Cascade(members, "conductor"))
    store = post.Store(ship.runtime)
    svc = SimpleNamespace(ship=ship, store=store)
    question = store.new("question", "operator", "conductor", "Invent a garden plan.")
    changes = []

    def effort(name, wanted):
        changes.append((name, wanted))
        return dict(requested=wanted, actual=wanted, detail="Synthetic control confirmed.")

    runner = cascade.Runner(ship, store, lambda name, order: None, effort)
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
        cascade.Runner(ring.ship, post.Store(ring.ship.runtime), lambda name, order: None,
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
    ring.runner.health = lambda name, order: "owned pane is dead"
    ring.runner.advance(ring.path)
    assert "running" in cascade.start(ring.service, other.id, "operator", ["high"])
    assert cascade.folder(ring.ship.runtime, other.id) != ring.path
    ring.runner.advance(cascade.folder(ring.ship.runtime, other.id))
    assert (ring.path / "responses.md").read_text() != (cascade.folder(ring.ship.runtime, other.id) / "responses.md").read_text()


def test_named_fewer_laps_run_in_the_named_order(tmp_path, ring):
    # A separate ordinary question starts after the first run ends.
    ring.runner.health = lambda name, order: "held at limit with no eligible destination"
    ring.runner.advance(ring.path)
    ring.runner.health = lambda name, order: None
    question = ring.store.new("question", "operator", "conductor", "A second plan.")
    cascade.start(ring.service, question.id, "operator", ["high", "low"])
    ring.path = cascade.folder(ring.ship.runtime, question.id)
    for _ in range(6):
        ring.runner.advance(ring.path)
        report(ring)
        ring.runner.advance(ring.path)
    assert [level for _, level in ring.changes] == ["high"] * 3 + ["low"] * 3
    assert cascade.load(ring.path)["status"] == "done"


@pytest.mark.parametrize("reason", ["owned pane is dead", "held at limit with no eligible destination"])
def test_early_stop_names_current_seat_preserves_last_answer_and_posts_final_once(ring, reason):
    ring.runner.advance(ring.path)
    answer = report(ring)
    ring.runner.advance(ring.path)
    outstanding = current(ring)
    ring.runner.health = lambda name, order: reason
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
    recovered = cascade.Runner(ring.ship, post.Store(ring.ship.runtime), lambda name, order: None, ring.runner.effort)
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
        ring.runner.health = lambda name, order: "held at limit with no eligible destination"
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
    assert ring.ship.groups == {}, "the landed groups default keeps its constructor position"
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


@pytest.mark.parametrize("reason", [None, "owned pane is dead", "held at limit with no eligible destination"])
def test_health_delegates_order_free_dead_and_limit_observation_to_shared_helper(ring, monkeypatch, reason):
    from quarterdeck import group_post
    calls = []

    def common(service, name):
        calls.append((service, name))
        return reason

    monkeypatch.setattr(group_post, "current_seat_failure", common)
    # The shared helper owns absent/unknown pane and eligible-destination decisions.
    # No local pane/status proof is duplicated, and no record is needed for preflight.
    assert cascade.health(ring.service, "alpha") == reason
    assert calls == [(ring.service, "alpha")]


def ordinary_failure(ring, monkeypatch):
    """Invented program-error State/Input evidence, not a new native reader fixture."""
    from quarterdeck import group_post
    monkeypatch.setattr(group_post, "current_seat_failure", lambda service, name: None)
    order = ring.store.new("order", "ship", "alpha", "Improve the fictional garden plan.", re=ring.question.id)
    started = order.at + timedelta(seconds=10)
    ended = order.at + timedelta(seconds=30)
    failure = TurnFailure("Synthetic ordinary program stopped.", ended, kind="program error")
    turn = Turn(0, started, ended, "Earlier task.", "", "fake-1", Tokens(), failure=failure)
    evidence = dict(state=State(turn_open=False, failure=failure, completed_turns=(turn,)), inputs=[])
    launch = SimpleNamespace(pane="%synthetic", pid=1234, at="synthetic-launch")
    seat = SimpleNamespace(spec=ring.ship.seat("alpha"), launched=lambda: launch,
                           record=lambda: ring.path / "synthetic-ordinary-record",
                           _lifecycle=lambda **kwargs: nullcontext(),
                           harness=SimpleNamespace(state=lambda record: evidence["state"],
                                                   inputs=lambda record: evidence["inputs"]))
    service = SimpleNamespace(ship=ring.ship, store=ring.store, seat=lambda name: seat)
    return service, order, evidence, turn


def test_old_failure_after_post_but_before_actual_taking_cannot_end_current_lap(ring, monkeypatch):
    service, order, evidence, turn = ordinary_failure(ring, monkeypatch)
    assert turn.started > order.at, "posted-at alone would misclassify this older failure"
    assert cascade.health(service, "alpha", order) is None, "no native taking yet"
    evidence["inputs"] = [Input(post.typed([order]), order.at + timedelta(seconds=40))]
    assert cascade.health(service, "alpha", order) is None, "old failed turn precedes actual taking"


def test_failed_turn_from_before_current_order_remains_history(ring, monkeypatch):
    service, order, evidence, turn = ordinary_failure(ring, monkeypatch)
    failure = replace(turn.failure, at=order.at - timedelta(seconds=10))
    older = replace(turn, started=order.at - timedelta(seconds=20),
                    ended=order.at - timedelta(seconds=10), failure=failure)
    evidence["state"] = replace(evidence["state"], failure=failure, completed_turns=(older,))
    assert cascade.health(service, "alpha", order) is None


def test_current_failed_turn_at_or_after_measured_taking_ends_lap(ring, monkeypatch):
    service, order, evidence, turn = ordinary_failure(ring, monkeypatch)
    evidence["inputs"] = [Input(post.typed([order]), turn.started)]
    assert cascade.health(service, "alpha", order) == "program error"


def test_generic_associated_failure_does_not_become_a_pre_report_override(ring, monkeypatch):
    service, order, evidence, turn = ordinary_failure(ring, monkeypatch)
    evidence["inputs"] = [Input(post.typed([order]), turn.started)]
    assert cascade.associated_failure(service, "alpha", order) == "program error"
    assert cascade.known_refusal(service, "alpha", order) is None
    assert cascade.health(service, "alpha", order) == "program error"


def test_exact_failed_input_bundle_associates_order_without_guessing_turn_start(ring, monkeypatch):
    service, order, evidence, turn = ordinary_failure(ring, monkeypatch)
    bundle = post.briefed_header("alpha", "ring", [order]) + "\n" + post.typed([order])
    failure = replace(turn.failure, inputs=(Input(bundle, order.at + timedelta(seconds=1)),))
    evidence["state"] = replace(evidence["state"], failure=failure, completed_turns=())
    assert cascade.health(service, "alpha", order) == "program error"


@pytest.mark.parametrize("kind,offset,expected", [
    ("queued", 5, None), ("accepted", None, None), ("consumed", 20, None),
    ("consumed", 10, "program error")])
def test_unknown_queue_or_consumption_timing_never_uses_acceptance_clock(ring, monkeypatch, kind, offset, expected):
    service, order, evidence, turn = ordinary_failure(ring, monkeypatch)
    accepted = order.at + timedelta(seconds=5) if offset is not None else None
    consumed = order.at + timedelta(seconds=offset) if kind == "consumed" else None
    evidence["inputs"] = [Input(post.typed([order]), accepted, kind, consumed)]
    assert cascade.health(service, "alpha", order) == expected


def test_health_receives_exact_current_step_order_and_holds_unknown_evidence(ring):
    calls = []
    ring.runner.health = lambda name, order: calls.append((name, order)) or None
    ring.runner.advance(ring.path)
    order = current(ring)
    ring.runner.advance(ring.path)
    assert calls == [("alpha", None), ("alpha", order)]
    assert cascade.load(ring.path)["status"] == "running"


def test_unreadable_current_record_or_unknown_turn_state_cannot_end_lap(ring, monkeypatch):
    service, order, evidence, turn = ordinary_failure(ring, monkeypatch)
    evidence["inputs"] = [Input(post.typed([order]), turn.started)]
    evidence["state"] = replace(evidence["state"], turn_open=None)
    assert cascade.health(service, "alpha", order) is None

    def unreadable(record):
        raise OSError("Synthetic ordinary record temporarily unavailable")

    service.seat("alpha").harness.state = unreadable
    assert cascade.health(service, "alpha", order) is None


def test_fresh_orchestrator_recarriage_keeps_already_delivered_final_notice_once(ring):
    for _ in range(9):
        ring.runner.advance(ring.path)
        report(ring)
        ring.runner.advance(ring.path)
    final = [m for m in ring.store.for_seat("conductor") if m.sender == "ship"][0]
    attempts = post.Attempts(ring.ship.runtime)
    for message in (ring.question, final):
        attempt = attempts.open(message, "conductor", "%synthetic", 1234, "synthetic-old-launch")
        attempts.close(attempt, "delivered")
    seat = Seat(ring.ship, ring.ship.seat("conductor"), None)
    before = ring.store.all()
    first, incoming = seat.first_message(resume_id=None)
    assert incoming == [ring.question]
    rows = {row['id']: row for row in (json.loads(line) for line in first.splitlines() if line.startswith('{'))}
    assert rows[ring.question.id]['amendments'].count(final.id) == 1
    assert json.dumps(rows[ring.question.id]).count(final.id) == 1
    assert ring.question.body not in first and final.body not in first
    printed = post_threads.render(post_threads.whole(ring.store, ring.question.id, seat.name))
    assert printed.count(final.body) == 1 and printed.index(ring.question.body) < printed.index(final.body)
    assert seat.first_message(resume_id=None) == (first, incoming)
    assert ring.store.all() == before and ring.store.is_open(ring.question)
    assert len(attempts.for_message(final.id)) == 1, "carriage selection is not another delivery receipt"


def test_unraised_effort_and_limitations_appear_in_notes_and_final_notice(ring):
    ring.runner.effort = lambda name, wanted: dict(requested=wanted, actual="low",
                                                  detail="No proven in-session control; effort retained.")
    for _ in range(9):
        ring.runner.advance(ring.path)
        report(ring)
        ring.runner.advance(ring.path)
    notes = (ring.path / "responses.md").read_text()
    final = [m for m in ring.store.for_seat("conductor") if m.sender == "ship"][0]
    assert "Requested effort: high; actual: low" in notes
    assert "requested high; actual low" in final.body
    assert "No proven in-session control; effort retained." in notes and "No proven in-session control; effort retained." in final.body
    assert "limitations in your conclusion" in final.body and "requested levels are not proof" in final.body
