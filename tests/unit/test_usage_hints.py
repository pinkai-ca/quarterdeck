"""Summary hints lead to detail; detail preserves evidence without a circular hint."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from quarterdeck import cli, harness, post
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ledger import Ledger


@pytest.mark.parametrize("state", ["current", "stale", "failed", "unknown", "never"])
def test_cli_hints_are_contextual_and_readings_stay_read_only(tmp_path, monkeypatch, capsys, state):
    (tmp_path / "ship.toml").write_text('[ship]\ncode="checkout"\nname="synthetic"\n[pools.pool]\nprovider="claude"\n')
    monkeypatch.delenv("QD_SHIP", raising=False)
    monkeypatch.delenv("QD_SEAT", raising=False)
    monkeypatch.setenv("QD_TMUX_SOCKET", "qd-usage-hint-no-server")
    def forbidden(*args):
        pytest.fail("reading called a provider")
    monkeypatch.setattr(harness.get("claude"), "usage", forbidden)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(post, "now", lambda: now)
    at = now - timedelta(minutes=120 if state == "stale" else 10)
    reset = now + timedelta(hours=5)
    path = tmp_path / "runtime" / "ledger.sqlite"
    with Ledger(path) as book:
        if state not in ("never", "unknown"):
            book.record_usage("pool", Usage((Window("5h", .25, reset),), at))
        if state in ("failed", "unknown"):
            book.record_usage("pool", Usage((Window("5h", None, None),), now, "synthetic failure"))
    before = path.read_bytes(), path.stat().st_mtime_ns
    def run(*args, json_output=False):
        assert cli.main(["--ship", str(tmp_path), *(["--json"] if json_output else []), *args]) == 0
        captured = capsys.readouterr()
        assert not captured.err
        return json.loads(captured.out) if json_output else captured.out
    detail = run("ledger")
    assert "inspect with qd ledger" not in detail
    for verb in ("status", "watch"):
        summary = run(verb)
        if state in ("stale", "failed", "unknown"):
            assert "inspect with qd ledger" in summary
        if state in ("stale", "failed"):
            for text in (summary, detail):
                assert "LAST KNOWN" in text and at.isoformat() in text
                assert "reset " in text and "m ago)" in text
        if state == "unknown":
            assert "usage unknown" in summary and "usage unknown" in detail
        if state in ("failed", "unknown"):
            assert "synthetic failure" in summary and "synthetic failure" in detail
        data = run(verb, json_output=True)
        assert "inspect with" not in json.dumps(data)
        reading = data["usage"][0]["reading"]
        assert (reading is None) == (state == "never")
        if reading is not None:
            assert reading["used"] == (None if state == "unknown" else .25)
            assert data["usage"][0]["current"] == (state == "current")
    data = run("ledger", json_output=True)
    assert "inspect with" not in json.dumps(data)
    assert len(data["windows"]) == (0 if state == "never" else 1)
    if state in ("stale", "failed"):
        window = data["windows"][0]
        assert window["reading"]["read_at"] == at.isoformat()
        assert window["reading"]["resets_at"] == reset.isoformat()
        assert window["complete"] and not window["current"] and window["reason"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
