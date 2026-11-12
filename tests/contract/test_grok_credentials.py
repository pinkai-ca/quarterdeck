"""Synthetic credential metadata; no real grant, refresh or provider invocation."""

from datetime import datetime, timezone
import json

import pytest

from quarterdeck.harness.grok import GrokHarness


def test_native_grant_expiry_and_freshness_use_explicit_metadata(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"synthetic-profile": {
        "refresh_token": "synthetic-only", "expires_at": "2040-02-01T00:00:00Z"}}))
    adapter = GrokHarness()
    expiry = datetime(2040, 2, 1, tzinfo=timezone.utc)
    assert adapter.grant_expiry(path) == expiry
    assert adapter.freshness(path) == (expiry.timestamp(), path.stat().st_mtime)


@pytest.mark.parametrize("data", [{}, [], {"one": {"refresh_token": "synthetic-only"}},
    {"one": {"refresh_token": "synthetic-only", "expires_at": "unreadable"}},
    {"one": {"refresh_token": "synthetic-one", "expires_at": "2040-02-01T00:00:00Z"},
     "two": {"refresh_token": "synthetic-two", "expires_at": "2041-02-01T00:00:00Z"}}])
def test_missing_or_ambiguous_grant_metadata_is_unknown(tmp_path, data):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(data))
    assert GrokHarness().grant_expiry(path) is None
    assert GrokHarness().freshness(path) is None
