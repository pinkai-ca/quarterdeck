"""The Harness interface: everything an adapter knows about one agent program.

An adapter passes Quarterdeck's own files and overrides on the program's command line and
never edits the program's configuration or the user's files. It reads the least that
changes most: the record and the command line carry the facts; the screen is read only
for the box and the dialogs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..ship import Pool, SeatSpec, ShipError


@dataclass(frozen=True)
class Tokens:
    """As many classes as the provider bills; four for Anthropic."""

    read_cached: int = 0
    read_uncached: int = 0
    cache_written: int = 0
    written: int = 0

    def __add__(self, other: "Tokens") -> "Tokens":
        return Tokens(
            self.read_cached + other.read_cached,
            self.read_uncached + other.read_uncached,
            self.cache_written + other.cache_written,
            self.written + other.written,
        )

    @property
    def total(self) -> int:
        return self.read_cached + self.read_uncached + self.cache_written + self.written


@dataclass(frozen=True)
class ToolCall:
    name: str
    seconds: float | None
    failed: bool = False


@dataclass(frozen=True)
class Turn:
    """One ended turn: from an input to the next idle, summed over its steps."""

    index: int
    started: datetime
    ended: datetime
    input: str
    reply: str
    model: str | None
    tokens: Tokens
    tools: tuple[ToolCall, ...] = ()
    cost: float | None = None
    subagent_turns: int = 0

    @property
    def seconds(self) -> float:
        return (self.ended - self.started).total_seconds()


@dataclass(frozen=True)
class State:
    """The live reading of a record."""

    turn_open: bool = False
    open_tool: str | None = None
    open_tool_since: datetime | None = None
    last_write: datetime | None = None
    last_input: str | None = None
    last_input_at: datetime | None = None
    model: str | None = None
    effort: str | None = None
    queued: tuple[str, ...] = ()
    consumed: tuple[str, ...] = ()
    turns: int = 0


@dataclass(frozen=True)
class Join:
    """A seat joined to its record: the first written after its launch, or why not."""

    record: Path | None
    reason: str | None = None


@dataclass(frozen=True)
class Keys:
    """The keys a version uses, as tmux key names; None where the program lacks one."""

    submit: str = "Enter"
    send_now: tuple[str, ...] | None = None
    interrupt: str | None = None
    empty: tuple[str, ...] | None = None


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


@dataclass(frozen=True)
class Box:
    """How a program may show ``text`` in its box: whole, or folded to a placeholder naming this paste's size.

    Each program folds a long paste at a threshold of its own, so the door accepts either
    form; the placeholder carries a count, of lines or of characters, that only this paste matches.
    """

    text: str
    folded: str | None = None

    def matches(self, shown: str | None) -> bool:
        if shown is None:
            return False
        if " ".join(self.text.split()) == " ".join(shown.split()):
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


@dataclass(frozen=True)
class Window:
    name: str
    used: float
    resets_at: datetime | None


@dataclass(frozen=True)
class Usage:
    windows: tuple[Window, ...]
    read_at: datetime


def entries(record: Path) -> list[dict]:
    """Every parseable line of a JSON-lines record; a line still being written is skipped, not an error."""
    out: list[dict] = []
    try:
        lines = record.read_text().splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def when(value: str | None) -> datetime | None:
    """An ISO timestamp as an aware datetime; a bare one is read as UTC."""
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def credential(pool: Pool, link: Path, variable: str) -> Credential | None:
    """How a pool's credential reaches a seat home: a key by environment, a subscription by one link.

    A refreshed file in the link's place is left alone: the service reseeds the pool from it.
    """
    if pool.api_key:
        link.unlink(missing_ok=True)  # a key pool shares no file: whatever a former pool left here goes
        return Credential(variable=variable)
    if pool.config_dir is None:
        return None
    source = pool.config_dir / link.name
    link.unlink(missing_ok=True)  # a link to a former pool, or a refreshed file the service has reseeded from
    link.symlink_to(source)
    return Credential(link=(link, source))


def key_environment(pool: Pool, variable: str) -> dict[str, str]:
    """The variable a key pool's program reads, from ``env:NAME`` or ``file:PATH`` in the ship file."""
    if not pool.api_key:
        return {}
    kind, _, where = pool.api_key.partition(":")
    if kind == "env":
        value = os.environ.get(where)
        if not value:
            raise ShipError(f"pool {pool.name}: {where} is not set in the environment")
        return {variable: value}
    if kind == "file":
        try:
            return {variable: Path(where).expanduser().read_text().strip()}
        except OSError as error:
            raise ShipError(f"pool {pool.name}: cannot read {where}: {error}") from None
    raise ShipError(f"pool {pool.name}: api_key must be env:NAME or file:PATH, not {pool.api_key!r}")


def same_name(shown: str, wanted: str) -> bool:
    """``Sonnet 5.5`` and ``GPT-6-Astra`` name ``claude-sonnet-5-5`` and ``gpt-6-astra``: compare letters and digits only."""

    def key(name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", name.lower().removeprefix("claude-"))

    a, b = key(shown), key(wanted)
    return bool(a) and (a == b or b.endswith(a) or b.startswith(a))


def program_version(argv: tuple[str, ...]) -> str | None:
    """The installed version a program prints for ``--version``, read every time: programs update under a running ship."""
    try:
        out = subprocess.run(list(argv), capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\d+\.\d+(\.\d+)?", out)
    return match.group(0) if match else None


class Harness(ABC):
    """One agent program, as the framework knows it."""

    name: str
    home_variable: str | None
    keys: Keys
    brief_channel: str
    record_confirms: bool = True  # False where the record is opaque: the screen's echo confirms a delivery instead
    reads_usage: bool = False  # True where usage() reads the provider's windows; a pool of such a program is read before a move

    def queues_while_working(self, seat: SeatSpec) -> bool:
        """Whether a message typed during a turn is taken and queued, or refused until it ends."""
        return True

    # -- launch ------------------------------------------------------------------------

    @abstractmethod
    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        """Write a seat home from the ship's files: settings, trust, no self-update, nothing of the user's."""

    @abstractmethod
    def launch(
        self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None
    ) -> Launch:
        """The exact command line and environment, with or without resume."""

    def version(self) -> str | None:
        return None

    # -- record ------------------------------------------------------------------------

    @abstractmethod
    def records(self, home: Path) -> list[Path]:
        """Every record file in a home, oldest first."""

    @abstractmethod
    def first_timestamp(self, record: Path) -> datetime | None:
        pass

    @abstractmethod
    def session_id(self, record: Path) -> str | None:
        pass

    @abstractmethod
    def state(self, record: Path) -> State:
        pass

    @abstractmethod
    def turns(self, record: Path) -> list[Turn]:
        pass

    def read(self, record: Path) -> tuple[State, list[Turn]]:
        """The live state and the ended turns from one reading of the record."""
        return self.state(record), self.turns(record)

    def join(self, home: Path, launched_at: datetime) -> Join:
        """The first record written after the launch; ambiguity reported, never guessed."""
        after = []
        for record in self.records(home):
            first = self.first_timestamp(record)
            if first is not None and first >= launched_at:
                after.append((first, record))
        if not after:
            return Join(None, "no record yet")
        after.sort()
        if len(after) > 1 and after[1][0] == after[0][0]:
            names = ", ".join(r.name for _, r in after[:2])
            return Join(None, f"ambiguous: {names} were first written at the same moment")  # law: one-record
        return Join(after[0][1])

    # -- screen ------------------------------------------------------------------------

    @abstractmethod
    def screen(self, text: str) -> Screen:
        pass

    @abstractmethod
    def box(self, text: str) -> Box:
        pass

    def controls(self) -> tuple[Control, ...]:
        return ()

    def interruptions(self) -> tuple[Interruption, ...]:
        return ()

    def control(self, name: str) -> Control | None:
        return next((c for c in self.controls() if c.name == name), None)

    def recognize(self, screen: str) -> Interruption | None:
        return next((i for i in self.interruptions() if i.seen_in(screen)), None)

    def confirms(self, control: Control, value: str, screen: Screen, state: State) -> bool | None:
        """Whether the program's resulting state shows the change; None when it cannot be read."""
        same = same_name if control.name == "model" else (lambda shown, wanted: shown == wanted)
        if control.confirmed_by == "screen":
            shown = screen.shows.get(control.name) if screen else None
            return None if shown is None else same(shown, value)
        if control.confirmed_by == "record":
            reported = getattr(state, control.name, None) if state else None
            return None if reported is None else same(reported, value)
        return None

    # -- usage -------------------------------------------------------------------------

    def usage(self, pool: Pool) -> Usage | None:
        return None
