"""A program's own composer frame, as its adapter's screen reader names it: never a dialog's frame.

The systems seat's guard (acts.py) reads a dialog's words from the nearest top-frame row above its act. A program
that draws its own composer in such a frame would hide a question above it, so its adapter names that frame here.
Only Grok draws its composer so; every other program names none.
"""
from __future__ import annotations


def own_composer(program: str | None, rows: list[str]) -> tuple[int, int] | None:
    """The rows of the program's own composer frame, its top to its bottom border, or None where it names none."""
    if program == "grok":
        from . import grok_screen
        return grok_screen.composer_rows(rows)
    return None
