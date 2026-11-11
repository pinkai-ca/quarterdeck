"""Moving a seat by a qd act: another program by one qd set edit, or back to an earlier declared choice.

Synthetic ships only. A seat declared with one program switches program and pool in one validated
edit. A seat with inline pool choices selects one of them by name through the path failover uses,
with failover's refusals and an explicit choice of destination.
"""

from datetime import timedelta

import pytest

from quarterdeck import cli, post, service as servicemod, watch
from quarterdeck.ship import load
from .test_cross_program_failover import running, usage  # noqa: F401  (the fixture and its helper)

SINGLE = '''[ship]
name="synthetic"
[pools.codex]
provider="codex"
[pools.claude-primary]
provider="claude"
[sessions.crew]
[seats.worker]
session="crew"
harness="codex"
pool="codex"
model="synthetic-codex-model"
'''


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def single(tmp_path, monkeypatch):
    monkeypatch.setenv("QD_TMUX_SOCKET", "qd-test-nobody-here")
    (tmp_path / "ship.toml").write_text(SINGLE)
    return tmp_path


@pytest.mark.parametrize("order", [("harness=claude", "pool=claude-primary"), ("pool=claude-primary", "harness=claude")],
                         ids=["harness-first", "pool-first"])
def test_qd_set_switches_a_seats_program_as_one_edit(single, capsys, order):
    code, out, err = run(capsys, "--ship", str(single), "set", "worker", *order, "model=synthetic-claude-model")
    assert code == 0 and err == "", err
    seat = load(single).seat("worker")
    assert (seat.harness, seat.pools, seat.model) == ("claude", ("claude-primary",), "synthetic-claude-model")
    assert "  harness written; qd fresh worker applies it" in out.splitlines()
    assert "  pool written; qd fresh worker applies it with the harness" in out.splitlines()


@pytest.mark.parametrize("changes", [("harness=claude",), ("pool=claude-primary",), ("harness=claude", "pool=codex")],
                         ids=["harness-alone", "pool-alone", "mismatched-pair"])
def test_a_program_and_pool_that_do_not_fit_are_refused_together_and_nothing_is_written(single, capsys, changes):
    before = (single / "ship.toml").read_text()
    code, out, err = run(capsys, "--ship", str(single), "set", "worker", *changes)
    assert code != 0 and "worker" in err and (single / "ship.toml").read_text() == before


def test_qd_set_selects_a_declared_choice_through_the_service_without_writing_the_ship_file(running, capsys, monkeypatch):
    ship = running.seat.ship
    before = ship.file.read_text()
    asked = []
    monkeypatch.setattr(servicemod, "request", lambda runtime, kind, **data: asked.append((kind, data)) or "synthetic answer")
    code, out, err = run(capsys, "--ship", str(ship.folder), "set", "worker", "pool=first")
    assert code == 0 and asked == [("select", {"seat": "worker", "pool": "first"})] and "synthetic answer" in out
    assert ship.file.read_text() == before, "a selection is runtime state; the declaration is unchanged"
    for refused in (["pool=undeclared"], ["pool=first", "persona=Other"]):
        code, out, err = run(capsys, "--ship", str(ship.folder), "set", "worker", *refused)
        assert code != 0 and len(asked) == 1 and ship.file.read_text() == before


def select(r, pool):
    return r.svc.answer({"kind": "select", "seat": "worker", "pool": pool})


def test_a_seat_moves_back_to_its_first_choice_by_failovers_path(running):
    r = running
    r.svc.account()
    r.svc.failover()
    assert r.seat.pool.name == "second" and r.seat.harness.name == "claude"
    message = r.svc.store.new("order", "issuer", "worker", "synthetic open order")
    usage(r.svc, "first", 0.2)
    r.svc.account()
    answer = select(r, "first")
    assert "moved to first from second" in answer, answer
    assert r.seat.pool.name == "first" and r.seat.harness.name == "codex"
    assert r.seat.launched().pool == "first" and r.seat.launched().resume is None, "another program starts fresh"
    assert post.typed([message]) in r.launches[-1][-1], "with its brief and open messages"
    assert r.reseeds[-1][0] == "second", "the source credential is reseeded first"
    assert any(m.body == "moved to pool first from second by qd set" for m in r.svc.store.for_seat("worker"))
    assert (r.seat.folder / "pool").read_text().strip() == "first"


def test_the_same_program_moves_by_restart_with_resume(running):
    r = running
    r.svc.account()
    r.svc.failover()
    usage(r.svc, "third", 0.1)
    r.svc.account()
    assert "moved to third from second" in select(r, "third")
    assert r.seat.pool.name == "third" and r.seat.launched().resume is not None and "--resume" in r.launches[-1]


def test_a_selection_is_refused_where_failover_would_not_move(running):
    r = running
    r.svc.account()
    launched = r.seat.launched()
    assert "not one of its declared choices" in select(r, "undeclared")
    assert "already on first" in select(r, "first")
    usage(r.svc, "second", 1.0)
    assert "not moved: second is walled" in select(r, "second")
    usage(r.svc, "second", 0.0)
    r.source.begin("synthetic open turn", "synthetic-source", "high")  # a turn the record has not closed
    assert "not moved: a turn is open" in select(r, "second")
    assert r.seat.launched() == launched and not r.launches and r.seat.pool.name == "first"


def test_failover_stays_on_the_current_pool_while_it_has_room(running):
    r = running
    r.svc.account()
    r.svc.failover()
    usage(r.svc, "first", 0.0)
    for _ in range(3):
        r.svc.failover()
    assert r.seat.pool.name == "second" and len(r.launches) == 1


def retain_spent_reading(r, reset):
    """Advance the age of the spent first pool's cached reading without a provider call."""
    read = post.now() - timedelta(hours=2)
    r.svc.ledger.db.execute("UPDATE pools SET read_at=?, attempted_at=?, resets_at=? WHERE pool='first'",
                            (read.isoformat(), read.isoformat(), reset.isoformat()))
    r.svc.ledger.db.commit()


@pytest.mark.parametrize("running", [False, True], ids=["managed", "human"], indirect=True)
@pytest.mark.parametrize("earlier", ["spent", "unknown", "ready"])
def test_failover_from_the_last_choice_uses_earlier_pool_evidence(running, earlier):
    r = running
    r.svc.failover()
    usage(r.svc, "second", 1.0)
    r.svc.failover()
    assert r.seat.pool.name == "third" and len(r.launches) == 2
    usage(r.svc, "third", 1.0)
    reset = post.now() + timedelta(minutes=1 if earlier == "spent" else -1)
    retain_spent_reading(r, reset)
    assert not r.svc.ledger.usage_known("first")
    assert r.svc.ledger.walled("first", r.seat.ship) == (earlier == "spent")

    for _ in range(3):
        r.svc.failover()
    assert r.seat.pool.name == "third" and len(r.launches) == 2
    facts = watch.facts(r.seat.ship, r.seat.tmux, login=[])
    assert "third 5h" in facts.walls and "worker held at limit" in facts.line()
    if earlier != "ready":
        return

    open_order = r.svc.store.new("order", "issuer", "worker", "synthetic open order")
    unread = r.svc.store.new("report", "issuer", "worker", "synthetic unread report")
    usage(r.svc, "first", 0.2)
    usage(r.svc, "second", 0.2)
    r.svc.failover()
    assert r.seat.pool.name == "first" and r.seat.harness.name == "codex"
    assert len(r.launches) == 3 and r.seat.launched().resume is None
    assert r.seat.record() == r.created[-1].path
    assert post.typed([open_order]) in r.launches[-1][-1]
    assert post.typed([unread]) in r.launches[-1][-1]


def back_to_codex_after_a_wall(r, reset):
    r.svc.account()
    r.svc.failover()
    assert r.seat.pool.name == "second"
    retain_spent_reading(r, reset)
    return select(r, "first")


def test_an_unknown_destination_moves_on_the_explicit_act_and_says_so(running):
    r = running
    answer = back_to_codex_after_a_wall(r, post.now() - timedelta(minutes=1))
    assert not r.svc.ledger.usage_known("first") and not r.svc.ledger.walled("first", r.seat.ship)
    assert "moved to first from second; first usage unknown; moved on the explicit act" in answer, answer
    assert r.seat.pool.name == "first" and r.seat.harness.name == "codex"


def test_a_last_known_spent_destination_before_its_reset_is_refused_as_walled(running):
    r = running
    answer = back_to_codex_after_a_wall(r, post.now() + timedelta(hours=1))
    assert answer == "worker not moved: first is walled" and r.seat.pool.name == "second" and len(r.launches) == 1


def test_failover_still_skips_an_unknown_destination(running):
    from quarterdeck.harness.limits import Usage
    r = running
    r.svc.ledger.record_usage("second", Usage((), post.now(), "synthetic unavailable"))
    usage(r.svc, "third", 1.0)
    r.svc.account()
    r.svc.failover()
    assert r.seat.pool.name == "first" and not r.launches
