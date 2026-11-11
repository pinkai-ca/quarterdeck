"""The Harness interface: everything an adapter knows about one agent program.

An adapter passes Quarterdeck's own files and overrides on the program's command line and
never edits the program's configuration or the user's files. It reads the least that
changes most: the record and the command line carry the facts; the screen is read only
for the box and the dialogs.
"""

from __future__ import annotations

import re
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from .limits import Usage, Wall, Window

from ..ship import Pool, SeatSpec

from .json_record import RecordError, entries, when
from .home import credential, key_environment, merged, ship_template
from .types import Box, Control, Credential, Input, Interruption, Join, Keys, Launch, Screen, State, Tokens, ToolCall, Turn


def record_fingerprint(record: Path) -> tuple:
    """Presence, inode, size and mtime of a record and of its SQLite write-ahead log."""
    def stat_of(path: Path) -> tuple:
        try:
            s = path.stat()
        except FileNotFoundError:
            return (False, 0, 0, 0, 0, 0)
        return (True, s.st_ino, s.st_size, s.st_mtime_ns, s.st_dev, s.st_ctime_ns)
    return (stat_of(record), stat_of(Path(f"{record}-wal")))


def trimmed(lines: list[str]) -> list[str]:
    """Drop only trailing blank display rows; preserve text, padding and internal newlines."""
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    return lines[:end]


def same_name(shown: str, wanted: str) -> bool:
    """``Sonnet 5.5`` and ``GPT-6-Astra`` name ``claude-sonnet-5-5`` and ``gpt-6-astra``: compare letters and digits only."""

    def key(name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", name.lower().removeprefix("claude-"))

    a, b = key(shown), key(wanted)
    return bool(a) and (a == b or b.endswith(a) or b.startswith(a))


def program_version(argv: tuple[str, ...]) -> str | None:
    """The installed version a program prints for ``--version``, read every time: programs update under a running ship.

    Read from the standard output first and the error stream second: pi 0.74.2 prints its version on the latter.
    """
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for stream in (done.stdout, done.stderr):
        match = re.search(r"(?<![\d.])\d+\.\d+(?:\.\d+)?[A-Za-z0-9.+_-]*(?=\s|$)", stream)
        if match:
            return match.group(0)
    return None


class Harness(ABC):
    """One agent program, as the framework knows it."""

    name: str
    program: str
    native_provider: str | None = None
    synthetic: bool = False
    credential_file: str | None = None
    usage_interval: int = 60
    background_usage: bool = False
    usage_from_records = None  # optional reader of (joined record, saved launch time) sources
    headline_window: tuple[str | None, str] | None = None
    headline_fallback: tuple[str | None, str] | None = None  # shown when a good read carries no headline window
    screen_walls: bool = False
    home_variable: str | None
    keys: Keys
    reads_usage: bool = False  # True where usage() reads the provider's windows; a pool of such a program is read before a move
    reads_queue_error: bool = False  # state() can report unresolved native queue identity to automatic status
    remote_control: bool = False  # True where the program can open a seat to the operator's phone and claude.ai as well
    wrap_slack: int = 0  # columns the program keeps clear at the right when it wraps at a word, under the width where it breaks a word
    human_tools: tuple[str, ...] = ()
    human_tool_policy: str = "No native question-tool suppression proved for this adapter."

    def thinking_recovery(self, home: Path, evidence: dict | None, version: str | None, seat: SeatSpec, pool: Pool):
        """A proved native off/restore client, or no supported automatic recovery."""
        return None

    def compactions(self, record: Path):
        from .compactions import Compactions
        return Compactions()

    def waiting_for_human(self, state: State) -> bool:
        """A complete nonempty question-only set; a displayed first tool proves no exclusivity."""
        return (state.turn_open is True and bool(state.pending_tools)
                and all(name in self.human_tools for name in state.pending_tools))

    def drill_settings(self, slow_acceptance: bool = False) -> dict:
        """Program settings for the synthetic drill; real programs supply none."""
        return {}

    def queues_while_working(self, seat: SeatSpec) -> bool:
        """Whether a message typed during a turn is taken and queued, or refused until it ends."""
        return True

    def accepts(self, pool: Pool) -> str | None:
        """Why this program cannot run on a pool, or None when it can.

        By default a program runs on the pools of its own provider, a subscription or a key; a
        program that speaks to any OpenAI-compatible endpoint says so by taking endpoint pools instead.
        """
        from ..providers import display
        from . import native_name
        company = display(pool.provider)
        native = native_name(pool.provider)
        if pool.kind == "subscription" and (self.native_provider != pool.provider or self.name != native):
            return f"pool {pool.name} is a subscription for {company}; use its native harness {native or '(not installed)'}"
        if pool.kind == "endpoint":
            return f"pool {pool.name} is an endpoint operated by {company}; {self.name} requires its native {display(self.native_provider) if self.native_provider else 'provider'} subscription or key"
        if self.native_provider != pool.provider:
            return f"pool {pool.name} pays {company}; harness {self.name} requires its own provider's key (native harness {native or 'not installed'})"
        return None
    # -- launch ------------------------------------------------------------------------

    @abstractmethod
    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        """Write a seat home from the ship's files: settings, trust, no self-update, nothing of the user's."""

    @abstractmethod
    def launch(
        self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None
    ) -> Launch:
        """The exact command line and environment, with or without resume."""

    def preflight_resume(self, seat: SeatSpec, home: Path, resume_id: Callable[[], str | None]) -> None:
        """Check a program-specific resume requirement before retiring its current launch."""

    def version(self) -> str | None:
        return program_version((self.program, "--version"))

    def owns_launch(self, argv: tuple[str, ...] | list[str]) -> bool:
        return bool(argv) and Path(argv[0]).name == Path(self.program).name

    def freshness(self, path: Path) -> tuple[float, float] | None:
        return None

    def startup_ready(self, home: Path | None) -> bool:
        """Any program-specific startup decision required before its composer can take input."""
        return True

    def grant_expiry(self, path: Path) -> datetime | None:
        return None

    model_scoped_usage = False

    def usage_scope(self, group: str | None, model: str | None) -> bool | None:
        """True when a window applies; None when its model scope cannot be established."""
        return True  # Existing grouped adapters keep their pool-wide semantics.

    def shared_usage_key(self, pool: Pool) -> tuple | None:
        return None

    def price_turn(self, turn: Turn, snapshot: dict | None) -> Turn:
        """A record's stated cost by default; adapters that synthesize costs qualify theirs."""
        return turn

    def headline_windows(self) -> tuple[tuple[str | None, str], ...]:
        return (self.headline_window,) if self.headline_window else ()

    def is_headline(self, group: str | None, window: str) -> bool:
        return (group, window) in self.headline_windows()

    def quota_wall(self, text: str, at: datetime) -> Wall | None:
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

    reader = None

    @classmethod
    def state(cls, record: Path) -> State:
        return cls.read(record)[0]

    @classmethod
    def turns(cls, record: Path) -> list[Turn]:
        return cls.read(record)[1]

    @classmethod
    def read(cls, record: Path) -> tuple[State, list[Turn]]:
        """The live state and ended turns from one reading by this adapter's reader."""
        return cls.reader(record)

    def inputs(self, record: Path) -> list[Input]:
        """Timestamped owned-record evidence; optional ``skipped`` diagnostics contain no row contents."""
        raise NotImplementedError

    def attribution_inputs(self, record: Path) -> list[Input]:
        """Recorded message consumption for attribution; never broadens delivery receipts."""
        return self.inputs(record)

    def join(self, home: Path, launched_at: datetime, excluded: tuple[Path, ...] = ()) -> Join:
        """The first record written after the launch; ambiguity reported, never guessed."""
        after = []
        for record in self.records(home):
            if record in excluded:
                continue
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

    def box_columns(self, pane_width: int) -> int | None:
        """Text-area columns for a recognized ordinary box, never a guessed width."""
        return pane_width - 2

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
