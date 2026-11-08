"""Crowded lines prefer useful cached readings without reordering complete data."""

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


@pytest.mark.parametrize("unknown", ["never", "unknown"])
def test_wall_leaves_one_slot_for_measured_exhausted_value(unknown):
    rows = [item("a-unknown", unknown), item("z-known", "current")]
    facts = watch.Facts("ok", 0, 0, 0, walls=[WALL], usage=rows)
    before = output.value(facts)
    line = facts.line()
    assert "wall: " + WALL in line
    assert "z-known 5h 100% reset 01-01T05:00Z" in line
    assert "a-unknown" not in line and "+1 usage: qd status" in line
    assert len(line) <= 160
    assert output.value(facts) == before
    assert [row["pool"] for row in before["usage"]] == ["a-unknown", "z-known"]
    assert before["usage"][1]["reading"]["used"] == 1


@pytest.mark.parametrize("retained", ["stale", "failed"])
def test_overflow_prefers_current_then_last_known_then_unknown_with_stable_ties(monkeypatch, retained):
    monkeypatch.setattr(headlines, "WIDTH", 215)
    rows = [item("u", "never"), item("s2", retained), item("c2", "current"),
            item("s1", retained), item("c1", "current"), item("v", "unknown"), item("w", "never")]
    facts = watch.Facts("ok", 0, 0, 0, usage=rows)
    line = facts.line()
    assert line.index("c2 5h") < line.index("c1 5h") < line.index("s2 5h") < line.index("s1 5h")
    assert "LAST KNOWN" in line and "+3 usage" in line
    assert "u 5h" not in line and "v 5h" not in line
    assert len(line) <= 215
    assert facts.usage == rows


def test_no_overflow_preserves_order_and_exact_output():
    facts = watch.Facts("ok", 0, 0, 0, usage=[item("a", "never"), item("z", "current")])
    assert facts.line() == (
        "service ok · 0 waiting · a 5h unknown reset ? · z 5h 100% reset 01-01T05:00Z")


def test_oversized_headline_does_not_hide_a_fitting_measured_sibling():
    facts = watch.Facts("ok", 0, 0, 0, walls=[WALL],
                        usage=[item("long" * 50, "current"), item("z", "current")])
    line = facts.line()
    assert "z 5h 100%" in line and "+1 usage" in line
    assert "wall: " + WALL in line and len(line) <= 160


@pytest.mark.parametrize("wall_width", [107, 108, 109])
def test_overflow_reserves_the_plus_sign_at_the_160_column_boundary(wall_width):
    wall = "x" * wall_width
    facts = watch.Facts("ok", 0, 0, 0, walls=[wall], usage=[item("a", "never"), item("b", "never")])
    before = output.value(facts)
    line = facts.line()
    assert len(line) <= 160, line
    if wall_width == 107:
        assert len(line) == 160 and "wall: " + wall in line and "+2 usage: qd status" in line
    else:
        assert "1 walls" in line and "details: qd status" in line
    assert output.value(facts) == before, "only presentation is compacted; alerts and usage keep their source order"


@pytest.mark.parametrize("state", ["current", "stale", "failed", "unknown", "never"])
def test_overflow_keeps_ties_in_source_order(state):
    rows = [item(name, state) for name in ("first", "second", "third", "fourth", "fifth", "sixth")]
    line = watch.Facts("ok", 0, 0, 0, usage=rows).line()
    assert line.index("first 5h") < line.index("second 5h")
    assert "usage: qd status" in line and len(line) <= 160


def test_crowded_cli_json_retains_omitted_headlines_and_original_order(tmp_path, monkeypatch, capsys):
    import json
    from quarterdeck import cli

    (tmp_path / "ship.toml").write_text('[ship]\nname="synthetic"\n')
    monkeypatch.delenv("QD_SHIP", raising=False)
    monkeypatch.delenv("QD_SEAT", raising=False)
    facts = watch.Facts("ok", 0, 0, 0, walls=[WALL],
                        usage=[item("a-unknown", "never"), item("z-known", "current")])
    monkeypatch.setattr(watch, "facts", lambda *args: facts)
    assert cli.main(["--ship", str(tmp_path), "status", "--line"]) == 0
    text = capsys.readouterr().out
    assert "z-known 5h 100%" in text and "a-unknown" not in text and "+1 usage" in text
    assert cli.main(["--ship", str(tmp_path), "--json", "status", "--line"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == output.value(facts)
    assert [row["pool"] for row in data["usage"]] == ["a-unknown", "z-known"]
    assert data["usage"][0]["reading"] is None
    assert data["usage"][1]["current"] is True
