"""Synthetic composer/status frames matching the measured Codex 0.166.0 forms."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import idle
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.types import State, Tokens, Turn

FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'codex' / '0.166.0'
NATIVE = CodexHarness()
VERSION = '0.166.0'


def frame(name):
    return (FIXTURES / (name + '.txt')).read_text()


@pytest.mark.parametrize(('name', 'expected'), [
    ('background-none', ()), ('background-ended', ()),
    ('background-one', (('terminal', 1),)), ('background-two', (('terminal', 2),)),
    ('background-near-hint', None), ('background-out-of-place', None),
])
def test_measured_background_frames(name, expected):
    screen = NATIVE.screen(frame(name))
    assert screen.box == '' and screen.working is False
    assert NATIVE.background(screen, VERSION) == expected


@pytest.mark.parametrize('version', [None, '0.165.0', '0.166.1', 'future'])
def test_other_versions_do_not_supply_a_witness(version):
    assert NATIVE.background(NATIVE.screen(frame('background-none')), version) is None


@pytest.mark.parametrize(('old', 'new'), [
    ('? for shortcuts', '? for help'),
    ('/ps to view', '/ps for details'),
    ('1 background terminal', '1 background terminals'),
    ('1 background terminal', '0 background terminals'),
    ('1 background terminal', '01 background terminals'),
    ('1 background terminal', '2 background terminal'),
    ('1 background terminal', '1 background job'),
    ('› Ask Codex to do anything', '› synthetic unfinished input'),
    ('\n\n› Ask', '\n› Ask'),
])
def test_unmeasured_footer_variants_are_unknown(old, new):
    screen = NATIVE.screen(frame('background-one').replace(old, new))
    assert NATIVE.background(screen, VERSION) is None


def test_terminal_padding_is_display_only():
    text = '\n'.join(line + ' ' * 12 for line in frame('background-two').splitlines()) + '\n'
    assert NATIVE.background(NATIVE.screen(text), VERSION) == (('terminal', 2),)


def test_a_second_count_elsewhere_does_not_supply_a_witness():
    text = '  2 background terminals running · /ps to view · /stop to close\n' + frame('background-one')
    assert NATIVE.background(NATIVE.screen(text), VERSION) is None


def unread():
    at = datetime(2032, 4, 5, tzinfo=timezone.utc)
    turn = Turn(0, at, at + timedelta(seconds=3), 'synthetic input', 'synthetic reply', None, Tokens())
    return State(turn_open=False, pending_tools=None, last_input_at=at, completed_turns=(turn,))


@pytest.mark.parametrize(('name', 'count'), [('background-one', 1), ('background-two', 2)])
def test_second_witness_reports_terminals_as_work(name, count):
    seat = SimpleNamespace(harness=NATIVE, launched=lambda: SimpleNamespace(version=VERSION))
    state = unread()
    after, hold, saw = idle.second(seat, state, idle.Hold('tools-unknown', 'tool state unknown'),
                                   screen=NATIVE.screen(frame(name)))
    assert after is state
    assert hold.dimension == 'tools-pending'
    assert saw == f"{count} terminal{'s' if count != 1 else ''} in the background"
    assert 'inbox watch' not in saw


def test_second_witness_uses_the_measured_zero_footer():
    seat = SimpleNamespace(harness=NATIVE, launched=lambda: SimpleNamespace(version=VERSION))
    state = unread()
    after, hold, saw = idle.second(seat, state, idle.Hold('tools-unknown', 'tool state unknown'),
                                   screen=NATIVE.screen(frame('background-ended')))
    assert after == replace(state, pending_tools=(), active_tools=())
    assert hold is None and saw == 'nothing in the background'


@pytest.mark.parametrize('name', ['background-near-hint', 'background-out-of-place'])
def test_second_witness_preserves_unknown_for_near_forms(name):
    seat = SimpleNamespace(harness=NATIVE, launched=lambda: SimpleNamespace(version=VERSION))
    state = unread()
    after, hold, saw = idle.second(seat, state, idle.Hold('tools-unknown', 'tool state unknown'),
                                   screen=NATIVE.screen(frame(name)))
    assert after is state and after.pending_tools is None
    assert hold.dimension == 'tools-unknown'
    assert 'not a measured form' in saw
