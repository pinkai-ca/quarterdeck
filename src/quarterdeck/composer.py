"""Compare transport text with a composer, without erasing meaningful whitespace.

Terminal soft rows are joined by tmux before adapter parsing. A UI can also draw
its own rows: only a break forced by the known text-area width may consume no
source character. Hard newlines and spaces within a row remain significant.
"""

import re
import unicodedata


def cells(text: str) -> int | None:
    total = 0
    for char in text:
        if unicodedata.category(char).startswith('C') or unicodedata.combining(char):
            return None  # grapheme/control layout is not established by this reader
        total += 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
    return total


def matches(text: str, shown: str, columns: int | None = None, slack: int = 0) -> bool:
    # ``slack``: columns a program keeps clear at the right when it wraps at a word, below the
    # width at which it breaks a word that has no space (pi 0.74.2 keeps one).
    # Adapters omit the trailing blank composer rows; no interior whitespace is
    # normalized. In particular "a b", "ab", and "a\nb" are different inputs.
    wanted, shown = text.rstrip('\n'), shown.rstrip('\n')
    if wanted == shown:
        return True
    if not columns or columns < 1:
        return False
    # -J preserves painted ASCII spaces at the right edge, including partial
    # padding on a short row. Keep raw Screen.box for obstruction/ownership;
    # this is only visual verification of an already known transport payload.
    # A payload requiring trailing spaces cannot be established from padding.
    if any(line.endswith(' ') for line in wanted.split('\n')):
        return False
    rows = shown.split('\n')
    if any(len(row) - len(row.rstrip(' ')) > columns + 1 for row in rows):
        return False  # more than a pane's padding is not a supported layout
    rows = [row.rstrip(' ') for row in rows]
    if '\n'.join(rows) == wanted:
        return True
    pos = 0
    for index, row in enumerate(rows):
        if not wanted.startswith(row, pos):
            return False
        pos += len(row)
        if index == len(rows) - 1:
            return pos == len(wanted)
        if wanted[pos:pos + 1] == '\n':
            pos += 1
            continue
        width = cells(row)
        if width is None or width > columns:
            return False
        # A full row may wrap at ANY character, not just a space or hyphen.
        next_width = cells(wanted[pos:pos + 1])
        if next_width and width + next_width > columns and wanted[pos:pos + 1] != ' ':
            continue
        # First-fit word wrapping: a space or hyphen breakpoint is only a
        # layout break when the following piece cannot fit on this row.
        tail = wanted[pos:]
        spaces = len(tail) - len(tail.lstrip(' '))
        word = re.match(r'[^\s-]+-?', tail[spaces:])
        piece = cells(word[0]) if word else None
        if (spaces or row.endswith('-')) and piece and width + spaces + piece > columns - slack:
            pos += spaces  # the expected separator hangs off the rendered row
            continue
        return False
    return False
