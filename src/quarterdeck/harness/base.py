"""The Harness interface: everything an adapter knows about one agent program.

An adapter passes Quarterdeck's own files and overrides on the program's command line and
never edits the program's configuration or the user's files. It reads the least that
changes most: the record and the command line carry the facts; the screen is read only
for the box and the dialogs.
"""

from __future__ import annotations

import os
import json
import tomllib
import re
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from .limits import Usage, Wall, Window

from ..ship import Pool, SeatSpec, ShipError


from .json_record import RecordError, entries, when
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


def merged(base: dict, layer: dict) -> dict:
    """``layer`` over ``base``, table by table; lists are joined so a permission added never removes one."""
    out = dict(base)
    for key, value in layer.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merged(out[key], value)
        elif isinstance(value, list) and isinstance(out.get(key), list):
            out[key] = out[key] + [v for v in value if v not in out[key]]
        else:
            out[key] = value
    return out


def ship_template(home: Path | None, name: str) -> dict:
    """The ship's optional settings file; never the user's template."""
    path = home / name if home else None
    if path is None or not path.is_file():
        return {}
    return tomllib.loads(path.read_text()) if path.suffix == ".toml" else json.loads(path.read_text())


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
        match = re.search(r"\d+\.\d+(\.\d+)?", stream)
        if match:
            return match.group(0)
    return None


class Harness(ABC):
    """One agent program, as the framework knows it."""

    name: str
    program: str
    synthetic: bool = False
    credential_file: str | None = None
    usage_interval: int = 60
    background_usage: bool = False
    status_usage: bool = False
    headline_window: tuple[str | None, str] | None = None
    screen_walls: bool = False
    home_variable: str | None
    keys: Keys
    reads_usage: bool = False  # True where usage() reads the provider's windows; a pool of such a program is read before a move
    remote_control: bool = False  # True where the program can open a seat to the operator's phone and claude.ai as well
    wrap_slack: int = 0  # columns the program keeps clear at the right when it wraps at a word, under the width where it breaks a word

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
        if pool.kind == "endpoint":
            return f"pool {pool.name} is an endpoint, and {self.name} runs on a {self.name} subscription or key"
        if pool.provider != self.name:
            return f"pool {pool.name} pays {pool.provider}, and this seat runs {self.name}"
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

    def version(self) -> str | None:
        return program_version((self.program, "--version"))

    def owns_launch(self, argv: tuple[str, ...] | list[str]) -> bool:
        return bool(argv) and Path(argv[0]).name == Path(self.program).name

    def freshness(self, path: Path) -> tuple[float, float] | None:
        return None

    def grant_expiry(self, path: Path) -> datetime | None:
        return None

    def shared_usage_key(self, pool: Pool) -> tuple | None:
        return None

    def is_headline(self, group: str | None, window: str) -> bool:
        return self.headline_window == (group, window)

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
        """Timestamped evidence from an already owned record; never from a screen."""
        raise NotImplementedError

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
        """Text-area columns for a recognized ordinary composer, never a guessed width."""
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
