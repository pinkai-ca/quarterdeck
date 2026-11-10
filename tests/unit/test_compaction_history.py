"""Only completed native checkpoints in a complete current conversation count."""

from dataclasses import replace

import pytest

from quarterdeck.harness import get
from quarterdeck.ship import ShipError, _validate as validate
from tests.compaction_shapes import Native
from .test_compaction_protocol import world


@pytest.mark.parametrize('program', ['claude', 'codex'])
def test_checkpoints_count_but_progress_and_summaries_do_not(tmp_path, program):
    native = Native(tmp_path/'record', program)
    assert get(program).compactions(native.path).count == 0
    native.compact()
    if program == 'claude':
        native.add('system', subtype='informational', content='Synthetic compaction progress')
        native.add('user', isCompactSummary=True, message=dict(role='user', content='Synthetic summary'))
        native.rows[-1]['isCompactSummary'] = True
    else:
        native.add('event_msg', payload=dict(type='context_compacted'))
    native.flush()
    assert get(program).compactions(native.path).count == 1
    native.compact()
    assert get(program).compactions(native.path).count == 2


@pytest.mark.parametrize('program', ['claude', 'codex'])
@pytest.mark.parametrize('damage', ['empty', 'partial', 'missing-origin', 'malformed', 'mixed'])
def test_blind_or_incomplete_history_is_unknown_not_zero(tmp_path, program, damage):
    native = Native(tmp_path/'record', program)
    native.compact()
    if damage == 'empty':
        native.path.write_text('')
    elif damage == 'partial':
        with native.path.open('a') as out:
            out.write('{"type":')
    elif damage == 'malformed':
        with native.path.open('a') as out:
            out.write('{broken}\n')
    elif damage == 'missing-origin':
        native.rows.pop(0)
        native.flush()
    else:
        if program == 'claude':
            native.rows[-1]['sessionId'] = 'synthetic-other'
        else:
            native.rows.append(dict(type='session_meta', payload=dict(id='synthetic-other')))
        native.flush()
    reading = get(program).compactions(native.path)
    assert reading.count is None and reading.reason


def test_sidechain_checkpoint_does_not_count(tmp_path):
    native = Native(tmp_path/'record', 'claude')
    native.compact()
    native.rows[-1]['isSidechain'] = True
    native.flush()
    assert get('claude').compactions(native.path).count == 0


@pytest.mark.parametrize('program', ['pi', 'opencode', 'antigravity', 'fake'])
def test_unsupported_harness_is_unknown_even_for_empty_file(tmp_path, program):
    path = tmp_path/'record'
    path.write_text('')
    reading = get(program).compactions(path)
    assert reading.count is None and 'unsupported' in reading.reason


@pytest.mark.parametrize('value', [True, False, -1, '2', 2.0])
def test_bad_threshold_values_are_rejected(world, value):
    world.seat.ship.seats['worker'] = replace(world.seat.spec, fresh_after_compactions=value)
    with pytest.raises(ShipError, match='nonnegative integer'):
        validate(world.seat.ship)


@pytest.mark.parametrize('value', [None, 0, 1, 2, 9])
def test_absent_zero_and_positive_thresholds_are_framework_settings(world, value):
    world.seat.ship.seats['worker'] = replace(world.seat.spec, fresh_after_compactions=value)
    validate(world.seat.ship)


def test_threshold_uses_the_declaration_and_cli_zero_disables_it(drydock, monkeypatch):
    from types import SimpleNamespace
    from quarterdeck import cli, ship
    monkeypatch.setattr(cli, 'load_ship', lambda _: ship.load(drydock))
    args = SimpleNamespace(seat='captain', changes=['fresh_after_compactions=2'], adopt=False)
    assert 'threshold applies' in '\n'.join(cli.cmd_set(args))
    spec = ship.load(drydock).seat('captain')
    assert spec.fresh_after_compactions == 2 and 'fresh_after_compactions' not in spec.settings
    args.changes = ['fresh_after_compactions=0']
    cli.cmd_set(args)
    assert ship.load(drydock).seat('captain').fresh_after_compactions == 0
