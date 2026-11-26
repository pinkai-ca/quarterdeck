"""A seat moves between a subscription and a key, both ways, for each program that takes a key. Synthetic keys only."""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from types import SimpleNamespace

import pytest

from quarterdeck import credential, endpoint, fresh_handoff, harness, keyfile, post, quota, watch
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

RETURN_RATES = dict(currency='USD', unit='million_tokens', input=2, cache_read=1, cache_write=3, output=4)


def keyed(r, name, **options):
    ship = r.seat.ship
    ship.pools[name] = Pool(name, ship.pools[name].declared_provider, api_key='keys:paid', ship_folder=ship.folder,
                            **options)


def return_orders(r):
    return [m for m in r.svc.store.for_seat('worker') if m.subject == fresh_handoff.SUBJECT]


def return_reports(r):
    return [m.body for m in r.svc.store.for_seat('worker') if m.body.startswith('moved back to pool')]


@pytest.fixture
def key_return(running, key_file):
    r = running
    key_file.write(paid=KEY)
    keyed(r, 'second', price=RETURN_RATES)
    r.svc.failover()
    assert r.seat.pool.name == 'second' and len(r.launches) == 1
    r.svc.failover()
    assert r.seat.pool.name == 'second' and not return_orders(r), 'its subscription is still spent'
    usage(r.svc, 'first', 0.1)
    r.svc.failover()
    return r


def report_return_handoff(r, order):
    seat, record = r.seat, r.created[-1]
    launch = seat.launched()
    for message in r.svc.attempts.undelivered(r.svc.store, seat.name):
        r.svc.attempts.close(r.svc.attempts.open(message, seat.name, launch.pane, launch.pid, launch.at), 'delivered')
    record.begin(post.typed([order]), seat.spec.model, 'high')
    handoff = r.svc.store.new('order', seat.name, seat.name, 'The new notes written on the key.', re=order.id)
    r.svc.store.new('report', seat.name, post.SHIP, f'Handoff: {handoff.id}', re=order.id)
    return handoff


def test_a_cross_provider_return_waits_for_a_new_handoff_on_the_key(key_return):
    r = key_return
    before = r.seat.launched()
    [order] = return_orders(r)
    for _ in range(3):
        r.svc.fresh_handoffs()
        r.svc.failover()
    assert r.seat.launched() == before and return_orders(r) == [order] and not return_reports(r)

    def key_meter():
        return r.svc.ledger.db.execute(
            "SELECT count(*), sum(cost) FROM turns WHERE seat='worker' AND pool='second'"
        ).fetchone()

    r.svc.account()
    turns_before, cost_before = key_meter()
    handoff = report_return_handoff(r, order)
    r.svc.fresh_handoffs()
    assert r.seat.launched() == before and not return_reports(r), 'the report turn is still open'
    record = r.created[-1]
    record.end('Handoff ready.')
    r.svc.account()
    turns_after, cost_after = key_meter()
    assert turns_after == turns_before + 1
    cached, uncached, written_cache, output, cost = r.svc.ledger.db.execute(
        'SELECT read_cached, read_uncached, cache_written, written, cost FROM turns WHERE record=? ORDER BY turn DESC LIMIT 1',
        (str(record.path),),
    ).fetchone()
    assert cached == 2000 and written_cache == 200 and output == 53
    assert uncached == len(post.typed([order])) // 4 + 100
    assert cost_after - cost_before == pytest.approx(cost)
    assert (uncached, cost) == (239, pytest.approx(0.003290))

    for _ in range(3):
        r.svc.fresh_handoffs()
        r.svc.failover()
    assert r.seat.pool.name == 'first' and len(r.launches) == 2
    assert r.seat.launched().resume is None and r.seat.launched().pool_selected is None
    assert handoff.body in r.launches[-1][-1] and r.svc.store.is_open(handoff)
    assert return_orders(r) == [order]
    assert return_reports(r) == [
        'moved back to pool first from its key second: the subscription has room and started a new conversation'
    ]


def test_the_staged_return_holds_when_its_destination_has_no_room(key_return):
    r = key_return
    [order] = return_orders(r)
    report_return_handoff(r, order)
    r.created[-1].end('Handoff ready.')
    before = r.seat.launched()
    usage(r.svc, 'first', 1.0)
    for _ in range(3):
        r.svc.fresh_handoffs()
        r.svc.failover()
    assert r.seat.launched() == before and not return_reports(r) and return_orders(r) == [order]
    assert any('return to first held:' in line for line in watch.service_holds(r.svc.runtime))
    usage(r.svc, 'first', 0.1)
    r.svc.fresh_handoffs()
    assert r.seat.pool.name == 'first' and len(r.launches) == 2
    assert watch.service_holds(r.svc.runtime) == []


def test_a_return_rechecks_a_destination_that_changes_during_the_usage_read(key_return, monkeypatch):
    r = key_return
    [order] = return_orders(r)
    report_return_handoff(r, order)
    r.created[-1].end('Handoff ready.')
    before = r.seat.launched()
    known_open = r.svc.known_open

    def reads_then_walls(seat, pool):
        result = known_open(seat, pool)
        usage(r.svc, pool, 1.0)
        return result

    with monkeypatch.context() as refresh:
        refresh.setattr(r.svc, 'known_open', reads_then_walls)
        r.svc.fresh_handoffs()
    assert r.seat.launched() == before and not return_reports(r)
    assert return_orders(r) == [order]
    assert any('return to first held:' in line for line in watch.service_holds(r.svc.runtime))
    usage(r.svc, 'first', 0.1)
    r.svc.fresh_handoffs()
    assert r.seat.pool.name == 'first' and len(r.launches) == 2


@pytest.mark.parametrize('destination', ['first', 'third'])
def test_a_wall_takes_over_from_a_waiting_return(key_return, destination):
    r = key_return
    [order] = return_orders(r)
    if destination == 'first':
        usage(r.svc, 'third', 1.0)
    # No handoff turn can run on this key once the ledger proves its budget is spent.
    r.seat.ship.pools['second'] = replace(r.seat.pool, budget='0.000001 USD/day')
    r.svc.account()
    assert quota.current_walled(r.seat, r.svc.ledger)
    r.svc.fresh_handoffs()
    r.svc.failover()
    assert r.seat.pool.name == destination and len(r.launches) == 2
    assert not return_reports(r)
    r.svc.fresh_handoffs()
    assert not r.svc.store.is_open(order)


def test_a_different_launch_cannot_use_the_staged_return(key_return):
    r = key_return
    [order] = return_orders(r)
    handoff = report_return_handoff(r, order)
    r.created[-1].end('Handoff ready.')
    r.seat.fresh()
    replacement = r.seat.launched()
    assert replacement.pool == 'second' and len(r.launches) == 2
    r.svc.fresh_handoffs()
    assert r.seat.launched() == replacement and len(r.launches) == 2
    assert fresh_handoff.load(r.svc.runtime, r.seat.name)['phase'] == 'superseded'
    assert r.svc.store.is_open(handoff) and not return_reports(r)


def test_an_unconfirmed_return_is_held_without_another_start(key_return, monkeypatch):
    r = key_return
    [order] = return_orders(r)
    report_return_handoff(r, order)
    r.created[-1].end('Handoff ready.')
    starts = []

    def refuse(pool):
        starts.append(pool)
        raise ShipError('synthetic launch failure')

    monkeypatch.setattr(r.seat, '_fresh_on_pool', refuse)
    for _ in range(3):
        r.svc.fresh_handoffs()
        r.svc.failover()
    assert starts == ['first'] and return_orders(r) == [order]
    assert r.seat.pool.name == 'second' and not return_reports(r)
    assert any('return to first held: new conversation launch outcome unknown' in line
               for line in watch.service_holds(r.svc.runtime))


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
