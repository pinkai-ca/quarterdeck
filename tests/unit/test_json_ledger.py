"""All Ledger CLI forms retain numbers, uncertainty and stored window identity."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from quarterdeck import cli, post
from quarterdeck.harness.base import Tokens, ToolCall, Turn
from quarterdeck.harness.limits import Usage, Wall, Window
from quarterdeck.ledger import Ledger


@pytest.fixture
def invoke(drydock, capsys, monkeypatch):
    monkeypatch.delenv("QD_SHIP", raising=False)
    monkeypatch.delenv("QD_SEAT", raising=False)
    monkeypatch.delenv("QD_TMUX_SOCKET", raising=False)  # no server: the test ship's own, never started

    def run(*args, text=False):
        assert cli.main(["--ship", str(drydock), *([] if text else ["--json"]), *args]) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        return captured.out if text else json.loads(captured.out)

    return run


@pytest.mark.parametrize("options", [(), ("--by", "seat"), ("--by", "pool"), ("--by", "order"), ("--tools",)])
def test_missing_and_empty_ledger_are_distinct(invoke, drydock, options):
    data = invoke("ledger", *options)
    assert data["available"] is False and data["reason"].startswith("no ledger yet")
    with Ledger(drydock / "runtime" / "ledger.sqlite"):
        pass
    data = invoke("ledger", *options)
    assert data["available"] is True and data["reason"] is None
    assert data.get("rows", data.get("windows")) == []
    assert data["incomplete_records"] == [] and data["unknown_pool_turns"] == 0
    if not options:
        assert data["usage_absent_reason"].startswith("no usage read yet")


def test_rollups_and_tools_keep_partial_measurements_and_never_write(invoke, drydock):
    path = drydock / "runtime" / "ledger.sqlite"
    store = post.Store(drydock / "runtime")
    order = store.new("order", "desk", "captain", "synthetic accounting")
    with Ledger(path) as book:
        turn = Turn(0, order.at, order.at + timedelta(seconds=12), "input", "reply", "model",
                    Tokens(10, 20, 0, 3, 1), (ToolCall("lookup", None, None),), None)
        book.ingest("captain", "fake", drydock / "runtime" / "synthetic.jsonl", [turn], store)
        book.attributions.replay(store,drydock / "runtime")
        book.mark_record(drydock / "missing.jsonl", "captain", "fake", "synthetic unreadable")
    before = path.read_bytes(), path.stat().st_mtime_ns
    for by, key in (("seat", "captain"), ("pool", "fake"), ("order", order.id)):
        data = invoke("ledger", "--by", by)
        row = next(r for r in data["rows"] if r["key"] == key)
        assert row["turns"] == 1 and row["read_cached"] == 10 and row["written"] == 3
        assert row["cost"] is None and row["unmeasured"] > 0
        assert row["tool_calls"] == 1 and row["unjudged"] > 0
        assert data["incomplete_records"][0]["error"] == "synthetic unreadable"
        assert "10+" in invoke("ledger", "--by", by, text=True)
    data = invoke("ledger", "--tools")
    assert data["rows"] == [{"name": "lookup", "calls": 1, "seconds": 0, "failed": 0, "untimed": 1, "unjudged": 1}]
    text = invoke("ledger", "--tools", text=True)
    assert text.splitlines()[1] == f"{'lookup':<24} {1:>6} {'0.0+':>9} {'0+':>6}"
    invoke("status")
    invoke("status", "--line")
    invoke("watch", "captain")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before, "readings never write or migrate the Ledger"


def test_windows_spend_endpoints_failures_and_walls_are_source_data(invoke, drydock):
    declaration = drydock / "ship.toml"
    declaration.write_text(declaration.read_text() + '\n[pools.key]\nprovider="fake"\nkind="key"\n'
                           'api_key="env:SYNTHETIC_UNUSED_KEY"\nbudget="20 USD/month"\n'
                           '\n[pools.local]\nprovider="synthetic-endpoint"\nkind="endpoint"\nendpoint="http://127.0.0.1:1/v1"\n')
    at = datetime.now(timezone.utc) - timedelta(minutes=10)
    reset = at + timedelta(days=7)
    with Ledger(drydock / "runtime" / "ledger.sqlite") as book:
        book.record_usage("fake", Usage((Window("7d", .25, reset, group="synthetic", bucket="weekly"),
                                         Window("other", None, None, blocks=False, error="no measurement")), at))
        book.record_usage("fake", Usage((), at + timedelta(minutes=5), "synthetic timeout"))
        book.quota.observe("screen:synthetic", "captain", "fake", Wall("event", at, reset, "synthetic wall"), at)
    data = invoke("ledger")
    windows = {w["reading"]["window"]: w for w in data["windows"]}
    weekly = windows["7d"]
    assert weekly["reading"]["used"] == .25 and weekly["reading"]["resets_at"] == reset.isoformat()
    assert weekly["reading"]["read_at"] == at.isoformat() and weekly["reading"]["bucket"] == "weekly"
    assert weekly["reading"]["blocks"] is True and weekly["current"] is False and weekly["complete"] is True
    assert "synthetic timeout" in weekly["reason"] and weekly["age_seconds"] >= 600
    assert windows["other"]["reading"]["used"] is None and windows["other"]["reading"]["blocks"] is False
    assert windows["other"]["complete"] is False
    assert data["usage_attempts"][0]["error"]
    assert data["wall_events"][0]["source"] == "screen:synthetic"
    assert data["wall_events"][0]["resets_at"] == reset.isoformat()
    pools = {p["name"]: p for p in data["pools"]}
    assert pools["key"]["budget"] == {"amount": 20.0, "currency": "USD", "period": "month"}
    assert pools["key"]["spent"] is None and pools["key"]["unpriced"] == 0
    assert pools["local"]["endpoint"] == "http://127.0.0.1:1/v1"
    text = invoke("ledger", text=True)
    assert "LAST KNOWN" in text and "synthetic timeout" in text and "no cost stated" in text
    assert "estimated from first screen observation" in text


def test_headline_window_data_reaches_every_reading(invoke, drydock, monkeypatch):
    from quarterdeck.harness.fake import FakeHarness

    monkeypatch.setattr(FakeHarness, "reads_usage", True)
    monkeypatch.setattr(FakeHarness, "headline_window", (None, "7d"))
    at = datetime.now(timezone.utc)
    reset = at + timedelta(days=7)
    with Ledger(drydock / "runtime" / "ledger.sqlite") as book:
        book.record_usage("fake", Usage((Window("7d", .25, reset), Window("5h", .1, reset)), at))
    for command in (("status",), ("status", "--line"), ("watch", "captain")):
        data = invoke(*command)
        assert len(data["usage"]) == 1
        assert data["usage"][0]["reading"]["used"] == .25
        assert data["usage"][0]["reading"]["window"] == "7d"
        assert data["usage"][0]["current"] is True
        text = invoke(*command, text=True)
        if command == ("status", "--line"):
            assert "fake 7d" not in text and "usage" not in text and "reset" not in text
        else:
            assert "fake 7d 25%" in text
    with Ledger(drydock / "runtime" / "ledger.sqlite") as book:
        book.record_usage("fake", Usage((), at + timedelta(seconds=5), "synthetic timeout"))
    data = invoke("status", "--line")
    assert data["usage"][0]["reading"]["used"] == .25
    assert data["usage"][0]["current"] is False
    assert "synthetic timeout" in data["usage"][0]["reason"]
    assert "LAST KNOWN" in invoke("status", text=True)
    text = invoke("status", "--line", text=True)
    assert "LAST KNOWN" not in text and "fake 7d" not in text and "usage" not in text
