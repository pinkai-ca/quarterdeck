"""Startup is resolved by native decline/prevention evidence, never the early box."""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from quarterdeck import check
from quarterdeck import ship as shipmod
from quarterdeck.door import Door
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.types import _cells
from quarterdeck.seat import Seat
from quarterdeck.ship import Pool, SeatSpec
from quarterdeck.tmux import ControlFrame
from tests.conftest import FIXTURES


def frame(name):
    return (FIXTURES/'claude/2.1.317'/f'{name}.txt').read_text()


def signed_out_frame(stage):
    rule = '─' * 120
    if stage == 'trust dialog':
        return f"""{rule}
 Accessing workspace:

 /synthetic/workspace

 Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known open source
 project, or work from your team). If not, take a moment to review what's in this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel
"""
    caption = ' synthetic '
    top = rule[:-len(caption) - 1] + caption + '─'
    return (f'{top}\n❯\u00a0\n{rule}\n'
            '  ⏵⏵ auto mode on (shift+tab to cycle) · install gh for PR status · ← for agents'
            '            Not logged in · run /login\n'
            '                                                                                                    ◐ medium · /effort\n')


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


@pytest.mark.parametrize(('stage', 'reason', 'box'), [
    ('trust dialog', 'account-trust', None),
    ('login', 'account-login', ''),
])
def test_signed_out_startup_is_reported_at_the_door_and_launch_check(startup, monkeypatch, stage, reason, box):
    w = startup
    pane = w.seat.pane()
    pane.width, pane.height = 120, 40
    launch = SimpleNamespace(version='2.1.331', pane=pane.id, pid=pane.pid)
    monkeypatch.setattr(w.seat, 'launched', lambda: launch)
    monkeypatch.setattr(w.seat, 'record', lambda: None)
    w.state.write_text(json.dumps({'hasSeenAutoDefaultNudge': True}))
    w.shown[0] = signed_out_frame(stage)
    door = Door(w.seat)
    monkeypatch.setattr(door, 'wall', lambda screen: None)

    screen = door.screen()
    assert screen.box == box
    assert screen.interruption.name == stage
    assert screen.interruption.key is None and screen.interruption.click is None
    assert door.reading().reason == stage
    assert not check.ready_for_probe(w.seat, door)
    assert check.account_question(door) == reason
    assert w.calls == []

    launch.version = '2.1.317'
    assert door.screen().interruption is None
    launch.version = '2.1.331'
    launch.pid += 1
    assert door.screen().interruption is None


@pytest.mark.parametrize('text', [
    signed_out_frame('trust dialog').replace('❯ No, exit', '  No, exit'),
    signed_out_frame('login').replace('❯\u00a0', '❯ synthetic draft'),
    signed_out_frame('login').replace('← for agents', '← for tasks'),
    'A quoted notice: Not logged in · run /login\n',
])
def test_partial_startup_text_does_not_supply_an_account_reason(startup, monkeypatch, text):
    w = startup
    pane = w.seat.pane()
    pane.width, pane.height = 120, 40
    launch = SimpleNamespace(version='2.1.331', pane=pane.id, pid=pane.pid)
    monkeypatch.setattr(w.seat, 'launched', lambda: launch)
    w.shown[0] = text
    assert check.account_question(Door(w.seat)) is None


def test_detailed_transcript_has_its_own_stop_and_a_collapsed_notice_leaves_the_box(startup, monkeypatch):
    w = startup
    pane = w.seat.pane()
    pane.width, pane.height = 120, 50
    launch = SimpleNamespace(version='2.1.331', pane=pane.id, pid=pane.pid)
    monkeypatch.setattr(w.seat, 'launched', lambda: launch)
    monkeypatch.setattr(w.seat, 'record', lambda: None)
    door = Door(w.seat)
    monkeypatch.setattr(door, 'wall', lambda screen: None)
    w.state.write_text(json.dumps({'hasSeenAutoDefaultNudge': True}))
    rule = '─' * 120
    w.shown[0] = (f'Synthetic notice in the expanded transcript\n{rule}\n'
                  '  Showing detailed transcript · ctrl+o to toggle · ↑/↓ to scroll · v to open in vi · ? for shortcuts'
                  '            verbose\n')

    assert door.reading().reason == 'detailed transcript is open'
    assert check.account_question(door) is None
    assert door.screen().interruption.key is None
    pane.height = 40
    assert door.screen().interruption is None
    pane.height = 50
    w.shown[0] = (f'  1 more notice hidden (ctrl+o to expand)\n{rule}\n❯\u00a0\n{rule}\n'
                  '  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n')
    assert check.ready_for_probe(w.seat, door)
    assert w.calls == []


def test_signed_in_queue_and_held_paste_display_use_existing_readings():
    adapter = ClaudeHarness()
    rule = '─' * 120

    def screen(before, prompt, footer):
        return adapter.screen(f'{before}{rule}\n❯\u00a0{prompt}\n{rule}\n  witness status line\n{footer}\n')

    held = screen('', 'synthetic draft[Pasted text #1 +12 lines]', '  paste again to expand')
    assert held.box == 'synthetic draft[Pasted text #1 +12 lines]'
    assert held.working is False and not held.queued
    working = screen(
        '  ⎿  Queued · ctrl+x ctrl+s to send now\n✻ Deciphering… (2s · ↓ 12 tokens)\n'
        '                                                                        Removed 1 invisible character from your prompt\n',
        'Press up to edit queued messages', '  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt',
    )
    assert working.box == '' and working.working is True and working.queued == 1
    assert working.interruption is None


@pytest.fixture
def after_overload(startup, monkeypatch, request):
    w = startup
    word = 'Resend your message to try again'
    pane = w.seat.pane()
    pane.width, pane.height, label = getattr(request, 'param', (120, 40, 'overload-witness'))
    launch = SimpleNamespace(version='2.1.331', pane=pane.id, pid=pane.pid)
    monkeypatch.setattr(w.seat, 'launched', lambda: launch)
    monkeypatch.setattr(w.seat, 'record', lambda: None)
    rule = '─' * pane.width
    caption = f' {label} ─' if label else ''
    top = rule[:pane.width - _cells(caption)] + caption
    url = 'https://example.invalid/permissions'
    link = f'\x1b]8;id=synthetic;{url}\x1b\\{url}\x1b]8;;\x1b\\'
    padding = '\n' * (pane.height - 5)
    plain = f'▎ {url}\n{padding}{top}\n❯\u00a0{word}\n{rule}\n  ⏵⏵ auto mode on\n'
    styled = f'▎ {link}\n{padding}' + (
        f'\x1b[38;5;244m{top}\n'
        '\x1b[39m❯\u00a0\x1b[7mR\x1b[0;2mesend your message to try again\x1b[0m\n'
        f'\x1b[38;5;244m{rule}\n\x1b[39m  ⏵⏵ auto mode on\n'
    )
    control = [ControlFrame(pane, styled, False, (2, pane.height - 3))]
    monkeypatch.setattr(w.seat, 'read_control_frame', lambda: control[0])
    w.shown[0] = plain
    door = Door(w.seat)
    monkeypatch.setattr(door, 'wall', lambda screen: None)
    return SimpleNamespace(**vars(w), door=door, launch=launch, control=control, word=word,
                           top=top, rule=rule)


@pytest.mark.parametrize('after_overload', [
    (120, 40, 'overload-witness'), (319, 78, 'overload-witness'), (273, 65, 'overload-witness'),
    (80, 24, None), (220, 52, 'First Officer'), (200, 60, 'Crew 船'),
], indirect=True)
def test_retry_hint_and_identical_draft_read_differently_at_the_door(after_overload):
    w = after_overload
    assert w.door.screen().box == ''
    assert not w.door.reading().open, 'the ordinary startup gate still applies'
    w.state.write_text(json.dumps({'hasSeenAutoDefaultNudge': True}))
    assert w.door.reading().open
    frame = w.control[0]
    row = '❯\u00a0\x1b[7mR\x1b[0;2mesend your message to try again\x1b[0m'
    draft = f'❯\u00a0{w.word}\x1b[7m \x1b[0m'
    w.control[0] = replace(frame, text=frame.text.replace(row, draft), cursor=(34, frame.cursor[1]))
    assert w.door.screen().box == w.word
    assert w.door.reading().reason == 'text in the box'
    assert w.calls == []


@pytest.mark.parametrize('rule', ['short top', 'short bottom', 'long top', 'long bottom',
                                  'malformed label', 'missing bottom'])
def test_retry_hint_requires_two_complete_native_rules(after_overload, rule):
    w = after_overload
    original, reading = {
        'short top': (w.top, w.top[1:]),
        'short bottom': (w.rule, w.rule[1:]),
        'long top': (w.top, '─' + w.top),
        'long bottom': (w.rule, '─' + w.rule),
        'malformed label': (w.top, w.top.replace('witness ─', 'witness──')),
        'missing bottom': (w.rule, ''),
    }[rule]
    w.shown[0] = w.shown[0].replace(original, reading)
    frame = w.control[0]
    w.control[0] = replace(frame, text=frame.text.replace(original, reading))
    expected = w.seat.harness.screen(w.shown[0]).box
    assert expected and w.door.screen().box == expected


@pytest.mark.parametrize('after_overload', [(33, 24, None)], indirect=True)
def test_retry_hint_stays_text_when_the_pane_is_too_narrow(after_overload):
    w = after_overload
    assert w.door.screen().box == w.word


@pytest.mark.parametrize('after_overload', [(30, 24, None)], indirect=True)
def test_wrapped_retry_hint_stays_text(after_overload):
    w = after_overload
    w.shown[0] = w.shown[0].replace('try again', 'try\n  again')
    frame = w.control[0]
    w.control[0] = replace(frame, text=frame.text.replace('try again', 'try\n  again'))
    assert w.door.screen().box == 'Resend your message to try\nagain'


@pytest.mark.parametrize('missing', ['frame', 'style', 'cursor', 'pane', 'launch', 'current'])
def test_retry_words_stay_text_with_incomplete_evidence(after_overload, missing):
    w = after_overload
    frame = w.control[0]
    if missing == 'frame':
        w.control[0] = None
    elif missing == 'style':
        w.control[0] = replace(frame, text=frame.text.replace('\x1b[0;2m', '\x1b[0m'))
    elif missing == 'cursor':
        w.control[0] = replace(frame, cursor=(2, 36))
    elif missing == 'pane':
        w.control[0] = replace(frame, pane=SimpleNamespace(**(vars(frame.pane) | {'id': '%2'})))
    elif missing == 'launch':
        w.launch.version = '2.1.317'
    elif missing == 'current':
        w.control[0] = replace(frame, text=frame.text.replace('auto mode on', 'other footer'))
    assert w.door.screen().box == w.word


@pytest.mark.parametrize('working', [False, True])
def test_fable_usage_warning_above_composer_leaves_box_and_activity_alone(startup, working):
    rule = '─' * 120
    activity = '✻ Deciphering… (2s · ↓ 12 tokens)\n' if working else ''
    warning = ("  You've used 76% of your Fable limit · resets Nov 25, 4pm (America/Vancouver)"
               " · /usage-credits to keep using Claude …")
    startup.shown[0] = f'{activity}{warning}\n{rule}\n❯\u00a0\n{rule}\n  ⏵⏵ auto mode on\n'
    screen = Door(startup.seat).screen()
    assert screen.box == '' and screen.working is working
    assert screen.quota is None and screen.interruption is None


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


@pytest.mark.parametrize('version', ['2.1.317-beta.1', '2.1.317.1'])
@pytest.mark.parametrize('selected', ['yes', 'no'])
def test_unproven_version_suffixes_never_authorize_either_key(startup, version, selected):
    w = startup
    w.state.write_text(json.dumps({'hasSeenAutoDefaultNudge': True}))
    before = w.state.read_bytes()
    w.shown[0] = frame('auto-default-offer-'+selected).replace('v2.1.317', 'v'+version)
    assert Door(w.seat).deliver('synthetic work').result == 'withheld'
    assert w.calls == [] and w.state.read_bytes() == before


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
