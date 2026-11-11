"""Allowance facts stay complete while operational alerts use the line budget."""

from datetime import datetime, timedelta, timezone

import pytest

from quarterdeck import headlines, output, watch
from quarterdeck.ledger_reading import window_reading
from quarterdeck.usage_cache import Reading

AT = datetime(2032, 1, 1, tzinfo=timezone.utc)
WALL = "synthetic individual quota wall with reset 2032-01-01 05:00 UTC"


def item(pool, state):
    if state == "never":
        return {"pool": pool, "window": "5h", "reading": None, "reason": "usage unknown"}
    row = Reading(pool, "5h", None if state == "unknown" else 1,
                  (AT + timedelta(hours=5)).isoformat(), AT.isoformat(),
                  True, None, None, None, AT.isoformat(),
                  "synthetic read failed" if state in ("failed", "unknown") else None)
    return window_reading(row, AT + timedelta(hours=2) if state == "stale" else AT)


@pytest.mark.parametrize("state", ["current", "stale", "failed", "unknown", "never"])
def test_allowance_facts_do_not_enter_the_text_line_or_change_their_order(state):
    rows = [item("first", state), item("second", state)]
    facts = watch.Facts("ok", 0, 0, 0, walls=[WALL], usage=rows)
    before = output.value(facts)
    assert facts.line() == "service ok · 0 waiting"
    assert output.value(facts) == before
    assert [row["pool"] for row in before["usage"]] == ["first", "second"]


@pytest.mark.parametrize("extra_room", [-1, 0, 1])
def test_alert_only_overflow_uses_the_whole_presentation_budget(monkeypatch, extra_room):
    name = "synthetic-worker"
    expected = f"service ok · 0 waiting · {name} needs a human"
    facts = watch.Facts("ok", 0, 0, 0, needs_human=[name], usage=[item("unused", "current")])
    monkeypatch.setattr(headlines, "WIDTH", len(expected) + extra_room)
    line = facts.line()
    if extra_room >= 0:
        assert line == expected
    else:
        assert "1 need human" in line and "details: qd status" in line
    assert "unused" not in line and "usage" not in line


def test_crowded_cli_json_retains_all_wall_and_usage_facts(tmp_path, monkeypatch, capsys):
    import json
    from quarterdeck import cli

    (tmp_path / "ship.toml").write_text('[ship]\nname="synthetic"\n')
    monkeypatch.delenv("QD_SHIP", raising=False)
    monkeypatch.delenv("QD_SEAT", raising=False)
    facts = watch.Facts("ok", 0, 0, 0, held=["worker"], walls=[WALL],
                        usage=[item("a-unknown", "never"), item("z-known", "current")])
    monkeypatch.setattr(watch, "facts", lambda *args: facts)
    assert cli.main(["--ship", str(tmp_path), "status", "--line"]) == 0
    text = capsys.readouterr().out
    assert text.rstrip() == "service ok · 0 waiting · worker held at limit"
    assert cli.main(["--ship", str(tmp_path), "--json", "status", "--line"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == output.value(facts)
    assert data["walls"] == [WALL] and data["held"] == ["worker"]
    assert [row["pool"] for row in data["usage"]] == ["a-unknown", "z-known"]
    assert data["usage"][0]["reading"] is None
    assert data["usage"][1]["current"] is True
