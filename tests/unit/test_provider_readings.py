"""Public program/company/paying-pool identities, without changing native evidence."""

import json

import pytest

from quarterdeck import cli, ledger_reading, output, providers, watch
from quarterdeck.ship import load
from .test_cross_program_failover import running  # noqa: F401


pytestmark = pytest.mark.usefixtures('synthetic_disk_room')


def test_status_watch_and_ledger_distinguish_running_program_company_and_pool(running):
    r = running
    ship = r.seat.ship
    seat = output.value(watch.reading(r.seat))
    assert {k: seat[k] for k in ("harness", "program", "provider_id", "provider_name", "pool")} == {
        "harness": "codex", "program": "codex", "provider_id": "openai", "provider_name": "OpenAI", "pool": "first"}
    status = output.value(watch.status_reading(ship, r.seat.tmux))
    full = output.value(watch.seat_reading(r.seat))
    assert status["pools"] == full["pools"] == providers.identities(ship)["pools"]
    assert "codex" in '\n'.join(watch.render_table(watch.status_reading(ship, r.seat.tmux)))
    assert "provider OpenAI · pool first" in '\n'.join(watch.render_full(watch.seat_reading(r.seat)))
    assert next(p for p in full["pools"] if p["name"] == "second") == {
        "name": "second", "kind": "subscription", "provider_id": "anthropic",
        "provider_name": "Anthropic", "native_harness": "claude"}
    r.svc.account()
    for kwargs in ({}, {"by": "pool"}, {"by": "seat"}, {"by": "order"}, {"tools": True}):
        data = output.value(ledger_reading.collect(r.svc.ledger, ship, **kwargs))
        assert data["pools"] == full["pools"]
        assert data["harnesses"] == full["harnesses"]
        assert all("provider" not in p for p in data["pools"])
    assert "provider OpenAI" in '\n'.join(ledger_reading.render(ledger_reading.collect(r.svc.ledger, ship)))
    assert r.seat.launched().harness == "codex"  # unchanged source evidence


def test_doctor_json_adds_declared_identities_and_keeps_its_check_shape(tmp_path, monkeypatch, capsys):
    (tmp_path / "ship.toml").write_text('[ship]\nname="synthetic"\n[pools.paid]\nprovider="claude"\n')
    monkeypatch.setattr(cli.doctor, "program_version", lambda a: "synthetic")
    code = cli.main(["--ship", str(tmp_path), "--json", "doctor"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["pools"][0]["provider_id"] == "anthropic" and data["harnesses"] == []
    assert any(c["name"] == "provider spelling paid" for c in data["checks"])
    assert all(set(c) == {"name", "passed", "detail"} for c in data["checks"])


def test_open_program_metadata_does_not_claim_the_model_brand_is_the_provider(tmp_path):
    (tmp_path / "ship.toml").write_text('''[ship]
name="synthetic"
[pools.remote]
provider="private-operator"
endpoint="https://synthetic.invalid/v1"
[sessions.crew]
[seats.worker]
session="crew"
harness="pi"
pool="remote"
model="claude-synthetic-model"
''')
    data = providers.identities(load(tmp_path))
    assert data["pools"][0]["provider_id"] == "private-operator"
    assert data["pools"][0]["native_harness"] is None
    assert data["harnesses"][0]["native_provider_id"] is None
    assert data["harnesses"][0]["program"] == "pi"
