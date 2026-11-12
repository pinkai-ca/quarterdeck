"""Ordinary synthetic closed-launch readings; no native program or transport."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import harness, pending, record_owner
from quarterdeck.harness import closed_record
from quarterdeck.harness.types import Input, Inputs
from quarterdeck.seat import Launched
from quarterdeck.tmux import TmuxError


T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


def stamp(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat()


def append(record, *rows):
    with record.open('a') as stream:
        for row in rows:
            stream.write(json.dumps(row) + '\n')


def native_input(name, text, at):
    if name == 'codex':
        return dict(type='response_item', timestamp=at, payload=dict(
            type='message', role='user', content=[dict(type='input_text', text=text)]))
    if name == 'claude':
        return dict(type='user', timestamp=at, message=dict(role='user', content=text))
    return dict(type='message', timestamp=at, message=dict(role='user', content=[dict(type='text', text=text)]))


@pytest.fixture(params=['codex', 'claude', 'pi'])
def world(tmp_path, request):
    name = request.param
    folder = tmp_path / 'seat'
    folder.mkdir()
    record = folder / 'synthetic.jsonl'
    if name == 'codex':
        row = dict(type='session_meta', timestamp=stamp(0), payload=dict(id='synthetic'))
    elif name == 'claude':
        row = native_input(name, 'Earlier ordinary input.', stamp(0))
    else:
        row = dict(type='session', timestamp=stamp(0), id='synthetic')
    append(record, row)
    record_owner.register(folder, record, name)
    launched = Launched('%1', 100, stamp(1), 'synthetic', None, ['synthetic'], 'synthetic', name)
    live = dict(pane=SimpleNamespace(id='%1', pid=100), launched=launched, earlier=False)
    seat = SimpleNamespace(folder=folder, launched=lambda: live['launched'],
                           tmux=SimpleNamespace(pane=lambda ident: live['pane']),
                           _earlier_server=lambda: live['earlier'])
    # The old durable journal shape has no later protocol/baseline fields.
    data = dict(seat='worker', identity=dict(pane='%1', pid=100, launch=stamp(1), harness=name),
                at=stamp(10), since=stamp(20), payload='Exact  ordinary payload.\n',
                phase='submitted', record=str(record), shown='Exact  ordinary payload.',
                attempts=['synthetic-1.json'])
    proof = pending.Proof(folder / 'proof.json', data)
    proof.save()
    return SimpleNamespace(name=name, seat=seat, live=live, record=record, proof=proof)


def inventory(folder):
    return {str(path.relative_to(folder)): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in folder.rglob('*') if path.is_file()}


@pytest.mark.parametrize('closure', ['gone', 'later-launch', 'earlier-server'])
def test_old_shaped_closed_absent_reading_is_repeatable_and_writes_nothing(world, closure):
    w = world
    if closure == 'gone':
        w.live['pane'] = None
    elif closure == 'later-launch':
        w.live['launched'] = replace(w.live['launched'], pane='%2', pid=200, at=stamp(30))
    else:
        w.live['earlier'] = True
    before = inventory(w.seat.folder)
    assert closed_record.not_in_closed_record(w.seat, w.proof)
    assert closed_record.not_in_closed_record(w.seat, w.proof)
    assert inventory(w.seat.folder) == before
    assert w.proof.data['phase'] == 'submitted', 'the reading cannot itself settle or transport'


def test_live_launch_never_supplies_closed_absence(world):
    assert not closed_record.not_in_closed_record(world.seat, world.proof)


def test_input_since_original_attempt_blocks_absence_even_before_later_confirmation_window(world):
    w = world
    w.live['pane'] = None
    append(w.record, native_input(w.name, w.proof.data['payload'], stamp(11)))
    assert not closed_record.not_in_closed_record(w.seat, w.proof)


def test_stale_identical_input_is_outside_original_attempt_window(world):
    w = world
    w.live['pane'] = None
    append(w.record, native_input(w.name, w.proof.data['payload'], stamp(9)))
    assert closed_record.not_in_closed_record(w.seat, w.proof)


@pytest.mark.parametrize('kind', ['accepted', 'queued', 'consumed', 'opaque'])
def test_presence_is_independent_of_confirmation_or_disposition(world, monkeypatch, kind):
    w = world
    w.live['pane'] = None
    inputs = Inputs()
    inputs.append(Input(w.proof.data['payload'].strip(), T0 + timedelta(seconds=10), kind))
    reader = SimpleNamespace(inputs=lambda record: inputs)
    monkeypatch.setattr(harness, 'get', lambda name: reader)
    assert not closed_record.not_in_closed_record(w.seat, w.proof)


@pytest.mark.parametrize('damage', ['missing', 'empty', 'partial', 'unterminated', 'unknown', 'malformed', 'mixed', 'untimed'])
def test_unreadable_incomplete_or_unrecognized_input_history_holds(world, damage):
    w = world
    w.live['pane'] = None
    if damage == 'missing':
        w.record.unlink()
    elif damage == 'empty':
        w.record.write_text('')
    elif damage == 'partial':
        with w.record.open('a') as stream:
            stream.write('{"type":')
    elif damage == 'unterminated':
        w.record.write_text(w.record.read_text().rstrip('\n'))
    elif damage == 'unknown':
        append(w.record, dict(type='unrecognized-input', timestamp=stamp(15), text='Unknown input.'))
    elif damage == 'malformed':
        append(w.record, native_input(w.name, ['invalid-text-type'], stamp(15)))
    elif damage == 'mixed':
        foreign = 'pi' if w.name != 'pi' else 'codex'
        append(w.record, native_input(foreign, 'Other native format.', stamp(15)))
    else:
        append(w.record, native_input(w.name, 'Unknown input time.', None))
    assert not closed_record.not_in_closed_record(w.seat, w.proof)


@pytest.mark.parametrize('ownership', ['missing', 'conflicting', 'malformed'])
def test_missing_or_ambiguous_pinned_reader_is_not_adopted(world, ownership):
    w = world
    w.live['pane'] = None
    registry = w.seat.folder / record_owner.REGISTRY
    if ownership == 'missing':
        registry.unlink()
    elif ownership == 'conflicting':
        record_owner.register(w.seat.folder, w.record, 'pi' if w.name != 'pi' else 'codex')
    else:
        registry.write_text('{')
    before = inventory(w.seat.folder)
    assert not closed_record.not_in_closed_record(w.seat, w.proof)
    assert inventory(w.seat.folder) == before


def test_tmux_read_error_and_changed_identity_do_not_mean_closed(world):
    w = world
    w.live['pane'] = SimpleNamespace(id='%1', pid=999)
    assert not closed_record.not_in_closed_record(w.seat, w.proof)
    def unreadable(ident):
        raise OSError('synthetic unreadable pane')
    w.seat.tmux.pane = unreadable
    assert not closed_record.not_in_closed_record(w.seat, w.proof)


def test_unsupported_bare_input_list_cannot_supply_absence(world, monkeypatch):
    world.live['pane'] = None
    monkeypatch.setattr(harness, 'get', lambda name: SimpleNamespace(inputs=lambda record: []))
    assert not closed_record.not_in_closed_record(world.seat, world.proof)


def test_tmux_query_failure_holds_without_becoming_positive_pane_absence(world):
    def unreadable(ident):
        raise TmuxError('synthetic failed pane query')
    world.seat.tmux.pane = unreadable
    before = inventory(world.seat.folder)
    assert not closed_record.not_in_closed_record(world.seat, world.proof)
    assert inventory(world.seat.folder) == before


def test_adapter_without_approved_diagnostic_coverage_holds(world, monkeypatch):
    w = world
    w.live['pane'] = None
    w.proof.data['identity']['harness'] = 'grok'
    (w.seat.folder / record_owner.REGISTRY).write_text(
        json.dumps(dict(record=str(w.record), harness='grok')) + '\n')
    monkeypatch.setattr(harness, 'get', lambda name: SimpleNamespace(inputs=lambda record: Inputs()))
    assert not closed_record.not_in_closed_record(w.seat, w.proof)


def test_positive_later_launch_witness_does_not_require_a_tmux_read(world):
    w = world
    w.live['launched'] = replace(w.live['launched'], pane='%2', pid=200, at=stamp(30))
    def unreadable(ident):
        raise OSError('synthetic unavailable transport')
    w.seat.tmux.pane = unreadable
    assert closed_record.not_in_closed_record(w.seat, w.proof)


def test_pi_diagnostics_preserve_valid_text_receipt_and_bound_unknown_content(tmp_path):
    from quarterdeck import input_receipt
    record = tmp_path / 'ordinary.jsonl'
    append(record, dict(type='session', id='synthetic', timestamp=stamp(0)),
           native_input('pi', 'Ordinary text.', stamp(11)))
    assert input_receipt.confirmed('pi', record, 'Ordinary text.', T0) == 'delivered'
    assert harness.get('pi').inputs(record).diagnostics()['total'] == 0
    append(record, dict(type='message', timestamp=stamp(12), message=dict(role='user', content=[
        dict(type='text', text='Another ordinary input.'), dict(type='image', data='synthetic')])) )
    assert input_receipt.confirmed('pi', record, 'Another ordinary input.', T0) == 'delivered'
    assert harness.get('pi').inputs(record).diagnostics()['total'] == 1


def test_pi_unknown_rows_do_not_hide_valid_receipts_on_either_side(tmp_path):
    from quarterdeck import input_receipt
    record = tmp_path / 'ordinary.jsonl'
    append(record, native_input('pi', 'Before.', stamp(1)),
           dict(type='new-input-kind', text='unrecognized'),
           native_input('pi', 'After.', stamp(2)))
    assert input_receipt.confirmed('pi', record, 'Before.', T0) == 'delivered'
    assert input_receipt.confirmed('pi', record, 'After.', T0) == 'delivered'
    assert harness.get('pi').inputs(record).diagnostics()['total'] == 1
