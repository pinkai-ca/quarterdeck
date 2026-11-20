"""Held-box wrap matching for the Claude composer."""

import re

from .types import _cells, held_box_matches


def held_edge_wraps(text: str, shown: str | None, columns: int | None, width: int) -> bool:
    """Check a Claude held box that wraps just before a word ending exactly at the text edge."""
    if shown is None or type(columns) is not int or type(width) is not int or not 0 < columns <= width:
        return False
    pos = unchanged = 0
    parts = []
    for raw in shown.rstrip("\n").split("\n")[:-1]:
        row = raw.rstrip(" ")
        if not text.startswith(row, pos):
            return False
        pos += len(row)
        if text[pos:pos + 1] == "\n":
            pos += 1
            continue
        separator = pos
        while text[pos:pos + 1] == " ":
            pos += 1
        if pos != separator + 1:
            continue
        filled, painted = _cells(row), _cells(raw)
        if filled is None or painted is None or painted < columns:
            continue
        gap = columns - filled
        if not 2 <= gap <= 4:  # the observed row stopped four cells short of the painted edge
            continue
        word = re.match(r"\S+", text[pos:])
        if word and _cells(word[0]) == gap - 1:
            # Validate the rest with the shared matcher. In this comparison only, the
            # single payload space whose word ends at the edge stands for the display break.
            parts.extend((text[unchanged:separator], "\n"))
            unchanged = pos
    return bool(parts) and held_box_matches("".join(parts) + text[unchanged:], shown, columns, width)
