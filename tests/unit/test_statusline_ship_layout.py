"""Ordinary synthetic footer layout; expected rows also compared with the ship's pure renderer."""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import struct

import pytest

from quarterdeck.harness import claude_statusline as line


NOW = 1700000000
WHO = 'Example  builder · Sample Model · max · context 23%'
EXPECTED = {
    80: [WHO, '● Primary 32%    Secondary 86% (93%)    Codex 54%    Google 22%    Grok 88%'],
    120: [WHO, '● Primary 32%        Secondary 86% (93%)        Codex 54%        Google 22%        Grok 88%'],
    160: [WHO, '● Primary ━╋━───── 32%        Secondary ━━━━━━╋─ 86% (93%)        Codex ━━━━──── 54%        Google ━━────── 22%        Grok ━━━━━━━─ 88%'],
}


@pytest.fixture
def reading(monkeypatch):
    monkeypatch.setattr(line.time, 'time', lambda: NOW)
    ship = {'pools': {'claude-primary': {'provider': 'anthropic'},
                      'claude-secondary': {'provider': 'anthropic'},
                      'codex': {'provider': 'openai'}, 'google': {'provider': 'google'}, 'grok': {'provider': 'xai'}}}
    seat = {'persona': 'Example', 'role': 'builder', 'model': 'claude-fable-5.2', 'effort': 'max'}
    session = {'model': {'id': 'claude-fable-5.2', 'display_name': 'Sample Model'},
               'effort': {'level': 'max'}, 'context_window': {'used_percentage': 23}}

    def window(pool, used, days, hours, group=None, **extra):
        return dict(pool=pool, used=used, window='7d', group=group, blocks=True, wall=False,
                    complete=True, current=True,
                    resets_at=(datetime.fromtimestamp(NOW, timezone.utc) + timedelta(days=days, hours=hours)).isoformat(),
                    **extra)

    windows = [window('claude-primary', .32, 4, 22), window('claude-primary', .26, 4, 22, 'fable'),
               window('claude-secondary', .86, 4, 22), window('claude-secondary', .93, 1, 3, 'fable'),
               window('codex', .54, 4, 6), window('google', .22, 3, 4), window('grok', .88, 2, 7)]
    return ship, seat, session, dict(windows=windows, ledger_at=NOW)


def rows(reading):
    ship, _, _, cache = reading
    return line.allowances(cache, ship, 'claude-primary')


def plain(text):
    return line.ANSI.sub('', text)


@pytest.mark.parametrize('columns', [80, 120, 160])
def test_same_readings_match_ship_layout_at_required_widths(reading, columns):
    _, seat, session, _ = reading
    rendered = line.draw(columns, seat, 'sample-seat', session, rows(reading))
    assert [plain(text) for text in rendered] == EXPECTED[columns]
    assert len(rendered) == 2
    assert all(line.width(text) <= columns - line.MARGIN for text in rendered)
    assert all('weekly' not in plain(text) and 'fable' not in plain(text) for text in rendered)


def test_everything_uses_one_row_when_it_fits(reading):
    _, seat, session, _ = reading
    rendered = line.draw(160, seat, 'sample-seat', session, rows(reading)[:1])
    assert len(rendered) == 1 and WHO in plain(rendered[0])
    assert 'Primary' in plain(rendered[0]) and '4d 22h' in plain(rendered[0])
    assert line.width(rendered[0]) <= 156


def test_model_notch_is_one_bar_and_only_tighter_figure_is_bracketed(reading):
    primary, secondary = rows(reading)[:2]
    for row in (primary, secondary):
        text = plain(line.cell(row, 3))
        assert sum(text.count(c) for c in '━─╋╂') == line.BAR
    assert '(26%)' not in plain(line.cell(primary, 3))
    text = plain(line.cell(secondary, 3))
    assert '86% (93%)' in text and '1d 3h' in text
    assert 'fable' not in text and '7d' not in text


def test_detail_is_shed_in_the_ship_order(reading):
    row = rows(reading)[1]
    full, no_reset, no_bar, tight = [plain(line.cell(row, detail)) for detail in (3, 2, 1, 0)]
    assert '1d 3h' in full and '1d 3h' not in no_reset
    assert '╋' in no_reset and not any(c in no_bar for c in '━─╋╂')
    assert '86% (93%)' in no_bar and '93%' in tight and '86%' not in tight


def test_own_marker_keeps_equal_column_space(reading):
    row = rows(reading)[0]
    own = line.cell(row, 3)
    row['own'] = False
    other = line.cell(row, 3)
    assert plain(own).startswith('● Primary') and plain(other).startswith('  Primary')
    assert line.width(own) == line.width(other)


def test_provider_order_and_short_names_are_independent_of_own_seat(reading):
    ship, _, _, cache = reading
    ship['pools'] = {'provider-secondary': {'provider': 'first'}, 'different-primary': {'provider': 'second'},
                     'provider-primary': {'provider': 'first'}, 'standalone': {'provider': 'second'}}
    first = line.allowances(cache, ship, 'provider-primary')
    second = line.allowances(cache, ship, 'different-primary')
    expected = ['provider-primary', 'provider-secondary', 'different-primary', 'standalone']
    assert [row['pool'] for row in first] == [row['pool'] for row in second] == expected
    assert line.label('provider-primary-and-extra') == 'Primary And Extra'
    assert line.label('standalone') == 'Standalone'


def test_warning_does_not_invent_a_spent_allowance(reading):
    _, _, _, cache = reading
    cache['windows'][4]['used'] = .95
    row = rows(reading)[2]
    assert '95%' in plain(line.cell(row, 3)) and 'back in' not in plain(line.cell(row, 3))
    cache['windows'][4]['wall'] = True
    assert 'back in 4d 6h' in plain(line.cell(rows(reading)[2], 3))
    cache['windows'][4].update(wall=False, used=1.0)
    assert 'back in 4d 6h' in plain(line.cell(rows(reading)[2], 3))


def test_tighter_model_wall_uses_its_own_reset(reading):
    _, _, _, cache = reading
    cache['windows'][3].update(used=1.0, wall=True)
    text = plain(line.cell(rows(reading)[1], 3))
    assert '86% (100%)' in text and 'back in 1d 3h' in text and '4d 22h' not in text


def test_an_older_model_constraint_stays_marked_even_at_low_detail(reading):
    _, _, _, cache = reading
    cache['windows'][3]['current'] = False
    row = rows(reading)[1]
    assert '86% (93%?)' in plain(line.cell(row, 3))
    assert '93%?' in plain(line.cell(row, 0))


def test_ambiguous_group_membership_is_not_guessed_from_the_model_name(reading):
    _, _, _, cache = reading
    cache['windows'].append({**cache['windows'][3], 'group': 'another-family', 'used': .99})
    row = rows(reading)[1]
    assert row['used'] == 86 and row['model'] is None and row['unknown']
    text = plain(line.cell(row, 3))
    assert '86%' in text and '+ unknown' in text and not any(c in text for c in '╋╂')


def test_grouped_only_reading_is_not_drawn_as_a_whole_allowance_bar(reading):
    _, _, _, cache = reading
    cache['windows'] = [cache['windows'][3]]
    row = rows(reading)[1]
    assert row['used'] == 93 and not row['whole'] and row['unknown']
    text = plain(line.cell(row, 3))
    assert '93%' in text and '+ unknown' in text and not any(c in text for c in '━─╋╂')


def test_an_unplaced_wall_is_kept_without_assigning_it_the_totals_reset(reading):
    _, _, _, cache = reading
    cache['windows'].append({**cache['windows'][3], 'group': 'another-family', 'wall': True})
    text = plain(line.cell(rows(reading)[1], 3))
    assert '86%' in text and '+ unknown' in text and 'WALL' in text
    assert 'back in' not in text and '4d 22h' not in text


def test_partial_data_is_not_hidden_by_detail_shedding(reading):
    row = rows(reading)[0]
    row['unknown'] = True
    assert all('+ unknown' in plain(line.cell(row, detail)) for detail in range(4))


def test_unknown_subscriptions_are_all_kept_even_when_two_rows_cannot_fit(reading):
    ship, seat, session, _ = reading
    rendered = line.draw(80, seat, 'sample-seat', session, line.allowances({}, ship, None))
    assert len(rendered) > 2
    text = plain('\n'.join(rendered))
    assert all(line.label(pool) + ' not read' in text for pool in ship['pools'])
    assert all(line.width(text) <= 76 for text in rendered)


def test_display_width_counts_wide_and_combining_characters():
    assert line.width(line.BLUE + '東京e\u0301' + line.RESET) == 5


def test_terminal_width_uses_the_controlling_terminal_without_tmux(monkeypatch):
    monkeypatch.delenv('QD_STATUSLINE_WIDTH', raising=False)
    monkeypatch.delenv('COLUMNS', raising=False)
    monkeypatch.setattr(line, 'open', lambda *a: nullcontext(object()), raising=False)
    monkeypatch.setattr(line.fcntl, 'ioctl', lambda *a: struct.pack('hh', 24, 160))
    monkeypatch.setattr(line.subprocess, 'run', lambda *a, **kw: pytest.fail('no tmux or provider command for width'))
    assert line.columns_of_terminal() == 160


def test_explicit_width_takes_precedence(monkeypatch):
    monkeypatch.setenv('QD_STATUSLINE_WIDTH', '80')
    monkeypatch.setenv('COLUMNS', '120')
    assert line.columns_of_terminal() == 80
