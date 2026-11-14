"""The values the adapters and the framework pass between them: inputs, tokens, turns, state, keys, controls, screens, boxes, credentials and launches."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from .limits import Wall

if TYPE_CHECKING:
    from .record_file import ProjectionUsage


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


AUTHENTICATION = 'sign-in failed in the program'


@dataclass(frozen=True)
class TurnFailure:
    """An ended turn's recorded error and its input/output evidence; absent output certainty is unknown.

    ``kind`` is ``provider refusal`` only for a native form the adapter positively classifies as the
    provider refusing the conversation. A measured native authentication failure is
    ``sign-in failed in the program``; other errors are ``provider error`` or ``program error``.
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

    inputs: tuple[Input, ...] | None = None  # explicit per-turn membership; old readers leave it unknown
    end_reason: str | None = None  # native terminal reason, only where the reader preserves it
    successful: bool | None = None  # adapter-proved successful completion; unknown is not a successful turn

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
class ActiveTool:
    """Observed call detail; completeness is established only by State.pending_tools."""

    name: str
    started: datetime | None = None
    inbox_watch_started: datetime | None = None  # exact native command candidate, not permission


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
    active_tools: tuple[ActiveTool, ...] = ()  # observed active subset, including across accounting boundaries
    # The latest ended turn stopped at the program's output limit: True or False where the reader has measured its
    # program's form (pi's stopReason, OpenCode's finish, both 'length'); None where it has not, never guessed.
    ended_at_limit: bool | None = None
    projection_usage: ProjectionUsage | None = field(default=None, repr=False, compare=False)


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
    """A dialog or notice: how it shows on the screen, and the key that dismisses it.

    ``click`` names the one choice the framework answers it with, by the choice's text on the screen: a
    question the program puts to its account holder whose answer the ship's orders fix.

    ``by_label``: the program takes no mouse; the choice is pressed by the one key its own text names in
    brackets (``[0] Skip`` is ``0``), and the question stands in place of the input box. ``recurs``: the program
    may ask it again in one launch; after a press that cleared it, it is answered again, after one that did not,
    never.
    """

    name: str
    pattern: str
    key: str | None = None
    click: str | None = None
    by_label: bool = False
    recurs: bool = False

    @property
    def label_key(self) -> str | None:
        """The key a ``by_label`` choice names in its own text: ``[0] Skip`` names ``0``; any other form names none."""
        found = re.fullmatch(r"\[([0-9A-Za-z])\] \S.*", self.click or "") if self.by_label else None
        return found.group(1) if found else None

    def seen_in(self, screen: str) -> bool:
        return re.search(self.pattern, screen) is not None

    @property
    def dismissable(self) -> bool:
        return self.key is not None


@dataclass(frozen=True)
class ControlInterruption(Interruption):
    """A question that only the request for this control may answer."""

    control: str = ''

    @property
    def dismissable(self) -> bool:
        return False


@dataclass(frozen=True)
class ControlAnswer:
    """An adapter-proved option and gesture for one exact requested value."""

    control: str
    value: str
    option: str
    keys: tuple[str, ...]


@dataclass(frozen=True)
class ControlContext:
    """Adapter-proved current selection context, never inferred from a display name alone."""

    version: str
    target: str
    display: str
    screen_reader_off: bool | None = None
    focused: bool | None = None
    composer_hidden: bool | None = None
    cursor_visible: bool | None = None
    yes_highlighted: bool | None = None
    scope: tuple = ()


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
    # True when the program's working sign is seen; False only when the part that decides it, the box,
    # is seen without that sign; None (unknown) otherwise (law: record-first: no default reported as a fact).
    working: bool | None = None
    interruption: Interruption | None = None
    unknown: str | None = None
    queued: int = 0
    shows: dict[str, str] = field(default_factory=dict)
    echoes: tuple[str, ...] = ()  # inputs the transcript shows above a running turn, possibly cut short with …
    quota: str | None = None
    starting: bool = False  # a recognized drawing/loading frame, never permission to type or clear
    # A composer the adapter knows only as drawn after this question was answered: read unknown here, and
    # idle only where the launch's own record shows that question answered and cleared (the Door decides).
    fresh: str | None = None

    def __post_init__(self):
        # Not seeing the working sign proves idle only where the deciding part of the screen was read:
        # no box, an unknown layout, a start-up frame or a composer unread as fresh is unknown, never idle.
        if self.working is not True and (self.box is None or self.unknown or self.starting or self.fresh):
            object.__setattr__(self, 'working', None)


def _cells(text: str) -> int | None:
    total = 0
    for char in text:
        if unicodedata.category(char).startswith("C") or unicodedata.combining(char):
            return None  # grapheme/control layout is not established by this reader
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


# Established Unicode 15 line-break classes and their printable-ASCII pairs.
# Codex 0.166.0 and Grok 1.0.77 use textwrap 0.16.2 / unicode-linebreak 0.1.5, FirstFit.
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
                 textwrap_ascii: bool = False, pi_word_wrap: bool = False,
                 wrap_hyphens: bool = True) -> bool:
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
        word = re.match(r"-*[^\s-]+-?|-+" if wrap_hyphens else r"[^\s]+", tail[spaces:])
        piece = _cells(word[0]) if word else None
        if (spaces or wrap_hyphens and row.endswith("-")) and piece and width + spaces + piece > columns - slack:
            pos += spaces  # the expected separator hangs off the rendered row
            continue
        return False
    return False


def held_box_matches(text: str, shown: str | None, columns: int | None, width: int | None) -> bool:
    """A held box equals ``text`` once trailing spaces and soft wraps are taken out of it, and nothing else.

    For qd submit and the door's second look only, after the adapter's own comparison has refused.
    ``columns`` is the text area and ``width`` the pane's width, both as read with this screen. Row by
    row, the box must show the message's exact characters. Between two rows, a newline the message
    carries is the line break. Any other break is a soft wrap, and only where the screen row, its
    painted spaces included, reaches the text area's edge: either a word broken at a full row of
    text, or a wrap at the message's own spaces (hung at the row's end) before a word that would not
    have fit on that row. A break with neither, the message's newline missing, or any other
    character changed is refused.
    """
    if shown is None or type(columns) is not int or type(width) is not int or not 0 < columns <= width:
        return False
    wanted, rows = text.rstrip("\n"), shown.rstrip("\n").split("\n")
    if any(line.endswith(" ") for line in wanted.split("\n")):
        return False  # a message's own trailing spaces cannot be told from padding
    pos = 0
    for index, raw in enumerate(rows):
        painted, row = _cells(raw), raw.rstrip(" ")
        filled = _cells(row)
        if painted is None or filled is None or painted > width or filled > columns:
            return False
        if not wanted.startswith(row, pos):
            return False
        pos += len(row)
        if index == len(rows) - 1:
            return pos == len(wanted)
        if wanted[pos:pos + 1] == "\n":
            pos += 1  # the message's own newline is this line break
            continue
        if painted < columns or pos == len(wanted):
            return False  # a short screen row, or a row past the message's end, is no soft wrap
        tail = wanted[pos:]
        spaces = len(tail) - len(tail.lstrip(" "))
        if not spaces:
            if filled != columns:
                return False  # a word breaks only at a full row of text
            continue
        word = re.match(r"[^\s]+", tail[spaces:])
        following = _cells(word[0]) if word else None
        if following is None or filled + 1 + following <= columns:
            return False  # the next word would have fit: this break is not the program's wrap
        pos += spaces  # the message's spaces hung at the row's end, taken out with its padding
    return False


def _wrapped_line(line: str, columns: int, slack: int, *, textwrap_ascii: bool,
                  pi_word_wrap: bool) -> list[str] | None:
    """Derive established first-fit rows, retaining input whitespace inside a row."""
    width = _cells(line)
    if width is None:
        return None
    if width <= columns - slack:
        return [line]
    if pi_word_wrap:
        return _pi_wrap(line, columns)
    # Short lines above need no line-break knowledge. Wrapped lines must have
    # established classes; indentation wrapping has not been measured here.
    if line.startswith(' ') or any(_line_class(char) is None for char in line):
        return None
    stops = (_textwrap_breaks(line) if textwrap_ascii else
             sorted({match.end() for match in re.finditer(' +', line)} | {len(line)}))
    fragments = []
    start = 0
    for stop in stops:
        chunk, start = line[start:stop], stop
        word = chunk.rstrip(' ')
        spaces = chunk[len(word):]
        pieces, piece, cells = [], '', 0
        for char in word:
            size = _cells(char)
            if size is None or size > columns:
                return None
            if cells + size > columns:
                pieces.append(piece)
                piece, cells = '', 0
            piece += char
            cells += size
        pieces.append(piece)
        fragments.extend((part, '') for part in pieces[:-1])
        fragments.append((pieces[-1], spaces))
    rows, row, spaces = [], '', ''
    for word, following in fragments:
        candidate = row + spaces + word
        if row and _cells(candidate) > columns - slack:
            rows.append(row)
            row = word
        else:
            row = candidate
        spaces = following
    return rows + [row]


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
    wrap_hyphens: bool = True  # Claude keeps a hyphenated word together when it fits a fresh row

    def wrapped_rows(self, columns: int | None, slack: int = 0) -> list[str] | None:
        """A measured row layout for a scrolled comparison, or unknown.

        This is independent of which viewport the program exposes. Whole and
        folded comparisons retain their existing rules, including Unicode
        layouts for which a complete row reconstruction is not established.
        """
        if type(columns) is not int or columns < 1 or not 0 <= slack < columns:
            return None
        lines = self.text.rstrip('\n').split('\n')
        if any(line.endswith(' ') for line in lines):
            return None  # required trailing spaces cannot be separated from painted padding
        rows = []
        for line in lines:
            wrapped = _wrapped_line(line, columns, slack, textwrap_ascii=self.textwrap_ascii,
                                    pi_word_wrap=self.pi_word_wrap)
            if wrapped is None:
                return None
            rows.extend(wrapped)
        return rows if self.matches('\n'.join(rows), columns, slack) else None

    def minimum_rows(self, columns: int | None) -> int | None:
        """A lower bound, not a renderer or a whole-message comparison.

        Only printable ASCII/LF is established. Ignore spaces and empty lines:
        native word wrapping may discard separators, but not the remaining
        characters or the line breaks between nonempty logical lines.
        """
        if type(columns) is not int or columns < 1:
            return None
        if any(char != '\n' and not 32 <= ord(char) <= 126 for char in self.text):
            return None
        return sum((len(line.replace(' ', '')) + columns - 1) // columns
                   for line in self.text.split('\n'))

    def matches(self, shown: str | None, columns: int | None = None, slack: int = 0) -> bool:
        if shown is None:
            return False
        if _box_matches(self.text, shown, columns, slack, self.textwrap_ascii, self.pi_word_wrap,
                        self.wrap_hyphens):
            return True
        return bool(self.folded) and re.fullmatch(self.folded, shown.strip()) is not None


@dataclass(frozen=True)
class BoxScroll:
    """An adapter's measured scroll mark and fixed viewport position.

    A gutter mark occupies the cell immediately outside the text columns.
    A row mark occupies its own row; a named ``hidden`` group also binds the
    number of omitted visual rows. Raw Screen.box is never rewritten.
    """

    mark: str
    anchor: str
    placement: str = 'gutter'
    marker_row: str = 'first'

    def matches(self, box: Box, shown: str | None, columns: int | None, slack: int = 0) -> bool:
        if (shown is None or type(columns) is not int or columns < 1
                or self.anchor not in ('top', 'bottom') or self.marker_row not in ('first', 'last')):
            return False
        visible = shown.rstrip('\n').split('\n')
        widths = [_cells(row) for row in visible]
        if any(width is None or width > columns + 1 for width in widths):
            return False  # text plus the measured single reserved gutter cell
        index = 0 if self.marker_row == 'first' else len(visible) - 1
        marked = visible[index]
        if self.placement == 'gutter':
            # Find the cell boundary, including established wide characters;
            # the native mark must be outside the entire text area.
            at, cells = 0, 0
            while at < len(marked) and cells < columns:
                size = _cells(marked[at])
                if size is None:
                    return False
                cells += size
                at += 1
            if cells != columns:
                return False
            marker = re.fullmatch(self.mark, marked[at:])
            visible[index] = marked[:at]
        elif self.placement == 'row':
            marker = re.fullmatch(self.mark, marked.rstrip(' '))
            visible.pop(index)
        else:
            return False
        if marker is None or not marker.group() or not visible:
            return False
        if any(len(row) - len(row.rstrip(' ')) > columns + 1 for row in visible):
            return False
        expected = box.wrapped_rows(columns, slack)
        if expected is None or len(visible) >= len(expected):
            return False  # a scroll mark must name an actual partial viewport
        hidden = marker.groupdict().get('hidden')
        if hidden is not None:
            try:
                if int(hidden) != len(expected) - len(visible):
                    return False
            except ValueError:
                return False
        window = expected[:len(visible)] if self.anchor == 'top' else expected[-len(visible):]
        return [row.rstrip(' ') for row in visible] == window


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
    notice: str | None = None  # information-only launch evidence/inventory, captured before the pane opens
    deferred_opening: str | None = None  # opt-in: startup proof, then the ordinary Door, once
