"""The values the adapters and the framework pass between them: inputs, tokens, turns, state, keys, controls, screens, boxes, credentials and launches."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from .limits import Wall


@dataclass(frozen=True)
class Input:
    """An input accepted by a record, waiting in its queue, or consumed from it.

    ``at`` is its original acceptance time, never a later poll or dequeue time.
    A consumed queue entry also needs a valid ``consumed_at`` to prove it was taken.
    """

    text: str
    at: datetime | None
    kind: str = "accepted"
    consumed_at: datetime | None = None


class Inputs(list[Input]):
    """Input evidence with exact skip counts and at most eight content-free examples."""

    def __init__(self):
        super().__init__()
        self.skipped = []
        self.skip_counts = Counter()

    def skip(self, line: int, kind: str, reason: str) -> None:
        self.skip_counts[kind, reason] += 1
        self.skipped.append(dict(line=line, kind=kind, reason=reason))
        self.skipped.sort(key=lambda item: item['line'])
        del self.skipped[8:]

    def diagnostics(self) -> dict:
        total = sum(self.skip_counts.values())
        return dict(total=total, counts=[dict(kind=kind, reason=reason, count=count)
                    for (kind, reason), count in sorted(self.skip_counts.items())],
                    examples=self.skipped, omitted=total-len(self.skipped))


@dataclass(frozen=True)
class Tokens:
    """As many classes as the provider bills; four for Anthropic. A class a provider never reports is zero.

    ``unmeasured`` counts the steps whose usage the record did not hold. Their tokens are missing from
    the four sums, so a figure built on them is shown with a trailing ``+``: at least this much.
    """

    read_cached: int = 0
    read_uncached: int = 0
    cache_written: int = 0
    written: int = 0
    unmeasured: int = 0

    @classmethod
    def unknown(cls) -> "Tokens":
        """One step whose usage the record does not hold."""
        return cls(unmeasured=1)

    @classmethod
    def measured(cls, *, read_cached=0, read_uncached=0, cache_written=0, written=0) -> "Tokens":
        """A present measurement: omitted classes are zero, invalid counts are unknown."""
        counts = (read_cached, read_uncached, cache_written, written)
        return cls(*counts) if all(type(n) is int and n >= 0 for n in counts) else cls.unknown()

    @classmethod
    def from_usage(cls, usage: object, fields: tuple[str, str, str, str], *, total: str | None = None) -> "Tokens":
        """JSON measurement presence needs a recognized count; empty/foreign shapes prove nothing."""
        if not isinstance(usage, dict) or not any(k in usage for k in (*fields, *((total,) if total else ()))):
            return cls.unknown()
        if total and total in usage and (type(usage[total]) is not int or usage[total] < 0):
            return cls.unknown()
        counts = [usage.get(k, 0) for k in fields]
        return cls.measured(read_cached=counts[0], read_uncached=counts[1], cache_written=counts[2], written=counts[3])

    def __add__(self, other: "Tokens") -> "Tokens":
        return Tokens(
            self.read_cached + other.read_cached,
            self.read_uncached + other.read_uncached,
            self.cache_written + other.cache_written,
            self.written + other.written,
            self.unmeasured + other.unmeasured,
        )

    @property
    def total(self) -> int:
        return self.read_cached + self.read_uncached + self.cache_written + self.written

    @property
    def complete(self) -> bool:
        return self.unmeasured == 0


@dataclass(frozen=True)
class CostStep:
    """One response's explicit model, measured tokens and normalized recorded amount."""

    model: str | None
    tokens: Tokens
    recorded: float | None


@dataclass(frozen=True)
class ToolCall:
    name: str
    seconds: float | None
    failed: bool | None = False


@dataclass(frozen=True)
class TurnFailure:
    """An ended turn's recorded error and its input/output evidence; absent output certainty is unknown.

    ``kind`` is ``provider refusal`` only for a native form the adapter positively classifies as the
    provider refusing the conversation; any other recorded error is ``provider error`` or ``program error``.
    """

    message: str
    at: datetime | None
    inputs: tuple[Input, ...] = ()
    model_output: bool | None = None
    kind: str = 'provider error'
    code: str | None = None  # the native error classification, verbatim, when the record names one


@dataclass(frozen=True)
class Turn:
    """One ended turn: from an input to the next idle, summed over its steps."""

    index: int
    started: datetime | None
    ended: datetime | None
    input: str
    reply: str
    model: str | None
    tokens: Tokens
    tools: tuple[ToolCall, ...] = ()
    cost: float | None = None
    subagent_turns: int | None = 0
    timed: bool = True  # False where the end is the last step's time, not a completion the record stated: the duration is unknown

    recorded_cost: float | None = None  # raw cost stated by a reader whose default zero cannot establish a price
    cost_basis: str | None = None
    price_model: str | None = None  # one model established for every measured step, for stated rates
    cost_steps: tuple[CostStep, ...] | None = None
    unpriced_steps: int = 0
    failure: TurnFailure | None = None

    @property
    def seconds(self) -> float | None:
        if not self.timed or self.started is None or self.ended is None:
            return None
        return (self.ended - self.started).total_seconds()


@dataclass(frozen=True)
class CappedGeneration:
    """A native, ended reasoning-only length stop; an adapter must still prove its allowance."""

    conversation: str
    work: str
    user: str
    generation: str
    model: str
    provider: str
    reasoning: int | None  # None when the native record has no separate reasoning counter
    written: int
    signature: str


@dataclass(frozen=True)
class State:
    """The live reading of a record."""

    turn_open: bool | None = False
    open_tool: str | None = None
    open_tool_since: datetime | None = None
    last_write: datetime | None = None
    last_input: str | None = None
    last_input_at: datetime | None = None
    model: str | None = None
    effort: str | None = None
    queued: tuple[str, ...] = ()
    consumed: tuple[str, ...] = ()
    turns: int | None = 0
    walls: tuple[Wall, ...] = ()
    completed_turns: tuple[Turn, ...] = ()  # explicit completions in the active conversation, never children or inferred ends
    pending_tools: tuple[str, ...] | None = None  # complete relevant tool evidence; None is unknown, never an empty proof
    conversation: str | None = None
    input_id: str | None = None
    work_id: str | None = None  # native automatic continuation cannot create a new recovery allowance
    capped: CappedGeneration | None = None
    queue_error: str | None = None  # queued is candidate history, not proved membership, when unresolved
    failure: TurnFailure | None = None  # latest ended turn only; cleared by a new native turn


@dataclass(frozen=True)
class Join:
    """A seat joined to its record: the first written after its launch, or why not."""

    record: Path | None
    reason: str | None = None


@dataclass(frozen=True)
class PasteClearRule:
    """An adapter's versioned capability for clearing each whole logical input line."""

    name: str
    version: str
    line_keys: tuple[str, ...]

    def keys(self, lines: int) -> tuple[str, ...]:
        return self.line_keys * lines


@dataclass(frozen=True)
class Keys:
    """The keys a version uses, as tmux key names; None where the program lacks one.

    ``interrupt`` may name a sequence, space-separated, where the program wants a key pressed twice
    in quick succession: the door sends the sequence in one command.
    """

    submit: str = "Enter"
    send_now: tuple[str, ...] | None = None
    interrupt: str | None = None


@dataclass(frozen=True)
class Interruption:
    """A dialog or notice: how it shows on the screen, and the key that dismisses it."""

    name: str
    pattern: str
    key: str | None = None

    def seen_in(self, screen: str) -> bool:
        return re.search(self.pattern, screen) is not None

    @property
    def dismissable(self) -> bool:
        return self.key is not None


@dataclass(frozen=True)
class Control:
    """A setting of the program: its launch form, its in-session form, and what confirms it."""

    name: str
    launch: str | None
    session: str | None
    confirmed_by: str

    def command(self, value: str) -> str | None:
        return self.session.format(value=value) if self.session else None


@dataclass(frozen=True)
class Screen:
    """What is on the screen, as far as the adapter can read it."""

    text: str
    box: str | None
    working: bool = False
    interruption: Interruption | None = None
    unknown: str | None = None
    queued: int = 0
    shows: dict[str, str] = field(default_factory=dict)
    echoes: tuple[str, ...] = ()  # inputs the transcript shows above a running turn, possibly cut short with …
    quota: str | None = None
    starting: bool = False  # a recognized drawing/loading frame, never permission to type or clear


def _cells(text: str) -> int | None:
    total = 0
    for char in text:
        if unicodedata.category(char).startswith("C") or unicodedata.combining(char):
            return None  # grapheme/control layout is not established by this reader
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


# Established Unicode 15 line-break classes and their printable-ASCII pairs.
# Codex 0.166.0 uses textwrap 0.16.2 / unicode-linebreak 0.1.5, FirstFit.
# Parentheses, quotes, numbers and leading hyphens retain their context rules;
# punctuation is not a blanket permission to break. Unknown context is local,
# never a reason to reject an independently established break elsewhere.
_LINE_CLASSES = {"SP": " ", "BA": "|", "HY": "-", "CL": "}", "CP": ")]", "EX": "!?",
                 "OP": "([{", "QU": "\"'", "IS": ",.:;", "NU": "0123456789", "PO": "%", "PR": "$+\\", "SY": "/"}
_BREAK_BEFORE = {
    "SP": {"BA", "HY", "OP", "QU", "NU", "PO", "PR", "AL", "IN"},
    "BA": {"OP", "NU", "PO", "PR", "AL"},
    "CL": {"OP", "NU", "AL"},
    "CP": {"OP"},
    "EX": {"OP", "NU", "PO", "PR", "AL"},
    "IS": {"OP", "PO", "PR"},
    "PO": {"PO", "PR"},
    "PR": {"PO", "PR"},
    "SY": {"OP", "PO", "PR", "AL"},
    "IN": {"OP", "NU", "PO", "PR", "AL"},
    "QU-SP": {"BA", "HY", "QU", "NU", "PO", "PR", "AL", "IN"},
}


def _line_class(char: str) -> str | None:
    if " " <= char <= "~":
        return next((name for name, chars in _LINE_CLASSES.items() if char in chars), "AL")
    # unicode-linebreak 0.1.5 resolves these specific AI symbols as AL; U+2026
    # is IN (inseparable). Neither is permission to classify other punctuation.
    if char in "\u00b7\u2192":
        return "AL"
    if char == "\u2026":
        return "IN"
    # Latin-1 and Extended-A/B letters are AL in unicode-linebreak 0.1.5.
    # Do not infer that other letters/scripts have the same line-break class.
    if "\u00c0" <= char <= "\u024f" and unicodedata.category(char).startswith("L"):
        return "AL"
    return None


def _textwrap_breaks(line: str) -> list[int]:
    """Established boundaries, with unknown context retained until known text resolves it."""
    breaks = []
    state = "START"
    for index, char in enumerate(line):
        kind = _line_class(char)
        if kind in _BREAK_BEFORE.get(state, ()):
            breaks.append(index)
        # textwrap suppresses Unicode HY breaks, then splits only alphanumeric-surrounded hyphens.
        if index > 1 and line[index - 1] == "-" and line[index - 2].isalnum() and char.isalnum():
            breaks.append(index)
        if state == "UNKNOWN" and kind not in ("AL", "NU"):
            continue  # punctuation can retain unknown context (for example Hebrew before a bar)
        if kind == "SP" and state in ("OP", "OP-SP", "QU", "QU-SP"):
            state = state.removesuffix("-SP") + "-SP"
        else:
            state = kind or "UNKNOWN"  # AL and NU are context resets in the pinned line-break table
    return sorted(set(breaks)) + [len(line)]


def _textwrap_piece(text: str, position: int) -> int | None:
    """Known prefix width up to the next boundary or unknown class; never assume the latter joins."""
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    line = text[start:end if end >= 0 else len(text)]
    breaks = _textwrap_breaks(line)
    offset = position - start
    if offset not in breaks:
        return None
    following = next((stop for stop in breaks if stop > offset), len(line))
    unknown = next((i for i in range(offset, following) if _line_class(line[i]) is None), following)
    if unknown < following and unicodedata.category(line[unknown])[0] in "CM" and line[unknown - 1:unknown] != " ":
        return None  # combining/control layout may affect the prefix itself
    # An unknown later class may introduce a boundary, so count only what is
    # already known. If even that prefix cannot fit, the observed break is safe.
    return _cells(line[offset:unknown].rstrip(" "))


def _pi_wrap(line: str, columns: int) -> list[str] | None:
    """pi-tui 0.74.2 wordWrapLine: whitespace boundaries, then hard word splits.

    Only printable ASCII wrapping is established here. Short Unicode lines need
    no wrapping; grapheme/control layouts on wrapped lines remain unsupported.
    """
    width = _cells(line)
    if width is not None and width <= columns:
        return [line]
    if any(not ' ' <= char <= '~' for char in line):
        return None
    rows, start, width, boundary, boundary_width = [], 0, 0, -1, 0
    for index, char in enumerate(line):
        if width + 1 > columns:
            if boundary >= 0 and width - boundary_width + 1 <= columns:
                rows.append(line[start:boundary])
                start, width = boundary, width - boundary_width
            elif start < index:
                rows.append(line[start:index])
                start, width = index, 0
            boundary = -1
        width += 1
        if char == ' ' and index + 1 < len(line) and line[index + 1] != ' ':
            boundary, boundary_width = index + 1, width
    return rows + [line[start:]]


def _box_matches(text: str, shown: str, columns: int | None = None, slack: int = 0,
                 textwrap_ascii: bool = False, pi_word_wrap: bool = False) -> bool:
    # ``slack``: columns a program keeps clear at the right when it wraps at a word, below the
    # width at which it breaks a word that has no space (the adapter supplies that margin).
    # Adapters omit the trailing blank box rows; no interior whitespace is
    # normalized. In particular "a b", "ab", and "a\nb" are different inputs.
    wanted, shown = text.rstrip("\n"), shown.rstrip("\n")
    if wanted == shown:
        return True
    if not columns or columns < 1:
        return False
    # -J preserves painted ASCII spaces at the right edge, including partial
    # padding on a short row. Keep raw Screen.box for obstruction/ownership;
    # this is only visual verification of an already known transport payload.
    # A payload requiring trailing spaces cannot be established from padding.
    if any(line.endswith(" ") for line in wanted.split("\n")):
        return False
    rows = shown.split("\n")
    if any(len(row) - len(row.rstrip(" ")) > columns + 1 for row in rows):
        return False  # more than a pane's padding is not a supported layout
    rows = [row.rstrip(" ") for row in rows]
    if "\n".join(rows) == wanted:
        return True
    if pi_word_wrap:
        expected = []
        for line in wanted.split('\n'):
            wrapped = _pi_wrap(line, columns)
            if wrapped is None:
                return False
            expected.extend(row.rstrip(' ') for row in wrapped)
        return rows == expected
    pos = 0
    for index, row in enumerate(rows):
        if not wanted.startswith(row, pos):
            return False
        pos += len(row)
        if index == len(rows) - 1:
            return pos == len(wanted)
        if wanted[pos:pos + 1] == "\n":
            pos += 1
            continue
        width = _cells(row)
        if width is None or width > columns:
            return False
        # A full row may wrap at ANY character, not just a space or hyphen.
        next_width = _cells(wanted[pos:pos + 1])
        if next_width and width + next_width > columns and wanted[pos:pos + 1] != " ":
            continue
        # First-fit word wrapping: a space or hyphen breakpoint is only a
        # layout break when the following piece cannot fit on this row.
        tail = wanted[pos:]
        spaces = len(tail) - len(tail.lstrip(" "))
        if textwrap_ascii:
            piece = _textwrap_piece(wanted, pos + spaces)
            if piece and width + spaces + piece > columns:
                pos += spaces
                continue
            return False
        # Leading hyphens belong to the next piece (for example a command
        # flag); they do not make it an empty word that could fit anywhere.
        word = re.match(r"-*[^\s-]+-?|-+", tail[spaces:])
        piece = _cells(word[0]) if word else None
        if (spaces or row.endswith("-")) and piece and width + spaces + piece > columns - slack:
            pos += spaces  # the expected separator hangs off the rendered row
            continue
        return False
    return False


@dataclass(frozen=True)
class Box:
    """How a program may show ``text`` in its box: whole, or folded to a placeholder naming this paste's size.

    Each program folds a long paste at a threshold of its own, so the door accepts either
    form; the placeholder carries a count, of lines or of characters, that only this paste matches.
    """

    text: str
    folded: str | None = None
    textwrap_ascii: bool = False
    pi_word_wrap: bool = False

    def matches(self, shown: str | None, columns: int | None = None, slack: int = 0) -> bool:
        if shown is None:
            return False
        if _box_matches(self.text, shown, columns, slack, self.textwrap_ascii, self.pi_word_wrap):
            return True
        return bool(self.folded) and re.fullmatch(self.folded, shown.strip()) is not None


@dataclass(frozen=True)
class Credential:
    """How a pool's credential reaches a seat home: by environment, or by one link."""

    variable: str | None = None
    link: tuple[Path, Path] | None = None


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    price: dict | None = None
    recovery: dict | None = None  # adapter-owned effective-source evidence, captured before the pane opens
    notice: str | None = None  # information-only workspace inventory, captured before the pane opens
