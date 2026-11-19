"""The Claude headline falls back to 7d when a good read carries no five-hour figure.

A good read without a 5h window means the provider reports none for the account, which
is not 'unknown'; a 5h figure in a later good read is the headline again; a failed read
still reads as unknown; and a spent window's wall is unchanged. Synthetic ledgers only.
"""

from datetime import datetime, timedelta, timezone

import pytest

from quarterdeck import post, watch
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ledger import Ledger
from quarterdeck.ship import load

AT = datetime(2032, 1, 1, tzinfo=timezone.utc)
NO_5H = "provider reports no 5h window"
CLOCK: dict = {}


@pytest.fixture
def ship(tmp_path, monkeypatch):
    (tmp_path / "ship.toml").write_text('[ship]\nname="synthetic"\n[pools.claude-primary]\nprovider="claude"\n'
                                       '[pools.claude-secondary]\nprovider="claude"\n')
    clock = {"now": AT + timedelta(minutes=2)}
    CLOCK.clear()
    CLOCK.update(clock)
    monkeypatch.setattr(post, "now", lambda: CLOCK["now"])
    return load(tmp_path)


def read(ship, pool, *windows, at=AT, error=None):
    with Ledger(ship.runtime / "ledger.sqlite") as book:
        book.record_usage(pool, Usage(tuple(windows), at, error), attempted_at=at)


def week(used=.45):
    return Window("7d", used, AT + timedelta(days=7))


def five(used=.25):
    return Window("5h", used, AT + timedelta(hours=5))


def headline(ship, pool="claude-primary"):
    return next(row for row in watch.headline_readings(ship) if row["pool"] == pool)


def text(ship, pool="claude-primary"):
    return next(line for line in watch.ledger_usage(ship) if line.startswith(pool + " "))


def line(ship):
    status = watch.Facts("ok", 0, 0, 0, usage=watch.headline_readings(ship)).line()
    assert len(status) <= 160, status
    return status


def test_a_good_read_without_a_five_hour_figure_heads_with_the_seven_day_window(ship):
    read(ship, "claude-primary", week())
    read(ship, "claude-secondary", week(.6))
    row = headline(ship)
    assert row["reading"].window == "7d" and row["current"] and row["note"] == NO_5H
    assert text(ship).startswith("claude-primary 7d 45% used") and NO_5H in text(ship)
    assert "unknown" not in text(ship)
    status = line(ship)
    assert status == "service ok · 0 waiting · qd status"
    assert "unknown" not in status


def test_a_five_hour_figure_that_stops_coming_falls_back_and_returns_as_the_headline(ship):
    read(ship, "claude-primary", five(), week())
    assert headline(ship)["reading"].window == "5h" and "note" not in headline(ship)
    read(ship, "claude-primary", week(.5), at=AT + timedelta(minutes=1))
    row = headline(ship)
    assert row["reading"].window == "7d" and row["note"] == NO_5H and row["reading"].used == .5
    assert line(ship) == "service ok · 0 waiting · qd status"
    read(ship, "claude-primary", five(.3), week(.5), at=AT + timedelta(minutes=2))
    row = headline(ship)
    assert row["reading"].window == "5h" and row["reading"].used == .3 and "note" not in row
    assert text(ship).startswith("claude-primary 5h 30% used") and NO_5H not in text(ship)
    assert line(ship) == "service ok · 0 waiting · qd status"


@pytest.mark.parametrize("before", [(), ("7d",), ("5h", "7d")], ids=["never-read", "7d-only", "both"])
def test_a_failed_read_still_reads_as_unknown(ship, before):
    windows = {"5h": five(), "7d": week()}
    if before:
        read(ship, "claude-primary", *(windows[name] for name in before))
    read(ship, "claude-primary", at=AT + timedelta(minutes=1), error="HTTP 429, retry-after 120")
    row = headline(ship)
    assert row.get("note") is None and (row["reading"] is None or row["reading"].window == "5h")
    assert NO_5H not in text(ship) and NO_5H not in line(ship)
    if not before or before == ("7d",):
        assert "claude-primary 5h usage unknown: " in text(ship) and "HTTP 429, retry-after 120" in text(ship)
        assert line(ship) == "service ok · 0 waiting · qd status"
    else:
        assert "LAST KNOWN" in text(ship)


def test_an_hour_old_good_read_without_five_hours_keeps_the_seven_day_headline_last_known(ship):
    read(ship, "claude-primary", week())
    CLOCK["now"] = AT + timedelta(minutes=61)
    row = headline(ship)
    assert row["reading"].window == "7d" and not row["current"] and row["note"] == NO_5H
    assert "LAST KNOWN" in text(ship) and line(ship) == "service ok · 0 waiting · qd status"


def test_the_wall_rule_is_unchanged_by_the_headline(ship):
    now = datetime.now(timezone.utc)
    with Ledger(ship.runtime / "ledger.sqlite") as book:
        book.record_usage("claude-primary", Usage((Window("7d", 1.0, now + timedelta(hours=3)),), now - timedelta(minutes=5)))
        assert book.walls(ship) == ["claude-primary 7d"]
        book.record_usage("claude-primary", Usage((), now - timedelta(minutes=1), "timeout"))
        assert book.walls(ship) == ["claude-primary 7d · last known"]
