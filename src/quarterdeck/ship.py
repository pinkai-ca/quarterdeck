"""The Ship: one file, ``ship.toml``, declaring the sessions, the pools and the seats.

The ship file is the only configuration a ship has (law: ship-file). It is written in a
plain form, one key per line, so that ``qd set`` can edit the one line a change needs and
keep everything else, comments included. A ship folder is its own git repository; the
runtime inside it is a nested repository with no commits, a fence that keeps git run
above it out of the ship's state.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import subprocess
import tomllib
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from . import keyfile

RUNTIME = "runtime"
TICK = 1.0  # the service's default wait after each pass, in seconds
TICK_BOUNDS = (0.05, 5.0)  # inclusive; the heartbeat stays well inside servicectl's stale age
STATUS_REFRESH = 30.0  # unchanged status: seconds after collection finishes
STATUS_REFRESH_BOUNDS = (5.0, 300.0)
CARRY_BYTES = 100_000  # ship policy, not a provider capacity measurement


class ShipError(Exception):
    """A refusal: the reason, and what would lift it."""


class KeyRefused(ShipError):
    """A launch refused because its pool's key is missing or refused (keyfile): the one seat stays down, no other."""


class ProgramPinError(ShipError):
    """The declared program could not be verified."""


@dataclass(frozen=True)
class ProgramPin:
    name: str
    path: Path
    sha256: str

    def verified(self) -> Path:
        """Verify the ship's private copy before handing its absolute path to a caller."""
        folder = self.path.parent

        def refuse(reason: str) -> ProgramPinError:
            return ProgramPinError(f"{self.name} pin {self.path}: {reason}. Restore the declared runtime copy.")

        directory = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            with ExitStack() as opened:
                runtime_fd = os.open(folder.parent, directory)
                opened.callback(os.close, runtime_fd)
                folder_fd = os.open(folder.name, directory, dir_fd=runtime_fd)
                opened.callback(os.close, folder_fd)
                parent = os.fstat(folder_fd)
                if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
                    raise refuse("runtime/programs must belong to this user and have mode 0700")
                fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder_fd)
                with os.fdopen(fd, "rb") as binary:
                    before = os.fstat(fd)
                    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                            or stat.S_IMODE(before.st_mode) != 0o500):
                        raise refuse("needs an owned regular executable with mode 0500")
                    digest = hashlib.file_digest(binary, "sha256").hexdigest()
                    after = self.path.lstat()
                    if (not os.path.samestat(before, after) or before.st_ctime_ns != after.st_ctime_ns
                            or not os.path.samestat(parent, folder.lstat())):
                        raise refuse("file changed during verification")
                    if digest != self.sha256:
                        raise refuse("SHA-256 does not match ship.toml")
        except FileNotFoundError:
            raise refuse("file is missing") from None
        except OSError:
            raise refuse("cannot open the executable and its directories without following links") from None
        return self.path

    def proof(self, runtime: Path, version: str) -> Path:
        return runtime / "proven" / f"{self.name}-{version}-{self.sha256}"


def program_adapter(ship, adapter):
    """Give a program consumer its ship's pin, if it has one."""
    pins = getattr(ship, "program_pins", None)
    return adapter.with_program_pin(pin) if pins and (pin := pins.get(adapter.name)) is not None else adapter


@dataclass(frozen=True)
class Pool:
    """One way of paying: a subscription, named by its credential directory; an API key; or an endpoint.

    An endpoint pool is a model served at an OpenAI-compatible URL, local or remote. It has no
    credential of its own and no quota wall: a local server is paid for by the machine that runs
    it, and a remote one by a key named beside the URL, by ``api_key`` as for a key pool.
    """

    name: str
    provider: str
    config_dir: Path | None = None
    api_key: str | None = None
    budget: str | None = None
    endpoint: str | None = None  # such as http://127.0.0.1:8000/v1
    context: int | None = None  # the context window of what the endpoint serves, where the endpoint does not say
    price: dict | None = None  # explicit USD rates per million tokens; validated on load
    output: int | None = None  # endpoint response allowance; omission preserves the native adapter default
    ship_folder: Path | None = field(default=None, compare=False)  # where its key file is decided (keyfile.path)

    def __post_init__(self):
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ShipError(f"pool {self.name}: provider must name a company or declared endpoint operator")
        from .providers import canonical
        object.__setattr__(self, "_provider_input", self.provider)
        object.__setattr__(self, "provider", canonical(self.provider))

    @property
    def declared_provider(self) -> str:
        return self._provider_input

    @property
    def kind(self) -> str:
        if self.endpoint:
            return "endpoint"
        return "key" if self.api_key else "subscription"

    @property
    def paid_by_key(self) -> bool:
        """Whether a key pays for this pool: a key pool, or an endpoint with a key named beside its URL."""
        return bool(self.api_key)


@dataclass(frozen=True)
class Session:
    name: str
    title: str
    human: bool = False


@dataclass(frozen=True)
class SeatSpec:
    """A seat as declared. What runs in it is the Seat module's business."""

    name: str
    session: str
    role: str
    harness: str
    pools: tuple[str, ...]
    model: str | None = None
    effort: str | None = None
    persona: str | None = None
    cwd: Path | None = None
    up: bool = True
    fresh: str | None = None
    remote_control: bool = False  # the same session open from the operator's phone, where the program has it
    sandbox: bool | None = None  # None: a seat on an endpoint pool is jailed, every other seat runs open; true or false says
    settings: dict = field(default_factory=dict)
    pool_choices: dict[str, dict] = field(default_factory=dict)
    human_session: bool = False  # resolved from Session by Seat, never a ship-file setting
    temporary: bool = False  # qd check's own seat, made by temporary_seat; never a ship-file setting
    fresh_after_compactions: int | None = None
    model_mode: str | None = None
    effort_mode: str | None = None
    # The cwd as the declaration writes it, joined to the ship folder but never resolved: ``cwd`` is resolved at
    # load, so a link on its way would read as its target. The jail's own-work check walks this one.
    declared_cwd: Path | None = field(default=None, compare=False)

    def for_pool(self, name: str) -> SeatSpec:
        if name not in self.pools:
            raise ShipError(f"seat {self.name}: pool {name!r} is not in its declared list")
        return replace(self, **self.pool_choices[name]) if self.pool_choices else self

    @property
    def display(self) -> str:
        return self.persona or self.name


@dataclass(frozen=True)
class Group:
    """A declared group: one lead and the seats that receive the same brief."""

    name: str
    members: tuple[str, ...]
    lead: str


@dataclass(frozen=True)
class Cascade:
    """An ordered ring; its per-question notes live only under the runtime."""

    seats: tuple[str, str, str]
    orchestrator: str
    efforts: tuple[str, ...] = ("low", "medium", "high")
    notes: str = "ring"
    note_chars: int = 800


@dataclass(frozen=True)
class PullRequestScreen:
    """[pr_screen]: the pull-request screen's hand, or two hands one on each model, and the seat that reads a clean
    request. The desk, told of a flagged one, is the seat whose role is desk unless ``desk`` names another."""

    hands: tuple[str, ...]
    reader: str
    desk: str | None = None


@dataclass(frozen=True)
class Ship:
    folder: Path
    name: str
    operator: str
    workspace: Path
    pools: dict[str, Pool]
    sessions: dict[str, Session]
    seats: dict[str, SeatSpec]
    groups: dict[str, Group] = field(default_factory=dict)
    cascade: Cascade | None = None
    tick: float = TICK  # seconds the service waits after each pass; [ship] tick
    code: str | None = None
    carry_bytes: int = CARRY_BYTES  # first message and each index page, UTF-8 bytes; [ship] carry_bytes
    kind: str = "crew"  # an office has only the desk and the person's own seats
    forward: dict[str, str] = field(default_factory=dict)
    push_reminder: str | None = None  # the seat ordered to read the hosted runs after pushes of main; [ship] push_reminder
    pr_screen: PullRequestScreen | None = None  # [pr_screen]; with none, the ship has no pull-request screen
    status_refresh: float = STATUS_REFRESH  # [ship] status_refresh; file changes still request collection
    # The workspace as the declaration writes it, joined to the ship folder but never resolved: ``workspace`` is
    # resolved at load when relative. The jail's work check walks a seat's declared cwd from this one.
    declared_workspace: Path | None = field(default=None, compare=False)
    program_pins: dict[str, ProgramPin] = field(default_factory=dict)

    @property
    def file(self) -> Path:
        return self.folder / "ship.toml"

    @property
    def pr_screen_desk(self) -> str | None:
        """The seat told of a flagged pull request: [pr_screen] desk, else the one seat whose role is desk."""
        if self.pr_screen is None:
            return None
        return self.pr_screen.desk or next(seat.name for seat in self.seats.values() if seat.role == "desk")

    @property
    def runtime(self) -> Path:
        return self.folder / RUNTIME

    @property
    def is_repository(self) -> bool:
        return (self.folder / ".git").exists()

    def seat(self, name: str) -> SeatSpec:
        if name not in self.seats:
            raise ShipError(f"no seat {name!r} in {self.file}; declared: {', '.join(self.seats)}")
        return self.seats[name]

    def pool(self, name: str) -> Pool:
        if name not in self.pools:
            raise ShipError(f"no pool {name!r} in {self.file}; declared: {', '.join(self.pools)}")
        return self.pools[name]

    def session_of(self, seat: SeatSpec) -> Session:
        return self.sessions[seat.session]

    def cwd_of(self, seat: SeatSpec) -> Path:
        return seat.cwd or self.workspace

    def jailed(self, seat: SeatSpec, pool: Pool | None = None) -> bool:
        """Whether a seat runs its program in a sandbox: as the ship says, else when its pool is an endpoint.

        A model served locally is trusted less than the subscription programs, so a seat on an endpoint
        pool is jailed unless the ship says ``sandbox = false``; any seat is jailed with ``sandbox = true``.
        """
        if seat.sandbox is not None:
            return seat.sandbox
        return (pool or self.pool(seat.pools[0])).kind == "endpoint"


# -- reading --------------------------------------------------------------------------


def find(argument: str | None = None) -> Path:
    """The ship folder: ``--ship``, else ``QD_SHIP`` in the environment, else the default ship (see ``choose``)."""
    return choose(argument)[0]


# The folders under ships/ that travel with the framework: templates, and the practice ship (drydock).
TEMPLATES = frozenset({"default", "drydock", "forum", "cascade", "shuttle", "office"})
MARK = ".default"  # ships/.default: the default ship's folder name, relative to ships/


def choose(argument: str | None = None, *, project: Path | None = None) -> tuple[Path, str | None]:
    """The ship folder, and the default ship's name when it was chosen as the default.

    ``--ship`` wins, then ``QD_SHIP``. With neither, a person's shell uses the default ship: the one ships/.default
    marks, else the only ship under ships/ that is not a template or the practice ship. A seat, which always runs
    with its own QD_SHIP, never reads the mark.
    """
    chosen = argument or os.environ.get("QD_SHIP")
    if chosen:
        folder = Path(chosen).expanduser().resolve()
        if not (folder / "ship.toml").is_file():
            raise ShipError(f"{folder} holds no ship.toml")
        return folder, None
    ships = ships_folder(project)
    if os.environ.get("QD_SEAT"):
        raise ShipError("no ship named: a seat runs with its own QD_SHIP and never uses the default ship")
    folder, _ = _default_ship(ships)
    return folder, folder.name


def ships_folder(project: Path | None = None) -> Path:
    if project is None:
        from . import standing
        if standing.checkout_dir() is None:
            raise ShipError("this is a ship's own copy; name its ship with --ship or QD_SHIP")
        project = standing.PROJECT_DIR
    return Path(project) / "ships"


def default_ship(project: Path | None = None) -> tuple[Path, str]:
    """The default ship's folder and how it was chosen: ``mark`` or ``only ship``. Never a silent fallback."""
    return _default_ship(ships_folder(project))


def read_default_mark(ships: Path) -> str | None:
    """Read the stored folder name without resolving or loading its ship."""
    mark = ships / MARK
    try:
        text = mark.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        raise ShipError(f"the default mark {mark} is unreadable ({type(error).__name__}); "
                        "qd default <ship> rewrites it") from None
    name = text.strip()
    if not valid_name(name) or text not in (name, name + "\n"):
        raise ShipError(f"the default mark {mark} does not hold one ship folder name; qd default <ship> rewrites it")
    return name


def load_marked_ship(folder: Path, *, resolved: Path | None = None) -> Ship:
    """Load the named default, reusing a resolved folder when the caller has checked it."""
    try:
        return _validate_default(folder, resolved=resolved)
    except ShipError as error:
        raise ShipError(f"the default mark {folder.parent / MARK}: {error}") from None


def _default_ship(ships: Path) -> tuple[Path, str]:
    name = read_default_mark(ships)
    if name is not None:
        return load_marked_ship(ships / name).folder, "mark"
    found = _candidates(ships)
    if not found:
        raise ShipError("no ship named: pass --ship <folder>, or make a ship and mark it with qd default <ship>")
    if len(found) > 1:
        raise ShipError(f"no default ship among several: {', '.join(found)}; qd default <ship> sets one, "
                        "or pass --ship <folder>")
    return _validate_default(ships / found[0]).folder, "only ship"


def candidates(project: Path | None = None) -> list[str]:
    """The ships under ships/ that are not templates or the practice ship, by folder name."""
    return _candidates(ships_folder(project))


def _candidates(ships: Path) -> list[str]:
    try:
        entries = sorted(ships.iterdir())
    except FileNotFoundError:
        return []
    return [entry.name for entry in entries if valid_name(entry.name) and entry.name not in TEMPLATES
            and (entry / "ship.toml").is_file()]


def valid_name(name: str) -> bool:
    """One folder directly under ships/: no path, no hidden name."""
    return bool(name) and name.isprintable() and name == name.strip() and not name.startswith(".") \
        and "/" not in name and "\\" not in name


def mark_default(name: str, project: Path | None = None) -> Path:
    """Write ships/.default for a ship under ships/; the only file this writes. Returns the ship's folder."""
    from . import durable
    ships = ships_folder(project)
    if not valid_name(name):
        raise ShipError(f"{name!r} is not a ship folder name under {ships}; name the folder, such as qd default mine")
    folder = _validate_default(ships / name).folder
    durable.replace_file(ships / MARK, name + "\n")  # law: one-folder
    return folder


def _validate_default(folder: Path, *, resolved: Path | None = None) -> Ship:
    remedy = "pass --ship <folder>, or qd default <ship> marks another"
    if resolved is None:
        resolved = folder.resolve()
    if folder.name in TEMPLATES or resolved.name in TEMPLATES:
        raise ShipError(f"{folder.name} is a template or practice ship; {remedy}")
    if not (resolved / "ship.toml").is_file():
        raise ShipError(f"{folder} is missing or holds no ship.toml; {remedy}")
    try:
        return load(resolved)
    except ShipError as error:
        raise ShipError(f"{folder.name} has an invalid declaration ({error}); {remedy}") from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise ShipError(f"{folder.name} has an unreadable or invalid declaration ({type(error).__name__}); "
                        f"{remedy}") from None


def load(folder: Path) -> Ship:
    folder = Path(folder).expanduser().resolve()
    try:
        text = (folder / "ship.toml").read_text()
    except FileNotFoundError:
        raise ShipError(f"{folder} holds no ship.toml") from None
    ship = _from_text(text, folder)
    _validate(ship)
    return ship


def _session(name: str, table: dict, ship_name: str) -> Session:
    human = bool(table.get("human", False))
    if "title" in table:
        return Session(name, table["title"], human)
    capital = name[:1].upper() + name[1:]
    if not human:
        for char in (".", ":"):
            if char in str(ship_name):
                raise ShipError(f"ship name contains {char!r}; cannot derive tmux session {name!r}; use another ship name or an explicit title")
    title = capital if human else f"{ship_name} {capital}"
    for char in (".", ":"):
        if char in capital:
            raise ShipError(f"session key {name!r} contains {char!r}; cannot derive its tmux title; use an explicit title")
    return Session(name, title, human)


_TOML_PLACE = re.compile(r"\(at line (\d+), column (\d+)\)\Z")  # the parser's own suffix; its lineno is 3.14 only


def toml_table(text: str) -> tuple[dict | None, str | None]:
    """The table, or None and where the parser stopped. Its message can quote the file, a pasted key with it, so only
    the line and column leave here; a caller raises outside this handler, so no exception chain carries the message."""
    try:
        return tomllib.loads(text), None
    except tomllib.TOMLDecodeError as error:
        found = _TOML_PLACE.search(str(error))
        return None, f"line {found[1]}, column {found[2]}" if found else "the end of the file"


def endpoint_fits(value) -> bool:
    """An http or https URL with a host and nothing that can carry a credential: no user part, query or fragment."""
    if not isinstance(value, str) or not re.fullmatch(r"https?://\S+", value) or "?" in value or "#" in value:
        return False
    try:
        parts = urlsplit(value)
        return bool(parts.hostname) and "@" not in parts.netloc and (parts.port is None or parts.port >= 0)
    except ValueError:  # a port that is no number: its message would quote the value
        return False


def loopback_endpoint(url: str) -> bool:
    """Whether the URL's host is this machine's own loopback, by name or by address."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def _from_text(text: str, folder: Path) -> Ship:
    data, place = toml_table(text)
    if data is None:
        raise ShipError(f"{folder / 'ship.toml'}: not valid TOML at {place}")
    head = data.get("ship", {})
    from .copy_state import code_mode
    try:
        code = code_mode(head)
    except (ValueError, TypeError):
        raise ShipError('ship.code must be absent, or "checkout"') from None
    pools = {name: _pool(name, table, folder) for name, table in data.get("pools", {}).items()}
    sessions = {
        name: _session(name, table, head.get("name", folder.name))
        for name, table in data.get("sessions", {}).items()
    }
    seats = {name: _seat(name, table, folder) for name, table in data.get("seats", {}).items()}
    groups = {name: _group(name, table) for name, table in data.get('groups', {}).items()}
    from .cascade import declaration
    return Ship(
        folder=folder,
        name=head.get("name", folder.name),
        operator=head.get("operator", "operator"),
        workspace=_path(head.get("workspace", "."), folder),
        declared_workspace=_declared_path(head.get("workspace", "."), folder),
        pools=pools,
        sessions=sessions,
        seats=seats,
        groups=groups,
        cascade=declaration(data.get("cascade")),
        tick=_tick(head.get("tick", TICK)),
        status_refresh=_status_refresh(head.get("status_refresh", STATUS_REFRESH)),
        code=code,
        carry_bytes=_carry_bytes(head.get("carry_bytes", CARRY_BYTES)),
        kind=head.get("kind", "crew"),
        forward=_forward(data.get('forward', {})),
        push_reminder=_push_reminder(head.get("push_reminder")),
        pr_screen=_pr_screen(data.get("pr_screen")),
        program_pins=_program_pins(data.get("programs", {}), folder),
    )


def _program_pins(programs: object, folder: Path) -> dict[str, ProgramPin]:
    if not isinstance(programs, dict):
        raise ShipError("programs must be a table of program pins")
    from .harness import get

    pins = {}
    for name, pin in programs.items():
        section = f"programs.{name}"
        if not hasattr(get(name), "with_program_pin"):
            raise ShipError(f"{section}: executable pins are unsupported for this program")
        if not isinstance(pin, dict) or set(pin) != {"path", "sha256"}:
            raise ShipError(f"[{section}] requires path and sha256")
        path, digest = pin["path"], pin["sha256"]
        if not isinstance(path, str) or not re.fullmatch(r"runtime/programs/[A-Za-z0-9][A-Za-z0-9._+-]*", path):
            raise ShipError(f"{section}.path must be runtime/programs/<filename>, relative to ship.toml")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ShipError(f"{section}.sha256 must be a 64-digit hexadecimal SHA-256")
        pins[name] = ProgramPin(name, folder / path, digest.lower())
    return pins


def _pr_screen(table: object) -> PullRequestScreen | None:
    """[pr_screen] read as written; whether its seats fit is judged with the whole ship (_validate)."""
    if table is None:
        return None
    if not isinstance(table, dict) or set(table) - {"hands", "reader", "desk"}:
        raise ShipError('pr_screen takes hands = ["a"] or ["a", "b"], reader = "seat" and, optionally, desk = "seat"')
    hands, reader, desk = table.get("hands"), table.get("reader"), table.get("desk")
    if (not isinstance(hands, list) or len(hands) not in (1, 2) or len(set(hands)) != len(hands)
            or not all(isinstance(name, str) and valid_name(name) for name in hands)):
        raise ShipError(f"pr_screen.hands must name one seat, or exactly two different seats; got {hands!r}")
    for key, value in (("reader", reader), ("desk", desk)):
        if (value is not None or key == "reader") and (not isinstance(value, str) or not valid_name(value)):
            raise ShipError(f"pr_screen.{key} must name a declared seat; got {value!r}")
    return PullRequestScreen(tuple(hands), reader, desk)


def _push_reminder(value: object) -> str | None:
    if value is not None and (not isinstance(value, str) or not valid_name(value)):
        raise ShipError(f"ship.push_reminder must name a declared seat, such as \"reviewer\"; got {value!r}")
    return value


def _forward(table):
    if not isinstance(table, dict):
        raise ShipError('forward must map old seat names to seat@ship addresses')
    for name, target in table.items():
        if (not valid_name(name) or '@' in name or not isinstance(target, str)
                or len(target.split('@')) != 2
                or not all(valid_name(part) and '@' not in part for part in target.split('@'))):
            raise ShipError('forward must map old seat names to seat@ship addresses')
    return dict(table)


def _carry_bytes(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ShipError(f"ship.carry_bytes must be a positive integer count of bytes; got {value!r}")
    return value


def _status_refresh(value: object) -> float:
    low, high = STATUS_REFRESH_BOUNDS
    refused = ShipError(f"ship.status_refresh must be a number of seconds from {low:g} to {high:g}; got {value!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise refused
    if isinstance(value, int) and not low <= value <= high:
        raise refused
    if not math.isfinite(value) or not low <= value <= high:
        raise refused
    return float(value)


def _tick(value: object) -> float:
    """The service's wait after each pass: a finite number of seconds within TICK_BOUNDS, else refused by name."""
    low, high = TICK_BOUNDS
    refused = ShipError(f"ship.tick must be a number of seconds from {low:g} to {high:g}; got {value!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise refused
    if isinstance(value, int) and not low <= value <= high:  # compared as an int: a huge one never converts
        raise refused
    if not math.isfinite(value) or not low <= value <= high:
        raise refused
    return float(value)


def _group(name: str, table: dict) -> Group:
    if not isinstance(table, dict) or set(table) - {'members', 'lead'}:
        raise ShipError(f'group {name}: declare members and lead only')
    members, lead = table.get('members'), table.get('lead')
    if (not isinstance(members, list) or not members
            or any(not isinstance(member, str) or not member for member in members)
            or len(set(members)) != len(members)):
        raise ShipError(f'group {name}: members must be a nonempty list of unique seat names')
    if not isinstance(lead, str) or not lead:
        raise ShipError(f'group {name}: declare a lead seat')
    return Group(name, tuple(members), lead)


def pool_config_dir(table: dict, folder: Path | None) -> Path | None:
    """Anchor a declared native home while keeping aliases available for revalidation."""
    spelling = table.get("config_dir", table.get("home"))
    if not spelling:
        return None
    path = Path(spelling).expanduser()
    return folder / path if folder is not None else path  # setup can inspect a pool before the declaration exists


def _pool(name: str, table: dict, folder: Path | None = None) -> Pool:
    endpoint = table.get("endpoint")
    price = table.get("price")
    if price is not None:
        from dataclasses import asdict
        from .pricing import Price
        try:
            price = asdict(Price.parse(price))
        except ValueError as error:
            raise ShipError(f"pool {name}: {error}") from None
        if not endpoint and "api_key" not in table:
            raise ShipError(f"pool {name}: stated price is supported only for an endpoint pool or a pool paid by a key")
    if "api_key" in table:
        # Never quotes what was written: a pasted key would be printed from a committed file.
        if reason := keyfile.reference_refusal(name, table["api_key"]):
            raise ShipError(reason)
    return Pool(
        name=name,
        provider=table["provider"],
        config_dir=pool_config_dir(table, folder),
        api_key=table.get("api_key"),
        budget=table.get("budget"),
        endpoint=str(endpoint).rstrip("/") if endpoint else None,
        context=table.get("context"), price=price, output=table.get("output"), ship_folder=folder,
    )


def _seat(name: str, table: dict, folder: Path) -> SeatSpec:
    if "sandbox" in table and type(table["sandbox"]) is not bool:
        raise ShipError(f"seat {name}: sandbox must be true or false (a TOML boolean)")
    pool = table.get("pool", [])
    entries = pool if isinstance(pool, list) else [pool]
    pools, choices = [], {}
    defaults = dict(harness=table['harness'], model=table.get('model'), effort=table.get('effort'),
                    settings=dict(table.get('settings', {})), model_mode=table.get('model_mode'),
                    effort_mode=table.get('effort_mode'))
    for entry in entries:
        if isinstance(entry, str):
            selected, choice = entry, defaults
        elif isinstance(entry, dict):
            if set(entry) - {'pool', 'harness', 'model', 'effort', 'settings', 'model_mode', 'effort_mode'}:
                raise ShipError(f"seat {name}: unknown pool choice fields")
            selected = entry.get('pool')
            if (not isinstance(entry.get('harness'), str) or not entry['harness'].strip()
                    or entry.get('model_mode') != 'program-default' and
                    (not isinstance(entry.get('model'), str) or not entry['model'].strip())):
                raise ShipError(f"seat {name}: each inline pool choice needs an explicit harness and model")
            if (entry.get('effort') is not None and not isinstance(entry['effort'], str)
                    or not isinstance(entry.get('settings', {}), dict)):
                raise ShipError(f"seat {name}: pool choice effort must be text and settings must be a table")
            choice = dict(harness=entry['harness'], model=entry.get('model'), effort=entry.get('effort'),
                          settings=dict(entry.get('settings', {})), model_mode=entry.get('model_mode'),
                          effort_mode=entry.get('effort_mode'))
        else:
            raise ShipError(f"seat {name}: pool choices must be names or inline tables")
        if not isinstance(selected, str) or not selected.strip() or selected in pools:
            raise ShipError(f"seat {name}: pool choice names must be nonempty and unique")
        pools.append(selected)
        choices[selected] = choice
    return SeatSpec(
        name=name,
        session=table["session"],
        role=table.get("role", name),
        harness=table["harness"],
        pools=tuple(pools),
        model=table.get("model"),
        effort=table.get("effort"),
        persona=table.get("persona"),
        cwd=_path(table["cwd"], folder) if "cwd" in table else None,
        declared_cwd=_declared_path(table["cwd"], folder) if "cwd" in table else None,
        up=bool(table.get("up", True)),
        fresh=table.get("fresh"),
        fresh_after_compactions=table.get("fresh_after_compactions"),
        model_mode=table.get("model_mode"),
        effort_mode=table.get("effort_mode"),
        remote_control=bool(table.get("remote_control", False)),
        sandbox=table.get("sandbox"),
        settings=dict(table.get("settings", {})),
        pool_choices=choices if any(isinstance(entry, dict) for entry in entries) else {},
    )


SEAT_KEYS = frozenset({"session", "role", "harness", "pool", "model", "effort", "persona", "cwd", "up", "fresh", "remote_control", "sandbox", "fresh_after_compactions", "model_mode", "effort_mode"})
"""The keys of ``[seats.<name>]`` the framework reads; any other key belongs to the program, under ``.settings``."""


def _path(value: str, folder: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (folder / path).resolve()


def _declared_path(value: str, folder: Path) -> Path:
    """A path as the declaration writes it, relative to the ship folder: normalized, no link followed."""
    path = Path(value).expanduser()
    return Path(os.path.normpath(path if path.is_absolute() else folder / path))


def _validate(ship: Ship) -> None:
    from . import harness as harnesses  # the adapters import this module, so it is imported here, not above
    from .harness.capabilities import validate_mode, validate_selection

    if ship.kind not in ("crew", "office"):
        raise ShipError('ship.kind must be "crew" or "office"; omit it for an existing crew')
    if ship.kind == "office":
        desk = ship.seats.get("desk")
        if desk is None or desk.role != "desk" or sum(s.role == "desk" for s in ship.seats.values()) != 1:
            raise ShipError('an office needs one seat named desk with role = "desk"')
        if not ship.sessions or any(not session.human for session in ship.sessions.values()):
            raise ShipError('an office has only the person\'s sessions; declare human = true on each')
        crew = {"captain", "first-officer", "engineering", "engineer", "worker", "hand",
                "operations", "systems", "security", "lead", "reviewer", "orchestrator", "ring"}
        if any(seat.role in crew for seat in ship.seats.values()) or ship.groups or ship.cascade:
            raise ShipError('an office has no captain or crew, groups or cascade; declare only the desk and office seats')

    if (screen := ship.pr_screen) is not None:
        for name in (*screen.hands, screen.reader, *([screen.desk] if screen.desk else [])):
            if name not in ship.seats:
                raise ShipError(f"pr_screen names {name!r}, which is not a declared seat; declared: {', '.join(ship.seats)}")
        chosen = [ship.seats[name] for name in screen.hands]
        # Every declared pool of a hand, not its first alone: a hand moved on to a later pool screens there too.
        for hand in chosen:
            if unjailed := [pool for pool in hand.pools if not ship.jailed(hand, ship.pool(pool))]:
                raise ShipError(f"pr_screen.hands: {hand.name} is not jailed on pool {unjailed[0]}; a hand that reads "
                                "an outsider's words must run in a jail (an endpoint pool, or sandbox = true)")

        def models(hand):
            return {hand.for_pool(pool).model for pool in hand.pools} - {None}
        if len(chosen) == 2:  # two hands: one on each of two models, as before; one hand screens alone
            first, second = chosen
            if set(first.pools) & set(second.pools) or models(first) & models(second):
                raise ShipError(f"pr_screen.hands: {first.name} and {second.name} run on the same model; the screen "
                                "needs one hand on each of two models, on every pool each declares")
        if screen.reader in screen.hands:
            raise ShipError("pr_screen.reader must not be one of its hands")
        # The hands read the planted check's instruction as well as the outsider's text, and the reader reads the
        # outsider's change: none of them may reach a program whose provider's safeguard refuses such reading.
        readers = [("hands", ship.seats[name]) for name in screen.hands] + [("reader", ship.seats[screen.reader])]
        for key, seat in readers:
            for pool in seat.pools:
                program = harnesses.get(seat.for_pool(pool).harness)
                if program.refuses_planted_instructions:
                    raise ShipError(f"pr_screen.{key}: {seat.name} runs on {program.display_name or program.name} "
                                    f"(pool {pool}), whose provider's safeguard can refuse, and so lose, a "
                                    "conversation that reads an outsider's text or a planted instruction; name a "
                                    "seat on another program")
        for hand in chosen:
            # A hand on a subscription keeps a sign-in in its home that qd does not write fresh at each launch, and a
            # refresh there is carried into the pool's own file: a hand that reads an outsider's text has none.
            if paid := [pool for pool in hand.pools if ship.pools[pool].kind != "endpoint"]:
                raise ShipError(f"pr_screen.hands: {hand.name} has pool {paid[0]}, which is not an endpoint; a hand "
                                "that reads an outsider's text runs only on endpoint pools, which keep no sign-in in "
                                "its home")
        if screen.desk is None and sum(seat.role == "desk" for seat in ship.seats.values()) != 1:
            raise ShipError('pr_screen needs one seat with role = "desk", or desk = "seat" in [pr_screen]')

    if ship.push_reminder is not None and ship.push_reminder not in ship.seats:
        raise ShipError(f"ship.push_reminder names {ship.push_reminder!r}, which is not a declared seat; "
                        f"declared: {', '.join(ship.seats)}")

    for name, target in ship.forward.items():
        if name in ship.groups or name in ('ship', ship.operator.lower()):
            raise ShipError('forward names an old seat, not a group or silent sink')
        if target.rsplit('@', 1)[1] == ship.folder.name:
            raise ShipError('forward names a seat of another ship')

    for group in ship.groups.values():
        if group.name in ship.seats or group.name.lower() in (ship.operator.lower(), 'ship'):
            raise ShipError(f'group {group.name}: address also names a seat, operator or ship')
        if group.lead not in ship.seats:
            raise ShipError(f'group {group.name}: lead {group.lead!r} is not a declared seat')
        for member in group.members:
            if member not in ship.seats:
                raise ShipError(f'group {group.name}: member {member!r} is not a declared seat')
        if group.lead in group.members:
            raise ShipError(f'group {group.name}: lead must be separate from its members')
    for pool in ship.pools.values():
        if pool.kind != "endpoint" and harnesses.native_name(pool.provider) is None:
            raise ShipError(f"pool {pool.name}: unsupported {pool.kind} provider {pool.provider!r}; "
                            f"choose a company with a native harness, or declare an endpoint URL")
        if pool.config_dir and pool.api_key:
            raise ShipError(f"pool {pool.name}: declare config_dir (or home) or api_key, not both")
        if pool.endpoint and pool.config_dir:
            raise ShipError(f"pool {pool.name}: an endpoint pool names no config_dir; a key beside the endpoint is api_key")
        if pool.endpoint and not endpoint_fits(pool.endpoint):  # never quoted: a key pasted there would be printed
            raise ShipError(f"pool {pool.name}: endpoint must be an http or https URL such as http://127.0.0.1:8000/v1, "
                            "with no credentials, query or fragment; a key is named by api_key")
        if pool.endpoint and pool.api_key and pool.endpoint.startswith("http://") and not loopback_endpoint(pool.endpoint):
            raise ShipError(f"pool {pool.name}: a key travels only over https, or over http to this machine's loopback; "
                            "give the endpoint an https address")
        if pool.context is not None and (isinstance(pool.context, bool) or not isinstance(pool.context, int) or pool.context <= 0):
            raise ShipError(f"pool {pool.name}: context must be a count of tokens, not {pool.context!r}")
        if pool.output is not None:
            if type(pool.output) is not int or pool.output <= 0:
                raise ShipError(f"pool {pool.name}: output must be a positive count of tokens, not {pool.output!r}")
            if pool.kind != 'endpoint':
                raise ShipError(f"pool {pool.name}: output is supported only for an endpoint pool")
            if pool.context is not None and pool.output >= pool.context:
                raise ShipError(f"pool {pool.name}: output must be smaller than context, leaving room for input")
    for seat in ship.seats.values():
        if seat.session not in ship.sessions:
            raise ShipError(f"seat {seat.name}: session {seat.session!r} is not declared")
        for pool in seat.pools:
            if pool not in ship.pools:
                raise ShipError(f"seat {seat.name}: pool {pool!r} is not declared")
        if not seat.pools:
            raise ShipError(f"seat {seat.name}: declare a pool")
        harnesses.get(seat.harness)  # the legacy/default program remains a valid declaration
        validate_mode(seat.model, seat.model_mode, 'model', f'seat {seat.name}')
        validate_mode(seat.effort, seat.effort_mode, 'effort', f'seat {seat.name}')
        for pool in seat.pools:
            selected = seat.for_pool(pool)
            harness = harnesses.get(selected.harness)
            try:
                validate_selection(selected, harness)
                harness.check_defaults(selected, ship.folder / 'homes' / harness.name)
            except ShipError as error:
                raise ShipError(f"pool {pool}: {error}") from None
            if reason := keyfile.accepts(harness, ship.pools[pool]):
                raise ShipError(f"seat {seat.name}: {reason}")
            if seat.pool_choices and selected.remote_control and not harness.remote_control:
                raise ShipError(f"seat {seat.name}: {harness.name} has no Remote Control")
        if seat.fresh not in (None, "after-order"):
            raise ShipError(f"seat {seat.name}: fresh = {seat.fresh!r}; the one value is \"after-order\"")
        if seat.fresh_after_compactions is not None and (type(seat.fresh_after_compactions) is not int or seat.fresh_after_compactions < 0):
            raise ShipError(f"seat {seat.name}: fresh_after_compactions must be a nonnegative integer; 0 disables it")
    if ship.cascade is not None:
        from .cascade import validate
        validate(ship)


# -- the line editor -------------------------------------------------------------------

_HEADER = re.compile(r"^\s*\[([^\]]+)\]\s*(#.*)?$")


def _key_line(key: str) -> re.Pattern[str]:
    return re.compile(rf"^(\s*){re.escape(key)}\s*=\s*(.*?)(\s+#.*)?\s*$")


def literal(value: object) -> str:
    """A TOML literal for the values the ship file uses."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(literal(v) for v in value) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ShipError("inline table keys must be strings")
        return "{ " + ", ".join(f"{json.dumps(key)} = {literal(item)}" for key, item in value.items()) + " }"
    if isinstance(value, Path):
        return json.dumps(str(value))
    if isinstance(value, str):
        return json.dumps(value)
    raise ShipError(f"unsupported declaration value type: {type(value).__name__}")


def edit(text: str, table: str, key: str, value: object) -> str:
    """Return ``text`` with ``key = value`` set in ``[table]``, every other line kept."""
    lines = text.splitlines()
    start, end = _table_span(lines, table)
    rendered = literal(value)
    if start is None:
        tail = [] if not lines or lines[-1] == "" else [""]
        return "\n".join(lines + tail + [f"[{table}]", f"{key} = {rendered}"]) + "\n"
    pattern = _key_line(key)
    for index in range(start + 1, end):
        match = pattern.match(lines[index])
        if match:
            indent, _, comment = match.groups()
            lines[index] = f"{indent}{key} = {rendered}{comment or ''}"
            break
    else:
        last = end
        while last > start + 1 and lines[last - 1].strip() == "":
            last -= 1
        lines.insert(last, f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


def _table_span(lines: list[str], table: str) -> tuple[int | None, int]:
    """Line index of ``[table]`` and of the next header (or the end); None when absent."""
    start = None
    for index, line in enumerate(lines):
        match = _HEADER.match(line)
        if not match:
            continue
        if start is not None:
            return start, index
        if match.group(1).strip() == table:
            start = index
    return start, len(lines)


def set_value(ship: Ship, table: str, key: str, value: object) -> str:
    """Write one line of the ship file and commit it when the ship folder is a repository.

    Returns the proof line.
    """
    return set_values(ship, [(table, key, value)])[0]


def _edited(ship: Ship, edits: list[tuple[str, str, object]]) -> tuple[str, str, list, list]:
    """The ship file's text after ``edits``, its text before, a proof line for each edit with whether it changed the
    text, and the changes as the commit names them."""
    text = original = ship.file.read_text()
    lines, changed = [], []
    for table, key, value in edits:
        edited = edit(text, table, key, value)
        lines.append((f"{ship.file.name}: {table}.{key} = {literal(value)}", edited != text))
        if edited != text:
            changed.append(f"{table}.{key} = {literal(value)}")
        text = edited
    return text, original, lines, changed


def _commit_follows(ship: Ship, text: str, original: str) -> bool:
    """The rule set_values commits by: the edits change the ship file, and the ship is a repository of its own."""
    return text != original and ship.is_repository


def commits(ship: Ship, edits: list[tuple[str, str, object]]) -> bool:
    """Whether set_values would commit ``edits``, by its own rule, without writing anything: so a caller asks for a
    Git identity only where a commit follows."""
    text, original, _, _ = _edited(ship, edits)
    return _commit_follows(ship, text, original)


def set_values(ship: Ship, edits: list[tuple[str, str, object]]) -> list[str]:
    """Write several lines of the ship file as one edit: validated together, written once, committed once.

    A change that is only valid whole, such as a seat's program with its pool, is never half written.
    Returns one proof line per edit.
    """
    text, original, lines, changed = _edited(ship, edits)
    if text == original:
        return [f"{line} already" for line, _ in lines]
    _validate(_from_text(text, ship.folder))  # the file never holds a declaration qd cannot load
    _write_atomically(ship.file, text)
    if _commit_follows(ship, text, original):
        import os
        caller = os.environ.get("QD_SEAT")
        # The seat that runs qd set is named in its commit, so the change to the briefs is not carried back to it
        # (brief_change.editor). A caller that is no seat of this ship names none.
        trailer = f"\n\nQd-Seat: {caller}" if caller in ship.seats else ""
        suffix = f" · committed {commit(ship.folder, [ship.file.name], 'qd set ' + ', '.join(changed) + trailer)}"
    else:
        suffix = f" · written; {ship.folder.name} is not a repository, so not committed"
    return [line + (suffix if edited else " already") for line, edited in lines]


def _write_atomically(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def create(folder: Path, text: str, orders: str, *, identity: dict[str, str] | None = None) -> Ship:
    """Create one new ship from a validated declaration; never replace an existing folder."""
    _validate(_from_text(text, folder))
    try:
        folder.mkdir()  # exclusive creation; its parent must already exist
    except FileExistsError:
        raise ShipError(f"{folder} already exists; rerun qd setup to view or change its ship") from None
    _write_atomically(folder / "ship.toml", text)
    _write_atomically(folder / "orders.md", orders)
    _write_atomically(folder / ".gitignore", f"/{RUNTIME}/\n")
    try:
        _git(folder, "init", "--quiet")
        commit(folder, ["ship.toml", "orders.md", ".gitignore"], "qd setup: declare crew", identity=identity)
    except ShipError as error:
        raise ShipError(f"ship files saved in {folder}, but the first commit is unfinished: {error}; "
                        "resolve the git reason yourself, initialize this ship's repository if needed, "
                        "then add and commit only ship.toml, orders.md and .gitignore before rerunning qd setup") from None
    return load(folder)


# -- git -------------------------------------------------------------------------------


def _git(folder: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True)
    if done.returncode:
        raise ShipError(f"git {' '.join(args)} in {folder}: {done.stderr.strip()}")
    return done.stdout.strip()


def commit(folder: Path, paths: list[str], message: str, *, identity: dict[str, str] | None = None) -> str:
    """Stage ``paths`` and commit them in ``folder``; the short sha, or 'nothing' when clean."""
    _git(folder, "add", "--", *paths)
    if not _git(folder, "status", "--porcelain", "--", *paths):
        return "nothing"
    # New ships have independent repositories: carry the identity resolved in the
    # project into this commit only, without writing anyone's Git configuration.
    config = [part for key, value in (identity or {}).items() for part in ("-c", f"{key}={value}")]
    _git(folder, *config, "commit", "--quiet", "--only", "-m", message, "--", *paths)  # the named files, nothing swept in (law: nothing-leaks)
    return _git(folder, "rev-parse", "--short", "HEAD")


def fence(ship: Ship) -> str:
    """Make ``runtime/`` a repository of its own, so git run above it never enters it.

    Refuses a ship whose own repository tracks the runtime (law: one-folder).
    """
    runtime = ship.runtime
    if ship.is_repository:
        if _git(ship.folder, "ls-files", "--", RUNTIME):  # law: nothing-leaks
            raise ShipError(
                f"{ship.folder.name} tracks {RUNTIME}/ in git; "
                f"`git -C {ship.folder} rm -r --cached {RUNTIME}` lifts it"
            )
        _ignore_runtime(ship)
    runtime.mkdir(parents=True, exist_ok=True)
    if (runtime / ".git").exists():
        return f"{RUNTIME}/ fenced"
    _git(runtime, "init", "--quiet")
    (runtime / ".git" / "description").write_text("quarterdeck runtime: a fence, never committed to\n")
    return f"{RUNTIME}/ fenced: nested repository created"


def _ignore_runtime(ship: Ship) -> None:
    ignore = ship.folder / ".gitignore"
    lines = ignore.read_text().splitlines() if ignore.exists() else []
    if f"{RUNTIME}/" in lines or RUNTIME in lines:
        return
    ignore.write_text("\n".join(lines + [f"{RUNTIME}/"]) + "\n")
    commit(ship.folder, [".gitignore"], f"qd up: ignore {RUNTIME}/")
