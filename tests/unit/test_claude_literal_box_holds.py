"""Hold gates for Claude's literal paste box: only the message, wrapped as Claude wraps it, matches.

Law one-door: Claude Code 2.1.317 keeps a short multi-line paste inline, wraps before a whole
whitespace-delimited word (hyphens included) and pads each row with spaces. Accepting that layout may
only widen what counts as the message wrapped by the program. A box with other words, fewer or more
words, a row wider than the text area, or a fold naming another line count is not the message. (Where a
box shows the same words broken at another place in a full row, it still shows the message; that is a
layout question, not a hold.)
"""

import textwrap

import pytest

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
