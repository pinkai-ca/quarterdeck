"""Fresh requires the closing turn, from synthetic pi and OpenCode records."""

from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from quarterdeck import post, service
from quarterdeck.harness.base import Screen, State
from quarterdeck.seat import Launched, Seat
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from .test_opencode_record import AT, MS, database, session, user, assistant, part
from .resting_attempts import UNSETTLED, save_attempt, settle


def at(ms):
    return AT + timedelta(milliseconds=ms)


class Record:
    def __init__(self, path, program):
        self.path, self.program = path, program
        self.rows = []
        self.con = database(path) if program == "opencode" else None
        if self.con:
            session(self.con)
            self.con.commit()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        self.serial = 0

    def pi(self, ms, **message):
        self.rows.append({"type": "message", "timestamp": at(ms).isoformat() if ms is not None else None, "message": message})
        self.path.write_text("".join(json.dumps(row) + "\n" for row in self.rows))

    def begin(self, ms=10):
        self.serial += 1
        self.input = f"input-{self.serial}"
        if self.con:
            user(self.con, self.input, "root", ms, "Synthetic work")
            self.con.commit()
        else:
            self.pi(ms, role="user", content="Synthetic work")

    def tool(self, reason=None):
        if self.con:
            assistant(self.con, "tool-step", self.input, "root", 20,
                      finish="tool-calls" if reason is None else reason)
            part(self.con, "report-tool", "tool-step", "root", 20,
                 {"type": "tool", "tool": "bash", "state": {"status": "running", "time": {"start": MS + 20}}})
            self.con.commit()
        else:
            self.pi(20, role="assistant", stopReason="toolUse" if reason is None else reason,
                    content=[{"type": "toolCall", "id": "report-tool", "name": "bash",
                              "arguments": {"command": "qd post report --re ord-synthetic done"}}])

    def result(self):
        if self.con:
            self.con.execute("UPDATE part SET data=? WHERE id='report-tool'", (json.dumps(
                {"type": "tool", "tool": "bash", "state": {"status": "completed", "time": {"start": MS + 20, "end": MS + 30}}}),))
            self.con.commit()
        else:
            self.pi(30, role="toolResult", toolCallId="report-tool", toolName="bash", content=[])

    def finish(self, ms=40, reason="stop"):
        if self.con:
            name = f"final-{self.serial}-{ms}"
            data = assistant(self.con, name, self.input, "root", ms or 40, finish=reason)
            if ms is None:
                data["time"].pop("completed")
                self.con.execute("UPDATE message SET data=? WHERE id=?", (json.dumps(data), name))
            self.con.commit()
        else:
            self.pi(ms, role="assistant", stopReason=reason, content=[{"type": "text", "text": "Done"}])


@pytest.fixture(params=["pi", "opencode"])
def worker(tmp_path, monkeypatch, request):
    program = request.param
    spec = SeatSpec("worker", "crew", "worker", program, ("local",), fresh="after-order")
    pool = Pool("local", "local", endpoint="http://synthetic.invalid/v1")
    ship = Ship(tmp_path / "ship", "synthetic", "operator", tmp_path, {"local": pool},
                {"crew": Session("crew", "Synthetic")}, {"worker": spec})
    seat = Seat(ship, spec, SimpleNamespace(capture=lambda _: "idle screen"))
    record = Record(tmp_path / ("synthetic.db" if program == "opencode" else "synthetic.jsonl"), program)
    launch = [Launched("%1", 123, AT.isoformat(), "local", "root", [program], "synthetic", harness=program)]
    monkeypatch.setattr(seat, "pane", lambda: SimpleNamespace(dead=False, id="%1"))
    monkeypatch.setattr(seat, "record", lambda: record.path)
    monkeypatch.setattr(seat, "launched", lambda: launch[0])
    monkeypatch.setattr(seat.harness, "screen", lambda _: Screen("", ""))
    fresh = []

    def restart():
        fresh.append(True)
        launch[0] = Launched("%2", 124, at(100).isoformat(), "local", "new", [program], "synthetic", harness=program)
        return "synthetic fresh"

    monkeypatch.setattr(seat, "fresh", restart)
    monkeypatch.setattr(service, "seats", lambda *_: [seat])
    clock = [at(1)]
    monkeypatch.setattr(post, "now", lambda: clock[0])
    svc = service.Service(ship, seat.tmux)
    order = svc.store.new("order", "operator", "worker", "Synthetic work")
    clock[0] = at(25)
    report = svc.store.new("report", "worker", "operator", "Done", re=order.id)
    try:
        yield SimpleNamespace(svc=svc, seat=seat, record=record, fresh=fresh, clock=clock, order=order, report=report)
    finally:
        svc.ledger.close()
        if record.con:
            record.con.close()


def held(w):
    for _ in range(2):
        w.svc.fresh_after_order()
    assert w.fresh == [], "report files and idle screens cannot prove the closing turn ended"


def released_once(w):
    for _ in range(3):
        w.svc.fresh_after_order()
    assert w.fresh == [True]


def test_report_tool_and_following_model_step_hold_until_final_completion(worker):
    w = worker
    w.record.begin()
    w.record.tool()
    held(w)
    w.record.result()
    held(w)
    w.record.finish()
    released_once(w)


@pytest.mark.parametrize("reason", [None, "unfamiliar"])
def test_unknown_terminal_reason_cannot_release_fresh(worker, reason):
    w = worker
    w.record.begin()
    w.record.tool()
    w.record.result()
    w.record.finish(reason=reason)
    held(w)
    w.record.finish(50)
    released_once(w)


@pytest.mark.parametrize("missing", ["empty", "absent", "unreadable", "unknown", "untimed", "stale", "later-turn"])
def test_missing_or_unrelated_completion_never_borrows_idle_screen(worker, monkeypatch, missing):
    w = worker
    if missing == "absent":
        monkeypatch.setattr(w.seat, "record", lambda: None)
    elif missing == "unreadable":
        monkeypatch.setattr(w.seat.harness, "state", lambda _: (_ for _ in ()).throw(OSError("unreadable")))
    elif missing == "unknown":
        monkeypatch.setattr(w.seat.harness, "state", lambda _: State(turn_open=None))
    elif missing == "untimed":
        w.record.begin()
        w.record.finish(None)
    elif missing == "stale":
        w.record.begin(2)
        w.record.finish(5)
    elif missing == "later-turn":
        w.record.begin(30)
        w.record.finish(40)
    held(w)


def test_duplicate_report_after_fresh_does_not_trigger_another_fresh(worker):
    w = worker
    w.record.begin()
    w.record.finish()
    released_once(w)
    w.clock[0] = at(125)
    w.svc.store.new("report", "worker", "operator", "Duplicate", re=w.order.id)
    w.record.begin(110)
    w.record.finish(140)
    w.svc.fresh_after_order()
    assert w.fresh == [True], "only the first report closes the order"


def test_later_open_order_still_holds_fresh(worker):
    w = worker
    w.record.begin()
    w.record.finish()
    w.clock[0] = at(35)
    w.svc.store.new("order", "operator", "worker", "Next work")
    held(w)


def test_latest_closing_report_cannot_use_an_earlier_orders_completion(worker):
    w = worker
    w.record.begin()
    w.record.finish()
    w.clock[0] = at(45)
    later = w.svc.store.new("order", "operator", "worker", "Later work")
    w.clock[0] = at(55)
    w.svc.store.new("report", "worker", "operator", "Later report", re=later.id)
    held(w)
    w.record.begin(50)
    w.record.finish(60)
    released_once(w)


def test_queued_input_holds_fresh_after_current_turn_ends(worker, monkeypatch):
    w = worker
    w.record.begin()
    w.record.finish()
    completed = w.seat.harness.state(w.record.path)
    monkeypatch.setattr(w.seat.harness, "state", lambda _: replace(completed, queued=("Still queued",)))
    held(w)
    monkeypatch.setattr(w.seat.harness, "state", lambda _: completed)
    released_once(w)


def test_later_turn_must_finish_without_losing_the_closing_turn_proof(worker):
    w = worker
    w.record.begin()
    w.record.finish()
    w.record.begin(50)
    held(w)
    w.record.finish(60)
    released_once(w)


@pytest.mark.parametrize("worker", ["opencode"], indirect=True)
def test_child_completion_never_supplies_a_root_closing_turn(worker):
    w = worker
    con = w.record.con
    # The active root is empty; a completed child spans the report time.
    session(con, "child", 2, "root")
    user(con, "child-input", "child", 10, "Child work")
    assistant(con, "child-final", "child-input", "child", 40)
    con.commit()
    state, turns = w.seat.harness.read(w.record.path)
    assert not state.turn_open and len(turns) == 1, "the child still belongs in accounting"
    held(w)


@pytest.mark.parametrize("worker", ["opencode"], indirect=True)
def test_queue_transition_is_not_terminal_evidence_for_the_closing_turn(worker):
    w = worker
    w.record.begin()
    w.record.tool()
    w.record.result()
    # The tool step itself completed before the report. A later answered input
    # proves consumption, not a final completion for the earlier closing turn.
    w.record.begin(35)
    w.record.finish(40)
    state, turns = w.seat.harness.read(w.record.path)
    assert not state.turn_open and len(turns) == 2
    held(w)


def test_a_report_on_a_question_does_not_qualify_as_an_order_closure(worker):
    w = worker
    # Move the actual order's closure before this launch; only a report on a
    # question occurs in the current launch, which cannot trigger fresh.
    old_launch = w.seat.launched()
    w.seat.launched = lambda: replace(old_launch, at=at(26).isoformat())
    w.clock[0] = at(30)
    question = w.svc.store.new("question", "operator", "worker", "A question")
    w.clock[0] = at(35)
    w.svc.store.new("answer", "worker", "operator", "Answered", re=question.id)
    w.svc.store.new("report", "worker", "operator", "Extra report", re=question.id)
    w.record.begin(30)
    w.record.finish(40)
    held(w)


def asked(w, *, first_answer_by):
    """A hand whose order in hand is a question, given to this launch: the earlier order closed before it."""
    from quarterdeck import hand_door
    w.seat.spec = replace(w.seat.spec, role="hand")
    old_launch = w.seat.launched()
    launched = replace(old_launch, at=at(26).isoformat())
    current = [launched]
    w.seat.launched = lambda: current[0]
    started = w.seat.fresh

    def fresh():  # a fresh start is a new launch, as the fixture's own makes one
        proof = started()
        current[0] = replace(launched, pane="%2", at=at(100).isoformat())
        return proof
    w.seat.fresh = fresh
    w.clock[0] = at(30)
    question = w.svc.store.new("question", "operator", "worker", "A synthetic question")
    attempt = w.svc.attempts.open(question, "worker", "%1", 123, launched.at)
    w.svc.attempts.close(attempt, "delivered", "synthetic")
    w.clock[0] = at(33)
    following = w.svc.store.new("order", "operator", "worker", "The next synthetic order")
    w.clock[0] = at(35)
    if first_answer_by != "worker":
        w.svc.store.new("answer", first_answer_by, "worker", "Answered by another", re=question.id)
        w.clock[0] = at(36)
    w.svc.store.new("answer", "worker", "operator", "Answered", re=question.id)
    w.record.begin(30)
    w.record.finish(40)
    return hand_door, question, following


def test_a_hand_that_answers_its_question_starts_fresh_and_its_next_order_arrives(worker):
    w = worker
    hand_door, question, following = asked(w, first_answer_by="worker")
    assert hand_door.order_in_hand(w.svc.store, "worker").id == following.id
    assert hand_door.held(w.seat, w.svc.store, w.svc.attempts, following).startswith("waits: it comes at the fresh start")
    released_once(w)
    assert hand_door.held(w.seat, w.svc.store, w.svc.attempts, following) is None, "the fresh launch takes it"


def test_a_hand_whose_question_another_answered_first_does_not_start_fresh(worker):
    w = worker
    asked(w, first_answer_by="operator")
    held(w)


@pytest.mark.parametrize('phase', UNSETTLED)
def test_ready_after_order_waits_for_an_intentless_resting_move(worker, phase):
    w = worker
    w.record.begin()
    w.record.finish()
    assert w.svc._fresh_after_order_ready(w.seat)
    _, attempt = save_attempt(w.seat, phase)
    assert not w.svc._fresh_after_order_ready(w.seat)
    w.svc.fresh_after_order()
    assert not w.fresh
    settle(w.seat, attempt)
    released_once(w)
