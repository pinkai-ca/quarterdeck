"""qd submit and the second look accept a held box that only pads or soft-wraps the pending message."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import pending, post
from quarterdeck.door import Door
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.claude_held_box import held_edge_wraps
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.types import _cells, held_box_matches
from quarterdeck.post_message import typed


def words(length: int, seed: int = 0) -> str:
    """Synthetic prose of exactly ``length`` characters, no trailing space."""
    sizes = [3, 7, 2, 5, 11, 4, 9, 1, 6, 8, 12, 3, 5]
    out, index = [], seed
    while len(' '.join(out)) < length:
        size = sizes[index % len(sizes)]
        out.append(chr(ord('a') + index % 26) * size)
        index += 1
    text = ' '.join(out)[:length]
    return text[:-1] + '.' if text.endswith(' ') else text


def rows_for(text: str, columns: int) -> list[str]:
    """Claude 2.1.317's composer: wrap before a whole word; only a word wider than the text area splits."""
    rows = []
    for line in text.split('\n'):
        row = ''
        for word in line.split(' '):
            while len(word) > columns:  # an over-wide word fills rows, then continues
                if row:
                    rows.append(row)
                    row = ''
                rows.append(word[:columns])
                word = word[columns:]
            if not row:
                row = word
            elif len(row) + 1 + len(word) <= columns:
                row += ' ' + word
            else:
                rows.append(row)  # the separating space hangs off the row, painted like padding
                row = word
        rows.append(row)
    return rows


def pane_text(text: str, width: int, *, rows: list[str] | None = None, padding: str = 'pane') -> str:
    """A Claude pane at ``width``, its composer rows wrapped at the text area (width - 4) and padded.

    'pane': each box row, as the adapter reads it, padded with spaces to the pane's whole width, as
    the two held readings were ('padded at the screen width'). 'measured': padded only to the two
    cells beyond the text area that 2.1.317 was measured to paint, which the adapter already accepts.
    'none': keep each supplied row's own padding.
    """
    rows = rows_for(text, width - 4) if rows is None else rows
    pad = {'pane': width, 'measured': width - 2, 'none': 0}[padding]
    body = [('❯ ' if index == 0 else '  ') + row.ljust(pad) for index, row in enumerate(rows)]
    return '\n'.join(['earlier output', '─' * width, *body, '─' * width, '  ⏵⏵ bypass permissions on'])


def candidate(payload: str, text: str, width: int):
    """pending._submit_candidate, the check qd submit and held readings share, on a parsed Claude pane."""
    harness = ClaudeHarness()
    pane = SimpleNamespace(id='%1', width=width, height=60, dead=False, in_mode=False)
    seat = SimpleNamespace(name='synthetic-seat', harness=harness, tmux=None, pane=lambda: pane,
                           session=SimpleNamespace(human=False))
    screen = harness.screen(text)
    assert screen.box is not None and not screen.interruption
    proof = SimpleNamespace(data=dict(phase='submitted', payload=payload), same_launch=lambda seat: True)
    return pending._submit_candidate(Door(seat), screen, pane, [proof])


MESSAGE = '\n'.join([words(420, 0), '', words(300, 5), '- ' + words(160, 9), 'x' * 130 + ' ' + words(40, 3)])


@pytest.mark.parametrize('padding', ['pane', 'measured'])
@pytest.mark.parametrize('width', [80, 120])
def test_a_padded_wrapped_message_of_a_thousand_characters_is_accepted(width, padding):
    assert 980 <= len(MESSAGE) <= 1100
    proof, refusal = candidate(MESSAGE, pane_text(MESSAGE, width, padding=padding), width)
    assert proof is not None, refusal
    assert held_box_matches(MESSAGE + '\n', ClaudeHarness().screen(pane_text(MESSAGE, width)).box,
                            width - 4, width)


@pytest.mark.parametrize('width', [80, 120])
@pytest.mark.parametrize('change', ['one character', 'newline missing', 'blank line missing',
                                    'newline added', 'word added',
                                    'word missing', 'short row joined'])
def test_any_other_difference_is_refused(width, change):
    columns = width - 4
    rows = rows_for(MESSAGE, columns)
    if change == 'one character':
        shown = rows_for(MESSAGE[:500] + 'Q' + MESSAGE[501:], columns)
    elif change == 'newline missing':  # the message's own newline, joined as if it were a space
        # A join that lands exactly on a soft wrap paints the very same box, which no reader can tell
        # apart; take the first newline whose loss changes what the box shows.
        shown = next(joined for joined in (rows_for(MESSAGE[:at] + ' ' + MESSAGE[at + 1:], columns)
                                           for at, char in enumerate(MESSAGE) if char == '\n')
                     if joined != rows)
    elif change == 'blank line missing':
        shown = rows_for(MESSAGE.replace('\n\n', '\n', 1), columns)
    elif change == 'newline added':  # a person's Enter where the message has a space that fit
        at = MESSAGE.index(' ')  # inside the first row at either width
        shown = rows_for(MESSAGE[:at] + '\n' + MESSAGE[at + 1:], columns)
    elif change == 'word added':
        shown = rows_for(MESSAGE + ' more', columns)
    elif change == 'word missing':
        shown = rows_for(MESSAGE.replace(' ' + MESSAGE.split(' ')[3] + ' ', ' ', 1), columns)
    else:  # a row cut short and wrapped where the next word would have fit
        index = next(i for i, row in enumerate(rows) if len(row) > 20 and ' ' in row and i + 1 < len(rows)
                     and rows[i + 1] and MESSAGE.find(row + ' ' + rows[i + 1]) >= 0)
        head, tail = rows[index].rsplit(' ', 1)
        shown = rows[:index] + [head, tail + ' ' + rows[index + 1]] + rows[index + 2:]
        assert ' '.join(shown[index:index + 2]) == ' '.join(rows[index:index + 2])
    assert shown != rows
    proof, refusal = candidate(MESSAGE, pane_text(MESSAGE, width, rows=shown), width)
    assert proof is None and 'not exactly a pending message' in refusal


def test_a_wrap_is_soft_only_where_the_row_reaches_the_text_edge():
    # Text area 10 columns, pane 12: rows may carry painted spaces up to the pane's width.
    assert held_box_matches('alpha beta gamma', 'alpha beta'.ljust(12) + '\ngamma', 10, 12)
    assert held_box_matches('alpha beta gamma', 'alpha beta\ngamma', 10, 12)  # a full row, unpadded
    assert held_box_matches('abcdefghijkl', 'abcdefghij\nkl', 10, 12)  # a word broken at a full row
    assert held_box_matches('alpha\nbeta', 'alpha'.ljust(12) + '\n' + 'beta'.ljust(12), 10, 12)  # its own newline
    assert held_box_matches('alpha\n', 'alpha'.ljust(12) + '\n', 10, 12)  # a final newline, either side
    assert not held_box_matches('alpha beta gamma', 'alpha'.ljust(9) + '\nbeta gamma', 10, 12)  # short row
    assert not held_box_matches('abcdefghijkl', 'abcdefghi'.ljust(12) + '\njkl', 10, 12)  # broken short of full
    assert not held_box_matches('alpha beta', 'alpha'.ljust(12) + '\nbeta', 10, 12)  # 'beta' would have fit
    assert not held_box_matches('alphabeta', 'alpha'.ljust(12) + '\nbeta', 10, 12)  # joined across no space
    assert not held_box_matches('alpha', 'alpha'.ljust(12) + '\n' + ' ' * 12, 10, 12)  # a row past the end
    assert not held_box_matches('alpha beta gamma', 'alpha beta'.ljust(13) + '\ngamma', 10, 12)  # past the pane
    assert not held_box_matches('one \ntwo', 'one'.ljust(12) + '\ntwo', 10, 12)  # a trailing space of its own
    assert not held_box_matches('alpha', 'alpha', None, 12) and not held_box_matches('alpha', None, 10, 12)


@pytest.mark.parametrize('case', ['exact edge', 'wide cells', 'changed character', 'missing newline',
                                  'missing separator', 'word fits', 'less text', 'less padding', 'legacy padding'])
def test_a_held_claude_word_can_wrap_at_the_exact_text_edge(case):
    edge = 315
    width = edge + 4
    header, first = words(50), words(edge, 1)
    second = 'seventh ' + words(303, 2)
    last = 'end ' + words(61, 3)
    if case == 'wide cells':
        second = second[:-2] + '界'
        last = '界x' + last[3:]
    elif case == 'word fits':
        last = 'to ' + words(62, 3)
    elif case == 'less text':
        second = second[:-1]
        last = 'last ' + words(60, 3)

    second_padding = edge - _cells(second)
    if case == 'less padding':
        second_padding -= 1
    elif case == 'legacy padding':
        second_padding += 3
    rows = [header, first, second + ' ' * second_padding, last + ' ' * (317 - _cells(last))]
    captured = pane_text('', width, rows=rows, padding='none')
    payload = header + '\n' + first + ' ' + second + ' ' + last + '\n'
    if case == 'changed character':
        payload = payload[:-2] + 'Z\n'
    elif case == 'missing newline':
        payload = payload.replace('\n', ' ', 1)
    elif case == 'missing separator':
        payload = header + '\n' + first + ' ' + second + last + '\n'

    harness = ClaudeHarness()
    screen = harness.screen(captured)
    shown = screen.box
    legacy_match = case in ('exact edge', 'wide cells', 'legacy padding')
    assert held_edge_wraps(payload, shown, edge, width) is legacy_match
    if case in ('exact edge', 'wide cells'):
        assert [(_cells(row.rstrip(' ')), _cells(row)) for row in shown.split('\n')] == [
            (50, 50), (315, 315), (311, 315), (65, 317),
        ]
        assert not harness.box(payload).matches(shown, edge, harness.wrap_slack)
        assert not held_box_matches(payload, shown, edge, width)
    if case == 'legacy padding':
        assert not harness.box(payload).matches(shown, edge, 1)
        assert not held_box_matches(payload, shown, edge, width)

    proof, refusal = candidate(payload, captured, width)
    if legacy_match or case in ('less text', 'less padding'):
        # The complete draft can prove the exact wrap before the held fallback is needed.
        assert proof is not None, refusal

        # Give Codex the same text edge with its three-column inset.
        codex = CodexHarness()
        seat = SimpleNamespace(harness=codex, tmux=None, pane=lambda: SimpleNamespace(width=edge + 3))
        assert not Door(seat).held_matches(codex.box(payload), screen)
    else:
        assert proof is None and 'not exactly a pending message' in refusal


def sentry_shape(kind_short: str, sender: str, body_length: int, seed: int):
    """A delivery the door types: its qd header line, the body, the trailing newline (post_message.typed)."""
    message = post.Message(f'ans-20261115-2032-{sender}-{kind_short}', 'answer', sender, 'synthetic-seat',
                           datetime(2026, 11, 15, tzinfo=timezone.utc), words(body_length, seed),
                           re=f'ans-20261115-2031-synthetic-{kind_short[::-1]}')
    return typed([message])


@pytest.mark.parametrize('width', [80, 120])
@pytest.mark.parametrize('shape', [('b6d0c1', 'host', 717, 50, 769), ('8ca7f0', 'hosts', 540, 51, 593)])
def test_the_two_held_shapes_are_accepted_and_refused_when_changed(width, shape):
    short, sender, body, header, total = shape
    payload = sentry_shape(short, sender, body, len(sender))
    assert [len(line) for line in payload.split('\n')] == [header, body, 0] and len(payload) == total
    assert payload.split('\n')[0].startswith('qd > answer from ')
    # The final newline: by the door's existing rule, a final newline of the message or of the box is no
    # difference (both are taken off), and Claude's trailing blank composer row is not part of the box.
    proof, refusal = candidate(payload, pane_text(payload, width), width)
    assert proof is not None, refusal
    changed = payload[:200] + ('Z' if payload[200] != 'Z' else 'Y') + payload[201:]
    proof, refusal = candidate(payload, pane_text(changed, width), width)
    assert proof is None and 'not exactly a pending message' in refusal
    joined = payload.replace('\n', ' ', 1)  # the header's own newline missing
    proof, refusal = candidate(payload, pane_text(joined, width), width)
    assert proof is None and 'not exactly a pending message' in refusal
