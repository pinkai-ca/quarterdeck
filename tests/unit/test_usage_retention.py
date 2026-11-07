"""Synthetic measurements: one atomic latest-good tuple and one latest verdict per window."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace

import pytest

from quarterdeck import service, watch
from quarterdeck.harness import antigravity
from quarterdeck.harness.antigravity_quota import parse_usage, unknown, wall
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ledger import Ledger, pools_table
from quarterdeck.service import Service
from quarterdeck.ship import Pool
from .test_antigravity_quota import document
from .test_quota_refresh import declared_ship

AT = datetime(2030, 1, 1, tzinfo=timezone.utc)


def reading(at=AT, used=.25, **changes):
    return replace(Window('7d', used, at+timedelta(days=7), group='Gemini 4 Argon',
                          bucket='synthetic-original'), **changes)


def rows(book):
    return {r.window: r for r in book.usage.rows('google')}


def tuple_of(row):
    return row.used, row.resets_at, row.read_at, row.group, row.bucket, row.bucket_name


def test_first_failure_then_good_failure_restart_and_recovery(tmp_path):
    path = tmp_path/'ledger.sqlite'
    book = Ledger(path)
    book.record_usage('google', unknown('synthetic first failure', AT))
    first = rows(book)['7d']
    assert tuple_of(first)[:3] == (None, None, None)
    assert 'usage unknown' in first.describe(AT) and 'LAST KNOWN' not in first.describe(AT)
    book.record_usage('google', Usage((reading(),), AT))
    original = tuple_of(rows(book)['7d'])
    tables = book.db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    for index in range(1, 4):
        failed_at = AT+timedelta(minutes=5*index)
        book.record_usage('google', unknown(f'synthetic failure {index}', failed_at))
        book.close()
        book = Ledger(path)
        retained = rows(book)['7d']
        assert tuple_of(retained) == original
        assert retained.attempted_at == failed_at.isoformat()
        assert retained.error == f'synthetic failure {index}'
        assert not retained.current(failed_at)
        text = retained.describe(failed_at)
        assert 'LAST KNOWN' in text and f'({5*index}m ago)' in text and AT.isoformat() in text
        assert book.db.execute('SELECT count(*) FROM pools').fetchone() == (1,)
        assert book.db.execute('SELECT count(*) FROM usage_reads').fetchone() == (1,)
    assert book.db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall() == tables
    recovered_at = AT+timedelta(minutes=20)
    book.record_usage('google', Usage((reading(recovered_at, .4),), recovered_at))
    fresh = rows(book)['7d']
    assert tuple_of(fresh)[:3] == (.4, (recovered_at+timedelta(days=7)).isoformat(), recovered_at.isoformat())
    assert fresh.error is None and fresh.current(recovered_at)
    assert 'LAST KNOWN' not in fresh.describe(recovered_at)
    assert book.quota.usage_notes() == []
    assert book.db.execute('SELECT count(*) FROM pools').fetchone() == (1,)
    book.close()


@pytest.mark.parametrize('changes', [
    {'used': None}, {'used': True}, {'used': float('nan')}, {'used': float('inf')},
    {'used': -0.1}, {'used': 1.1}, {'used': 10**500}, {'used': '0.3'},
    {'resets_at': None}, {'resets_at': 'invalid'}, {'resets_at': AT.replace(tzinfo=None)},
    {'resets_at': AT-timedelta(seconds=1)},
])
def test_invalid_component_never_splices_or_replaces_complete_tuple(tmp_path, changes):
    book = Ledger(tmp_path/'ledger.sqlite')
    book.record_usage('google', Usage((reading(),), AT))
    before = tuple_of(rows(book)['7d'])
    later = AT+timedelta(minutes=5)
    book.record_usage('google', Usage((reading(later, .7, **changes) if 'used' not in changes
                                    else reading(later, **changes),), later))
    retained = rows(book)['7d']
    assert tuple_of(retained) == before and retained.error and not retained.current(later)
    assert retained.attempted_at == later.isoformat()
    book.close()


@pytest.mark.parametrize('bad_time', [None, 'invalid', AT.replace(tzinfo=None)])
def test_invalid_reading_time_cannot_replace_good_tuple(tmp_path, bad_time):
    book = Ledger(tmp_path/'ledger.sqlite')
    book.record_usage('google', Usage((reading(),), AT))
    before = tuple_of(rows(book)['7d'])
    attempted_at = AT+timedelta(minutes=5)
    book.record_usage('google', Usage((reading(used=.6),), bad_time), attempted_at=attempted_at)
    assert tuple_of(rows(book)['7d']) == before
    assert rows(book)['7d'].error == 'reading time unavailable or invalid'
    assert rows(book)['7d'].attempted_at == attempted_at.isoformat()
    book.close()


def test_partial_group_bucket_loss_and_invalid_values_leave_valid_siblings_current(tmp_path):
    book = Ledger(tmp_path/'ledger.sqlite')
    book.record_usage('google', parse_usage(document(), AT))
    before = {k: tuple_of(v) for k,v in rows(book).items()}
    doc = document()
    groups = doc['command']['data']['groups']
    groups[0]['buckets'].pop(0)  # missing Gemini weekly
    groups[1]['buckets'][0]['remaining_fraction'] = None  # invalid Claude/GPT weekly
    groups[2]['buckets'][0]['remaining_fraction'] = .5  # good Argon
    later = AT+timedelta(minutes=5)
    book.record_usage('google', parse_usage(doc, later))
    after = rows(book)
    assert len(after) == 5
    for name in ('Gemini Models / weekly', 'Claude and GPT models / weekly'):
        assert tuple_of(after[name]) == before[name]
        assert after[name].error and not after[name].current(later)
    for name in ('7d', 'Gemini Models / 5h', 'Claude and GPT models / 5h'):
        assert after[name].read_at == later.isoformat() and after[name].current(later)
    assert after['7d'].used == .5
    # Entire group disappearance retains both old windows, never silently deletes them.
    doc['command']['data']['groups'] = [groups[2]]
    book.record_usage('google', parse_usage(doc, later+timedelta(minutes=5)))
    assert len(rows(book)) == 5
    assert all(r.error for name,r in rows(book).items() if name != '7d')
    assert rows(book)['7d'].error is None
    book.close()


def test_identity_conflict_is_local_retains_original_id_and_recovers(tmp_path):
    book = Ledger(tmp_path/'ledger.sqlite')
    sibling = reading(name='other / weekly', blocks=False, group='other', bucket='sibling')
    book.record_usage('google', Usage((reading(), sibling), AT))
    before = tuple_of(rows(book)['7d'])
    later = AT+timedelta(minutes=5)
    book.record_usage('google', Usage((reading(later, .7, bucket='synthetic-changed'),
                                     replace(sibling, used=.4)), later))
    assert tuple_of(rows(book)['7d']) == before
    assert 'bucket identity changed' in rows(book)['7d'].error
    assert rows(book)['other / weekly'].current(later)
    assert rows(book)['other / weekly'].used == .4
    book.record_usage('google', Usage((reading(later, None, bucket='synthetic-changed'), sibling), later))
    assert 'bucket identity changed' in rows(book)['7d'].error
    assert tuple_of(rows(book)['7d']) == before
    book.record_usage('google', unknown('missing id failure', later+timedelta(minutes=5)))
    assert tuple_of(rows(book)['7d']) == before
    recovered_at = later+timedelta(minutes=10)
    book.record_usage('google', Usage((reading(recovered_at, .8), sibling), recovered_at))
    assert rows(book)['7d'].bucket == 'synthetic-original' and rows(book)['7d'].used == .8
    assert rows(book)['7d'].error is None and rows(book)['7d'].current(recovered_at)
    assert book.db.execute('SELECT count(*) FROM pools').fetchone() == (2,)
    book.close()


def test_never_read_ambiguous_duplicates_do_not_establish_measurement_or_identity(tmp_path):
    book = Ledger(tmp_path/'ledger.sqlite')
    doc = document()
    argon = doc['command']['data']['groups'][-1]
    argon['buckets'].append(dict(argon['buckets'][0], id='synthetic-conflict'))
    book.record_usage('google', parse_usage(doc, AT))
    ambiguous = rows(book)['7d']
    assert (ambiguous.used, ambiguous.resets_at, ambiguous.read_at, ambiguous.bucket) == (None, None, None, None)
    assert 'ambiguous duplicate' in ambiguous.error
    # A first unambiguous complete reading can establish the identity.
    book.record_usage('google', Usage((reading(bucket=None),), AT))
    assert rows(book)['7d'].bucket is None and rows(book)['7d'].current(AT)
    book.record_usage('google', Usage((reading(),), AT))
    assert rows(book)['7d'].bucket == 'synthetic-original'
    book.close()


def test_migration_preserves_existing_measurement_rows_and_identity(tmp_path):
    path = tmp_path/'ledger.sqlite'
    old = sqlite3.connect(path)
    old.execute('CREATE TABLE pools (pool TEXT, window TEXT, used REAL, resets_at TEXT, read_at TEXT, '
                'blocks INTEGER DEFAULT 1, group_name TEXT, bucket TEXT, bucket_name TEXT, PRIMARY KEY(pool,window))')
    values = ('google', '7d', .3, (AT+timedelta(days=7)).isoformat(), AT.isoformat(), 1,
              'Gemini 4 Argon', 'synthetic-original', 'Synthetic allowance')
    old.execute('INSERT INTO pools VALUES (?,?,?,?,?,?,?,?,?)', values)
    old.commit()
    old.close()
    book = Ledger(path)
    assert book.db.execute('SELECT pool,window,used,resets_at,read_at,blocks,group_name,bucket,bucket_name FROM pools').fetchone() == values
    assert rows(book)['7d'].attempted_at == AT.isoformat() and rows(book)['7d'].error is None
    book.record_usage('google', unknown('synthetic failure after migration', AT+timedelta(minutes=5)))
    assert book.db.execute('SELECT pool,window,used,resets_at,read_at,blocks,group_name,bucket,bucket_name FROM pools').fetchone() == values
    book.close()


def test_expired_and_aged_retained_values_are_dated_never_current(tmp_path):
    book = Ledger(tmp_path/'ledger.sqlite')
    book.record_usage('google', Usage((reading(resets_at=AT+timedelta(minutes=30)),), AT))
    retained = rows(book)['7d']
    expired = AT+timedelta(minutes=31)
    assert not retained.current(expired)
    assert 'LAST KNOWN' in retained.describe(expired) and 'reset has passed' in retained.describe(expired)
    assert AT.isoformat() in retained.describe(expired) and '(31m ago)' in retained.describe(expired)
    assert retained.resets_at == (AT+timedelta(minutes=30)).isoformat()
    book.record_usage('google', Usage((reading(),), AT))
    assert not rows(book)['7d'].current(AT+timedelta(hours=1))
    assert 'older than one hour' in rows(book)['7d'].describe(AT+timedelta(hours=1))
    book.close()


def test_service_retention_partial_cadence_selection_presentation_and_observed_wall(tmp_path, monkeypatch):
    ship = declared_ship(tmp_path, 'empty')
    calls = []
    doc = document()
    def fetch():
        calls.append(1)
        return parse_usage(doc, datetime.now(timezone.utc))
    monkeypatch.setattr(antigravity, 'fetch_usage', fetch)
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    svc = Service(ship, SimpleNamespace())
    svc.account()
    original = tuple_of(rows(svc.ledger)['7d'])
    # Selection and status cannot make provider calls, even for a reader without usage support.
    seat = SimpleNamespace(harness=SimpleNamespace(reads_usage=False))
    assert svc.known_open(seat, 'google')
    doc['command']['data']['groups'][-1]['buckets'][0]['id'] = 'synthetic-conflict'
    svc.ledger.quota.usage_read('google', datetime.now(timezone.utc)-timedelta(seconds=301), None)
    svc.account()
    assert tuple_of(rows(svc.ledger)['7d']) == original
    assert 'bucket identity changed' in svc.ledger.quota.usage_notes()[0], 'caller must not overwrite cache diagnostics'
    assert not svc.known_open(seat, 'google')
    assert 'LAST KNOWN' in watch.ledger_usage(ship)[0]
    assert original[2] in watch.ledger_usage(ship)[0]
    assert 'LAST KNOWN' in '\n'.join(pools_table(svc.ledger))
    assert len(calls) == 2
    svc.ledger.close()
    svc = Service(ship, SimpleNamespace())
    svc.account()
    assert len(calls) == 2 and tuple_of(rows(svc.ledger)['7d']) == original
    # A valid Argon with an invalid sibling is independently current and available.
    doc['command']['data']['groups'][-1]['buckets'][0]['id'] = 'synthetic-argon-week'
    doc['command']['data']['groups'][0]['buckets'][0]['remaining_fraction'] = None
    svc.ledger.quota.usage_read('google', datetime.now(timezone.utc)-timedelta(seconds=301), None)
    svc.account()
    assert svc.known_open(seat, 'google') and 'LAST KNOWN' not in watch.ledger_usage(ship)[0]
    assert 'LAST KNOWN' in '\n'.join(pools_table(svc.ledger))
    now = datetime.now(timezone.utc)
    event = wall('Individual quota reached. Resets in 2h.', 'synthetic-event', now)
    svc.ledger.quota.observe('synthetic-record', 'worker', 'google', event, now)
    observed = svc.ledger.db.execute('SELECT * FROM wall_events').fetchall()
    prior_tuples = {k: tuple_of(v) for k,v in rows(svc.ledger).items()}
    monkeypatch.setattr(antigravity, 'fetch_usage', lambda: unknown('synthetic timeout', datetime.now(timezone.utc)))
    svc.account()
    assert {k: tuple_of(v) for k,v in rows(svc.ledger).items()} == prior_tuples
    assert not svc.known_open(seat, 'google') and svc.ledger.walled('google')
    assert svc.ledger.db.execute('SELECT * FROM wall_events').fetchall() == observed
    assert len(rows(svc.ledger)) == 5 and not svc.ledger.pool_windows('codex')
    assert 'synthetic timeout' in watch.ledger_usage(ship)[0]
    svc.ledger.close()


@pytest.mark.parametrize('failure_kind', ['none', 'empty', 'partial'])
def test_inactive_non_google_fallback_recovers_at_attempt_cadence_across_restart(tmp_path, monkeypatch, failure_kind):
    ship = declared_ship(tmp_path, 'empty')
    ship.pools['fallback'] = Pool('fallback', 'claude')
    now = datetime.now(timezone.utc)
    clock = [now]
    monkeypatch.setattr(service.post, 'now', lambda: clock[0])
    svc = Service(ship, SimpleNamespace())
    good_at = now-timedelta(minutes=10)
    window = Window('5h', .2, now+timedelta(hours=2))
    svc.ledger.record_usage('fallback', Usage((window,), good_at))
    original = svc.ledger.pool_windows('fallback')
    svc.ledger.record_usage('fallback', Usage((), now-timedelta(minutes=2), 'synthetic timeout'))
    assert svc.ledger.pool_windows('fallback') == original
    calls = []
    outcomes = [None] if failure_kind == 'none' else [
        Usage((), now, 'synthetic timeout') if failure_kind == 'empty'
        else Usage((replace(window, resets_at=None),), now)]
    def fetch(pool):
        assert pool.name == 'fallback'
        calls.append(clock[0])
        return outcomes.pop(0) if outcomes else Usage((replace(window, used=.3),), clock[0])
    seat = SimpleNamespace(name='worker', harness=SimpleNamespace(reads_usage=True, usage=fetch))
    # The old good timestamp is only ten minutes old; retry must not wait an hour.
    assert not svc.known_open(seat, 'fallback') and calls == [now]
    assert svc.ledger.pool_windows('fallback') == original
    assert svc.ledger.db.execute("SELECT at FROM usage_reads WHERE pool='fallback'").fetchone() == (now.isoformat(),)
    for seconds in (0, 30, 59):
        clock[0] = now+timedelta(seconds=seconds)
        assert not svc.known_open(seat, 'fallback')
    assert len(calls) == 1
    svc.ledger.close()
    svc = Service(ship, SimpleNamespace())
    assert not svc.known_open(seat, 'fallback') and len(calls) == 1
    assert svc.ledger.pool_windows('fallback') == original
    clock[0] = now+timedelta(seconds=60)
    assert svc.known_open(seat, 'fallback') and calls == [now, clock[0]]
    fresh = svc.ledger.usage.rows('fallback')[0]
    assert fresh.used == .3 and fresh.read_at == clock[0].isoformat() and fresh.error is None
    # A successful fresh cache does not query each tick or at the retry boundary.
    clock[0] = now+timedelta(minutes=3)
    assert svc.known_open(seat, 'fallback') and len(calls) == 2
    assert svc.ledger.db.execute("SELECT count(*) FROM pools WHERE pool='fallback'").fetchone() == (1,)
    svc.ledger.close()


def test_fallback_key_and_no_usage_cases_do_not_query_provider(tmp_path):
    ship = declared_ship(tmp_path, 'empty')
    ship.pools['key'] = Pool('key', 'claude', api_key='env:SYNTHETIC_KEY')
    ship.pools['unmeasured'] = Pool('unmeasured', 'fake')
    svc = Service(ship, SimpleNamespace())
    def forbidden(pool):
        raise AssertionError('key/no-usage pool must not query subscription usage')
    seat = SimpleNamespace(harness=SimpleNamespace(reads_usage=True, usage=forbidden))
    assert svc.known_open(seat, 'key')
    seat.harness.reads_usage = False
    assert svc.known_open(seat, 'unmeasured')
    assert not svc.ledger.pool_windows()
    svc.ledger.close()
