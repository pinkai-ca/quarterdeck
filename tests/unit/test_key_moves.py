"""A seat moves between a subscription and a key, both ways, for each program that takes a key. Synthetic keys only."""

from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest

from quarterdeck import credential, endpoint, harness, keyfile, quota
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.grok import GrokHarness
from quarterdeck.harness.opencode import OpenCodeHarness
from quarterdeck.harness.pi import PiHarness
from quarterdeck.ship import Pool, SeatSpec, ShipError
from .test_cross_program_failover import running, usage  # noqa: F401

KEY = 'sk-synthetic-Q7wM2xC9zT4lP8rV3nH6'


def seat_for(program, pool):
    return SeatSpec('w', 'crew', 'worker', program, (pool.name,))


@pytest.fixture
def home(tmp_path):
    folder = tmp_path / 'runtime' / 'seats' / 'w' / 'home'
    folder.mkdir(parents=True, mode=0o700)
    return folder


def test_claude_moves_onto_a_key_and_back(home, tmp_path):
    adapter = ClaudeHarness()
    signed = tmp_path / 'signed'
    signed.mkdir()
    sign_in = {'claudeAiOauth': {'refreshToken': 'synthetic-refresh', 'expiresAt': 4102444800000}}
    (signed / '.credentials.json').write_text(json.dumps(sign_in))
    subscription = Pool('sub', 'anthropic', config_dir=signed)
    paid = Pool('paid', 'anthropic', api_key='keys:anthropic', ship_folder=tmp_path)
    adapter.build_home(home, seat_for('claude', subscription), subscription, None)
    assert (home / '.credentials.json').is_symlink()
    state = json.loads((home / '.claude.json').read_text())
    (home / '.claude.json').write_text(json.dumps({**state, 'oauthAccount': {'emailAddress': 'synthetic@example.invalid'}}))

    adapter.build_home(home, seat_for('claude', paid), paid, None)
    keyfile.prepare_home(keyfile.route('claude', paid), home)
    assert not (home / '.credentials.json').exists() and not (home / '.credentials.json').is_symlink()
    assert json.loads((home / 'settings.json').read_text())['apiKeyHelper'] == keyfile.HELPER_COMMAND
    assert 'oauthAccount' not in json.loads((home / '.claude.json').read_text())
    assert 'ANTHROPIC_API_KEY' not in adapter.launch(seat_for('claude', paid), paid, home, tmp_path / 'b', None, None).env

    adapter.build_home(home, seat_for('claude', subscription), subscription, None)
    assert 'apiKeyHelper' not in json.loads((home / 'settings.json').read_text())
    assert (home / '.credentials.json').resolve() == (signed / '.credentials.json').resolve()
    assert json.loads((signed / '.credentials.json').read_text()) == sign_in


def test_codex_moves_onto_a_key_and_back_and_the_pools_sign_in_is_untouched(home, tmp_path):
    adapter = CodexHarness()
    signed = tmp_path / 'signed'
    signed.mkdir()
    sign_in = {'tokens': {'refresh_token': 'synthetic-refresh'}}
    (signed / 'auth.json').write_text(json.dumps(sign_in))
    subscription = Pool('sub', 'openai', config_dir=signed)
    paid = Pool('paid', 'openai', api_key='keys:openai', ship_folder=tmp_path)
    adapter.build_home(home, seat_for('codex', subscription), subscription, None)
    assert (home / 'auth.json').is_symlink()
    adapter.build_home(home, seat_for('codex', paid), paid, None)
    assert not (home / 'auth.json').is_symlink() and not (home / 'auth.json').exists()
    (home / 'auth.json').write_text(json.dumps({'auth_mode': 'apikey', 'OPENAI_API_KEY': KEY}))  # its own key login
    adapter.build_home(home, seat_for('codex', subscription), subscription, None)
    assert (home / 'auth.json').is_symlink() and (home / 'auth.json').resolve() == (signed / 'auth.json').resolve()
    assert KEY not in (signed / 'auth.json').read_text()
    adapter.build_home(home, seat_for('codex', paid), paid, None)
    assert not (home / 'auth.json').exists(), 'the subscription link goes; the login writes its own file at launch'


@pytest.fixture
def grok(home, tmp_path):
    signed = tmp_path / 'signed'
    signed.mkdir()
    sign_in = {'account': {'expires_at': '2099-01-01T00:00:00Z', 'refresh_token': 'synthetic-refresh'}}
    (signed / 'auth.json').write_text(json.dumps(sign_in))
    subscription = Pool('sub', 'xai', config_dir=signed)
    paid = Pool('paid', 'xai', api_key='keys:xai', ship_folder=tmp_path)
    ship = SimpleNamespace(pools={'sub': subscription, 'paid': paid})
    return SimpleNamespace(adapter=GrokHarness(), ship=ship, subscription=subscription, paid=paid, signed=signed)


def test_grok_leaves_its_plain_copy_for_a_key_and_takes_a_new_one_back(home, grok):
    (home / 'auth.json').write_bytes((grok.signed / 'auth.json').read_bytes())
    (home / 'auth.json').chmod(0o600)
    credential.drop_plain_copy(grok.ship, grok.adapter, home)
    assert not (home / 'auth.json').exists()
    assert credential_of(grok.paid, home, grok.adapter).variable == 'XAI_API_KEY'
    credential_of(grok.subscription, home, grok.adapter)
    assert (home / 'auth.json').read_bytes() == (grok.signed / 'auth.json').read_bytes()
    assert stat.S_IMODE((home / 'auth.json').stat().st_mode) == 0o600


def credential_of(pool, home, adapter):
    from quarterdeck.harness.home import credential as attach
    return attach(pool, home / adapter.credential_file, 'XAI_API_KEY', copy=adapter.credential_copy)


def test_a_copy_that_differs_may_hold_the_only_live_token_and_stays(home, grok):
    newer = {'account': {'expires_at': '2099-06-01T00:00:00Z', 'refresh_token': 'synthetic-newer'}}
    (home / 'auth.json').write_text(json.dumps(newer))
    credential.drop_plain_copy(grok.ship, grok.adapter, home)
    assert json.loads((home / 'auth.json').read_text()) == newer
    with pytest.raises(ShipError, match='may hold the only'):
        credential_of(grok.paid, home, grok.adapter)
    assert json.loads((home / 'auth.json').read_text()) == newer


@pytest.mark.parametrize('adapter', [OpenCodeHarness(), PiHarness()], ids=['opencode', 'pi'])
def test_an_endpoint_program_moves_between_a_keyed_endpoint_and_a_keyless_one(adapter, home, tmp_path, key_file, monkeypatch):
    key_file.write(openrouter=KEY)
    monkeypatch.setattr(endpoint, 'models', lambda url, timeout=0, **options: [endpoint.Served('synthetic-model', 4096)])
    keyed = Pool('remote', 'openrouter', endpoint='https://example.invalid/v1', api_key='keys:openrouter', ship_folder=tmp_path)
    local = Pool('local', 'local', endpoint='http://127.0.0.1:9/v1')
    for pool, variable in ((keyed, endpoint.KEY_VARIABLE), (local, None), (keyed, endpoint.KEY_VARIABLE)):
        built = adapter.build_home(home, seat_for(adapter.name, pool), pool, None)
        assert (built.variable if built else None) == variable
        assert all(KEY not in file.read_text(errors='replace') for file in home.rglob('*') if file.is_file())


# -- the service: to a key when the subscriptions are spent, and back when one has room ---------------------------------

def keyed(r, name):
    ship = r.seat.ship
    ship.pools[name] = Pool(name, ship.pools[name].declared_provider, api_key='keys:paid', ship_folder=ship.folder)


def test_a_seat_goes_to_its_key_at_the_wall_and_back_when_the_subscription_has_room(running, key_file):
    r = running
    key_file.write(paid=KEY)
    keyed(r, 'second')
    r.svc.failover()
    assert r.seat.pool.name == 'second' and len(r.launches) == 1
    r.svc.failover()
    assert r.seat.pool.name == 'second' and len(r.launches) == 1, 'its subscription is still spent'
    usage(r.svc, 'first', 0.1)
    r.svc.failover()
    assert r.seat.pool.name == 'first' and len(r.launches) == 2
    assert any('moved back to pool first' in m.body for m in r.svc.store.for_seat('worker'))


def test_a_seat_on_a_key_is_not_moved_back_while_it_works(running, key_file, monkeypatch):
    r = running
    key_file.write(paid=KEY)
    keyed(r, 'second')
    r.svc.failover()
    usage(r.svc, 'first', 0.1)
    monkeypatch.setattr(r.svc, '_wall_idle_hold', lambda seat: SimpleNamespace(reason='working', dimension='turn'))
    r.svc.failover()
    assert r.seat.pool.name == 'second' and len(r.launches) == 1


def test_a_seat_first_on_a_key_never_moves_to_a_later_subscription_with_room(running, key_file):
    r = running
    key_file.write(paid=KEY)
    keyed(r, 'first')
    usage(r.svc, 'second', 0.1)
    r.svc._back_from_key(r.seat)
    assert r.seat.pool.name == 'first' and r.launches == [], 'a key first in the list is the choice that was made'
