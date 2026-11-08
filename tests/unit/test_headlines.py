"""Subscription headlines are cached data, complete in JSON and bounded on a line."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from quarterdeck import cli, harness, post, watch
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ledger import Ledger
from quarterdeck.ship import load

AT = datetime(2032, 1, 1, tzinfo=timezone.utc)


def ship_at(path):
    (path / 'ship.toml').write_text('''[ship]
name="synthetic"
[pools.claude]
provider="claude"
[pools.google]
provider="antigravity"
[pools.unsupported]
provider="pi"
[pools.key]
provider="claude"
api_key="env:SYNTHETIC_UNUSED"
[pools.local]
provider="local"
endpoint="http://127.0.0.1:1/v1"
''')
    return load(path)


def seed(ship):
    with Ledger(ship.runtime / 'ledger.sqlite') as book:
        book.record_usage('claude', Usage((Window('5h', .25, AT + timedelta(hours=5)),
                                          Window('7d', .45, AT + timedelta(days=7))), AT))
        book.record_usage('google', Usage((Window('7d', .75, AT + timedelta(days=7), group='Gemini 4 Argon'),
                                          Window('other', 1, AT + timedelta(days=7), False, 'Other')), AT))


@pytest.mark.parametrize('state', ['fresh', 'stale', 'failed', 'never', 'first-failure'])
def test_no_seat_headlines_are_read_only_complete_data_in_text_and_json(tmp_path, monkeypatch, capsys, state):
    ship = ship_at(tmp_path)
    monkeypatch.delenv('QD_SHIP', raising=False)
    monkeypatch.delenv('QD_SEAT', raising=False)
    monkeypatch.setenv('QD_TMUX_SOCKET', 'qd-headline-no-server')
    now = AT + timedelta(minutes=2 if state != 'stale' else 61)
    monkeypatch.setattr(post, 'now', lambda: now)
    for name in ('claude', 'antigravity'):
        def forbidden(*args):
            raise AssertionError('a reading must not fetch provider usage')
        monkeypatch.setattr(harness.get(name), 'usage', forbidden)
    if state == 'first-failure':
        with Ledger(ship.runtime / 'ledger.sqlite') as book:
            book.record_usage('claude', Usage((), now, 'synthetic initial failure'))
    elif state != 'never':
        seed(ship)
        if state == 'failed':
            with Ledger(ship.runtime / 'ledger.sqlite') as book:
                book.record_usage('claude', Usage((), now, 'synthetic failure'))
    path = ship.runtime / 'ledger.sqlite'
    before = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
    rows = watch.headline_readings(ship)
    assert [r['pool'] for r in rows] == ['claude', 'google']
    if state in ('never', 'first-failure'):
        assert all(r['reading'] is None for r in rows)
        assert [r['window'] for r in rows] == ['5h', '7d']
        if state == 'first-failure':
            assert rows[0]['attempted_at'] is not None and 'synthetic initial failure' in rows[0]['reason']
    else:
        assert rows[0]['reading'].used == .25 and rows[0]['reading'].read_at == AT.isoformat()
        assert rows[0]['current'] == (state == 'fresh')
    for verb in (['status'], ['watch'], ['status', '--line']):
        assert cli.main(['--ship', str(tmp_path), '--json', *verb]) == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data['usage']) == 2
        if state not in ('never', 'first-failure'):
            assert data['usage'][0]['reading']['used'] == .25
            assert data['usage'][0]['current'] == (state == 'fresh')
        assert cli.main(['--ship', str(tmp_path), *verb]) == 0
        text = capsys.readouterr().out
        assert 'claude' in text and 'google' in text
        if state in ('stale', 'failed'):
            assert 'LAST KNOWN' in text
        if verb == ['status', '--line']:
            assert len(text.rstrip('\n')) <= 160 and 'reset' in text
    if before:
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        with Ledger.reading(path) as book:
            assert len(book.usage.rows()) == (0 if state == 'first-failure' else 4), 'non-headline windows remain available'
    else:
        assert not path.exists()


def test_overflow_names_omitted_usage_and_keeps_every_alert_category(tmp_path, monkeypatch):
    ship = ship_at(tmp_path)
    seed(ship)
    monkeypatch.setattr(post, 'now', lambda: AT)
    rows = watch.headline_readings(ship)
    ordinary = watch.Facts('ok', 0, 0, 0, usage=rows)
    assert len(ordinary.line()) <= 160
    assert 'claude 5h 25%' in ordinary.line() and 'google 7d 75%' in ordinary.line()
    many = watch.Facts('down', 2, 4, 1, needs_human=['long-seat-name' * 20],
                       walls=['long wall reason' * 20], pending=['pending uncertain' * 20],
                       unarmed=['other'], disks=[('disk', 95)], login=[('pool', 'soon')],
                       queued=2, usage=rows * 10)
    line = many.line()
    for label in ('SERVICE DOWN', 'waiting', 'queued', 'blind', 'need human', 'unarmed',
                  'walls', 'pending', 'disk', 'login', 'usage', 'qd status'):
        assert label in line
    assert len(line) <= 160
    assert len(many.usage) == 20 and many.walls == ['long wall reason' * 20]
