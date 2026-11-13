"""Ordinary synthetic box geometry and reason metadata; no native renderer measurement."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from quarterdeck import pending, post, watch
from quarterdeck.door import Door, Reading
from quarterdeck.harness.types import Box
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.pi import PiHarness
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.opencode import OpenCodeHarness
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.harness.grok import GrokHarness

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


@pytest.mark.parametrize('adapter', [CodexHarness(), PiHarness(), ClaudeHarness(), OpenCodeHarness(), GrokHarness()])
def test_only_declared_adapter_text_columns_support_the_lower_bound(adapter):
    assert adapter.minimum_box_rows(TEXT, 100) == 22
    assert adapter.minimum_box_rows('a\tb', 100) is None


def test_adapter_without_declared_text_columns_keeps_unknown():
    assert AntigravityHarness().minimum_box_rows(TEXT, 100) is None


def frame(program, content, height):
    if program == 'codex':
        rows = ['› ' + content.split('\n')[0], *('  ' + x for x in content.split('\n')[1:]),
                '  synthetic-model high · synthetic status']
    else:
        rows = ['─' * 100, *content.split('\n'), '─' * 100, '~/invented-project',
                'synthetic stats  synthetic-model']
    return '\n'.join(([''] * max(0, height - len(rows)) + rows)[-height:])


def seat_for(adapter, height=16):
    pane = SimpleNamespace(id='invented-pane', width=100, height=height, dead=False)
    return SimpleNamespace(harness=adapter, tmux=SimpleNamespace(), pane=lambda: pane,
                           session=SimpleNamespace(human=False)), pane


@pytest.mark.parametrize('program,adapter', [('codex', CodexHarness()), ('pi', PiHarness())])
@pytest.mark.parametrize('height', [16, 50])
@pytest.mark.parametrize('folded', [False, True])
def test_synthetic_short_frame_and_tall_control_keep_the_existing_box_decision(program, adapter, height, folded):
    seat, pane = seat_for(adapter, height)
    marker = f'[Pasted Content {len(TEXT)} chars]' if program == 'codex' else f'[paste #1 +{TEXT.count(chr(10)) + 1} lines]'
    capture = frame(program, marker if folded else TEXT, height)
    seat.read_pane = lambda: (pane, capture)
    door = Door(seat)
    shown = door.box_shows(adapter.box(TEXT), seconds=0)
    matched = door.matches(adapter.box(TEXT), shown)
    assert matched == (folded or height == 50)
    assert ('' if matched else door.short_pane_reason(TEXT, pane)) == (REASON if height == 16 and not folded else '')


@pytest.mark.parametrize('adapter,marker', [
    (CodexHarness(), f'[Pasted Content {len(TEXT)} chars]'),
    (PiHarness(), f'[paste #1 +{TEXT.count(chr(10)) + 1} lines]'),
    (ClaudeHarness(), f'[Pasted text #1 +{TEXT.count(chr(10))} lines]'),
    (OpenCodeHarness(), f'[Pasted ~{TEXT.count(chr(10)) + 1} lines]'),
    (AntigravityHarness(), f'[Pasted text #1 +{TEXT.count(chr(10))} lines]'),
    (GrokHarness(), f'[Pasted: {TEXT.count(chr(10)) + 1} lines]'),
])
@pytest.mark.parametrize('height', [16, 50])
def test_known_fold_contract_still_fits_one_row_at_either_height(adapter, marker, height):
    assert len(marker) < adapter.box_columns(100)
    assert height > 1
    assert adapter.box(TEXT).matches(marker, adapter.box_columns(100), adapter.wrap_slack)


@pytest.fixture
def diagnostic(tmp_path, monkeypatch):
    seat, pane = seat_for(CodexHarness())
    seat.ship = SimpleNamespace(runtime=tmp_path)
    data = dict(phase='pasting', payload=TEXT, paste_geometry=[100, 16], attempts=['synthetic-attempt'])
    proof = SimpleNamespace(data=data, same_launch=lambda seat: True)
    attempt = SimpleNamespace(message='synthetic-message', path=tmp_path / 'synthetic-attempt')
    monkeypatch.setattr(pending, 'journals', lambda seat: ([proof], []))
    monkeypatch.setattr(post.Attempts, 'all', lambda self: [attempt])
    return Door(seat), pane, proof, attempt


def test_displayed_geometry_is_read_only_and_does_not_change_the_outcome(diagnostic):
    door, pane, proof, _ = diagnostic
    before = deepcopy(proof.data)
    assert door.pane_obstruction() == REASON
    assert proof.data == before
    pane.height = 50
    assert door.pane_obstruction() == ''
    assert proof.data == before and proof.data['phase'] == 'pasting'


@pytest.mark.parametrize('phase', ['prepared', 'submitting', 'submitted', 'queued', 'delivered', 'cancelled', 'uncertain'])
def test_other_outcomes_acquire_no_failed_unfolded_paste_label(diagnostic, phase):
    door, _, proof, _ = diagnostic
    proof.data['phase'] = phase
    assert door.pane_obstruction() == ''
    assert proof.data['phase'] == phase


def test_diagnostic_stays_with_its_launch_width_and_latest_attempt(diagnostic):
    door, pane, proof, attempt = diagnostic
    pane.width = 101
    assert door.pane_obstruction() == ''
    pane.width = 100
    proof.same_launch = lambda seat: False
    assert door.pane_obstruction() == ''
    proof.same_launch = lambda seat: True
    attempt.path = attempt.path.with_name('later-attempt')
    assert door.pane_obstruction() == ''


@pytest.mark.parametrize('geometry', [None, [], [True, 16], [100, -1], '100x16'])
def test_unknown_geometry_keeps_the_existing_generic_reading(diagnostic, geometry):
    door, _, proof, _ = diagnostic
    proof.data['paste_geometry'] = geometry
    assert door.pane_obstruction() == ''


def test_human_readings_get_no_geometry_annotation(diagnostic):
    door, pane, _, _ = diagnostic
    door.seat.session.human = True
    assert door.pane_obstruction() == ''
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
    assert 'invented-seat: ' + REASON in line
    if overflow:
        assert 'details: qd status' in line
