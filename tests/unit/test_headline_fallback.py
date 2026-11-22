"""The Claude headline falls back to 7d when a good read carries no five-hour figure.

A good read without a 5h window means the provider reports none for the account, which
is not 'unknown'; a 5h figure in a later good read is the headline again; a failed read
still reads as unknown; and a spent window's wall is unchanged. Synthetic ledgers only.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import ledger, ledger_reading, post, quota, watch
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ledger import Ledger
from quarterdeck.ship import load
from quarterdeck.usage_cache import MISSING

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


SKIPPED_REASONS = [
    "access expired, waiting for renewal",
    "access expired, login needed",
    "no access token in the credential file",
]


def skip_read(ship, reason):
    at = AT + timedelta(minutes=1)
    with Ledger(ship.runtime / "ledger.sqlite") as book:
        book.record_usage("claude-primary", Usage((), at, reason, called=False), attempted_at=at)


def pool_status(ship):
    data = watch.status_reading(ship, None, collected=SimpleNamespace())
    return [line for line in watch.render_table(data) if line.startswith("claude-primary ")]


@pytest.mark.parametrize("reason", SKIPPED_REASONS, ids=["renewal", "login", "tokenless"])
def test_skipped_read_shows_retained_week_and_group_until_reset(ship, reason):
    weekly = week()
    group = Window("Sonnet / 7d", .63, weekly.resets_at, group="Sonnet", warning=True)
    read(ship, "claude-primary", weekly, group)
    skip_read(ship, reason)
    expected_reason = f"usage read skipped: {reason}"
    path = ship.runtime / "ledger.sqlite"
    before = path.read_bytes()

    with Ledger.reading(path) as book:
        pool = ("claude-primary",)
        stored_row_error = f"window missing from latest usage read: {reason}"
        assert book.db.execute(
            "SELECT window,error FROM pools WHERE pool=? ORDER BY window", pool
        ).fetchall() == [("7d", stored_row_error), ("Sonnet / 7d", stored_row_error)]
        assert book.db.execute(
            "SELECT error FROM usage_reads WHERE pool=?", pool
        ).fetchall() == [(f"{reason}; 7d: {stored_row_error}; Sonnet / 7d: {stored_row_error}",)]
        ledger_lines = ledger_reading.render_pools(ledger_reading.collect(book, ship))
        assert f"claude-primary latest usage attempt · {expected_reason}" in ledger_lines

    for elapsed in (timedelta(seconds=-1), timedelta(0), timedelta(seconds=1)):
        CLOCK["now"] = weekly.resets_at + elapsed
        status = pool_status(ship)
        assert len(status) == 2
        for status_line, label, percentage in zip(status, ("7d", "Sonnet / 7d"), ("45%", "63%")):
            assert status_line.startswith(f"claude-primary {label} ")
            assert expected_reason in status_line and MISSING not in status_line and NO_5H not in status_line
            assert AT.isoformat() in status_line and quota.reset_text(weekly.resets_at.isoformat()) in status_line
            if elapsed < timedelta(0):
                assert f"{percentage} used" in status_line and "LAST KNOWN" in status_line
                assert "reset has passed" not in status_line
            else:
                assert "usage unknown" in status_line and "reset has passed" in status_line
                assert percentage not in status_line and "provider warns" not in status_line
        assert not headline(ship)["current"]
        assert line(ship) == "service ok · 0 waiting · qd status"
    assert path.read_bytes() == before


def test_skipped_read_shows_own_five_hour_reset_after_the_weekly_headline(ship):
    weekly, short = week(), five()
    read(ship, "claude-primary", weekly, short)
    skip_read(ship, SKIPPED_REASONS[0])
    status = pool_status(ship)
    assert status[0].startswith("claude-primary 7d 45% used")
    assert status[1].startswith("claude-primary 5h 25% used")
    assert quota.reset_text(weekly.resets_at.isoformat()) in status[0]
    assert quota.reset_text(short.resets_at.isoformat()) in status[1]
    CLOCK["now"] = short.resets_at
    status = pool_status(ship)
    assert status[0].startswith("claude-primary 7d 45% used") and "LAST KNOWN" in status[0]
    assert status[1].startswith("claude-primary 5h usage unknown") and "reset has passed" in status[1]
    assert "25%" not in status[1]


@pytest.mark.parametrize("reason", SKIPPED_REASONS, ids=["renewal", "login", "tokenless"])
def test_skipped_first_read_does_not_name_a_window(ship, reason):
    read(ship, "claude-secondary", five(), week())
    skip_read(ship, reason)
    assert pool_status(ship) == [f"claude-primary usage unknown · usage read skipped: {reason}"]
    row = headline(ship)
    assert row["window"] is None and row["reading"] is None
    assert row["attempted_at"] == (AT + timedelta(minutes=1)).isoformat()


def test_http_429_after_a_skipped_read_uses_the_existing_failure_wording(ship):
    read(ship, "claude-primary", week())
    skip_read(ship, SKIPPED_REASONS[0])
    error = "HTTP 429, retry-after 120"
    read(ship, "claude-primary", error=error, at=AT + timedelta(minutes=2))
    assert pool_status(ship) == [
        f"claude-primary 5h usage unknown: {error}; 7d: {MISSING}: {error}"
    ]


def test_skipped_reads_preserve_spent_walls_and_do_not_prove_room(ship, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return CLOCK["now"].astimezone(tz)

    monkeypatch.setattr(ledger, "datetime", Clock)
    spent, available = "claude-primary", "claude-secondary"
    read(ship, spent, week(1))
    read(ship, available, week())
    skip_read(ship, SKIPPED_REASONS[1])
    with Ledger.reading(ship.runtime / "ledger.sqlite") as book:
        assert book.walls(ship) == [f"{spent} 7d · last known"]
        assert not quota.cached_open(ship, book, spent)
        assert quota.cached_open(ship, book, available)
        CLOCK["now"] = week().resets_at
        assert book.walls(ship) == []
        assert not quota.cached_open(ship, book, spent)

    with Ledger(ship.runtime / "ledger.sqlite") as book:
        fresh = CLOCK["now"] + timedelta(minutes=1)
        book.record_usage(spent, Usage((Window("7d", .1, fresh + timedelta(days=7)),), fresh))
        CLOCK["now"] = fresh
        assert quota.cached_open(ship, book, spent)
