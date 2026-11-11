"""Opening readiness and one-submit queued check transport with synthetic native steps."""

from types import SimpleNamespace
import re

import pytest

from quarterdeck import check, door as doormod, post
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.harness.types import State
from tests.antigravity_shapes import AntigravityShape
from .test_input_receipt import bind
from .test_pending_delivery import setup  # noqa: F401
from .test_screen_queue import queued, CONFIRM


@pytest.mark.parametrize('turns,active,working,queue,expected', [
    (None, None, False, (), False), (0, False, False, (), False),
    (1, None, False, (), False), (1, True, True, (), False),
    (1, False, True, (), False), (1, False, False, ('pending',), False),
    (1, False, False, (), True),
])
def test_opening_turn_needs_known_ended_record_and_ready_screen(turns, active, working, queue, expected):
    adapter = AntigravityHarness()
    adapter.state = lambda path: State(turns=turns, turn_open=active, queued=queue)
    seat = SimpleNamespace(harness=adapter, record=lambda: 'synthetic-record')
    door = SimpleNamespace(reading=lambda: doormod.Reading(True, working=working))
    assert check.ready_for_probe(seat, door) == expected


@pytest.mark.parametrize('receipt', [True, False])
def test_check_waits_for_the_original_queued_input_and_counts_its_task_turn(setup, monkeypatch, receipt):
    svc, original, live, _ = setup
    shape = AntigravityShape(original.home, svc.ship.workspace, 'synthetic-check', 'gemini-3.8-flash-medium')
    shape.begin('Synthetic opening brief', 'gemini-3.8-flash-medium', 'medium')
    seat = bind(svc, original, 'antigravity', shape.path)
    live.update(queue=None, working=True)
    monkeypatch.setattr(seat, 'up', lambda **kw: 'synthetic launch')

    def capture(*args, **kwargs):
        if live['queue'] is not None:
            return queued(live['queue'])
        rule = '─'*80
        return '\n'.join(['⣯ Running command...' if live['working'] else '',rule,
            '> '+live['box'].replace('\n','\n  '),rule,'  Gemini 3.8 Flash · medium'])

    def keys(pane, key):
        assert key == 'Enter'
        live['submitted'] += 1
        live['queue'], live['box'] = live['box'], ''

    monkeypatch.setattr(doormod.Door, 'confirm', lambda self, payload: CONFIRM(self, payload, seconds=0))
    monkeypatch.setattr(svc.tmux, 'capture', capture)
    monkeypatch.setattr(svc.tmux, 'keys', keys)
    events = []

    def opening_ended():
        assert not live['submitted'], 'check task was sent during the opening brief'
        shape.end('Ready'); live['working'] = False; events.append('opening ended')

    def take_input():
        message, = [m for m in svc.store.all() if m.kind == 'order']
        shape.begin(post.typed([message]), 'gemini-3.8-flash-medium', 'medium')
        live.update(queue=None, working=True); events.append('record accepted original input')

    def answer():
        message, = [m for m in svc.store.all() if m.kind == 'order']
        match = re.search(r'Write the file (.+) containing exactly one line: (.+)\n', message.body)
        from pathlib import Path
        Path(match[1]).write_text(match[2]+'\n')
        svc.store.new('report', seat.name, 'ship', 'written', re=message.id)
        shape.end('Written'); live['working'] = False; events.append('task ended and reported')

    transitions = iter([opening_ended, take_input, answer] if receipt else [opening_ended])
    def wait(predicate, seconds):
        assert seconds > 0
        for _ in range(5):
            if value := predicate(): return value
            event = next(transitions, None)
            if event is None: return None  # deterministic deadline, no retry or native call
            event()
        raise AssertionError('unexpected predicate loop')
    monkeypatch.setattr(check, 'wait_for', wait)
    result = check.probe(svc.ship, seat, [])
    assert all(c.passed for c in result) == receipt
    paste = next(c for c in result if 'paste matched' in c.name)
    assert 'queued:' in paste.detail and ('→ delivered:' in paste.detail) == receipt
    assert live['submitted'] == live['pasted'] == 1
    assert events[0] == 'opening ended'
    assert len(seat.harness.turns(shape.path)) == (2 if receipt else 1)
    assert svc.attempts.all()[0].result == ('delivered' if receipt else 'queued')
