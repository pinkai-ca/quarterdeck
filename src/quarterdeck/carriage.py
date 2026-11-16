"""Canonical carriage and final-command kernel byte checks.

The ship-policy planner in carriage_plan.py builds bounded thread indexes and
a fresh seat's own full handoff. Kernel argument and environment limits remain a
separate final check, after adapter and jail encoding. Native launches copy no general message bodies; the old monolithic account/write
path is retired.
"""

from __future__ import annotations

import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import post
from .post_threads import IndexRow

ARGUMENT_LIMIT = 131_072  # MAX_ARG_STRLEN with 4 KiB pages; argument_limit() measures this machine's
INLINE_BYTES = ARGUMENT_LIMIT // 4  # 32,768: soft inline budget for a first message, independent of the ship policy
WORD_BYTES = ARGUMENT_LIMIT // 2  # 65,536: the soft bound on any one word of the final command, its NUL included
TOTAL_BYTES = 1_048_576  # the soft bound on the whole command, or half ARG_MAX where that is smaller
POINTER = 8  # the kernel also counts each argument's and variable's pointer against ARG_MAX
SUBJECT_BYTES = 200
FOLDER = "carried"
CUT = "…"


# A hand with nothing open has no task: its fresh opening ends with this line, the same words as its card.
HAND_IDLE = "Nothing is open for you: run no command, and wait for an order to arrive in your pane."


@dataclass(frozen=True)
class Carriage:
    """A fresh self-handoff, indexed incoming inventory and metadata-only thread rows."""

    text: str | None
    carried: list[post.Message]
    header: str = ""
    rows: tuple[IndexRow, ...] = ()  # open incoming, undelivered and sent-awaiting threads
    history: str = ''  # bounded notice-settlement count, without message bodies or ids

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8")) if self.text else 0


def utf8(text: str) -> int:
    return len(text.encode("utf-8", "surrogateescape"))


def _sysconf(name: str) -> int | None:
    try:
        value = os.sysconf(name)
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def argument_limit() -> int:
    """This machine's limit on one argument with its NUL: 32 pages."""
    page = _sysconf("SC_PAGE_SIZE")
    return 32 * page if page else ARGUMENT_LIMIT


def total_limit() -> int | None:
    """This machine's ARG_MAX, or None where it cannot be read."""
    return _sysconf("SC_ARG_MAX")


def sizes(words: list[str], env: dict[str, str]) -> tuple[int, int, int]:
    """The largest word with its NUL, its index in the command, and the whole with every NUL and pointer, in bytes."""
    counts = [utf8(word) + 1 for word in words]
    variables = [utf8(f"{k}={v}") + 1 for k, v in env.items()]
    largest = max(counts, default=0)
    index = counts.index(largest) if counts else -1
    return largest, index, sum(counts) + sum(variables) + POINTER * (len(counts) + len(variables) + 2)


def misfit(words: list[str], env: dict[str, str]) -> str | None:
    """Why this command should not carry its first message whole: a soft bound, which never refuses a launch."""
    largest, index, total = sizes(words, env)
    if largest > WORD_BYTES:
        return f"word {index} of the command would be {largest} bytes with its NUL, over the {WORD_BYTES} kept for one"
    limit = total_limit()
    budget = min(TOTAL_BYTES, limit // 2) if limit else TOTAL_BYTES
    if total > budget:
        return f"its arguments and environment would be {total} bytes, over the {budget} kept for them"
    return None


def refused(words: list[str], env: dict[str, str]) -> str | None:
    """Why the kernel would refuse this exec for its size, or None. ``env`` is the whole environment it gets."""
    largest, index, total = sizes(words, env)  # sizes and positions only: never a word, a message or a value
    if largest > argument_limit():
        return f"word {index} of the command would be {largest} bytes with its NUL, and this machine allows {argument_limit()}"
    limit = total_limit()
    if limit and total > limit:
        return f"its arguments and environment would be {total} bytes, and this machine allows {limit}"
    return None


def clean(text: str | None, limit: int) -> str:
    """One line of whole characters: controls and format characters escaped, cut visibly at ``limit`` bytes."""
    if not text:
        return ""
    shown = "".join(f"\\u{ord(c):04x}" if unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") else c for c in text)
    if utf8(shown) <= limit:
        return shown
    room, kept = limit - utf8(CUT), []
    for char in shown:
        room -= utf8(char)
        if room < 0:
            break
        kept.append(char)
    return "".join(kept) + CUT


def folder_of(seat_folder: Path) -> Path:
    """The seat's own folder for carried files: a plain folder of this user's, private, never a link."""
    folder = seat_folder / FOLDER
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError:
        pass
    found = folder.lstat()
    if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid():
        raise OSError(f"{folder} is not a folder of this user's")
    if stat.S_IMODE(found.st_mode) != 0o700:
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)  # a link swapped in is refused
        try:
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)
    return folder
