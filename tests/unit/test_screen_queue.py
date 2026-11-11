"""Synthetic native queue visibility is distinct from an exact record receipt."""

from datetime import timedelta
from pathlib import Path

import pytest

from quarterdeck import door as doormod, pending, post
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.harness.types import Screen
from quarterdeck.service import Service
from tests.antigravity_shapes import AntigravityShape
from .test_input_receipt import bind
from .test_pending_delivery import setup  # noqa: F401

FIXTURES = Path(__file__).parents[1] / 'fixtures/antigravity/1.5.5'
CONFIRM = doormod.Door.confirm
TEXT = 'qd > order from issuer · ord-1234 · Synthetic task\nexact  body\nsecond line'


def queued(text=TEXT, *, hint=True, box='', working=True):
    header = text.splitlines()[0]
    rule = '─'*80
    return '\n'.join(['⣯ Running command...' if working else '',
        f'▸ {header[:58]}... (alt+m to send immediately)', rule,
        '> '+box.replace('\n', '\n  '), rule,
        ('  Press up to edit queued messages' if hint else '')+'  Gemini 3.8 Flash · medium'])


def test_current_native_fixture_has_queue_hint_work_and_footer():
    screen = AntigravityHarness().screen((FIXTURES/'working-queued.txt').read_text())
    assert screen.box == '' and screen.working and screen.queued == 1
    assert screen.shows == dict(model='Gemini 3.8 Flash', effort='medium')


@pytest.mark.parametrize('change', ['none', 'foreign', 'no-hint', 'already-queued', 'draft', 'no-envelope'])
def test_queue_notice_requires_new_matching_native_envelope_and_empty_box(change):
    adapter = AntigravityHarness()
    before = Screen('synthetic pasted box', TEXT, working=True)
    after = adapter.screen(queued())
    wanted = TEXT
    if change == 'foreign': after = adapter.screen(queued(TEXT.replace('ord-1234','ord-5678')))
    if change == 'no-hint': after = adapter.screen(queued(hint=False))
    if change == 'already-queued': before = Screen('old queue', TEXT, working=True, queued=1)
    if change == 'draft': after = adapter.screen(queued(box='foreign draft'))
    if change == 'no-envelope': wanted = 'ordinary prefix'; after = adapter.screen(queued(wanted))
    assert bool(adapter.queued_input(wanted, before, after)) == (change == 'none')


@pytest.mark.parametrize('mode', ['receipt', 'teardown', 'crash-before-verdict'])
def test_visible_queue_survives_restart_until_exact_native_input_without_a_second_submit(setup, monkeypatch, mode):
    svc, original, live, _ = setup
    shape = AntigravityShape(original.home, svc.ship.workspace, 'synthetic-queue', 'gemini-3.8-flash-medium')
    shape.begin('Synthetic opening brief', 'gemini-3.8-flash-medium', 'medium')
    seat = bind(svc, original, 'antigravity', shape.path)
    message = svc.store.new('order', 'issuer', seat.name, 'Synthetic task\nexact body')
    text = post.typed([message])
    live['queue'] = None

    def capture(*args, **kwargs):
        if live['queue'] is not None:
            return queued(live['queue'])
        rule = '─'*80
        return '\n'.join(['⣯ Running command...',rule,'> '+live['box'].replace('\n','\n  '),rule,'  Gemini 3.8 Flash · medium'])

    def keys(pane, key):
        assert key == 'Enter'
        live['submitted'] += 1
        live['queue'], live['box'] = live['box'], ''

    monkeypatch.setattr(doormod.Door, 'confirm', lambda self, payload: CONFIRM(self, payload, seconds=0))
    monkeypatch.setattr(svc.tmux, 'capture', capture)
    monkeypatch.setattr(svc.tmux, 'keys', keys)
    result = svc.deliver(seat, [message])
    assert 'queued:' in result and 'record' in result
    proof, = pending.active(seat)
    assert proof.data['phase'] == 'submitted' and proof.data['screen_queue'] is True
    assert pending.held(seat, seat.harness.screen(capture()))[2] is True
    assert svc.attempts.latest()[message.id] == 'queued'
    assert 'queued' in str(doormod.Door(seat).reading())
    screen = seat.harness.screen(capture())
    expired = proof.reading(seat, screen, post.now()+timedelta(seconds=pending.WAIT_SECONDS+1))
    assert expired[1:] == ('needs a human', False)
    assert proof.reading(seat, None)[1:] == ('needs a human', False)
    with pytest.raises(ValueError, match='screen queue'):
        pending.validate({**proof.data, 'screen_queue': 'yes'}, seat.name)
    original_time = proof.data['at']
    if mode == 'crash-before-verdict':
        # Native queue evidence reached disk, but its attempt verdict did not.
        svc.attempts.close(svc.attempts.all()[0], None, 'synthetic interrupted verdict write')
    restarted = Service(svc.ship, svc.tmux)
    try:
        for _ in range(2): restarted.reconcile()
        assert svc.attempts.latest()[message.id] == ('pending' if mode == 'crash-before-verdict' else 'queued')
        assert 'queued on screen' in str(doormod.Door(seat).reading())
        assert pending.active(seat)[0].data['at'] == original_time
        assert live['pasted'] == live['submitted'] == 1
        assert not proof.confirmed(seat)
        if mode == 'teardown':
            pending.abandon(svc.runtime, seat.name, 'synthetic temporary operation ended')
            assert svc.attempts.latest()[message.id] == 'abandoned'
        shape.end('Ready')
        shape.begin(text, 'gemini-3.8-flash-medium', 'medium')
        shape.end('Synthetic reply')
        restarted.reconcile()
        restarted.reconcile()
        assert svc.attempts.latest()[message.id] == ('abandoned' if mode == 'teardown' else 'delivered')
        assert not pending.active(seat)
        assert live['pasted'] == live['submitted'] == 1
        assert len(seat.harness.turns(shape.path)) == 2
    finally:
        restarted.ledger.close()
