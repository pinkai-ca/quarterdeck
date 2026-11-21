"""Unsupported subscription/key providers fail at load; endpoint labels are not adapter names."""

from types import SimpleNamespace

from datetime import datetime, timezone

import pytest

from quarterdeck import credential, harness, watch
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.service import Service
from quarterdeck.ship import ShipError, load


@pytest.mark.parametrize("key", [False, True])
@pytest.mark.parametrize("sibling", [False, True])
def test_unused_unsupported_provider_is_refused_by_pool_before_metadata_consumers(tmp_path, key, sibling):
    text = '[ship]\nname="synthetic"\n'
    if sibling:
        text += '[pools.valid]\nprovider="claude"\n'
    text += '[pools.spare]\nprovider="synthetic-unknown"\n'
    if key:
        text += 'api_key="keys:synthetic-key"\n'
    (tmp_path / "ship.toml").write_text(text)
    kind = "key" if key else "subscription"
    with pytest.raises(ShipError, match=f"pool spare: unsupported {kind} provider 'synthetic-unknown'.*declare an endpoint URL"):
        load(tmp_path)


@pytest.mark.parametrize("label", ["synthetic-unknown", "opencode"])
@pytest.mark.parametrize("key", [False, True])
def test_endpoint_labels_and_valid_siblings_work_across_generic_metadata(tmp_path, monkeypatch, label, key):
    text = ('[ship]\nname="synthetic"\n'
            '[pools.valid]\nprovider="claude"\n'
            '[pools.valid-key]\nprovider="claude"\napi_key="keys:synthetic-key"\n'
            f'[pools.local]\nprovider="{label}"\nendpoint="https://synthetic.invalid/v1"\n')
    if key:
        text += 'api_key="keys:synthetic-key"\n'
    (tmp_path / "ship.toml").write_text(text)
    ship = load(tmp_path)
    endpoint = ship.pools["local"]
    assert endpoint.kind == "endpoint" and endpoint.provider == label and not ship.seats
    read = []
    def usage(pool):
        read.append(pool.name)  # a stand-in: no provider is called
        from quarterdeck.harness.limits import Usage
        return Usage((), datetime.now(timezone.utc), "synthetic: no provider here", called=False)
    monkeypatch.setattr(ClaudeHarness, "usage", staticmethod(usage))
    original = harness.get
    def get(name):
        assert name == "claude", "an endpoint label must never be looked up as a program"
        return original(name)
    monkeypatch.setattr(harness, "get", get)
    assert credential.pool_file(endpoint) is None
    assert credential.freshness(endpoint, tmp_path / "not-a-credential") is None
    assert credential.grant_expiry(endpoint) is None
    assert credential.login_needed(ship) == []
    assert watch.ledger_usage(ship) == ['valid 5h usage unknown']
    assert read == [], 'metadata never queries a provider'
    svc = Service(ship, SimpleNamespace())
    try:
        svc.account()  # includes the declared-pool refresh metadata scan, with no active seats
        # A declared Claude subscription pool is read on the service's tick with its own sign-in, seat or none;
        # the key pool and the endpoint never are.
        assert read == ['valid'], read
        assert svc.known_open(SimpleNamespace(), "local")
        assert svc.known_open(SimpleNamespace(), "valid-key")
        assert credential.login_needed(ship) == []
        assert watch.ledger_usage(ship) == ['valid 5h usage unknown: synthetic: no provider here'], 'shown as such'
    finally:
        svc.ledger.close()
