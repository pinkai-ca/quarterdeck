"""The cascade at the ship's own protections: refusal, positive failure, runtime confinement, carriage.

Claude-only validation, wholly synthetic: invented questions, Codex-shaped stand-in records built by
the refusal tests' world, fake ring seats and throwaway folders. No native program, provider, real
identity, capture or credential is used, and nothing here adds a recognizer or a policy.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import cascade, cascade_runtime, post, refusal, ship as shipmod
from quarterdeck.seat import Seat
from quarterdeck.ship import Cascade, Pool, SeatSpec, Session, Ship, ShipError
from tests.unit import refusal_ship
from tests.unit.refusal_ship import REFUSED, World

RING = ("worker", "tester", "spock")
HOLDS = (ShipError, ValueError, KeyError, TypeError)
REFUSAL_STOP = "provider refused this step"


def unchanged_effort(name, wanted):
    return dict(requested=wanted, actual="unknown", detail="Synthetic: no effort control exercised.")


def ship_posts(store):
    return [m for m in store.all() if m.sender == post.SHIP]


def finals(store, to):
    return [m for m in ship_posts(store) if m.to == to]


def step_order(store, path):
    data = cascade.load(path)
    return store.get(data["steps"][-1]["order"])


def reply(store, order, note="An invented improvement.", answer="The invented full answer."):
    return store.new("report", order.to, post.SHIP, f"{cascade.NOTE}\n{note}\n{cascade.ANSWER}\n{answer}", re=order.id)


# -- a Codex-shaped ring on the refusal tests' world ---------------------------------------


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = World(tmp_path)
    (tmp_path / "ship.toml").write_text(refusal_ship.SHIP + '''
[seats.conductor]
session = "crew"
role = "worker"
harness = "codex"
pool = "test"
''')
    # Declared after loading: the ring's three-provider rule is the declaration's, not this boundary's.
    built.svc.ship = replace(shipmod.load(tmp_path), cascade=Cascade(RING, "conductor"))
    built.fresh = []

    def fresh(seat):
        first, carried = seat.first_message()
        built.fresh.append((seat.name, first, [m.id for m in carried]))
        built.launch(seat.name)
        return f"{seat.name} fresh (stand-in)"

    monkeypatch.setattr(Seat, "fresh", fresh)
    # The service pass uses its own effort control; no control is exercised at this boundary.
    monkeypatch.setattr(cascade, "effort", lambda service, name, wanted: unchanged_effort(name, wanted))
    built.runner = cascade.Runner(built.svc.ship, built.svc.store,
                                  lambda name, order: cascade.health(built.svc, name, order), unchanged_effort)
    built.question = built.order("conductor", "Invent a fictional footbridge.", sender="operator", kind="question")
    cascade.start(built.svc, built.question.id, "conductor")
    built.path = cascade.folder(built.svc.runtime, built.question.id)
    yield built
    built.svc.ledger.close()


def provider_words_absent(world):
    texts = [m.body for m in ship_posts(world.svc.store)] + [(world.path / "responses.md").read_text(),
                                                             (world.path / "state.json").read_text()]
    for words in (REFUSED["message"], REFUSED["codex_error_info"]):
        assert all(words not in text for text in texts), "category only, never the provider's words"


def withheld(final, *bodies):
    assert final.body.startswith("Cascade stopped: provider refused this step.")
    assert "The last full answer is withheld" in final.body
    for body in bodies:
        assert body not in final.body, "no refused step input reaches another conversation"


def test_refused_current_step_ends_by_category_withholds_input_then_quarantines_once(world):  # law: context-is-cache
    world.launch("worker")
    world.runner.advance(world.path)
    order = step_order(world.svc.store, world.path)
    assert order.to == "worker"
    world.turn("worker", order, error=REFUSED)
    world.runner.advance(world.path)
    data = cascade.load(world.path)
    assert data["status"] == "stopped" and data["reason"] == f"worker: {REFUSAL_STOP}"
    assert data["stop_category"] == "provider-refusal" and data["stopped_report"] is None
    [final] = finals(world.svc.store, "conductor")
    assert final.re == world.question.id and "Seat: worker; lap: 1." in final.body
    withheld(final, order.body, world.question.body)
    provider_words_absent(world)
    # The existing refusal step then does exactly its own work: quarantine, one fresh start, a stub.
    world.svc.refusals()
    assert refusal.excluded(world.svc.runtime, "worker") == {order.id}
    assert len(world.fresh) == 1
    name, first, carried = world.fresh[0]
    assert name == "worker" and order.id not in carried
    assert order.body not in first and order.short in first and refusal.WITHHELD in first
    assert world.svc.store.is_open(order) and world.svc.store.is_open(world.question)
    assert not [m for m in world.svc.store.all() if m.re == order.id], "no sender notice invented for the ship"
    before = world.svc.store.all()
    world.svc.refusals()
    world.runner.advance(world.path)
    assert world.svc.store.all() == before and len(world.fresh) == 1, "once"
    assert not [m for m in before if m.kind == "order" and m.to == "tester"], "nothing relayed past the refusal"


@pytest.mark.parametrize("filed", [False, True])
def test_quarantine_seen_first_ends_the_ring_before_any_filed_report_is_accepted(world, filed):
    """The refusal step may relaunch the seat before the ring reads its record: the exclusion still ends it."""
    world.launch("worker")
    world.runner.advance(world.path)
    order = step_order(world.svc.store, world.path)
    world.turn("worker", order, error=REFUSED)
    world.svc.refusals()  # quarantined and relaunched first: the new record has no failure
    assert len(world.fresh) == 1 and refusal.excluded(world.svc.runtime, "worker") == {order.id}
    report = reply(world.svc.store, order, note="A note that must not be appended.",
                   answer="An answer that must not be relayed.") if filed else None
    notes = (world.path / "responses.md").read_bytes()
    world.runner.advance(world.path)
    data = cascade.load(world.path)
    assert data["status"] == "stopped" and data["stop_category"] == "provider-refusal"
    assert data["stopped_report"] == (report.id if filed else None)
    assert "report" not in data["steps"][-1] and data["last"] is None, "the filed report is not accepted"
    assert (world.path / "responses.md").read_bytes() == notes, "no note of the refused step"
    assert not [m for m in world.svc.store.all() if m.kind == "order" and m.to == "tester"]
    [final] = finals(world.svc.store, "conductor")
    withheld(final, order.body, world.question.body, *((report.body,) if filed else ()))
    if filed:
        assert report.id in final.body and world.svc.store.get(report.id).body == report.body
    for _ in range(2):
        world.runner.advance(world.path)
    assert len(finals(world.svc.store, "conductor")) == 1


def test_an_unreadable_exclusion_journal_holds_visibly_without_accepting_or_guessing(world):
    world.launch("worker")
    world.runner.advance(world.path)
    order = step_order(world.svc.store, world.path)
    report = reply(world.svc.store, order, note="A note held while unknown.")
    journal = refusal.path(world.svc.runtime, "worker")
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{not json")
    for expected in ([(world.question.id, "cascade step exclusion evidence is unreadable")], []):
        assert cascade.run(world.svc) == expected, "changed once, then unchanged"
    data = cascade.load(world.path)
    assert data["status"] == "running" and "report" not in data["steps"][-1] and data["last"] is None
    assert "A note held while unknown." not in (world.path / "responses.md").read_text()
    assert finals(world.svc.store, "conductor") == [], "neither a refusal nor any stop is guessed"
    assert not [m for m in world.svc.store.all() if m.kind == "order" and m.to == "tester"]
    assert cascade_runtime.holds(world.svc.runtime) == [
        f"{world.question.short} cascade held: cascade step exclusion evidence is unreadable"]
    journal.unlink()
    assert cascade.run(world.svc) == []
    assert cascade_runtime.holds(world.svc.runtime) == [], "recovery clears the derived hold"
    data = cascade.load(world.path)
    assert data["steps"][0]["report"] == report.id and step_order(world.svc.store, world.path).to == "tester"


def test_a_dead_pane_after_a_filed_report_still_accepts_the_report_first(world):
    seat = world.launch("worker")
    world.runner.advance(world.path)
    order = step_order(world.svc.store, world.path)
    report = reply(world.svc.store, order)
    launched = seat.launched()
    world.panes[launched.pane] = replace(world.panes[launched.pane], dead=True)
    world.runner.advance(world.path)
    data = cascade.load(world.path)
    assert data["status"] == "running" and data["steps"][0]["report"] == report.id
    assert step_order(world.svc.store, world.path).to == "tester" and finals(world.svc.store, "conductor") == []


def test_an_older_refusal_of_unrelated_work_never_ends_the_current_lap(world):
    world.launch("worker")
    unrelated = world.order("worker", "An invented unrelated order.", sender="tester")
    world.turn("worker", unrelated, error=REFUSED)
    world.runner.advance(world.path)
    order = step_order(world.svc.store, world.path)
    world.runner.advance(world.path)  # posted, not yet taken: the old failure is history
    assert cascade.load(world.path)["status"] == "running"
    world.event("worker", {"type": "task_started"})
    world.typed("worker", order)
    world.runner.advance(world.path)  # taken, turn still open
    assert cascade.load(world.path)["status"] == "running"
    world.event("worker", {"type": "task_complete", "last_agent_message": "Invented reply."})
    world.runner.advance(world.path)  # ended normally: nothing to associate
    assert cascade.load(world.path)["status"] == "running"
    reply(world.svc.store, order)
    world.runner.advance(world.path)
    assert step_order(world.svc.store, world.path).to == "tester"
    assert finals(world.svc.store, "conductor") == [], "no interim orchestrator post"
    provider_words_absent(world)


@pytest.mark.parametrize("pane,expected", [
    ("absent", None), ("other pid", None), ("dead", "worker: owned pane is dead"), ("unlaunched", None)])
def test_shared_positive_failure_reading_holds_on_absence_and_ends_only_on_owned_death(world, pane, expected):
    seat = world.launch("worker")
    world.runner.advance(world.path)
    launched = seat.launched()
    if pane == "absent":
        del world.panes[launched.pane]
    elif pane == "other pid":
        world.panes[launched.pane] = replace(world.panes[launched.pane], pid=launched.pid + 1000)
    elif pane == "dead":
        world.panes[launched.pane] = replace(world.panes[launched.pane], dead=True)
    else:
        (seat.folder / "launch.json").unlink()
    world.runner.advance(world.path)
    data = cascade.load(world.path)
    stops = finals(world.svc.store, "conductor")
    if expected is None:
        assert data["status"] == "running" and stops == [], "absence is not proof of death"
    else:
        assert data["status"] == "stopped" and data["reason"] == expected and len(stops) == 1
        assert data["stop_category"] == "seat" and "Last full answer:" in stops[0].body


def test_a_launch_replaced_while_reading_holds_instead_of_associating(world, monkeypatch):
    from quarterdeck import thread_context
    seat = world.launch("worker")
    world.runner.advance(world.path)
    order = step_order(world.svc.store, world.path)
    world.turn("worker", order, error=REFUSED)
    launch = seat.folder / "launch.json"
    saved = launch.read_bytes()
    original = thread_context.consumed_ids
    moved = []

    def moving(*args, **kwargs):
        found = original(*args, **kwargs)
        if not moved:  # another launch is written while the old record is being read
            data = json.loads(saved)
            launch.write_text(json.dumps(dict(data, at="2099-01-01T00:00:00+00:00")))
            moved.append(True)
        return found

    monkeypatch.setattr(thread_context, "consumed_ids", moving)
    assert cascade.health(world.svc, "worker", order) is None
    assert moved == [True]
    launch.write_bytes(saved)
    found = cascade.health(world.svc, "worker", order)
    assert found == REFUSAL_STOP and found.category == "provider-refusal", "the hold came from the move alone"


# -- confinement of the per-question run under the ship runtime ----------------------------


def fake_ring(tmp_path, start=True):
    members = ("alpha", "beta", "gamma")
    specs = {name: SeatSpec(name, "crew", "ring", "fake", ("fake",), effort="low")
             for name in (*members, "conductor")}
    ship = Ship(tmp_path / "ship", "example", "operator", tmp_path / "ship",
                {"fake": Pool("fake", "synthetic")}, {"crew": Session("crew", "Example")}, specs,
                cascade=Cascade(members, "conductor"))
    ship.runtime.mkdir(parents=True)
    store = post.Store(ship.runtime)
    question = store.new("question", "operator", "conductor", "Invent a fictional orchard.")
    service = SimpleNamespace(ship=ship, store=store, runtime=ship.runtime)
    runner = cascade.Runner(ship, store, lambda name, order: None, unchanged_effort)
    ring = SimpleNamespace(ship=ship, store=store, service=service, runner=runner, question=question,
                           path=cascade.folder(ship.runtime, question.id), outside=tmp_path / "outside")
    ring.outside.mkdir()
    if start:
        cascade.start(service, question.id, "conductor")
    return ring


def snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): (p.is_symlink(), None if p.is_dir() or p.is_symlink() else p.read_bytes())
            for p in sorted(folder.rglob("*"))}


def hold_lines_are_fixed(ring, *forbidden):
    lines = cascade_runtime.holds(ring.ship.runtime)
    assert lines and all(" cascade held: " in line for line in lines)
    assert all(word not in line for line in lines for word in forbidden)
    return lines


@pytest.mark.parametrize("linked", ["ring", "question"])
def test_a_linked_run_folder_is_refused_and_nothing_is_written_outside(tmp_path, linked):
    ring = fake_ring(tmp_path, start=False)
    if linked == "ring":
        (ring.ship.runtime / "ring").symlink_to(ring.outside, target_is_directory=True)
    else:
        (ring.ship.runtime / "ring").mkdir()
        ring.path.symlink_to(ring.outside, target_is_directory=True)
    before = ring.store.all()
    with pytest.raises(ShipError, match="inside the ship runtime"):
        cascade.start(ring.service, ring.question.id, "conductor")
    assert snapshot(ring.outside) == {} and ring.store.all() == before


@pytest.mark.parametrize("same", [True, False])
def test_a_linked_notes_file_is_never_followed_and_holds_visibly_until_restored(tmp_path, monkeypatch, same):
    ring = fake_ring(tmp_path)
    # Only the notes boundary is exercised: the pass's seat readings and controls are stand-ins.
    monkeypatch.setattr(cascade, "health", lambda service, name, order=None: None)
    monkeypatch.setattr(cascade, "effort", lambda service, name, wanted: unchanged_effort(name, wanted))
    notes = ring.path / "responses.md"
    original = notes.read_bytes()
    target = ring.outside / "followed.md"
    target.write_bytes(original if same else b"Invented outside text.\n")
    before = target.read_bytes()
    notes.unlink()
    notes.symlink_to(target)
    ring.runner.advance(ring.path)
    reply(ring.store, step_order(ring.store, ring.path), note="A note kept until the file is restored.")
    # A followed read would see the outside text and report inconsistency; nofollow reports unreadable.
    assert cascade.run(ring.service) == [(ring.question.id, "cascade evidence is unreadable")]
    journal = (ring.path / "state.json").read_bytes()  # the saved append intent, before any write
    assert cascade.run(ring.service) == [], "logged once"
    assert target.read_bytes() == before and notes.is_symlink()
    assert cascade.load(ring.path)["status"] == "running" and finals(ring.store, "conductor") == []
    assert not [m for m in ring.store.all() if m.kind == "order" and m.to == "beta"]
    assert (ring.path / "state.json").read_bytes() == journal, "the append intent is retained unchanged"
    hold_lines_are_fixed(ring, str(target), "Invented outside text.", "A note kept")
    notes.unlink()
    notes.write_bytes(original)
    notes.chmod(0o600)
    assert cascade.run(ring.service) == [] and cascade_runtime.holds(ring.ship.runtime) == []
    assert notes.read_text().count("A note kept until the file is restored.") == 1
    assert step_order(ring.store, ring.path).to == "beta" and target.read_bytes() == before


def test_a_linked_journal_is_never_followed_or_rewritten_and_holds(tmp_path):
    ring = fake_ring(tmp_path)
    journal = ring.path / "state.json"
    target = ring.outside / "journal.json"
    target.write_bytes(journal.read_bytes())
    before = target.read_bytes()
    journal.unlink()
    journal.symlink_to(target)
    posts = ring.store.all()
    with pytest.raises(OSError):
        ring.runner.advance(ring.path)
    assert cascade.run(ring.service) == [(ring.question.id, "cascade evidence is unreadable")]
    assert target.read_bytes() == before and journal.is_symlink(), "retained, never written through"
    assert ring.store.all() == posts
    hold_lines_are_fixed(ring, str(target))


@pytest.mark.parametrize("text,reason", [
    ("{not json", "cascade evidence is malformed JSON"), ("[]", "cascade evidence is inconsistent"),
    (json.dumps({"format": 2}), "cascade evidence is inconsistent"),
    ("partial", "cascade evidence is inconsistent"), ("mismatched", "cascade evidence is inconsistent")])
def test_a_malformed_or_foreign_journal_holds_retained_and_blocks_a_second_start(tmp_path, text, reason):
    ring = fake_ring(tmp_path)
    ring.runner.advance(ring.path)
    data = cascade.load(ring.path)
    if text == "partial":
        text = json.dumps({"format": 1, "question": ring.question.id})
    elif text == "mismatched":
        text = json.dumps(dict(data, question="que-20990101-0000-operator-0000"))
    (ring.path / "state.json").write_text(text)
    before = (snapshot(ring.path), ring.store.all())
    with pytest.raises(HOLDS):
        ring.runner.advance(ring.path)
    for expected in ([(ring.question.id, reason)], []):
        assert cascade.run(ring.service) == expected, "logged once per unchanged evidence"
    other = ring.store.new("question", "operator", "conductor", "Invent a second orchard.")
    with pytest.raises(HOLDS):
        cascade.start(ring.service, other.id, "operator")
    after = [m for m in ring.store.all() if m.id != other.id]
    assert (snapshot(ring.path), after) == before, "retained byte for byte, nothing published"
    assert not cascade.folder(ring.ship.runtime, other.id).exists(), "no second run on a guessed state"
    assert hold_lines_are_fixed(ring) == [f"{ring.question.short} cascade held: {reason}"]


def test_a_conflicting_post_under_a_saved_intent_is_never_overwritten(tmp_path):
    ring = fake_ring(tmp_path)
    ring.runner.advance(ring.path)
    order = step_order(ring.store, ring.path)
    stored = ring.store.dir / f"{order.id}.json"
    forged = json.loads(stored.read_text())
    forged["body"] = "An invented different body."
    stored.write_text(json.dumps(forged))
    before = stored.read_bytes()
    with pytest.raises(ShipError, match="differs|conflicts"):
        ring.runner.advance(ring.path)
    assert cascade.run(ring.service) == [(ring.question.id, "cascade evidence is inconsistent")]
    assert stored.read_bytes() == before and cascade.load(ring.path)["status"] == "running"
    hold_lines_are_fixed(ring, "An invented different body.")


def test_a_final_notice_cut_between_intent_and_post_publishes_the_same_notice_once(tmp_path, monkeypatch):
    ring = fake_ring(tmp_path)
    ring.runner.health = lambda name, order: "owned pane is dead"
    write = ring.store.write

    def cut(message):
        raise OSError("synthetic cut before the post is written")

    monkeypatch.setattr(ring.store, "write", cut)
    with pytest.raises(OSError, match="synthetic cut"):
        ring.runner.advance(ring.path)
    intent = cascade.load(ring.path)["intents"]["final"]
    assert ship_posts(ring.store) == []
    monkeypatch.setattr(ring.store, "write", write)
    for _ in range(3):
        cascade.Runner(ring.ship, post.Store(ring.ship.runtime), ring.runner.health,
                       unchanged_effort).advance(ring.path)
    published = ship_posts(ring.store)
    assert [m.id for m in published] == [intent["id"]] and published[0].to == "conductor"


def test_a_stray_link_inside_the_ring_cannot_redirect_a_run(tmp_path):
    ring = fake_ring(tmp_path)
    ring.runner.health = lambda name, order: "owned pane is dead"
    ring.runner.advance(ring.path)  # the real run has ended; the pass only republishes nothing
    stray = ring.ship.runtime / "ring" / "aaa-stray"
    stray.symlink_to(ring.outside, target_is_directory=True)
    (ring.outside / "state.json").write_text(json.dumps({"format": 1, "question": stray.name}))
    before = snapshot(ring.outside)
    posts = ring.store.all()
    changed = cascade.run(ring.service)
    assert changed == [(cascade_runtime.key(stray.name), "cascade evidence is inconsistent")]
    assert snapshot(ring.outside) == before and ring.store.all() == posts
    assert hold_lines_are_fixed(ring, "aaa-stray", str(ring.outside)) == [
        "run cascade held: cascade evidence is inconsistent"]


# -- the final notice across a fresh orchestrator -------------------------------------------


def finished(tmp_path):
    ring = fake_ring(tmp_path)
    for _ in range(9):
        ring.runner.advance(ring.path)
        reply(ring.store, step_order(ring.store, ring.path))
        ring.runner.advance(ring.path)
    [final] = finals(ring.store, "conductor")
    return ring, final


def test_an_undelivered_final_notice_is_carried_once_by_a_fresh_orchestrator(tmp_path):  # law: context-is-cache
    ring, final = finished(tmp_path)
    seat = Seat(ring.ship, ring.ship.seat("conductor"), None)
    first, incoming = seat.first_message(resume_id=None)
    assert [m.id for m in incoming] == [ring.question.id, final.id]
    assert first.count(final.body) == 1 and first.index(ring.question.body) < first.index(final.body)
    assert ring.store.is_open(ring.question), "carriage answers nothing"


def test_a_refused_final_notice_is_named_by_stub_and_its_body_never_carried(tmp_path):  # law: context-is-cache
    ring, final = finished(tmp_path)
    refusal.save(ring.ship.runtime, "conductor", [{
        "launch": {"pane": "%synthetic", "pid": 4321, "at": "synthetic-launch"}, "record": "synthetic",
        "turn": None, "code": None, "words": "provider refusal", "stage": "launched", "reason": None,
        "successor": "synthetic-later", "notices": [],
        "quarantined": [{"message": final.id, "sender": post.SHIP, "kind": "report"}]}])
    seat = Seat(ring.ship, ring.ship.seat("conductor"), None)
    first, incoming = seat.first_message(resume_id=None)
    assert [m.id for m in incoming] == [ring.question.id]
    assert final.body not in first and "Last full answer" not in first
    assert refusal.stub(final) in first and ring.question.body in first
    assert ring.store.is_open(ring.question)


def test_the_ring_never_writes_outside_its_own_question_folder(tmp_path):
    ring = fake_ring(tmp_path)
    before = {p for p in ring.ship.runtime.rglob("*") if "ring" not in p.relative_to(ring.ship.runtime).parts}
    for _ in range(9):
        ring.runner.advance(ring.path)
        reply(ring.store, step_order(ring.store, ring.path))
        ring.runner.advance(ring.path)
    after = {p for p in ring.ship.runtime.rglob("*") if "ring" not in p.relative_to(ring.ship.runtime).parts}
    assert {p.relative_to(ring.ship.runtime).parts[0] for p in after - before} <= {"post"}, "posts only"
    assert not (ring.ship.runtime / "post" / "cascade-holds").exists(), "no hold on a healthy run"
    assert sorted(p.name for p in (ring.ship.runtime / "ring").iterdir()) == [ring.question.id]
    assert {p.name for p in ring.path.iterdir() if not p.name.startswith(".")} == {"state.json", "responses.md"}
    assert snapshot(ring.outside) == {}
    assert oct((ring.path / "responses.md").stat().st_mode & 0o777) == oct(0o600)
