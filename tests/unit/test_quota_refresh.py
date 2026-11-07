"""Declared Google pool refresh is independent of seats, with explicit account binding."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import service, watch
from quarterdeck.harness import antigravity
from quarterdeck.harness.antigravity_quota import parse_usage, unknown, wall
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from .test_antigravity_quota import document


def declared_ship(tmp_path, layout):
    pools = {'google': Pool('google', 'antigravity'), 'codex': Pool('codex', 'codex')}
    specs = {}
    if layout != 'empty':
        program = 'codex' if layout == 'codex' else 'antigravity'
        pool = 'codex' if program == 'codex' else 'google'
        specs['worker'] = SeatSpec('worker', 'crew', 'worker', program, (pool,), up=False)
    return Ship(tmp_path, 'example', 'admiral', tmp_path, pools,
                {'crew': Session('crew', 'crew')}, specs)


@pytest.mark.parametrize('layout', ['codex', 'google-down', 'empty'])
def test_declared_pool_refresh_without_live_google_seats_and_restart(tmp_path, monkeypatch, layout):
    ship = declared_ship(tmp_path, layout)
    calls = []
    def fetch():
        calls.append('default-sign-in')
        return parse_usage(document(), datetime.now(timezone.utc))
    monkeypatch.setattr(antigravity, 'fetch_usage', fetch)
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    svc = Service(ship, SimpleNamespace())
    svc.account()
    assert calls == ['default-sign-in']
    assert svc.ledger.usage_known('google')
    assert not svc.ledger.pool_windows('codex'), 'Google results never populate the Codex pool'
    svc.account()
    svc.ledger.close()
    svc = Service(ship, SimpleNamespace())
    svc.account()
    assert calls == ['default-sign-in'], 'service restart preserves cadence with no Google seats'
    assert 'google 7d 75% used' in watch.ledger_usage(ship)[0]
    assert calls == ['default-sign-in'], 'status reads only the cache'
    svc.ledger.quota.usage_read('google', datetime.now(timezone.utc)-timedelta(seconds=301), None)
    svc.account()
    assert len(calls) == 2
    svc.ledger.close()


def test_declared_pool_failure_pacing_and_new_wall_refresh_without_seat(tmp_path, monkeypatch):
    ship = declared_ship(tmp_path, 'empty')
    calls = []
    def fetch():
        calls.append('attempt')
        return unknown('synthetic command failure', datetime.now(timezone.utc))
    monkeypatch.setattr(antigravity, 'fetch_usage', fetch)
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    svc = Service(ship, SimpleNamespace())
    svc.account()
    svc.ledger.close()
    svc = Service(ship, SimpleNamespace())
    svc.account()
    assert calls == ['attempt'] and not svc.ledger.usage_known('google')
    assert 'synthetic command failure' in watch.ledger_usage(ship)[0]
    now = datetime.now(timezone.utc)
    event = wall('Individual quota reached. Resets in 2h.', 'synthetic-event', now)
    svc.ledger.quota.observe('synthetic-record', 'former-worker', 'google', event, now)
    svc.account()
    assert calls == ['attempt', 'attempt'], 'new relevant wall refreshes even without a current seat'
    svc.ledger.quota.observe('synthetic-record', 'former-worker', 'google', event, datetime.now(timezone.utc))
    svc.account()
    assert calls == ['attempt', 'attempt'], 'a repeated wall and a failed command do not bypass pacing'
    svc.ledger.close()


def test_default_account_aliases_share_one_read_other_accounts_and_keys_stay_separate(tmp_path, monkeypatch):
    ship = declared_ship(tmp_path, 'empty')
    ship.pools['google-alias'] = Pool('google-alias', 'antigravity')
    ship.pools['different-account'] = Pool('different-account', 'antigravity', config_dir=tmp_path/'another-account')
    ship.pools['key-pool'] = Pool('key-pool', 'antigravity', api_key='env:SYNTHETIC_KEY')
    calls = []
    def fetch():
        calls.append('default-sign-in')
        return parse_usage(document(), datetime.now(timezone.utc))
    monkeypatch.setattr(antigravity, 'fetch_usage', fetch)
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    svc = Service(ship, SimpleNamespace())
    svc.account()
    assert calls == ['default-sign-in'], 'aliases must not multiply authenticated requests'
    assert svc.ledger.usage_known('google') and svc.ledger.usage_known('google-alias')
    assert not svc.ledger.usage_known('different-account')
    assert not svc.ledger.pool_windows('key-pool') and not svc.ledger.pool_windows('codex')
    assert 'account directory is not qualified' in '\n'.join(svc.ledger.quota.usage_notes())
    svc.ledger.close()
