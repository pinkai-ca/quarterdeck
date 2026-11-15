"""OpenCode 1.19.5: measured fresh and after-turn composers, with synthetic text."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck.door import Door
from quarterdeck.harness import get
from quarterdeck.harness.opencode import OpenCodeHarness


ROOT = Path(__file__).parents[1] / 'fixtures/opencode/1.19.5'


def example(phase, case, width):
    return json.loads((ROOT / f'{phase}-{case}-{width}.json').read_text())


def door_for(reader, width):
    pane = SimpleNamespace(width=width, height=40)
    return Door(SimpleNamespace(harness=reader, pane=lambda: pane, tmux=None))


@pytest.mark.parametrize('phase,columns', [('capped', {64: 55, 80: 70, 112: 70}),
                                        ('session', {64: 55, 80: 71, 112: 103})])
@pytest.mark.parametrize('case', ['short-hyphen', 'unbroken', 'path'])
@pytest.mark.parametrize('width', [64, 80, 112])
def test_the_door_matches_both_native_layouts_without_changing_the_observation(phase, columns, case, width):
    fixture = example(phase, case, width)
    reader = OpenCodeHarness()
    screen = reader.screen(fixture['capture'])
    original_box = screen.box
    door = door_for(reader, width)

    assert screen.box is not None
    assert screen.unknown is None and screen.interruption is None and not screen.working
    assert [len(row.rstrip(' ')) for row in screen.box.splitlines()] == fixture['row_lengths']
    assert reader.box_columns_for_screen(width, screen) == columns[width]
    assert door.matches(reader.box(fixture['payload']), screen)
    assert screen.box == original_box, 'outside-composer padding stays in the native reading'
    changed = fixture['payload'] + 'z'
    assert not door.matches(reader.box(changed), screen)


def test_the_same_pane_width_reads_the_changed_layout_on_each_screen():
    reader = OpenCodeHarness()
    fresh = reader.screen(example('capped', 'unbroken', 112)['capture'])
    used = reader.screen(example('session', 'unbroken', 112)['capture'])
    assert reader.box_columns_for_screen(112, fresh) == 70
    assert reader.box_columns_for_screen(112, used) == 103
    assert reader.box_columns_for_screen(112, fresh) == 70


def test_an_unrecognised_rule_uses_the_legacy_bound_and_refuses_the_paste():
    fixture = json.loads((ROOT / 'unrecognised-rule.json').read_text())
    reader = OpenCodeHarness()
    screen = reader.screen(fixture['capture'])
    assert 'derived' in fixture['provenance']
    assert screen.box is None and screen.unknown is not None
    assert reader.box_columns_for_screen(80, screen) == reader.box_columns(80) == 73
    assert not door_for(reader, 80).matches(reader.box(fixture['payload']), screen)


@pytest.mark.parametrize('addition', ['x', ' ' * 200])
def test_comparison_does_not_remove_content_or_unbounded_padding(addition):
    fixture = example('capped', 'unbroken', 112)
    rows = fixture['capture'].splitlines()
    rows[1] += addition
    reader = OpenCodeHarness()
    screen = reader.screen('\n'.join(rows) + '\n')
    assert screen.box is not None
    assert not door_for(reader, 112).matches(reader.box(fixture['payload']), screen)


def test_nonspace_content_past_the_measured_text_edge_is_not_padding():
    fixture = example('capped', 'unbroken', 112)
    rows = fixture['capture'].splitlines()
    row = rows[1]
    # Column 92 is immediately after the 70-column text area starting at 22.
    rows[1] = row[:92] + 'z' + row[93:]
    reader = OpenCodeHarness()
    screen = reader.screen('\n'.join(rows) + '\n')
    assert screen.box is not None
    assert reader.box_comparison(screen, 112) is None
    assert not door_for(reader, 112).matches(reader.box(fixture['payload']), screen)


def test_a_rule_wider_than_the_pane_does_not_supply_geometry():
    fixture = example('capped', 'unbroken', 80)
    lines = fixture['capture'].splitlines()
    lines[-1] = '   ╹' + '▀' * 100
    reader = OpenCodeHarness()
    screen = reader.screen('\n'.join(lines))
    assert reader.box_columns_for_screen(80, screen) == reader.box_columns(80)
    assert not door_for(reader, 80).matches(reader.box(fixture['payload']), screen)


def test_the_door_requests_the_screen_bound_once():
    calls = []

    class Reader(OpenCodeHarness):
        def box_columns_for_screen(self, width, screen):
            calls.append((width, screen))
            return super().box_columns_for_screen(width, screen)

    fixture = example('capped', 'path', 112)
    reader = Reader()
    screen = reader.screen(fixture['capture'])
    assert door_for(reader, 112).matches(reader.box(fixture['payload']), screen)
    assert calls == [(112, screen)]


@pytest.mark.parametrize('name', ['claude', 'codex', 'pi', 'antigravity', 'grok', 'fake'])
def test_other_adapters_keep_their_existing_column_declaration(name):
    reader = get(name)
    screen = SimpleNamespace(text='unfamiliar drawing', box='raw box   ')
    assert reader.box_columns_for_screen(112, screen) == reader.box_columns(112)
    assert reader.box_comparison(screen, 112) == screen.box


@pytest.mark.parametrize('width', [64, 80, 112])
def test_native_opencode_slash_break_replaces_the_assumed_negative_contract(width):
    fixture = example('capped', 'slash', width)
    reader = OpenCodeHarness()
    screen = reader.screen(fixture['capture'])
    original_box = screen.box
    door = door_for(reader, width)
    assert screen.box.splitlines()[0].rstrip(' ').endswith('/')
    assert [len(row.rstrip(' ')) for row in screen.box.splitlines()] == fixture['row_lengths']
    assert door.matches(reader.box(fixture['payload']), screen)
    assert not door.matches(reader.box(fixture['payload'].replace('/branch', '/brunch')), screen)
    assert not door.matches(reader.box(fixture['payload'].replace('/branch', 'branch')), screen)
    assert not door.matches(reader.box(fixture['payload'] + 'z'), screen)
    assert screen.box == original_box


def test_a_slash_break_that_fits_the_row_still_refuses():
    fixture = example('capped', 'slash', 80)
    # Derived control keeps the same native wrapping but replaces most of its prefix.
    # The remaining word would fit; the short hard newline must not become a wrap.
    prefix = fixture['payload'].split('/')[0]
    capture = fixture['capture'].replace(prefix + '/', 'short/')
    reader = OpenCodeHarness()
    screen = reader.screen(capture)
    door = door_for(reader, 80)
    assert not door.matches(reader.box('short/branch'), screen)
    assert door.matches(reader.box('short/\nbranch'), screen)
