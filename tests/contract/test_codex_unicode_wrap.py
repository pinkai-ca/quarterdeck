"""Scrubbed 478-column composer: U+00B7 before a break, U+2026 later in the body."""

import json
from pathlib import Path

import pytest

from .test_padded_box import capture, door_for

FIXTURE = Path(__file__).parents[1] / 'fixtures/codex/unicode-wrap.json'


def test_saved_unicode_punctuation_wrap_verifies_without_opening_the_draft(monkeypatch):
    sample = json.loads(FIXTURE.read_text())
    door = door_for('codex', sample['capture'], monkeypatch, sample['pane_columns'])
    screen = door.screen()
    assert door.harness.box_columns(sample['pane_columns']) == sample['box_columns'] == 475
    rows = screen.box.splitlines()
    assert [len(row.rstrip(' ')) for row in rows] == [46, 473, 432, 30]
    assert rows[1].rstrip(' ').endswith('\u00b7')
    assert '\u2026' in rows[2]
    assert len(sample['payload'].splitlines()[1]) == 906
    assert door.matches(door.harness.box(sample['payload']), screen)
    assert not door.reading().open, 'a matching draft never grants permission to overwrite it'


@pytest.mark.parametrize('symbol', ['\u00b7', '\u2026', '\u2192'])
@pytest.mark.parametrize('payload,rows', [
    ('word{c} nextword', 'word{c}\nnextword'),
    ('longtag next{c}word', 'longtag\nnext{c}word'),
    ('longtagzz {c}nextword', 'longtagzz\n{c}nextword'),
    ('prefix {c} words', 'prefix {c}\nwords'),
])
def test_stated_symbols_at_established_word_boundaries(monkeypatch, symbol, payload, rows):
    payload, rows = payload.format(c=symbol), rows.format(c=symbol)
    door = door_for('codex', capture('codex', rows, 'full', 13), monkeypatch, 13)
    assert door.matches(door.harness.box(payload), door.screen())
    assert not door.reading().open
    for changed in (payload + '!', payload.replace(symbol, ''), payload.replace('word', 'wo rd', 1)):
        assert not door.matches(door.harness.box(changed), door.screen())
    wider = door_for('codex', capture('codex', rows, 'full', 43), monkeypatch, 43)
    assert not wider.matches(wider.harness.box(payload), wider.screen())


@pytest.mark.parametrize('payload,rows', [
    ('x part\u2026nextword', 'x part\u2026\nnextword'),
    ('x part\u2026\u2026nextword', 'x part\u2026\u2026\nnextword'),
    ('x part\u2026(nextword)', 'x part\u2026\n(nextword)'),
    ('x part\u202612345678', 'x part\u2026\n12345678'),
    ('x part\u2026+nextword', 'x part\u2026\n+nextword'),
    ('x part\u2026%nextword', 'x part\u2026\n%nextword'),
    ('saywords" \u2026nextword', 'saywords"\n\u2026nextword'),
])
def test_ellipsis_has_its_own_source_established_following_breaks(monkeypatch, payload, rows):
    door = door_for('codex', capture('codex', rows, 'full', 13), monkeypatch, 13)
    assert door.matches(door.harness.box(payload), door.screen())


@pytest.mark.parametrize('payload,rows', [
    ('x part\u00b7nextword', 'x part\u00b7\nnextword'),
    ('x part\u2192nextword', 'x part\u2192\nnextword'),
    ('x part\u2026nextword', 'x part\n\u2026nextword'),
    ('x part\u2026\u2026nextword', 'x part\u2026\n\u2026nextword'),
    ('x part\u2026.nextword', 'x part\u2026\n.nextword'),
    ('open( \u00b7nextword', 'open(\n\u00b7nextword'),
    ('open( \u2026nextword', 'open(\n\u2026nextword'),
    ('open( \u2192nextword', 'open(\n\u2192nextword'),
    ('x part\u2026"nextword', 'x part\u2026\n"nextword'),
    ('x part\u2026/nextword', 'x part\u2026\n/nextword'),
    ('x part\u2026|nextword', 'x part\u2026\n|nextword'),
    ('a 「 \u2026nextword', 'a 「\n\u2026nextword'),
    ('a אב|\u2026nextword', 'a אב|\n\u2026nextword'),
    ('part\u00b7\u00a0nextword', 'part\u00b7\nnextword'),
    ('part\u2192\u2060nextword', 'part\u2192\nnextword'),
    ('part\u2026\u200dnextword', 'part\u2026\nnextword'),
    ('a \u00b7\u0301nextword', 'a \u00b7\u0301\nnextword'),
    ('prefix \u2022nextword', 'prefix\n\u2022nextword'),
    ('prefix \u2190nextword', 'prefix\n\u2190nextword'),
    ('prefix \u2014nextword', 'prefix\n\u2014nextword'),
])
def test_symbols_do_not_authorize_arbitrary_or_unknown_contexts(monkeypatch, payload, rows):
    door = door_for('codex', capture('codex', rows, 'full', 13), monkeypatch, 13)
    assert not door.matches(door.harness.box(payload), door.screen())


@pytest.mark.parametrize('symbol', ['\u00b7', '\u2026', '\u2192'])
@pytest.mark.parametrize('unknown', ['\u0301', '\u200d', '\ufe0f', '\u2060', '\t'])
def test_preserved_unknown_grapheme_or_control_cannot_supply_piece_width(monkeypatch, symbol, unknown):
    payload = '123456789 ' + symbol + unknown + 'tail'
    rows = '123456789\n' + symbol + unknown + 'tail'
    door = door_for('codex', capture('codex', rows, 'full', 13), monkeypatch, 13)
    assert not door.matches(door.harness.box(payload), door.screen())
