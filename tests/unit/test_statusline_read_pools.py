"""The footer selects observed pools; detailed readings retain the declaration."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from quarterdeck import ledger_reading, output, watch
from quarterdeck.harness import claude_statusline as line
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ledger import Ledger
from quarterdeck.ship import Pool, Ship
from .test_statusline_default import cache, plain, window


def test_unread_own_pool_and_never_used_declaration_do_not_get_cells():
    ship = {'pools': {'sample-read': {}, 'sample-unread': {}, 'sample-never-used': {}}}
    data = cache([window('sample-read', .06)])
    data['seats'] = {'sample-seat': 'sample-read'}
    rows = line.allowances(data, ship, 'sample-unread')
    assert [row['pool'] for row in rows] == ['sample-read']
    text = plain('\n'.join(line.draw(160, {}, 'sample-seat', {}, rows)))
    assert '6%' in text and 'Unread' not in text and 'Never Used' not in text
    assert 'unknown' not in text and 'not read' not in text


def test_no_per_pool_attempt_is_invented_from_a_failed_global_ledger_refresh():
    ship = {'pools': {'sample-unread': {}, 'sample-never-used': {}}}
    assert line.allowances({'ledger_unavailable': True, 'ledger_attempted_at': 1}, ship, None) == []


def test_failed_attempt_without_a_window_stays_unknown_after_cache_refresh(tmp_path, monkeypatch):
    ledger = dict(available=True, windows=[], pools=[dict(name='sample-failed', kind='subscription')],
                  usage_attempts=[dict(pool='sample-failed', at='2030-01-01T00:00:00+00:00',
                                       error='synthetic failed provider read')])
    replies = iter([ledger, {'seats': []}])
    monkeypatch.setattr(line, 'qd_reading', lambda *args: next(replies))
    path = tmp_path / 'readings.json'
    line.refresh(tmp_path, path)
    data = line.read_json(path)
    assert data['attempted_pools'] == ['sample-failed']
    assert 'synthetic failed provider read' not in path.read_text()
    row, = line.allowances(data, {'pools': {'sample-failed': {}, 'sample-unread': {}}}, None)
    assert row['used'] is None and row['unknown']
    assert 'Failed unknown' in plain(line.cell(row, 3)) and '0%' not in plain(line.cell(row, 3))


def test_failed_window_with_retained_measurement_stays_visible_as_unknown(tmp_path, monkeypatch):
    w = window('sample-failed', .4)
    w['error'] = 'synthetic failed refresh'
    ledger = dict(available=True, pools=[dict(name='sample-failed', kind='subscription')],
                  windows=[dict(pool='sample-failed', current=False, complete=True, wall=False, reading=w)],
                  usage_attempts=[dict(pool='sample-failed', at='2030-01-01T00:00:00+00:00', error=w['error'])])
    replies = iter([ledger, {'seats': []}])
    monkeypatch.setattr(line, 'qd_reading', lambda *args: next(replies))
    path = tmp_path / 'readings.json'
    line.refresh(tmp_path, path)
    row, = line.allowances(line.read_json(path), {'pools': {'sample-failed': {}}}, None)
    text = plain(line.cell(row, 3))
    assert '40%?' in text and '+ unknown' in text


def test_successful_attempt_with_missing_windows_is_incomplete_not_unread():
    row, = line.allowances({'attempted_pools': ['sample']}, {'pools': {'sample': {}}}, None)
    assert row['unknown'] and row['used'] is None
    assert 'unknown' in plain(line.cell(row, 0))


def test_complete_grouped_reading_has_no_invented_unknown_or_whole_bar():
    data = cache([window('sample', .01, group='sample-group')])
    row, = line.allowances(data, {'pools': {'sample': {}}}, None)
    text = plain(line.cell(row, 3))
    assert row['used'] == 1 and not row['unknown'] and not row['whole']
    assert '1%' in text and 'unknown' not in text and not any(c in text for c in '━─╋╂')


def test_footer_selection_does_not_change_detailed_ledger_or_status(tmp_path, monkeypatch):
    pools = {name: Pool(name, 'anthropic') for name in ('sample-read', 'sample-failed', 'sample-never-used')}
    ship = Ship(tmp_path, 'sample', 'operator', tmp_path, pools, {}, {})
    ship.runtime.mkdir()
    now = datetime.now(timezone.utc)
    with Ledger(ship.runtime / 'ledger.sqlite') as book:
        book.record_usage('sample-read', Usage((Window('7d', .06, now + timedelta(days=7)),), now))
        book.record_usage('sample-failed', Usage((), now, 'synthetic failed read'))
        detailed = output.value(ledger_reading.collect(book, ship))
        before = deepcopy(detailed)
        monkeypatch.setattr(watch.post, 'now', lambda: now)
        status_before = output.value(watch.headline_readings(ship))
        replies = iter([detailed, {'seats': []}])
        monkeypatch.setattr(line, 'qd_reading', lambda *args: next(replies))
        path = tmp_path / 'readings.json'
        line.refresh(tmp_path, path)
        rows = line.allowances(line.read_json(path), {'pools': {name: {} for name in pools}}, None)
        assert {row['pool'] for row in rows} == {'sample-read', 'sample-failed'}
        assert detailed == before
        assert {pool['name'] for pool in detailed['pools']} == set(pools)
        assert output.value(watch.headline_readings(ship)) == status_before
        assert {row['pool'] for row in status_before} == set(pools)
