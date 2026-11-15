"""Unsent native composer measurements, 15 November 2026; synthetic text and furniture."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck.door import Door
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.pi import PiHarness


ROOT = Path(__file__).parents[1] / 'fixtures'
PROGRAMS = [
    ('claude', '2.1.325', ClaudeHarness),
    ('codex', '0.169.0', CodexHarness),
    ('pi', '0.79.0', PiHarness),
]


@pytest.mark.parametrize('program,version,adapter', PROGRAMS)
@pytest.mark.parametrize('case', ['short-hyphen', 'unbroken', 'path'])
@pytest.mark.parametrize('width', [64, 80, 112])
def test_native_unfolded_long_words_match_through_the_door(program, version, adapter, case, width):
    example = json.loads((ROOT / program / version / f'wrap-{case}-{width}.json').read_text())
    reader = adapter()
    shown = reader.screen(example['capture'])
    original_box = shown.box
    pane = SimpleNamespace(width=width, height=example['height'])
    seat = SimpleNamespace(harness=reader, pane=lambda: pane, tmux=None)
    door = Door(seat)

    assert shown.box is not None
    assert ('X' in shown.box) if case != 'path' else ('/example/' in shown.box)
    assert shown.unknown is None and shown.interruption is None and not shown.working
    assert [len(row.rstrip(' ')) for row in shown.box.splitlines()] == example['row_lengths']
    assert reader.box(example['payload']).matches(shown.box, reader.box_columns(width), reader.wrap_slack)
    assert door.matches(reader.box(example['payload']), shown)
    assert shown.box == original_box
    assert not door.matches(reader.box(example['payload'].replace('X', 'Y', 1) if case != 'path'
                                       else example['payload'].replace('/example/', '/different/')), shown)
