"""Synthetic subscription observations; no conversation or account data."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import harness, post, quota, watch, output
from quarterdeck.ledger import Ledger
from quarterdeck.seat import Seat, Launched
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship

AT = datetime(2032, 1, 1, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parents[1] / 'fixtures/codex/0.166.0/usage.jsonl'


def window(minutes=10080, used=42):
    return dict(window_minutes=minutes, used_percent=used, resets_at=int((AT + timedelta(days=7)).timestamp()))


def event(at=AT, primary=None, secondary=None, **extra):
    return dict(timestamp=at.isoformat(), type='event_msg', payload=dict(type='token_count', rate_limits={
        'limit_id': 'codex', 'primary': primary, 'secondary': secondary, **extra}))


def write(path, *events):
    path.write_text(''.join(json.dumps(e) + '\n' for e in events))
    return path


def read(sources, now=AT):
    return harness.get('codex').usage_from_records(sources, now)


def rows(usage):
    return {w.name: w for w in usage.windows}


def test_installed_record_shape_primary_is_seven_days_not_five_hours():
    usage = read([(FIXTURE, AT)])
    assert usage.read_at == AT
    assert rows(usage)['7d'].used == .42
    assert rows(usage)['7d'].resets_at == AT + timedelta(days=7)
    assert rows(usage)['5h'].used is None and rows(usage)['5h'].error


@pytest.mark.parametrize('primary,secondary,expected', [
    (window(300, 0), None, {'5h': 0, '7d': None}),
    (window(10080, 42), window(300, 10), {'5h': .1, '7d': .42}),
    (window(300, 10), window(10080, 42), {'5h': .1, '7d': .42}),
])
def test_duration_names_windows_and_zero_is_measured(tmp_path, primary, secondary, expected):
    usage = read([(write(tmp_path/'record', event(primary=primary, secondary=secondary)), AT)])
    assert {name: row.used for name, row in rows(usage).items()} == expected


def test_newest_snapshot_is_not_newest_complete_or_cross_seat_assembly(tmp_path):
    old = write(tmp_path/'old', event(primary=window(300, 20), secondary=window()))
    new = write(tmp_path/'new', event(AT + timedelta(seconds=1), primary=window(300, 30)))
    for sources in ([(old, AT), (new, AT)], [(new, AT), (old, AT)]):
        usage = read(sources, AT + timedelta(seconds=2))
        assert usage.read_at == AT + timedelta(seconds=1)
        assert rows(usage)['5h'].used == .3 and rows(usage)['7d'].used is None


def test_latest_malformed_window_retains_whole_previous_tuple(tmp_path):
    path = write(tmp_path/'record', event(primary=window()))
    with Ledger(tmp_path/'ledger.sqlite') as book:
        book.record_usage('subscription', read([(path, AT)]), attempted_at=AT)
        before = book.usage.rows('subscription')[1]
        write(path, event(primary=window()), event(AT + timedelta(seconds=1), primary=window(10080, 'bad'), secondary=window(300, 0)))
        book.record_usage('subscription', read([(path, AT)], AT + timedelta(seconds=2)), attempted_at=AT+timedelta(seconds=2))
        five, seven = book.usage.rows('subscription')
        assert five.used == 0 and five.current(AT + timedelta(seconds=2))
        assert (seven.used, seven.resets_at, seven.read_at) == (before.used, before.resets_at, before.read_at)
        assert seven.error and not seven.current(AT + timedelta(seconds=2))


@pytest.mark.parametrize('bad', [None, True, -1, 101, 10**400, '42', float('nan'), float('inf')])
def test_invalid_percent_is_unknown(tmp_path, bad):
    usage = read([(write(tmp_path/'record', event(primary=window(10080, bad))), AT)])
    assert rows(usage)['7d'].used is None and rows(usage)['7d'].error


@pytest.mark.parametrize('field,bad', [('resets_at', None), ('resets_at', True), ('resets_at', 'bad'),
                                     ('resets_at', 1e99), ('resets_at', AT.timestamp()),
                                     ('window_minutes', True), ('window_minutes', '10080'), ('window_minutes', 60)])
def test_bad_reset_or_unrecognized_duration_cannot_supply_a_measurement(tmp_path, field, bad):
    value = window(); value[field] = bad
    usage = read([(write(tmp_path/'record', event(primary=value)), AT)])
    assert all(w.used is None for w in usage.windows)


def test_prelaunch_and_other_bucket_do_not_supply_pool_usage(tmp_path):
    path = write(tmp_path/'record', event(AT-timedelta(seconds=1), primary=window()),
                 event(primary=window(), limit_id='another-meter'))
    assert all(w.used is None for w in read([(path, AT)]).windows)


@pytest.mark.parametrize('timestamp', [None, 'bad', '2032-01-01T00:00:00', '2033-01-01T00:00:00Z'])
def test_unplaceable_or_future_observation_does_not_bless_older_snapshot(tmp_path, timestamp):
    bad = event(primary=window()); bad['timestamp'] = timestamp
    usage = read([(write(tmp_path/'record', event(primary=window()), bad), AT)])
    assert all(w.used is None and w.error for w in usage.windows)


@pytest.mark.parametrize('kind', ['missing', 'nontext', 'empty', 'null', 'malformed', 'ambiguous'])
def test_absent_failed_and_ambiguous_source_is_explicitly_unknown(tmp_path, kind):
    path = tmp_path/'record'
    if kind == 'nontext': path.write_bytes(b'\xff')
    if kind == 'empty': path.write_text('')
    if kind == 'null':
        e = event(); e['payload']['rate_limits'] = None; write(path, e)
    if kind == 'malformed': path.write_text('not JSON')
    if kind == 'ambiguous': write(path, event(primary=window()), event(primary=window(10080, 25)))
    usage = read([(path, AT)])
    assert all(w.used is None and w.error for w in usage.windows)


def test_older_snapshot_cannot_overwrite_retained_newer_observation(tmp_path):
    path = tmp_path/'record'
    with Ledger(tmp_path/'ledger.sqlite') as book:
        for offset, used in [(10, 50), (0, 20)]:
            write(path, event(AT + timedelta(seconds=offset), primary=window(10080, used)))
            book.record_usage('subscription', read([(path, AT)], AT+timedelta(seconds=20)), attempted_at=AT+timedelta(seconds=30))
        seven = book.usage.rows('subscription')[1]
        assert seven.used == .5 and seven.read_at == (AT+timedelta(seconds=10)).isoformat()
        assert seven.error and 'older' in seven.error


def make_service(tmp_path):
    pools = {name: Pool(name, 'openai') for name in ('one', 'two')}
    specs = {name: SeatSpec(name, 'crew', name, 'codex', ('one', 'two'), up=False) for name in ('alpha', 'beta')}
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, pools, {'crew': Session('crew', 'crew')}, specs, code='checkout')
    return Service(ship, SimpleNamespace())


def launch(svc, name, pool, at, *events):
    seat = Seat(svc.ship, svc.ship.seats[name], svc.tmux)
    seat.folder.mkdir(parents=True, exist_ok=True)
    path = write(seat.folder/'synthetic.jsonl', *events)
    Launched('%0', 1, at.isoformat(), pool, None, ['codex'], '0.166.0', 'codex').write(seat.folder/'launch.json')
    seat._note_owned(path)
    (seat.folder/'record').write_text(str(path))
    return seat, path


def test_service_uses_saved_pool_and_postlaunch_current_records_only(tmp_path, monkeypatch):
    monkeypatch.setattr(quota, "datetime", SimpleNamespace(now=lambda tz: AT + timedelta(minutes=1)))
    svc = make_service(tmp_path)
    a, _ = launch(svc, 'alpha', 'one', AT, event(primary=window(10080, 25)))
    b, path = launch(svc, 'beta', 'two', AT+timedelta(seconds=1), event(primary=window(10080, 99)))
    a.move_to('two')  # current selection is not the saved paying launch
    quota.refresh_declared(svc)
    assert svc.ledger.usage.rows('one')[1].used == .25
    assert all(row.used is None for row in svc.ledger.usage.rows('two'))
    write(path, event(primary=window(10080, 99)), event(AT+timedelta(seconds=61), primary=window(10080, 60)))
    monkeypatch.setattr(quota, 'datetime', SimpleNamespace(now=lambda tz: AT + timedelta(seconds=62)))
    svc.ledger.db.execute('DELETE FROM usage_reads'); svc.ledger.db.commit()
    quota.refresh_declared(svc)
    assert svc.ledger.usage.rows('two')[1].used == .6
    assert svc.ledger.usage.rows('one')[1].used == .25
    svc.ledger.close()


def test_status_headlines_preserve_both_windows_outside_the_text_line(tmp_path, monkeypatch):
    svc = make_service(tmp_path)
    svc.ship.pools.pop('two')
    monkeypatch.setattr(post, 'now', lambda: AT)
    svc.ledger.record_usage('one', read([(FIXTURE, AT)]), attempted_at=AT)
    entries = watch.headline_readings(svc.ship)
    assert [row['reading'].window for row in entries] == ['5h', '7d']
    full = '\n'.join(watch.render_headlines(entries))
    assert 'one 5h usage unknown' in full and 'one 7d 42% used' in full
    facts = watch.Facts('ok', 0, 0, 0, walls=['synthetic quota wall with reset 2032-01-01 05:00 UTC padding long padding'], usage=entries)
    line = facts.line()
    assert line == 'service ok · 0 waiting'
    assert len(output.value(facts)['usage']) == 2
    svc.ledger.close()


def test_repeated_passive_read_keeps_observation_time_and_expires(tmp_path):
    path = write(tmp_path/'record', event(primary=window()))
    with Ledger(tmp_path/'ledger.sqlite') as book:
        for minutes in (0, 61):
            now = AT + timedelta(minutes=minutes)
            book.record_usage('one', read([(path, AT)], now), attempted_at=now)
            seven = book.usage.rows('one')[1]
            assert seven.read_at == AT.isoformat() and seven.current(now) == (minutes == 0)
        assert 'older than one hour' in seven.reason(now)
        assert 'reset has passed' in seven.reason(AT+timedelta(days=7))


def test_timezone_and_duplicate_window_identity(tmp_path):
    e = event(primary=window())
    e['timestamp'] = '2031-12-31T16:00:00-08:00'
    assert read([(write(tmp_path/'record', e), AT)]).read_at == AT
    usage = read([(write(tmp_path/'record', event(primary=window(), secondary=window())), AT)])
    with Ledger(tmp_path/'ledger.sqlite') as book:
        book.record_usage('one', usage, attempted_at=AT)
        assert book.usage.rows('one')[1].error == 'ambiguous duplicate bucket'


def test_restart_without_snapshot_retains_dated_measurement_and_missing_join_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(quota, 'datetime', SimpleNamespace(now=lambda tz: AT + timedelta(minutes=1)))
    svc = make_service(tmp_path)
    seat, path = launch(svc, 'alpha', 'one', AT, event(primary=window()))
    quota.refresh_declared(svc)
    Launched('%0', 1, (AT+timedelta(seconds=1)).isoformat(), 'one', None,
             ['codex'], '0.166.0', 'codex').write(seat.folder/'launch.json')
    for missing in (False, True):
        if missing: path.unlink()
        svc.ledger.db.execute('DELETE FROM usage_reads'); svc.ledger.db.commit()
        quota.refresh_declared(svc)
        seven = svc.ledger.usage.rows('one')[1]
        assert seven.used == .42 and seven.read_at == AT.isoformat() and seven.error
    svc.ledger.close()


def test_service_newest_seat_snapshot_wins_and_old_unjoined_records_do_not(tmp_path, monkeypatch):
    monkeypatch.setattr(quota, 'datetime', SimpleNamespace(now=lambda tz: AT + timedelta(minutes=1)))
    svc = make_service(tmp_path)
    launch(svc, 'alpha', 'one', AT, event(primary=window()))
    beta, _ = launch(svc, 'beta', 'one', AT, event(AT+timedelta(seconds=1), primary=window(300, 0)))
    write(beta.home/'unused.jsonl', event(AT+timedelta(seconds=30), primary=window(10080, 99)))
    quota.refresh_declared(svc)
    five, seven = svc.ledger.usage.rows('one')
    assert five.used == 0 and seven.used is None
    assert all(row.used is None for row in svc.ledger.usage.rows('two'))
    svc.ledger.close()


@pytest.mark.parametrize('kind', ['seven', 'five', 'both', 'partial', 'never'])
def test_cli_status_and_line_json_are_cache_only_and_include_both_windows(tmp_path, monkeypatch, capsys, kind):
    from quarterdeck import cli
    from quarterdeck.ship import load
    (tmp_path/'ship.toml').write_text('[ship]\ncode="checkout"\nname="synthetic"\n[pools.one]\nprovider="openai"\n')
    ship = load(tmp_path)
    monkeypatch.delenv('QD_SHIP', raising=False)
    monkeypatch.delenv('QD_SEAT', raising=False)
    monkeypatch.delenv('QD_TMUX_SOCKET', raising=False)  # no server: the test ship's own, never started
    monkeypatch.setattr(post, 'now', lambda: AT)
    def forbidden(*args):
        raise AssertionError('status must read only cached usage')
    monkeypatch.setattr(type(harness.get('codex')), 'usage_from_records', staticmethod(forbidden))
    if kind != 'never':
        from quarterdeck.harness.codex_usage import snapshot
        primary = window(10080, 'bad' if kind == 'partial' else 42) if kind != 'five' else None
        secondary = window(300, 0) if kind != 'seven' else None
        with Ledger(ship.runtime/'ledger.sqlite') as book:
            book.record_usage('one', snapshot({'primary': primary, 'secondary': secondary}, AT), attempted_at=AT)
    for verb in (['status'], ['status', '--line']):
        assert cli.main(['--ship', str(tmp_path), '--json', *verb]) == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data['usage']) == 2
        assert cli.main(['--ship', str(tmp_path), *verb]) == 0
        text = capsys.readouterr().out
        if '--line' in verb:
            assert 'one 5h' not in text and 'one 7d' not in text and 'usage' not in text
        else:
            assert 'one 5h' in text and 'one 7d' in text
            if kind in ('five', 'both', 'partial'): assert 'one 5h 0%' in text
            else: assert 'one 5h' in text and 'unknown' in text
            if kind in ('seven', 'both'): assert 'one 7d 42%' in text
        if '--line' in verb: assert len(text.rstrip()) <= 160


def test_equal_retained_snapshot_cannot_clear_a_newer_failed_observation(tmp_path):
    path = tmp_path/'record'
    with Ledger(tmp_path/'ledger.sqlite') as book:
        for offset, used in [(0, 42), (1, None), (0, 42)]:
            write(path, event(AT+timedelta(seconds=offset), primary=window(10080, used)))
            book.record_usage('one', read([(path, AT)], AT+timedelta(seconds=2)), attempted_at=AT+timedelta(seconds=3))
        seven = book.usage.rows('one')[1]
        assert seven.used == .42 and seven.read_at == AT.isoformat() and seven.error
        write(path, event(AT+timedelta(seconds=4), primary=window(10080, 50)))
        book.record_usage('one', read([(path, AT)], AT+timedelta(seconds=5)), attempted_at=AT+timedelta(seconds=5))
        assert book.usage.rows('one')[1].used == .5 and book.usage.rows('one')[1].error is None


def test_crowded_codex_cli_keeps_both_windows_in_full_text_and_json(tmp_path, monkeypatch, capsys):
    from quarterdeck import cli
    from quarterdeck.ship import load
    (tmp_path/'ship.toml').write_text('[ship]\ncode="checkout"\nname="synthetic"\n[pools.one]\nprovider="openai"\n')
    ship = load(tmp_path)
    with Ledger(ship.runtime/'ledger.sqlite') as book:
        book.record_usage('one', read([(FIXTURE, AT)]), attempted_at=AT)
    monkeypatch.setattr(post, 'now', lambda: AT)
    monkeypatch.delenv('QD_SHIP', raising=False)
    monkeypatch.delenv('QD_SEAT', raising=False)
    monkeypatch.delenv('QD_TMUX_SOCKET', raising=False)  # no server: the test ship's own, never started
    facts = watch.Facts('ok', 0, 0, 0,
                        walls=['synthetic quota wall with reset 2032-01-01 05:00 UTC padding long padding'],
                        usage=watch.headline_readings(ship))
    monkeypatch.setattr(watch, 'facts', lambda *args: facts)
    for verb in (['status'], ['status', '--line']):
        assert cli.main(['--ship', str(tmp_path), *verb]) == 0
        text = capsys.readouterr().out
        if '--line' in verb:
            assert text.rstrip() == 'service ok · 0 waiting'
        else:
            assert 'one 7d 42%' in text and 'one 5h usage unknown' in text
        assert cli.main(['--ship', str(tmp_path), '--json', *verb]) == 0
        data = json.loads(capsys.readouterr().out)
        assert [row['reading']['window'] for row in data['usage']] == ['5h', '7d']


@pytest.mark.parametrize('minutes', [300, 10080])
def test_reported_spent_window_is_a_wall_without_request_failure(tmp_path, minutes):
    path = write(tmp_path/'record', event(primary=window(minutes, 100)))
    with Ledger(tmp_path/'ledger.sqlite') as book:
        book.record_usage('one', read([(path, AT)]), attempted_at=AT)
        assert any(line.startswith('one ' + {300:'5h', 10080:'7d'}[minutes]) for line in book.walls(now=AT))
        assert not book.usage_known('one'), 'an absent sibling is not evidence for moving onto this pool'
        assert book.quota.active(AT) == [], 'no failed request was needed to see this wall'
