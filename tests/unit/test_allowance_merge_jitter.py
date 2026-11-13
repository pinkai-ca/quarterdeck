"""One allowance in two representations, a named model window and its list entry, read as one: within the reset
bound they reconcile; beyond it, or when they disagree, the one spent in full is kept whole as the wall, a severity
counting for nothing, and the conflict is named. Synthetic bodies only; no native response or provider request."""

from datetime import datetime, timedelta, timezone

import pytest

from quarterdeck.harness import claude_limits
from quarterdeck.harness.claude_limits import RESET_TOLERANCE, snapshot
from quarterdeck.ledger import Ledger

AT = datetime(2031, 3, 4, 5, 6, tzinfo=timezone.utc)
RESET = AT + timedelta(days=7)


def body(named=35, listed=35, severity='critical', offset=0.0, named_reset=RESET, **extra):
    data = {'seven_day': {'utilization': 80, 'resets_at': RESET.isoformat()}}
    if named is not None:
        data['seven_day_fable'] = {'utilization': named, 'resets_at': named_reset.isoformat() if named_reset else None}
    data['limits'] = [{'kind': 'weekly_scoped', 'group': 'weekly', 'scope': {'model': {'display_name': 'Fable'}},
                       'percent': listed, 'severity': severity, 'is_active': True,
                       'resets_at': (RESET + timedelta(seconds=offset)).isoformat(), **extra}]
    return data


def fable(measured):
    rows = [w for w in measured.windows if w.group == 'fable']
    assert len(rows) == 1, rows
    return rows[0]


def walled(tmp_path, measured):
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        book.record_usage('synthetic', measured)
        return book.walled('synthetic'), book.usage_known('synthetic')


def reversed_body(data):
    """The same two representations, the list entry first: the named field is read after it."""
    named = data.pop('seven_day_fable', None)
    return {**data, **({'seven_day_fable': named} if named is not None else {})}


@pytest.mark.parametrize('order', ['named-first', 'list-first'])
@pytest.mark.parametrize('used', [35, 100])
def test_the_equal_reset_control_reads_as_before(tmp_path, order, used):
    data = body(named=used, listed=used)
    measured = snapshot(data if order == 'named-first' else reversed_body(data), AT)
    row = fable(measured)
    assert (row.name, row.used, row.resets_at, row.warning, row.error) == ('fable / 7d', used / 100, RESET, True, None)
    assert not any('conflicting' in note for note in measured.diagnostics)
    assert walled(tmp_path, measured) == (used == 100, True)


@pytest.mark.parametrize('offset', [1.0002, -1.0002, 0.5, RESET_TOLERANCE, -RESET_TOLERANCE])
@pytest.mark.parametrize('used', [35, 100])
@pytest.mark.parametrize('severity', ['critical', 'normal'])
def test_a_jittered_reset_within_the_bound_is_one_reading_with_the_later_reset(tmp_path, offset, used, severity):
    measured = snapshot(body(named=used, listed=used, severity=severity, offset=offset), AT)
    row = fable(measured)
    assert (row.used, row.warning, row.error, row.uncertainty) == (used / 100, severity == 'critical', None, None)
    assert row.resets_at == max(RESET, RESET + timedelta(seconds=offset)), 'a wall never ends early'
    assert not any('conflicting' in note for note in measured.diagnostics)
    assert walled(tmp_path, measured) == (used == 100, True)


@pytest.mark.parametrize('order', ['named-first', 'list-first'])
@pytest.mark.parametrize('used', [35, 100])
def test_the_captains_probe_case_is_one_reading_in_both_input_orders(tmp_path, order, used):
    data = body(named=used, listed=used, offset=1.0002)
    measured = snapshot(data if order == 'named-first' else reversed_body(data), AT)
    row = fable(measured)
    assert row.error is None and row.used == used / 100 and row.warning
    assert walled(tmp_path, measured) == (used == 100, True), 'a spent allowance keeps its wall; a mark alone is none'


@pytest.mark.parametrize('offset', [RESET_TOLERANCE + 0.001, 60, -3600])
def test_beyond_the_bound_the_spent_one_is_kept_whole_and_the_conflict_named(tmp_path, offset):
    measured = snapshot(body(named=100, listed=100, offset=offset), AT)
    row = fable(measured)
    assert (row.used, row.error) == (1.0, None)
    assert row.resets_at == max(RESET, RESET + timedelta(seconds=offset)), 'the later wall, with its own reset'
    assert 'conflicting model allowance representations for fable / 7d: kept the spent one as the wall' in measured.diagnostics
    assert walled(tmp_path, measured)[0]


@pytest.mark.parametrize('named,listed,severity', [(35, 100, 'normal'), (100, 35, 'critical'), (100, 60, 'normal')])
def test_disagreeing_fractions_keep_the_spent_one_whole_whatever_the_severity(tmp_path, named, listed, severity):
    measured = snapshot(body(named=named, listed=listed, severity=severity), AT)
    row = fable(measured)
    assert row.used == 1.0 and row.error is None, 'the spent representation is kept whole, never mixed'
    assert row.warning == (listed == 100 and severity == 'critical'), 'its own warning, nothing borrowed'
    assert any('kept the spent one as the wall' in note for note in measured.diagnostics)
    assert walled(tmp_path, measured)[0]


@pytest.mark.parametrize('offset,named,listed,severity', [(60, 35, 35, 'critical'), (0, 35, 60, 'critical'),
                                                          (1.0, 35, 60, 'normal'), (0, 99, 35, 'critical')])
def test_a_conflict_with_nothing_spent_is_unknown_room_and_named_a_severity_counting_for_nothing(
        tmp_path, offset, named, listed, severity):
    measured = snapshot(body(named=named, listed=listed, severity=severity, offset=offset), AT)
    row = fable(measured)
    assert (row.used, row.resets_at, row.error) == (None, None, claude_limits.CONFLICT)
    assert 'conflicting model allowance representations for fable / 7d: room unknown' in measured.diagnostics
    assert walled(tmp_path, measured) == (False, False)


def test_a_missing_reset_on_one_side_never_drops_the_others_wall(tmp_path):
    measured = snapshot(body(named=35, listed=100, named_reset=None), AT)
    row = fable(measured)
    assert (row.used, row.resets_at, row.error) == (1.0, RESET, None)
    assert walled(tmp_path, measured)[0]


def test_two_spent_representations_in_conflict_keep_the_later(tmp_path):
    first = claude_limits.Window('fable / 7d', 1.0, RESET, group='fable')
    second = claude_limits.Window('fable / 7d', 1.0, RESET + timedelta(hours=1), group='fable', warning=True)
    for old, new in ((first, second), (second, first)):
        kept, conflicted = claude_limits.merge(old, new)
        assert kept is second and conflicted


def test_other_families_and_the_general_windows_are_untouched(tmp_path):
    data = body(named=100, listed=100, offset=1.0002)
    data['seven_day_opus'] = {'utilization': 10, 'resets_at': RESET.isoformat()}
    measured = snapshot(data, AT)
    names = {w.name: w for w in measured.windows}
    assert names['7d'].used == .8 and names['opus / 7d'].used == .1
    assert fable(measured).used == 1.0
