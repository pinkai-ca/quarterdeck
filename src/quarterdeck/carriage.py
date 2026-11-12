"""A launch's first message, bounded: typed whole when short, else a short account and a file read whole.

Linux refuses an exec one of whose arguments, with its NUL, passes 32 pages
(``MAX_ARG_STRLEN``: 131,072 bytes with 4 KiB pages), or whose arguments and environment,
with their NULs and pointers, pass ``ARG_MAX`` (2,097,152 bytes on this machine). A launch
carries its open messages as one argument, so a long carriage would end the start with
``Argument list too long``, exit 126.

Byte counts are UTF-8. Two kinds of bound:

- soft bounds only choose the form. The carriage is typed whole, as before, when it is at
  most ``INLINE_BYTES`` (a quarter of the per-argument limit) and the final command, after
  the adapter and any jail wrapper, keeps every word with its NUL at most half the
  per-argument limit and the whole at most ``TOTAL_BYTES`` or half ``ARG_MAX``. Otherwise
  the carriage goes whole into a file in the seat's own runtime folder, written durably
  under a name of its own launch, and the first message is an account: one line per open
  order and question, the file's path, and the order to read it whole before acting
  (law: context-is-cache). The account shrinks to fit, never silently: lines it cannot
  hold are counted on a line of their own, and the file begins with the complete list.
- hard bounds are the kernel's own, measured on this machine. A command past them would
  fail at exec whatever is carried, so the launch is refused before any window opens and
  the reason names the sizes. A soft bound never refuses a launch.
"""

from __future__ import annotations

import os
import secrets
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import durable, post
from .ship import ShipError

ARGUMENT_LIMIT = 131_072  # MAX_ARG_STRLEN with 4 KiB pages; argument_limit() measures this machine's
INLINE_BYTES = ARGUMENT_LIMIT // 4  # 32,768: a carriage this long or shorter is typed whole, as before
WORD_BYTES = ARGUMENT_LIMIT // 2  # 65,536: the soft bound on any one word of the final command, its NUL included
TOTAL_BYTES = 1_048_576  # the soft bound on the whole command, or half ARG_MAX where that is smaller
POINTER = 8  # the kernel also counts each argument's and variable's pointer against ARG_MAX
SUBJECT_BYTES = 200
ACCOUNT_BUDGETS = (24_576, 8_192, 2_048, 0)  # the account's own lines, tried largest first
FOLDER = "carried"
CUT = "…"


@dataclass(frozen=True)
class Carriage:
    """What a launch carries: the whole text, the messages it holds, and what an account needs."""

    text: str | None
    carried: list[post.Message]
    header: str = ""
    roots: tuple[post.Message, ...] = ()
    refused: frozenset[str] = frozenset()
    others: int = 0  # carried messages that are not open roots: amendments, answers, context, notices

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


def line(message: post.Message, refused: bool) -> str:
    """One open order or question: its kind, sender and full id, and its subject unless its provider refused it."""
    from .refusal import WITHHELD  # the refusal module reads seats; imported here, not above

    head = f"qd > open {message.kind} from {clean(message.sender, 64)} · {message.id}"
    if refused:
        return f"{head} · {WITHHELD}"  # law: context-is-cache -- named, never retyped, not even its subject
    return f"{head} · {clean(message.subject, SUBJECT_BYTES) or 'no subject'}"


def listing(carriage: Carriage) -> list[str]:
    return [line(m, m.id in carriage.refused) for m in carriage.roots]


def account(carriage: Carriage, path: Path, budget: int) -> str:
    """The first message in place of a long carriage; its own list holds at most ``budget`` bytes."""
    lines = listing(carriage)
    shown, used = [], 0
    for item in lines:
        if used + utf8(item) + 1 > budget:
            break
        shown.append(item)
        used += utf8(item) + 1
    parts = [carriage.header,
             f"qd > what you carry is too long to type here: {len(carriage.carried)} carried message"
             f"{'s' if len(carriage.carried) != 1 else ''}, {carriage.size} bytes, are written in full in {path}",
             f"qd > read {path} whole, to its last line, before you act on anything; if you cannot read it whole, "
             "act on nothing it holds and say so with qd post question to each sender",
             *shown]
    if len(shown) < len(lines):
        rest = len(lines) - len(shown)
        parts.append(f"qd > and {rest} more open order{'s' if rest != 1 else ''} or question{'s' if rest != 1 else ''}, "
                     "each listed at the top of that file")
    if carriage.others:
        parts.append(f"qd > also only in that file, in thread order: {carriage.others} amendment"
                     f"{'s' if carriage.others != 1 else ''}, answer{'s' if carriage.others != 1 else ''} "
                     "and other carried messages")
    parts.append("qd > end of account")
    return "\n".join(parts)


def contents(carriage: Carriage, seat: str, stamp: str) -> str:
    """The file: the complete list, then exactly the text the launch would have typed, then an end line."""
    return "\n".join([
        f"qd > carried for {seat} at launch {stamp}: read every line, through the end line",
        *listing(carriage),
        f"qd > the carried messages follow, exactly as they would have been typed: {carriage.size} bytes",
        carriage.text or "",
        f"qd > end of the carried messages for {seat} at launch {stamp}",
    ]) + "\n"


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
        os.chmod(folder, 0o700)
    return folder


def write(seat_folder: Path, seat: str, carriage: Carriage) -> Path:
    """Write the whole carriage durably under a name of this launch alone, or refuse the launch with the reason."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = seat_folder / FOLDER / f"{stamp}-{secrets.token_hex(4)}.txt"
    try:
        folder = folder_of(seat_folder)
        path = folder / path.name
        if os.path.lexists(path):
            raise FileExistsError(f"{path} already exists")
        durable.replace_file(path, contents(carriage, seat, stamp), encoding="utf-8")
        os.chmod(path, 0o600)
        found = path.lstat()
        if not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid():
            raise OSError(f"{path} is not a file of this user's")
    except (OSError, UnicodeError) as error:
        raise ShipError(f"{seat}: the carried messages could not be written to {path} ({error}); "
                        "nothing was started, and they are never put whole into one argument") from None
    return path
