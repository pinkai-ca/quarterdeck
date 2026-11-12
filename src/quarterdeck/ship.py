"""The Ship: one file, ``ship.toml``, declaring the sessions, the pools and the seats.

The ship file is the only configuration a ship has (law: ship-file). It is written in a
plain form, one key per line, so that ``qd set`` can edit the one line a change needs and
keep everything else, comments included. A ship folder is its own git repository; the
runtime inside it is a nested repository with no commits, a fence that keeps git run
above it out of the ship's state.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

RUNTIME = "runtime"
TICK = 1.0  # the service's default wait after each pass, in seconds
TICK_BOUNDS = (0.05, 5.0)  # inclusive; the heartbeat stays well inside servicectl's stale age


class ShipError(Exception):
    """A refusal: the reason, and what would lift it."""


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
    fresh_after_compactions: int | None = None
    model_mode: str | None = None
    effort_mode: str | None = None

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

    @property
    def file(self) -> Path:
        return self.folder / "ship.toml"

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
TEMPLATES = frozenset({"default", "drydock", "forum", "cascade"})
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
    if os.environ.get("QD_SEAT"):
        raise ShipError("no ship named: a seat runs with its own QD_SHIP and never uses the default ship")
    folder, _ = default_ship(project)
    return folder, folder.name


def ships_folder(project: Path | None = None) -> Path:
    if project is None:
        from .standing import PROJECT_DIR
        project = PROJECT_DIR
    return Path(project) / "ships"


def default_ship(project: Path | None = None) -> tuple[Path, str]:
    """The default ship's folder and how it was chosen: ``mark`` or ``only ship``. Never a silent fallback."""
    ships = ships_folder(project)
    mark = ships / MARK
    try:
        text = mark.read_text()
    except FileNotFoundError:
        text = None
    except (OSError, UnicodeError) as error:
        raise ShipError(f"the default mark {mark} is unreadable ({type(error).__name__}); "
                        "qd default <ship> rewrites it") from None
    if text is not None:
        name = text.strip()
        if not valid_name(name) or text not in (name, name + "\n"):
            raise ShipError(f"the default mark {mark} does not hold one ship folder name; qd default <ship> rewrites it")
        folder = ships / name
        if not (folder / "ship.toml").is_file():
            raise ShipError(f"the default ship {name} marked in {mark} is missing or holds no ship.toml; "
                            "pass --ship <folder>, or qd default <ship> marks another")
        return folder.resolve(), "mark"
    found = candidates(project)
    if not found:
        raise ShipError("no ship named: pass --ship <folder>, or make a ship and mark it with qd default <ship>")
    if len(found) > 1:
        raise ShipError(f"no default ship among several: {', '.join(found)}; qd default <ship> sets one, "
                        "or pass --ship <folder>")
    return (ships / found[0]).resolve(), "only ship"


def candidates(project: Path | None = None) -> list[str]:
    """The ships under ships/ that are not templates or the practice ship, by folder name."""
    ships = ships_folder(project)
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
    folder = ships / name
    if not (folder / "ship.toml").is_file():
        raise ShipError(f"{folder} is missing or holds no ship.toml; qd default marks only a ship that exists")
    durable.replace_file(ships / MARK, name + "\n")  # law: one-folder
    return folder.resolve()


def load(folder: Path) -> Ship:
    folder = Path(folder).expanduser().resolve()
    try:
        text = (folder / "ship.toml").read_text()
    except FileNotFoundError:
        raise ShipError(f"{folder} holds no ship.toml") from None
    ship = _from_text(text, folder)
    _validate(ship)
    return ship


def _from_text(text: str, folder: Path) -> Ship:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ShipError(f"{folder / 'ship.toml'}: {error}") from None
    head = data.get("ship", {})
    from .copy_state import code_mode
    try:
        code = code_mode(head)
    except (ValueError, TypeError):
        raise ShipError('ship.code must be absent, or "checkout"') from None
    pools = {name: _pool(name, table) for name, table in data.get("pools", {}).items()}
    sessions = {
        name: Session(name, table.get("title", name), bool(table.get("human", False)))
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
        pools=pools,
        sessions=sessions,
        seats=seats,
        groups=groups,
        cascade=declaration(data.get("cascade")),
        tick=_tick(head.get("tick", TICK)),
        code=code,
    )


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


def _pool(name: str, table: dict) -> Pool:
    config_dir = table.get("config_dir", table.get("home"))
    endpoint = table.get("endpoint")
    price = table.get("price")
    if price is not None:
        from dataclasses import asdict
        from .pricing import Price
        try:
            price = asdict(Price.parse(price))
        except ValueError as error:
            raise ShipError(f"pool {name}: {error}") from None
        if not endpoint:
            raise ShipError(f"pool {name}: stated price is supported only for an endpoint pool")
    return Pool(
        name=name,
        provider=table["provider"],
        config_dir=Path(config_dir).expanduser() if config_dir else None,
        api_key=table.get("api_key"),
        budget=table.get("budget"),
        endpoint=str(endpoint).rstrip("/") if endpoint else None,
        context=table.get("context"), price=price, output=table.get("output"),
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


def _validate(ship: Ship) -> None:
    from . import harness as harnesses  # the adapters import this module, so it is imported here, not above
    from .harness.capabilities import validate_mode, validate_selection

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
        if pool.endpoint and not re.match(r"^https?://\S+$", pool.endpoint):
            raise ShipError(f"pool {pool.name}: endpoint must be an http or https URL such as http://127.0.0.1:8000/v1, not {pool.endpoint!r}")
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
            if reason := harness.accepts(ship.pools[pool]):
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


def set_values(ship: Ship, edits: list[tuple[str, str, object]]) -> list[str]:
    """Write several lines of the ship file as one edit: validated together, written once, committed once.

    A change that is only valid whole, such as a seat's program with its pool, is never half written.
    Returns one proof line per edit.
    """
    text = original = ship.file.read_text()
    lines, changed = [], []
    for table, key, value in edits:
        edited = edit(text, table, key, value)
        lines.append((f"{ship.file.name}: {table}.{key} = {literal(value)}", edited != text))
        if edited != text:
            changed.append(f"{table}.{key} = {literal(value)}")
        text = edited
    if text == original:
        return [f"{line} already" for line, _ in lines]
    _validate(_from_text(text, ship.folder))  # the file never holds a declaration qd cannot load
    _write_atomically(ship.file, text)
    if ship.is_repository:
        suffix = f" · committed {commit(ship.folder, [ship.file.name], 'qd set ' + ', '.join(changed))}"
    else:
        suffix = f" · written; {ship.folder.name} is not a repository, so not committed"
    return [line + (suffix if edited else " already") for line, edited in lines]


def _write_atomically(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def create(folder: Path, text: str, orders: str) -> Ship:
    """Create one new ship from a validated declaration; never replace an existing folder."""
    _validate(_from_text(text, folder))
    try:
        folder.mkdir()  # exclusive creation; its parent must already exist
    except FileExistsError:
        raise ShipError(f"{folder} already exists; rerun qd setup to view or change its ship") from None
    _write_atomically(folder / "ship.toml", text)
    _write_atomically(folder / "orders.md", orders)
    _write_atomically(folder / ".gitignore", f"/{RUNTIME}/\n")
    _git(folder, "init", "--quiet")
    commit(folder, ["ship.toml", "orders.md", ".gitignore"], "qd setup: declare crew")
    return load(folder)


# -- git -------------------------------------------------------------------------------


def _git(folder: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True)
    if done.returncode:
        raise ShipError(f"git {' '.join(args)} in {folder}: {done.stderr.strip()}")
    return done.stdout.strip()


def commit(folder: Path, paths: list[str], message: str) -> str:
    """Stage ``paths`` and commit them in ``folder``; the short sha, or 'nothing' when clean."""
    _git(folder, "add", "--", *paths)
    if not _git(folder, "status", "--porcelain", "--", *paths):
        return "nothing"
    _git(folder, "commit", "--quiet", "--only", "-m", message, "--", *paths)  # the named files, nothing swept in (law: nothing-leaks)
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
