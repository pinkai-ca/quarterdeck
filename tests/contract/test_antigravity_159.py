"""Synthetic input layouts measured on one idle 1.5.9 composer; no native account data."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.door import Door
from quarterdeck.ship import SeatSpec


ROOT = Path(__file__).parents[1] / 'fixtures/antigravity/1.5.9'
PARAGRAPH = (
    'Please report the measured input box width and keep this synthetic paragraph together '
    'when it crosses the terminal edge. This harmless example contains ordinary words, '
    'punctuation, and enough text to wrap twice.'
)


@pytest.mark.parametrize('width, row_lengths', [(112, [105, 105]), (80, [76, 74, 59])])
def test_unfolded_paragraph_matches_measured_word_wrap(width, row_lengths):
    reader = AntigravityHarness()
    screen = reader.screen((ROOT / f'paragraph-{width}.txt').read_text())

    assert len(PARAGRAPH) == 211
    assert screen.unknown is None and screen.interruption is None and not screen.working
    assert [len(row.rstrip(' ')) for row in screen.box.splitlines()] == row_lengths
    assert reader.box_geometry_known
    assert reader.box(PARAGRAPH).matches(screen.box, reader.box_columns(width), reader.wrap_slack)


@pytest.mark.parametrize('width, row_lengths', [(112, [109, 109, 3]), (80, [77, 77, 67])])
def test_unfolded_ruler_matches_measured_character_wrap(width, row_lengths):
    reader = AntigravityHarness()
    screen = reader.screen((ROOT / f'ruler-{width}.txt').read_text())
    payload = 'X' * 221

    assert [len(row) for row in screen.box.splitlines()] == row_lengths
    assert reader.box(payload).matches(screen.box, reader.box_columns(width), reader.wrap_slack)
    assert reader.minimum_box_rows(payload, width) == len(row_lengths)


@pytest.mark.parametrize('width, lengths', [
    (64, [14, 61, 61, 57]), (80, [14, 77, 73, 28]), (112, [14, 109, 70]),
])
@pytest.mark.parametrize('envelope', [False, True])
def test_long_hyphenated_path_moves_whole_then_hard_wraps(width, lengths, envelope):
    reader = AntigravityHarness()
    payload = ('Write the file /example/long-path-' + 'x' * 120
               + '/result.txt containing exactly one line.')
    name = 'hyphen'
    if envelope:
        name = 'daily'
        payload = ('qd > synthetic wrapping example\n' + payload.removesuffix('.')
                   + ':\nsynthetic\nThen report written.')
        lengths = [31, *lengths, 9, 20]
    screen = reader.screen(json.loads((ROOT / f'unbroken-{name}-{width}.json').read_text())['screen'])
    assert screen.unknown is None and screen.interruption is None and not screen.working
    assert [len(row.rstrip(' ')) for row in screen.box.splitlines()] == lengths
    seat = SimpleNamespace(harness=reader, tmux=None, pane=lambda: SimpleNamespace(width=width, height=40))
    assert Door(seat).matches(reader.box(payload), screen)
    assert reader.box(payload).wrapped_rows(reader.box_columns(width), reader.wrap_slack) == [
        row.rstrip(' ') for row in screen.box.splitlines()
    ]


@pytest.mark.parametrize('width', [64, 80, 112])
def test_short_hyphenated_word_moves_whole_when_it_straddles_the_edge(width):
    reader = AntigravityHarness()
    payload = 'X' * (width - 13) + ' alpha-beta'
    screen = reader.screen(json.loads((ROOT / f'unbroken-short-hyphen-{width}.json').read_text())['screen'])
    assert [row.rstrip(' ') for row in screen.box.splitlines()] == ['X' * (width - 13), 'alpha-beta']
    assert reader.box(payload).matches(screen.box, reader.box_columns(width), reader.wrap_slack)


@pytest.mark.parametrize('width, offset, row_lengths', [
    (112, 4, [108, 1]), (112, 3, [106, 4]), (112, 2, [107, 4]),
    (80, 4, [76, 1]), (80, 3, [74, 4]), (80, 2, [75, 4]),
])
def test_word_boundary_fits_then_wraps_with_the_measured_margin(width, offset, row_lengths):
    reader = AntigravityHarness()
    payload = 'X' * (width - 3 - offset) + ' at z'
    screen = reader.screen((ROOT / f'boundary-{width}-{offset}.txt').read_text())

    assert [len(row.rstrip(' ')) for row in screen.box.splitlines()] == row_lengths
    assert reader.box(payload).matches(screen.box, reader.box_columns(width), reader.wrap_slack)


@pytest.mark.parametrize('width', [112, 80])
@pytest.mark.parametrize('offset', [None, 4, 3, 2])
def test_door_pastes_and_submits_unfolded_text_once(tmp_path, width, offset):
    """An ordinary idle composer renders the paste using the measured native layout."""
    reader = AntigravityHarness()
    name = f'paragraph-{width}' if offset is None else f'boundary-{width}-{offset}'
    payload = PARAGRAPH if offset is None else 'X' * (width - 3 - offset) + ' at z'
    shown = (ROOT / f'{name}.txt').read_text()
    rule = '─' * width
    idle = f'{rule}\n>\n{rule}\n? for shortcuts       Gemini 4 Argon · medium\n'
    pane = SimpleNamespace(id='%1', pid=123, width=width, height=40, dead=False, in_mode=False)
    events = []

    class Composer:
        surface = idle

        def paste(self, pane_id, text):
            events.append(('paste', pane_id, text))
            self.surface = shown

        def pane(self, pane_id):
            return pane

        def keys(self, pane_id, *keys):
            events.append(('keys', pane_id, keys))
            self.surface = idle

    composer = Composer()
    seat = SimpleNamespace(
        name='probe', harness=reader, tmux=composer, folder=tmp_path / 'probe',
        ship=SimpleNamespace(runtime=tmp_path / 'runtime'),
        session=SimpleNamespace(human=False), pool=SimpleNamespace(name='google'),
        spec=SeatSpec('probe', 'probe', 'worker', 'antigravity', ('google',)),
        pane=lambda: pane, read_pane=lambda: (pane, composer.surface),
        launched=lambda: None, record=lambda: None,
    )

    result = Door(seat).deliver(payload, confirm=False)

    assert result.result == 'typed'
    assert events == [('paste', '%1', payload), ('keys', '%1', ('Enter',))]
