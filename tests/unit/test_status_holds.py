"""The status line uses recorded routing evidence and names seats held at their limit."""

from dataclasses import replace
from datetime import timedelta
import sqlite3

import pytest

from quarterdeck import harness, output, post, watch
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ship import Pool
from .test_cross_program_failover import running, usage  # noqa: F401


@pytest.fixture
def view(running, monkeypatch):
    monkeypatch.setattr(watch, "service_state", lambda _: "ok")
    monkeypatch.setattr(watch, "disks", lambda _: [])
    monkeypatch.setattr(watch.credential, "login_needed", lambda _: [])
    return running


def read(r):
    return watch.facts(r.seat.ship, r.seat.tmux)


@pytest.mark.parametrize("destination", ["room", "spent", "unknown", "stale"])
def test_held_seat_uses_the_same_recorded_destination_evidence_as_routing(view, destination):  # law: nothing-waits-silently
    r = view
    usage(r.svc, "third", 1.0)
    if destination == "spent":
        usage(r.svc, "second", 1.0)
    elif destination == "unknown":
        r.svc.ledger.record_usage("second", Usage((), post.now(), "synthetic unread"))
    elif destination == "stale":
        old = post.now() - timedelta(hours=2)
        # Age the valid fixture reading without introducing a read failure.
        r.svc.ledger.db.execute("UPDATE pools SET read_at=? WHERE pool='second'", (old.isoformat(),))
        r.svc.ledger.db.commit()
    before = r.svc.ledger.path.read_bytes()
    facts = read(r)
    assert facts.held == ([] if destination == "room" else ["worker"])
    assert r.svc.ledger.path.read_bytes() == before
    if destination in ("room", "spent"):
        assert r.svc.known_open(r.seat, "second") == (destination == "room")
    text = facts.line()
    assert ("worker held at limit" in text) == (destination != "room")
    assert "wall:" not in text and "reset" not in text and "usage" not in text
    assert facts.walls and facts.usage


def test_an_eligible_move_is_not_held_even_while_its_turn_defers_the_move(view):
    r = view
    r.source.begin("synthetic active work", "synthetic-source", "high")
    r.svc.failover()
    assert r.seat.pool.name == "first"
    assert read(r).held == [] and "held at limit" not in read(r).line()


def test_earlier_choice_with_room_clears_the_hold_on_the_last_choice(view):
    r = view
    r.svc.failover()
    usage(r.svc, "second", 1.0)
    r.svc.failover()
    usage(r.svc, "third", 1.0)
    assert read(r).held == ["worker"]
    usage(r.svc, "first", .2)
    assert read(r).held == []
    r.svc.failover()
    assert r.seat.pool.name == "first" and read(r).held == []


def test_the_foreground_service_read_can_establish_room_after_the_read_only_hold(view, monkeypatch):
    r = view
    usage(r.svc, "third", 1.0)
    old = post.now() - timedelta(minutes=10)
    r.svc.ledger.record_usage("second", Usage((), post.now(), "synthetic unread"), attempted_at=old)
    r.svc.ledger.db.commit()
    calls = []
    reader = harness.get("claude")

    def fetch(pool):
        calls.append(pool.name)
        at = post.now()
        return Usage((Window("5h", .2, at + timedelta(hours=5)),), at)

    monkeypatch.setattr(type(reader), "usage", lambda self, pool: fetch(pool))
    assert r.svc.ledger.quota.due("second", post.now(), reader.usage_interval)
    assert read(r).held == ["worker"] and not calls
    r.svc.failover()
    assert calls == ["second"] and r.seat.pool.name == "second"
    assert read(r).held == []


def test_unused_spent_pools_do_not_change_text_but_remain_in_json(view):
    r = view
    usage(r.svc, "first", .2)
    before = read(r).line()
    r.seat.ship.pools["unused"] = Pool("unused", "claude")
    usage(r.svc, "unused", 1.0)
    facts = read(r)
    assert facts.line() == before
    assert any(wall.startswith("unused ") for wall in output.value(facts)["walls"])
    assert output.value(facts)["usage"]


def test_a_down_seat_does_not_contribute_a_limit_hold(view, monkeypatch):
    r = view
    usage(r.svc, "second", 1.0)
    usage(r.svc, "third", 1.0)
    monkeypatch.setattr(r.seat.tmux, "pane", lambda _: None)
    facts = read(r)
    assert facts.held == [] and facts.walls


@pytest.mark.parametrize("pool", [
    Pool("second", "fake"),
    Pool("second", "local", endpoint="http://127.0.0.1:9/v1"),
])  # a key pool is no destination at all: test_key_pool_refused holds the seat at its limit
def test_a_destination_without_subscription_usage_uses_its_walls(view, pool):
    r = view
    r.seat.ship.pools["second"] = pool
    r.seat.ship.seats["worker"] = replace(r.seat.ship.seat("worker"), pools=("first", "second"), pool_choices={})
    r.svc.ledger.record_usage("second", Usage((), post.now(), "synthetic unknown"))
    assert read(r).held == []


def test_unreadable_held_evidence_is_reported_as_unknown(view, monkeypatch):
    def unreadable(path):
        raise sqlite3.OperationalError("synthetic unavailable ledger")

    monkeypatch.setattr(watch.ledger.Ledger, "reading", unreadable)
    names, warnings = watch.limit_holds(view.seat.ship, [view.seat])
    assert names == [] and warnings == ["held seats unknown; ledger unreadable"]


def test_alert_names_use_the_room_released_by_allowance_text():
    facts = watch.Facts("ok", 0, 0, 0, needs_human=["synthetic-helper"], unarmed=["synthetic-watch"],
                        held=["synthetic-worker"], walls=["provider 7d reset tomorrow"],
                        usage=[dict(pool="provider", window="7d", reading=None, reason="unknown")])
    text = facts.line()
    print(len(text), text)
    assert "synthetic-helper needs a human" in text and "synthetic-watch: no watch armed" in text
    assert "synthetic-worker held at limit" in text and "details:" not in text
    assert "provider" not in text and "usage" not in text and "reset" not in text


def test_crowded_status_counts_immediate_alerts_and_groups_the_rest():  # law: nothing-waits-silently
    facts = watch.Facts("down", 1, 2, 1, needs_human=["long" * 40], unarmed=["watch" * 40],
                        held=["a", "b"], walls=["ignored reset"], disks=[("disk" * 40, 95)],
                        login=[("grant" * 40, "soon")], queued=1, pending=["pending" * 40],
                        routes=["route" * 40], unknown=["unknown" * 40], queue_unknown=["queue" * 40],
                        compactions=["compaction" * 40], usage=[dict(pool="ignored", reading=None, reason="unknown")])
    text = facts.line()
    print(len(text), text)
    from quarterdeck.headlines import columns
    for category in ("SERVICE DOWN", "1 waiting", "1 queued", "1 blind", "1 need human", "1 unarmed",
                     "held at limit", "1 pending", "1 disk alerts", "1 login needed", "more"):
        assert category in text
    assert columns(text) <= 160 and text.endswith(" · qd status")
    assert "walls" not in text and "usage" not in text and "reset" not in text


def test_grant_expiry_remains_visible_without_subscription_reset_text():
    facts = watch.Facts("ok", 0, 0, 0, login=[("synthetic-grant", "2030-01-02")],
                        walls=["provider reset tomorrow"], usage=[dict(pool="provider", reading="90%")])
    text = facts.line()
    assert "synthetic-grant" in text and "2030-01-02" in text
    assert "provider" not in text and "reset" not in text and "usage" not in text
