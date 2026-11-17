"""The usage log keeps how often a provider refused, without repeating states that make no call,
and a service restart keeps each live pool's read pacing.

Every failed provider call is logged (at most one per pool per interval). A state that made no
call (a record reader's missing snapshot, a missing credential or token) is logged when its reason
differs from the pool's last logged one, and again after a good read. Synthetic HTTP and ledgers.
"""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from quarterdeck import quota
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.service import Service
from quarterdeck.ship import Pool, Session, Ship
from .test_claude_usage_reads import Endpoint, ok, refused, subscription

NOW = datetime.now(timezone.utc)


def service_with(tmp_path, *pools: Pool) -> Service:
    ship = Ship(tmp_path, "example", "operator", tmp_path, {p.name: p for p in pools}, {"crew": Session("crew", "crew")}, {})
    return Service(ship, SimpleNamespace())


def logged(svc) -> list[dict]:
    if not svc.log_path.exists():
        return []
    return [entry for entry in map(json.loads, svc.log_path.read_text().splitlines()) if entry["event"] == "usage"]


def without_at(entries):
    return [{k: v for k, v in entry.items() if k != "at"} for entry in entries]


def test_no_call_states_log_once_again_on_change_and_after_a_good_read_while_calls_always_log(tmp_path):
    svc = service_with(tmp_path, Pool("pool", "claude"))
    no_call = lambda reason: Usage((), NOW, reason, called=False)
    calls = [no_call("credential file missing")] * 3 + [no_call("no access token in the credential file")] * 2
    calls += [Usage((Window("7d", .2, NOW + timedelta(days=2)),), NOW), no_call("no access token in the credential file")]
    calls += [Usage((), NOW, "HTTP 503")] * 3 + [no_call("no access token in the credential file")] * 2
    for usage in calls:
        quota.record_read(svc, "pool", usage, 300, NOW)
    assert without_at(logged(svc)) == [
        {"event": "usage", "pool": "pool", "error": "credential file missing", "no_call": True},
        {"event": "usage", "pool": "pool", "error": "no access token in the credential file", "no_call": True},
        {"event": "usage", "pool": "pool", "error": "no access token in the credential file", "no_call": True},
        {"event": "usage", "pool": "pool", "error": "HTTP 503"},
        {"event": "usage", "pool": "pool", "error": "HTTP 503"},
        {"event": "usage", "pool": "pool", "error": "HTTP 503"},
        {"event": "usage", "pool": "pool", "error": "no access token in the credential file", "no_call": True},
    ]
    svc.ledger.close()


def test_codex_missing_snapshot_is_logged_once_however_often_it_is_read(tmp_path):
    svc = service_with(tmp_path, Pool("codex", "codex"))
    for _ in range(4):
        svc.ledger.quota.usage_read("codex", datetime.now(timezone.utc) - timedelta(hours=1), None)  # due again
        quota.refresh_declared(svc)
    entries = logged(svc)
    assert len(entries) == 1 and entries[0]["no_call"] is True
    assert entries[0]["error"].startswith("no post-launch Codex usage snapshot")
    assert svc.ledger.db.execute("SELECT error FROM usage_reads WHERE pool='codex'").fetchone()[0], "each read is still recorded"
    svc.ledger.close()


def test_a_missing_claude_credential_makes_no_call_and_logs_once(tmp_path, monkeypatch):
    pool = subscription(tmp_path)
    (pool.config_dir / ".credentials.json").unlink()
    endpoint = Endpoint(monkeypatch)
    svc = service_with(tmp_path, pool)
    seat = SimpleNamespace(harness=ClaudeHarness(), pool=pool)
    clock = [5000.0]
    monkeypatch.setattr(quota, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    for _ in range(3):
        clock[0] += 300
        quota.refresh(svc, seat, True, SimpleNamespace(pool=pool.name))
    assert endpoint.calls == 0 and without_at(logged(svc)) == [
        {"event": "usage", "pool": pool.name, "error": "credential file missing", "no_call": True}]
    svc.ledger.close()


def test_repeated_provider_refusals_each_log(tmp_path, monkeypatch):
    pool = subscription(tmp_path)
    endpoint = Endpoint(monkeypatch, refused(503), refused(503), refused(429, "60"))
    svc = service_with(tmp_path, pool)
    seat = SimpleNamespace(harness=ClaudeHarness(), pool=pool)
    clock = [5000.0]
    monkeypatch.setattr(quota, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    for _ in range(3):
        clock[0] += 300
        quota.refresh(svc, seat, True, SimpleNamespace(pool=pool.name))
    assert endpoint.calls == 3
    assert [e["error"] for e in logged(svc)] == ["HTTP 503", "HTTP 503", "HTTP 429, retry-after 60"]
    assert "no_call" not in logged(svc)[0] and logged(svc)[-1]["backoff"] == 300
    svc.ledger.close()


def test_a_restart_within_the_interval_does_not_read_and_past_it_does(tmp_path, monkeypatch):
    pool = subscription(tmp_path)
    endpoint = Endpoint(monkeypatch)
    seat = SimpleNamespace(harness=ClaudeHarness(), pool=pool)
    launched = SimpleNamespace(pool=pool.name)
    svc = service_with(tmp_path, pool)
    quota.refresh(svc, seat, True, launched)
    assert endpoint.calls == 1
    svc.ledger.close()
    svc = service_with(tmp_path, pool)  # a restart: the in-memory pacing is gone
    quota.refresh(svc, seat, True, launched)
    assert endpoint.calls == 1, "the stored attempt time paces the first read after a restart"
    svc.ledger.quota.usage_read(pool.name, datetime.now(timezone.utc) - timedelta(seconds=299), None)
    svc.ledger.close()
    svc = service_with(tmp_path, pool)
    quota.refresh(svc, seat, True, launched)
    assert endpoint.calls == 1
    svc.ledger.quota.usage_read(pool.name, datetime.now(timezone.utc) - timedelta(seconds=301), None)
    svc.ledger.close()
    svc = service_with(tmp_path, pool)
    quota.refresh(svc, seat, True, launched)
    assert endpoint.calls == 2, "past the interval a restarted service reads"
    quota.refresh(svc, seat, True, launched)
    assert endpoint.calls == 2
    svc.ledger.close()


def test_a_pool_never_read_is_read_at_once_after_a_restart(tmp_path, monkeypatch):
    pool = subscription(tmp_path)
    endpoint = Endpoint(monkeypatch)
    svc = service_with(tmp_path, pool)
    quota.refresh(svc, SimpleNamespace(harness=ClaudeHarness(), pool=pool), True, SimpleNamespace(pool=pool.name))
    assert endpoint.calls == 1
    svc.ledger.close()
