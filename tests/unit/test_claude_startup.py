"""Startup is resolved by native decline/prevention evidence, never the early box."""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from quarterdeck import ship as shipmod
from quarterdeck.door import Door
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.seat import Seat
from quarterdeck.ship import Pool, SeatSpec
from tests.conftest import FIXTURES


def frame(name):
    return (FIXTURES/'claude/2.1.317'/f'{name}.txt').read_text()


@pytest.fixture
def startup(drydock, monkeypatch):
    ship = shipmod.load(drydock)
    spec = replace(ship.seat('worker-1'), harness='claude')
    shown = [frame('startup-before-auto-offer')]
    calls = []
    pane = SimpleNamespace(id='%1', pid=123, dead=False, width=348, in_mode=False)
    tmux = SimpleNamespace(pane=lambda _: pane, capture=lambda *a, **k: shown[0],
                           keys=lambda *a: calls.append(('keys', a)),
                           paste=lambda *a, **k: calls.append(('paste', a)))
    seat = Seat(ship, spec, tmux)
    seat.home.mkdir(parents=True)
    monkeypatch.setattr(seat, 'pane', lambda: pane)
    # Native state starts unresolved, like the incident, without credentials.
    state = seat.home/'.claude.json'
    state.write_text('{}')
    return SimpleNamespace(seat=seat, shown=shown, calls=calls, state=state)


def test_pre_offer_box_never_admits_paste_until_native_choice_is_resolved(startup):
    w = startup
    door = Door(w.seat)
    assert w.seat.harness.screen(w.shown[0]).box == ''
    assert not door.reading().open
    assert door.deliver('synthetic work').result == 'withheld'
    assert w.calls == []
    w.shown[0] = frame('startup-bypass-ready')
    assert not door.reading().open, 'a later idle-looking box alone is still no proof'
    w.state.write_text(json.dumps({'hasSeenAutoDefaultNudge': True}))
    assert door.reading().open


def test_known_offer_clears_by_observed_selection_without_toggling_back(startup, monkeypatch):
    w = startup
    w.shown[0] = frame('auto-default-offer-yes')
    def keys(pane, key):
        w.calls.append(key)
        if key == 'Down':
            assert '❯ Yes, set auto mode' in w.shown[0]
            w.shown[0] = frame('auto-default-offer-no')
        else:
            assert key == 'Enter' and '❯ No, keep bypass permissions' in w.shown[0]
            w.state.write_text(json.dumps({'hasSeenAutoDefaultNudge': True}))
            w.shown[0] = frame('startup-bypass-ready')
    monkeypatch.setattr(w.seat.tmux, 'keys', keys)
    door = Door(w.seat)
    door.clear_known()
    assert w.calls == ['Down'] and not door.reading().open
    door.clear_known()
    assert w.calls == ['Down', 'Enter'] and door.reading().open
    door.clear_known()
    assert w.calls == ['Down', 'Enter']


@pytest.mark.parametrize('state', [None, 'not JSON', '{"hasSeenAutoDefaultNudge": false}', '{"hasSeenAutoDefaultNudge": "true"}'])
def test_missing_invalid_or_unresolved_choice_cannot_certify_composer(startup, state):
    w = startup
    w.shown[0] = frame('startup-bypass-ready')
    if state is None:
        w.state.unlink()
    else:
        w.state.write_text(state)
    assert not Door(w.seat).reading().open
    assert Door(w.seat).deliver('synthetic work').result == 'withheld'
    assert w.calls == []


@pytest.mark.parametrize('change', ['title', 'description', 'option', 'unselected', 'both-selected', 'extra', 'version'])
def test_altered_or_ambiguous_offer_is_untouched(startup, change):
    w = startup
    text = frame('auto-default-offer-yes')
    if change == 'title':
        text = text.replace('Make auto mode your default permission mode?', 'Make another mode your default?')
    elif change == 'description':
        text = text.replace('blocks the rest', 'permits the rest')
    elif change == 'option':
        text = text.replace('No, keep bypass permissions', 'No, change permissions')
    elif change == 'unselected':
        text = text.replace('❯ Yes', '  Yes')
    elif change == 'both-selected':
        text = text.replace('     No, keep', '   ❯ No, keep')
    elif change == 'version':
        text = text.replace('v2.1.317', 'v2.1.999')
    else:
        text += '\nAn unrelated approval offer\n'
    w.shown[0] = text
    assert Door(w.seat).deliver('synthetic work').result == 'withheld'
    assert w.calls == []


def test_human_sessions_and_drafts_never_answer_the_offer(startup):
    w = startup
    w.shown[0] = frame('auto-default-offer-yes')
    w.seat.ship.sessions[w.seat.spec.session] = replace(w.seat.session, human=True)
    assert Door(w.seat).deliver('synthetic work').result == 'withheld'
    assert w.calls == []
    w.seat.ship.sessions[w.seat.spec.session] = replace(w.seat.session, human=False)
    w.shown[0] = frame('startup-before-auto-offer').replace('❯\u00a0', '❯ human draft')
    assert Door(w.seat).deliver('synthetic work').result == 'withheld'
    assert w.calls == []
    w.shown[0] = frame('auto-default-offer-yes') + '\n' + w.shown[0]
    assert Door(w.seat).deliver('synthetic work').result == 'withheld'
    assert w.calls == [], 'exact dialog prose beside a real composer/draft is not a dialog'


def test_prevented_startup_delivers_once_with_a_fresh_real_reader_receipt(startup, monkeypatch, tmp_path):
    from quarterdeck.harness.synthetic_record import ClaudeShape
    w = startup
    w.state.write_text(json.dumps({'hasSeenAutoDefaultNudge': True}))
    w.shown[0] = frame('startup-bypass-ready')
    record = ClaudeShape(w.seat.home, tmp_path, 'synthetic-receipt', 'synthetic')
    monkeypatch.setattr(w.seat, 'record', lambda: record.path)
    payload = 'exact  synthetic input'
    def paste(pane, text):
        w.calls.append(('paste', text))
        w.shown[0] = frame('startup-bypass-ready').replace('❯\u00a0', '❯ '+text)
    def keys(pane, key):
        w.calls.append(('key', key))
        assert key == 'Enter'
        record.begin(payload, 'synthetic', 'high')
        record.end('synthetic reply')
    monkeypatch.setattr(w.seat.tmux, 'paste', paste)
    monkeypatch.setattr(w.seat.tmux, 'keys', keys)
    assert Door(w.seat).deliver(payload).result == 'delivered'
    assert w.calls == [('paste', payload), ('key', 'Enter')]


def test_fresh_home_predeclines_only_the_proven_default_offer(tmp_path):
    home = tmp_path/'home'
    ClaudeHarness().build_home(home, SeatSpec('worker','crew','worker','claude',('pool',),cwd=tmp_path),
                               Pool('pool','claude'), None)
    state = json.loads((home/'.claude.json').read_text())
    assert state.get('hasSeenAutoDefaultNudge') is True
    assert not state.get('hasSeenAutoModeEntryWarning'), 'unrelated entry-warning consent is not fabricated'
    assert json.loads((home/'settings.json').read_text())['permissions']['defaultMode'] == 'bypassPermissions'
