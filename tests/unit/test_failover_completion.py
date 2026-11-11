"""Failover needs an explicit active completion, not an idle reader default."""

from dataclasses import replace
import json

import pytest

from .test_cross_program_failover import running
from .test_fresh_after_order import worker
from .test_opencode_record import session, user, assistant


@pytest.mark.parametrize('contents', ['empty', 'metadata', 'untimed-terminal', 'inferred', 'later-untimed'])
def test_codex_record_without_timestamped_completion_holds_cross_program_move(running, contents):
    r = running
    if contents == 'later-untimed':
        r.source.begin('later synthetic input', 'synthetic-source', 'high')
        r.source.end('later synthetic reply')
    rows = [json.loads(line) for line in r.source.path.read_text().splitlines()]
    if contents == 'empty':
        rows = []
    elif contents == 'metadata':
        rows = rows[:1]
    elif contents in ('untimed-terminal', 'later-untimed'):
        rows[-1].pop('timestamp')
    else:
        rows[-1]['payload']['type'] = 'task_started'
    r.source.path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    state, measured = r.seat.harness.read(r.source.path)
    assert state.turn_open is (contents == 'inferred') and not state.queued
    assert len(state.completed_turns) == (1 if contents == 'later-untimed' else 0)
    if contents == 'inferred':
        assert len(measured) == 1, 'the prior turn still has an inferred accounting boundary'
    r.svc.failover()
    assert not r.launches and r.seat.pool.name == 'first'
    assert r.seat.harness.read(r.source.path)[1] == measured


@pytest.mark.parametrize('evidence', ['empty', 'unknown-reason', 'missing-reason', 'untimed', 'terminal'])
def test_real_program_completion_boundaries_hold_or_release_same_program(worker, monkeypatch, evidence):
    w = worker
    w.seat.spec = replace(w.seat.spec, pools=('local', 'spare'))
    w.seat.ship.pools['spare'] = replace(w.seat.pool, name='spare')
    monkeypatch.setattr(w.svc.ledger, 'walled', lambda name, *args, **kwargs: name == 'local')
    moves = []
    monkeypatch.setattr(w.seat, 'failover_to', lambda name: moves.append(name) or w.seat.move_to(name) or 'synthetic move')
    if evidence != 'empty':
        w.record.begin()
        w.record.finish(None if evidence == 'untimed' else 40,
                        reason='unfamiliar' if evidence == 'unknown-reason' else None if evidence == 'missing-reason' else 'stop')
    state, measured = w.seat.harness.read(w.record.path)
    assert bool(state.completed_turns) == (evidence == 'terminal')
    for _ in range(2):
        w.svc.failover()
    assert moves == (['spare'] if evidence == 'terminal' else [])
    assert w.seat.harness.read(w.record.path)[1] == measured


@pytest.mark.parametrize('worker', ['opencode'], indirect=True)
@pytest.mark.parametrize('inferred', [False, True])
def test_child_and_inferred_accounting_completions_never_release_active_root(worker, monkeypatch, inferred):
    w = worker
    w.seat.spec = replace(w.seat.spec, pools=('local', 'spare'))
    w.seat.ship.pools['spare'] = replace(w.seat.pool, name='spare')
    monkeypatch.setattr(w.svc.ledger, 'walled', lambda name, *args, **kwargs: name == 'local')
    monkeypatch.setattr(w.seat, 'failover_to', lambda name: pytest.fail('child accounting cannot release the active root'))
    session(w.record.con, 'child', 2, 'root')
    user(w.record.con, 'child-input', 'child', 10, 'Synthetic child work')
    assistant(w.record.con, 'child-step', 'child-input', 'child', 20, finish='tool-calls' if inferred else 'stop')
    if inferred:
        user(w.record.con, 'later-child-input', 'child', 30, 'Later synthetic child work')
        assistant(w.record.con, 'later-child-step', 'later-child-input', 'child', 40)
    w.record.con.commit()
    state, measured = w.seat.harness.read(w.record.path)
    assert state.turn_open is False and not state.queued and not state.completed_turns
    assert len(measured) == (2 if inferred else 1)
    w.svc.failover()
    assert w.seat.harness.read(w.record.path)[1] == measured
