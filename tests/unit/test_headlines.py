"""Subscription readings stay complete in detailed text and JSON."""

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
provider="anthropic"
[pools.google]
provider="google"
[pools.unsupported]
provider="synthetic"  # native program without a usage reader
[pools.key]
provider="anthropic"
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
    monkeypatch.delenv('QD_TMUX_SOCKET', raising=False)  # no server: the test ship's own, never started
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
    # Each Google group is its own headline once read; before any reading the pool shows one unknown line.
    expected = ['claude', 'google'] if state in ('never', 'first-failure') else ['claude', 'google', 'google']
    assert [r['pool'] for r in rows] == expected
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
        assert len(data['usage']) == len(expected)
        if state not in ('never', 'first-failure'):
            assert data['usage'][0]['reading']['used'] == .25
            assert data['usage'][0]['current'] == (state == 'fresh')
        assert cli.main(['--ship', str(tmp_path), *verb]) == 0
        text = capsys.readouterr().out
        if verb == ['status', '--line']:
            assert len(text.rstrip('\n')) <= 160
            assert 'claude' not in text and 'google' not in text and 'reset' not in text and 'usage' not in text
        else:
            assert 'claude' in text and 'google' in text
            if state in ('stale', 'failed'):
                assert 'LAST KNOWN' in text
    if before:
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        with Ledger.reading(path) as book:
            assert len(book.usage.rows()) == (0 if state == 'first-failure' else 4), 'non-headline windows remain available'
    else:
        assert not path.exists()


def test_alert_overflow_counts_held_seats_and_keeps_allowance_facts(tmp_path, monkeypatch):
    ship = ship_at(tmp_path)
    seed(ship)
    monkeypatch.setattr(post, 'now', lambda: AT)
    rows = watch.headline_readings(ship)
    ordinary = watch.Facts('ok', 0, 0, 0, usage=rows)
    assert len(ordinary.line()) <= 160
    assert ordinary.line() == 'service ok · 0 waiting · qd status'
    many = watch.Facts('down', 2, 4, 1, needs_human=['long-seat-name' * 20],
                       held=['worker'], walls=['long wall reason' * 20], pending=['pending uncertain' * 20],
                       unarmed=['other'], disks=[('disk', 95)], login=[('pool', 'soon')],
                       queued=2, usage=rows * 10)
    line = many.line()
    for label in ('SERVICE DOWN', 'waiting', 'queued', 'blind', '1 needs a human', 'unarmed',
                  '1 limit hold', 'pending', 'disk: disk 95%', 'pool login needed by soon'):
        assert label in line
    assert 'walls' not in line and 'usage' not in line
    assert len(line) <= 160 and line.endswith(' · qd status')
    assert len(many.usage) == 10 * len(rows) and many.walls == ['long wall reason' * 20]
