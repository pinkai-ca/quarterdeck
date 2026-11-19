"""Ordinary synthetic box geometry and reason metadata; no native renderer measurement."""
from copy import deepcopy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from quarterdeck import pending, post, watch, pane_obstruction as pane_meta
from quarterdeck.door import Door, Reading
from quarterdeck.harness.types import Box
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.pi import PiHarness
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.opencode import OpenCodeHarness
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.harness.grok import GrokHarness
from quarterdeck.harness.fake import FakeHarness

TEXT = '\n'.join(f'invented line {n:02}: ' + 'plain synthetic words ' * 2 for n in range(22))
REASON = 'pane too short: at least 22 rows needed for the unfolded message (16 available); enlarge the pane'


@pytest.mark.parametrize('text,columns,expected', [
    ('x' * 98, 97, 2), ('a\nb\nc', 97, 3),
    ('a' + ' ' * 1000 + 'b', 97, 1),
    ('\n\n a \n\n b \n\n', 97, 2), ('   ', 97, 0),
    ('a\tb', 97, None), ('é', 97, None), ('e\u0301', 97, None),
    ('x\x1b', 97, None), ('a\r\nb', 97, None),
    ('x', None, None), ('x', 0, None), ('x', True, None),
])
def test_row_bound_does_not_invent_unknown_layout_or_count_discardable_spaces(text, columns, expected):
    assert Box(text).minimum_rows(columns) == expected


@pytest.mark.parametrize('adapter', [CodexHarness(), PiHarness(), ClaudeHarness(), OpenCodeHarness(), GrokHarness(), AntigravityHarness()])
def test_only_declared_adapter_text_columns_support_the_lower_bound(adapter):
    assert adapter.minimum_box_rows(TEXT, 100) == 22
    assert adapter.minimum_box_rows('a\tb', 100) is None


def test_adapter_without_declared_text_columns_keeps_unknown():
    assert FakeHarness().minimum_box_rows(TEXT, 100) is None
    seat, pane = seat_for(FakeHarness(), 16)
    assert Door(seat).short_pane_reason(TEXT, pane) == '', 'no declared columns: no short-pane reading at all'


# Each program whose fold is known, at 100 columns. The frames are synthetic: an invented payload inside each program's
# composer chrome as its measured fixtures draw it (tests/fixtures/<program>/<version>): Codex's '›' prompt and its
# status row (0.166.0); pi's rules (0.74.2); Claude Code's '❯' between rules (2.1.317, paste-3-lines-shown.txt);
# OpenCode's '┃' bar and '╹▀' rule (1.19.5, session-*.json); Grok Build's bordered composer (1.0.77,
# draft-multiline.json); Antigravity's '>' between rules (1.5.2, idle.txt). A 22-line paste shown whole is
# synthetic for every one of them: each folds a paste that long. It stands for any paste a program shows whole that
# is taller than its pane, and the folded form is each program's own marker for this paste.
PROGRAMS = [('codex', CodexHarness()), ('pi', PiHarness()), ('claude', ClaudeHarness()),
            ('opencode', OpenCodeHarness()), ('grok', GrokHarness()), ('antigravity', AntigravityHarness())]


def payload(program):
    """Grok's frames carry the same 22 lines with their line-end spaces stripped. Grok Build's box refuses a line that
    ends in a space inside its padded composer, at any pane height: a separate finding about the door's comparison,
    put to the captain on its own and not changed here. The short-pane decision is what these frames prove."""
    return '\n'.join(line.rstrip(' ') for line in TEXT.split('\n')) if program == 'grok' else TEXT


def marker(program):
    text = payload(program)
    lines, newlines = text.count('\n') + 1, text.count('\n')
    return {'codex': f'[Pasted Content {len(text)} chars]', 'pi': f'[paste #1 +{lines} lines]',
            'claude': f'[Pasted text #1 +{newlines} lines]', 'opencode': f'[Pasted ~{lines} lines]',
            'grok': f'[Pasted: {lines} lines]', 'antigravity': f'[Pasted text #1 +{newlines} lines]'}[program]


def composer(program, content, width=100):
    lines = content.split('\n')
    if program == 'codex':
        return ['› ' + lines[0], *('  ' + x for x in lines[1:]), '  synthetic-model high · synthetic status']
    if program == 'claude':
        return ['─' * (width - 15) + ' invented-seat ─', '❯ ' + lines[0], *('  ' + x for x in lines[1:]), '─' * width,
                '  ⏵⏵ auto mode on (shift+tab to cycle)']
    if program == 'antigravity':
        return ['─' * width, '> ' + lines[0], *('  ' + x for x in lines[1:]), '─' * width,
                '? for shortcuts' + ' ' * 60 + 'Synthetic Model · high']
    if program == 'opencode':
        return ['  ┃', *('  ┃  ' + x for x in lines), '  ┃', '  ┃  Build auto · Synthetic Model', '  ╹' + '▀' * (width - 4),
                '', '  /invented/work' + ' ' * 70 + '1.19.5']
    if program == 'grok':
        inner, tail = width - 6, ' Grok 4.7 (high) · always-approve ─╯'
        return ['  /invented/project', '', '  ╭' + '─' * inner + '╮',
                *('  │' + (prefix + x).ljust(inner) + '│' for prefix, x in zip([' ❯ '] + ['   '] * len(lines), lines)),
                '  ╰' + '─' * (width - 5 - len(tail)) + tail, '',
                '  Enter:send  │  Alt+Enter:newline  │  Shift+Tab:mode  │  Ctrl+x:shortcuts']
    return ['─' * width, *lines, '─' * width, '~/invented-project', 'synthetic stats  synthetic-model']


def frame(program, content, height):
    rows = composer(program, content)
    return '\n'.join(([''] * max(0, height - len(rows)) + rows)[-height:])


def seat_for(adapter, height=16):
    pane = SimpleNamespace(id='invented-pane', width=100, height=height, dead=False, pid=7)
    return SimpleNamespace(harness=adapter, tmux=SimpleNamespace(), pane=lambda: pane,
                           session=SimpleNamespace(human=False)), pane


@pytest.mark.parametrize('program,adapter', PROGRAMS)
@pytest.mark.parametrize('height', [16, 50])
@pytest.mark.parametrize('folded', [False, True])
def test_synthetic_short_frame_and_tall_control_keep_the_existing_box_decision(program, adapter, height, folded):
    seat, pane = seat_for(adapter, height)
    text = payload(program)
    capture = frame(program, marker(program) if folded else text, height)
    seat.read_pane = lambda: (pane, capture)
    door = Door(seat)
    shown = door.box_shows(adapter.box(text), seconds=0)
    matched = door.matches(adapter.box(text), shown)
    assert matched == (folded or height == 50)
    assert ('' if matched else door.short_pane_reason(text, pane)) == (REASON if height == 16 and not folded else '')


@pytest.mark.parametrize('program,adapter', PROGRAMS)
@pytest.mark.parametrize('height', [16, 50])
def test_known_fold_contract_still_fits_one_row_at_either_height(program, adapter, height):
    """The fold marker fits one row of the text area, and read from a frame of that height it is the box, whole."""
    text, folded = payload(program), marker(program)
    assert len(folded) < adapter.box_columns(100)
    assert adapter.box(text).matches(folded, adapter.box_columns(100), adapter.wrap_slack)
    seat, pane = seat_for(adapter, height)
    capture = frame(program, folded, height)
    assert len(capture.split('\n')) == height
    seat.read_pane = lambda: (pane, capture)
    door = Door(seat)
    shown = door.box_shows(adapter.box(text), seconds=0)
    assert shown.box.strip() == folded and door.matches(adapter.box(text), shown)


@pytest.fixture
def diagnostic(tmp_path):
    seat, pane = seat_for(CodexHarness())
    seat.name = 'invented-seat'
    seat.ship = SimpleNamespace(runtime=tmp_path)
    seat.launched = lambda: SimpleNamespace(pane=pane.id, pid=pane.pid, at='invented-launch')
    data = dict(phase='pasting', payload=TEXT, paste_geometry=[100, 16], attempts=['synthetic-attempt'],
                identity=dict(pane=pane.id, pid=pane.pid, launch='invented-launch', harness='codex'),
                since='2001-01-01T00:00:01+00:00')
    path = tmp_path / 'post' / 'pending' / seat.name / 'synthetic-journal.json'
    proof = pending.Proof(path, {})
    proof.save(**data)
    pane_meta.publish(proof)
    door = Door(seat)
    door.captured_pane = pane
    return door, pane, proof, path


def test_displayed_geometry_is_read_only_and_does_not_change_the_outcome(diagnostic):
    door, pane, proof, _ = diagnostic
    before = deepcopy(proof.data)
    assert door.pane_obstruction() == REASON
    assert proof.data == before
    pane.height = 50
    assert door.pane_obstruction() is None
    assert proof.data == before and proof.data['phase'] == 'pasting'


@pytest.mark.parametrize('phase', ['prepared', 'submitting', 'submitted', 'queued', 'delivered', 'cancelled', 'uncertain'])
def test_other_outcomes_acquire_no_failed_unfolded_paste_label(diagnostic, phase):
    door, _, proof, _ = diagnostic
    proof.save(phase=phase)
    assert door.pane_obstruction() is None
    assert proof.data['phase'] == phase


def test_diagnostic_stays_with_its_launch_width_and_later_confirmation(diagnostic):
    door, pane, proof, path = diagnostic
    pane.width = 101
    assert door.pane_obstruction() is None
    pane.width = 100
    original = dict(proof.data['identity'])
    proof.save(identity={**original, 'launch': 'earlier-launch'})
    assert door.pane_obstruction() is None
    proof.save(identity=original)
    later = pending.Proof(path.with_name('later-journal.json'), {})
    later.save(**{**proof.data, 'phase': 'delivered', 'since': '2001-01-01T00:00:02+00:00'})
    assert door.pane_obstruction() is None


@pytest.mark.parametrize('geometry', [None, [], [True, 16], [100, -1], '100x16'])
def test_unknown_geometry_keeps_the_existing_generic_reading(diagnostic, geometry):
    door, _, proof, _ = diagnostic
    proof.save(paste_geometry=geometry)
    assert door.pane_obstruction() is None


def test_human_readings_get_no_geometry_annotation(diagnostic):
    door, pane, _, _ = diagnostic
    door.seat.session.human = True
    assert door.pane_obstruction() is None
    assert door.short_pane_reason(TEXT, pane) == ''


@pytest.mark.parametrize('original', [Reading(True), Reading(False, 'old reason', 'old remedy'),
                                      Reading(False, 'old waiting', 'old remedy', waiting=True)])
def test_reason_metadata_keeps_every_admission_field(original):
    displayed = replace(original, diagnostic=REASON)
    assert replace(displayed, diagnostic='') == original
    assert REASON in str(displayed)


@pytest.mark.parametrize('overflow', [False, True])
def test_status_names_the_seat_and_short_pane_even_when_other_alerts_overflow(overflow):
    facts = watch.Facts(service='up', waiting=0, oldest=0, blind=1,
                        pane_obstructions=['invented-seat: ' + REASON],
                        unknown=['synthetic ordinary alert ' * 20] if overflow else [])
    line = facts.line()
    if overflow:
        from quarterdeck.headlines import details
        assert 'invented-seat: ' + REASON in line and line.endswith(' · qd status')
        assert 'invented-seat: ' + REASON in details(facts)
    else:
        assert 'invented-seat: ' + REASON in line


@pytest.mark.parametrize('program,adapter', PROGRAMS)
@pytest.mark.parametrize('height', [16, 50])
@pytest.mark.parametrize('folded', [False, True])
def test_delivery_path_adds_only_reason_metadata_to_the_synthetic_failed_paste(program, adapter, height, folded,
                                                                               tmp_path):
    """Through the door's own delivery: a paste shown whole that the pane cannot hold is held with the reason; a folded
    one, or one the pane holds, is submitted at either height."""
    text = payload(program)
    seat, pane = seat_for(adapter, height)
    pane.pid, pane.in_mode = 7, False
    seat.name = 'invented-seat'
    seat.ship = SimpleNamespace(runtime=tmp_path)
    seat.pool = SimpleNamespace(name='invented-pool')
    seat.launched = lambda: SimpleNamespace(pane=pane.id, pid=pane.pid, at='invented-launch',
                                             pool='invented-pool', version='invented-version')
    seat.record = lambda: None
    seat.home = tmp_path / 'home'  # Claude Code types only once its startup choice is answered, as a launch leaves it
    seat.home.mkdir()
    (seat.home / '.claude.json').write_text(json.dumps({'hasSeenAutoDefaultNudge': True}))

    class Terminal:
        pasted = False
        submits = []

        def paste(self, target, pasted):
            self.pasted = True
            assert target == pane.id and pasted == text

        def pane(self, target):
            return pane

        def keys(self, target, key):
            self.submits.append(key)

    terminal = Terminal()
    seat.tmux = terminal
    shown = marker(program) if folded else text
    seat.read_pane = lambda: (pane, frame(program, shown if terminal.pasted else '', height))

    class Journal:
        path = tmp_path / 'synthetic-journal.json'
        data = dict(phase='prepared', payload=text, since='2001-01-01T00:00:01+00:00',
                    identity=dict(pane=pane.id, pid=pane.pid, launch='invented-launch', harness=adapter.name))

        def same_launch(self, seat):
            return True

        def save(self, **fields):
            self.data.update(fields)
            self.path.write_text(json.dumps(self.data))

    proof = Journal()
    door = Door(seat)
    check = door.box_shows
    door.box_shows = lambda box: check(box, seconds=0)  # a stable synthetic frame needs no polling
    result = door._deliver(text, confirm=False, proof=proof)
    if height == 16 and not folded:
        assert result.result == 'blind' and result.detail.startswith(REASON), result.detail
        assert proof.data['phase'] == 'pasting'
        assert proof.data['paste_geometry'] == [100, 16]
        assert pane_meta.marker_path(proof.path).is_file()
        assert terminal.submits == []
    else:
        assert result.result == 'typed' and proof.data['phase'] == 'submitted', result.detail
        assert 'paste_geometry' not in proof.data
        assert terminal.submits == [adapter.keys.submit]


def test_no_short_pane_journal_needs_no_liveness_or_attempt_query(diagnostic, monkeypatch):
    door, _, proof, _ = diagnostic
    pane_meta.marker_path(proof.path).unlink()
    def unnecessary(*args):
        raise AssertionError('the diagnostic must not query a pane, launch, or attempts without its journal')
    door.seat.pane = unnecessary
    door.seat.launched = unnecessary
    monkeypatch.setattr(post.Attempts, 'all', unnecessary)
    assert door.pane_obstruction() is None


@pytest.mark.parametrize('captured', [False, True])
def test_journal_diagnostic_uses_only_the_owned_frame_or_its_saved_geometry(diagnostic, captured):
    door, _, proof, _ = diagnostic
    before = deepcopy(proof.data)
    if not captured:
        door.captured_pane = None
    def redundant(*args):
        raise AssertionError('no second pane query, including through Proof.same_launch')
    door.seat.pane = redundant
    proof.same_launch = redundant
    assert door.pane_obstruction() == (REASON if captured else 'last failed paste: ' + REASON)
    assert proof.data == before
