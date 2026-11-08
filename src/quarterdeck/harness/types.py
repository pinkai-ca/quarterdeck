"""The values the adapters and the framework pass between them: inputs, tokens, turns, state, keys, controls, screens, boxes, credentials and launches."""

from __future__ import annotations

import re
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
class ToolCall:
    name: str
    seconds: float | None
    failed: bool | None = False


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

    @property
    def seconds(self) -> float | None:
        if not self.timed or self.started is None or self.ended is None:
            return None
        return (self.ended - self.started).total_seconds()


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


@dataclass(frozen=True)
class Join:
    """A seat joined to its record: the first written after its launch, or why not."""

    record: Path | None
    reason: str | None = None


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


@dataclass(frozen=True)
class Box:
    """How a program may show ``text`` in its box: whole, or folded to a placeholder naming this paste's size.

    Each program folds a long paste at a threshold of its own, so the door accepts either
    form; the placeholder carries a count, of lines or of characters, that only this paste matches.
    """

    text: str
    folded: str | None = None

    def matches(self, shown: str | None, columns: int | None = None, slack: int = 0) -> bool:
        if shown is None:
            return False
        from ..composer import matches
        if matches(self.text, shown, columns, slack):
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
