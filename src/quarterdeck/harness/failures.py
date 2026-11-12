"""Bounded first words of a recorded error; the native facts stay in each adapter."""
import re

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
WORDS = 16
CHARS = 160


def words(value, fallback='error details unavailable'):
    """The error's first words, control sequences and characters removed, never its whole text."""
    if not isinstance(value, str) or not value.strip():
        return fallback
    text = ' '.join(''.join(c for c in ANSI.sub('', value) if c.isprintable() or c.isspace()).split()[:WORDS])
    return text[:CHARS] or fallback
