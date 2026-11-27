"""Hold gates for Claude's literal paste box: only the message, wrapped as Claude wraps it, matches.

Law one-door: Claude Code 2.1.317 keeps a short multi-line paste inline, wraps before a whole
whitespace-delimited word (hyphens included) and pads each row with spaces. Accepting that layout may
only widen what counts as the message wrapped by the program. A box with other words, fewer or more
words, a row wider than the text area, or a fold naming another line count is not the message. (Where a
box shows the same words broken at another place in a full row, it still shows the message; that is a
layout question, not a hold.)
"""

import json
import textwrap
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import pending
from quarterdeck.door import Door
from quarterdeck.harness.claude import ClaudeHarness

SHORT = '-'.join(('rep', '0' * 4)), '-'.join(('ord', '0' * 4))
HEADER = f'qd > report from synthetic-worker · {SHORT[0]} · re {SHORT[1]}'
BODY = ('Done: the synthetic-setup-consent change and its well-known follow-up-checks are built, '
        'and the long-hyphenated-compound-word stays together whenever it fits a fresh row of the box. '
        'Named and whole runs passed; nothing-else-changed.')
MESSAGE = f'{HEADER}\n{BODY}'
WIDTHS = [76, 117]


def claude_rows(text, columns, break_on_hyphens=False):
    rows = []
    for line in text.rstrip('\n').split('\n'):
        rows.extend(textwrap.wrap(line, width=columns, break_on_hyphens=break_on_hyphens,
                                  replace_whitespace=False) or [''])
    return rows


def shown(rows, columns):
    return '\n'.join(row.ljust(columns + 2) for row in rows)


def matches(text, box, columns):
    reader = ClaudeHarness()
    return reader.box(text).matches(box, columns, reader.wrap_slack)


@pytest.mark.parametrize('columns', WIDTHS)
def test_the_measured_literal_layout_matches(columns):
    """The control: the layout Claude draws matches, so every refusal below is the box's doing."""
    rows = claude_rows(MESSAGE, columns)
    assert len(rows) > 2, 'the fixture must wrap'
    assert matches(MESSAGE, shown(rows, columns), columns)


def spoiled(columns):
    rows = claude_rows(MESSAGE, columns)
    other = [r.replace('built', 'bilt', 1) for r in rows]
    fewer = claude_rows(MESSAGE.rsplit(' ', 1)[0], columns)
    more = rows + ["a person's own draft"]
    too_wide = [rows[0] + ' ' + rows[1]] + rows[2:]
    return {
        'other-word': shown(other, columns),
        'fewer-words': shown(fewer, columns),
        'more-words': shown(more, columns),
        'row-too-wide': '\n'.join(too_wide),
    }


@pytest.mark.parametrize('columns', WIDTHS)
@pytest.mark.parametrize('case', ['other-word', 'fewer-words', 'more-words', 'row-too-wide'])
def test_a_literal_box_that_is_not_the_message_wrapped_as_claude_wraps_does_not_match(columns, case):
    box = spoiled(columns)[case]
    assert box != shown(claude_rows(MESSAGE, columns), columns)
    assert not matches(MESSAGE, box, columns)


def test_a_fold_naming_another_line_count_does_not_match():
    reader = ClaudeHarness()
    box = reader.box(MESSAGE)
    assert box.matches('[Pasted text #7 +1 lines]', 76, reader.wrap_slack)
    assert not box.matches('[Pasted text #7 +2 lines]', 76, reader.wrap_slack)
    assert not box.matches('[Pasted text #7]', 76, reader.wrap_slack)


COMPLETE_DRAFTS = json.loads(
    (Path(__file__).parents[1] / 'fixtures' / 'claude' / 'complete-word-wrap.json').read_text()
)
FIRST_EDGE_ROW = {'short': 3, 'medium': 3, 'long': 3, 'full': 4, 'varied': 3}


def complete_draft_matches(payload, composer, *, held=False):
    harness = ClaudeHarness()
    pane = SimpleNamespace(width=112, height=34)
    seat = SimpleNamespace(name='synthetic-seat', harness=harness, tmux=None, pane=lambda: pane,
                           session=SimpleNamespace(human=False))
    door = Door(seat)
    screen = harness.screen('\n'.join(composer))
    if held:
        proof = SimpleNamespace(data=dict(phase='submitted', payload=payload),
                                same_launch=lambda current: current is seat)
        accepted, _ = pending._submit_candidate(door, screen, pane, [proof])
        return accepted is proof
    return door.matches(harness.box(payload), screen)


@pytest.fixture(params=['paste', 'held'])
def draft_matches(request):
    return partial(complete_draft_matches, held=request.param == 'held')


def composer_rows(rows):
    rule = '─' * 112
    return [rule, '❯ ' + rows[0], *('  ' + row for row in rows[1:]), rule]


@pytest.mark.parametrize('name', COMPLETE_DRAFTS)
def test_an_observed_complete_draft_can_wrap_a_word_at_the_exact_edge(draft_matches, name):
    assert draft_matches(**COMPLETE_DRAFTS[name])


@pytest.mark.parametrize('name', COMPLETE_DRAFTS)
def test_a_changed_character_at_an_observed_word_break_is_refused(draft_matches, name):
    capture = COMPLETE_DRAFTS[name]
    composer = capture['composer'].copy()
    following = FIRST_EDGE_ROW[name] + 1  # the upper rule is at index 0
    row = composer[following]
    assert row.startswith('  ') and row[2].isalpha()
    composer[following] = '  ' + ('Z' if row[2] != 'Z' else 'X') + row[3:]
    assert not draft_matches(capture['payload'], composer)


@pytest.mark.parametrize('word_width, wraps, accepted', [
    (5, False, True),  # fits with one cell left
    (5, True, False),  # breaking here is too early
    (6, True, True),  # would end in the last text cell
    (7, True, True),  # would exceed the text area by one
])
def test_complete_draft_word_wrap_boundaries(word_width, wraps, accepted):
    prefix, word = 'a' * 101, 'orchard'[:word_width]
    payload = f'Field notes\n{prefix} {word}\nNext entry.'
    draft = [prefix, word] if wraps else [f'{prefix} {word}']
    assert complete_draft_matches(payload, composer_rows(['Field notes', *draft, 'Next entry.'])) is accepted

    changed = draft.copy()
    changed[-1] = changed[-1][:-len(word)] + 'z' + word[1:]
    assert not complete_draft_matches(payload, composer_rows(['Field notes', *changed, 'Next entry.']))


@pytest.mark.parametrize('split', [107, 108, 109])
def test_a_complete_draft_can_split_a_long_word_only_at_hard_width(split):
    word = 'a' * 111
    payload = f'Field notes\n{word}\nNext entry.'
    rows = ['Field notes', word[:split], word[split:], 'Next entry.']
    assert complete_draft_matches(payload, composer_rows(rows)) is (split == 108)
    rows[2] = 'z' + rows[2][1:]
    assert not complete_draft_matches(payload, composer_rows(rows))


def test_an_incomplete_display_of_an_observed_draft_is_refused(draft_matches):
    capture = COMPLETE_DRAFTS['varied']
    composer = capture['composer']
    assert not draft_matches(capture['payload'], composer[3:])
    assert not draft_matches(capture['payload'], composer[:2] + composer[3:])
