"""Each adapter declares its sign-in's grant kind; only a grant a login cures is a login needed.

Synthetic credential metadata only: no real grant, refresh, program or provider is touched.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quarterdeck import credential, harness as harnesses
from quarterdeck.harness.base import GRANT_KINDS
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.grok import GrokHarness
from quarterdeck.ship import Pool, Ship


def stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shown(when: datetime) -> str:
    return when.astimezone().strftime("%m-%d %H:%M")


def grok_pool(tmp_path: Path, access_ends: datetime) -> tuple[Ship, Pool]:
    folder = tmp_path / "synthetic-grok-pool"
    folder.mkdir(parents=True)
    (folder / "auth.json").write_text(json.dumps({"synthetic-grant": {
        "refresh_token": "synthetic-only", "expires_at": stamp(access_ends)}}))
    pool = Pool("synthetic-grok", "xai", config_dir=folder)
    return Ship(tmp_path, "t", "operator", tmp_path, {pool.name: pool}, {}, {}), pool


def claude_pool(tmp_path: Path, grant_ends: datetime) -> tuple[Ship, Pool]:
    folder = tmp_path / "synthetic-claude-pool"
    folder.mkdir(parents=True)
    (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "refreshToken": "fake", "refreshTokenExpiresAt": int(grant_ends.timestamp() * 1000)}}))
    pool = Pool("synthetic-claude", "anthropic", config_dir=folder)
    return Ship(tmp_path, "t", "operator", tmp_path, {pool.name: pool}, {}, {}), pool


def test_each_adapter_declares_its_measured_grant_kind():
    declared = {name: harnesses.get(name).grant_kind for name in harnesses.NAMES if name != "fake"}
    assert declared == {"claude": "fixed", "grok": "self-renewing", "codex": "unknown",
                        "antigravity": "unknown", "opencode": "unknown", "pi": "unknown"}
    assert all(kind in GRANT_KINDS for kind in declared.values())


def test_a_self_renewing_grant_near_its_short_access_expiry_is_no_login_needed(tmp_path):
    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    ship, pool = grok_pool(tmp_path, soon)
    assert abs((credential.grant_expiry(pool) - soon).total_seconds()) < 1, "still read, as metadata"
    assert credential.login_needed(ship) == []
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    ship, pool = grok_pool(tmp_path / "past", past)
    assert credential.login_needed(ship) == [], "an access grant that lapsed while idle renews on the next use"
    detail = credential.grant_detail(pool)
    assert detail.startswith("; its access renews on use (current access until ")
    assert "when the grant that renews it ends is unknown" in detail and "renewal failed" not in detail


def test_a_failed_renewal_is_a_login_needed(tmp_path, monkeypatch):
    ship, pool = grok_pool(tmp_path, datetime.now(timezone.utc) + timedelta(days=20))
    monkeypatch.setattr(GrokHarness, "renewal_failed", lambda self, path: True)
    assert credential.login_needed(ship) == [("synthetic-grok", credential.RENEWAL_FAILED)]
    assert credential.grant_detail(pool).endswith("; its renewal failed: log in")
    monkeypatch.setattr(GrokHarness, "renewal_failed", lambda self, path: None)
    assert credential.login_needed(ship) == [], "an unknown renewal outcome is no failure"


def test_a_readable_renewing_grant_near_its_end_is_a_login_needed(tmp_path, monkeypatch):
    ship, pool = grok_pool(tmp_path, datetime.now(timezone.utc) + timedelta(hours=1))
    ends = datetime.now(timezone.utc) + timedelta(hours=30)
    monkeypatch.setattr(GrokHarness, "renewal_end", lambda self, path: ends)
    assert credential.login_needed(ship) == [("synthetic-grok", shown(ends))]
    assert f"; the grant that renews it ends {shown(ends)}" in credential.grant_detail(pool)
    far = datetime.now(timezone.utc) + timedelta(days=20)
    monkeypatch.setattr(GrokHarness, "renewal_end", lambda self, path: far)
    assert credential.login_needed(ship) == []


def test_self_renewal_observation_uses_the_renewal_deadline_and_failed_evidence(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    _, pool = grok_pool(tmp_path, now - timedelta(hours=1))
    source = credential.pool_file(pool)
    assert credential.freshness(pool, source)[0] < now.timestamp()
    assert credential.sign_in_status(pool, source, now) == 'renewable'
    monkeypatch.setattr(GrokHarness, "renewal_end", lambda self, path: now)
    assert credential.sign_in_status(pool, source, now) == 'expired'
    monkeypatch.setattr(GrokHarness, "renewal_end", lambda self, path: now + timedelta(days=1))
    monkeypatch.setattr(GrokHarness, "renewal_failed", lambda self, path: True)
    assert credential.sign_in_status(pool, source, now) == 'expired'


def test_a_fixed_grant_near_its_end_is_a_login_needed(tmp_path):
    soon = datetime.now(timezone.utc) + timedelta(hours=30)
    ship, pool = claude_pool(tmp_path, soon)
    assert credential.login_needed(ship) == [("synthetic-claude", shown(soon))]
    assert credential.grant_detail(pool) == f"; its grant expires {shown(soon)}"
    ship, pool = claude_pool(tmp_path / "later", datetime.now(timezone.utc) + timedelta(days=20))
    assert credential.login_needed(ship) == []


def test_an_unknown_grant_kind_never_counts_and_reads_unknown(tmp_path, monkeypatch):
    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    for adapter, (ship, pool) in ((GrokHarness, grok_pool(tmp_path / "grok", soon)),
                                  (ClaudeHarness, claude_pool(tmp_path / "claude", soon))):
        monkeypatch.setattr(adapter, "grant_kind", "unknown")
        monkeypatch.setattr(adapter, "renewal_failed", lambda self, path: True)
        assert credential.login_needed(ship) == []
        assert credential.grant_detail(pool) == "; grant kind unknown"
    codex = tmp_path / "synthetic-codex-pool"
    codex.mkdir()
    (codex / "auth.json").write_text(json.dumps({"tokens": {"refresh_token": "synthetic-only"}}))
    pool = Pool("synthetic-codex", "openai", config_dir=codex)
    ship = Ship(tmp_path, "t", "operator", tmp_path, {pool.name: pool}, {}, {})
    assert credential.login_needed(ship) == []
    assert credential.grant_detail(pool) == "; grant kind unknown"


@pytest.mark.parametrize("pool", [Pool("synthetic-key", "anthropic", api_key="env:SYNTHETIC_KEY"),
                                  Pool("synthetic-google", "google")])
def test_a_pool_without_a_credential_file_has_no_grant_reading(pool):
    assert credential.grant_detail(pool) == ""
