"""The Ship: one file, ``ship.toml``, declaring the sessions, the pools and the seats.

The ship file is the only configuration a ship has (law: ship-file). It is written in a
plain form, one key per line, so that ``qd set`` can edit the one line a change needs and
keep everything else, comments included. A ship folder is its own git repository; the
runtime inside it is a nested repository with no commits, a fence that keeps git run
above it out of the ship's state.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

RUNTIME = "runtime"


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

    def for_pool(self, name: str) -> SeatSpec:
        if name not in self.pools:
            raise ShipError(f"seat {self.name}: pool {name!r} is not in its declared list")
        return replace(self, **self.pool_choices[name]) if self.pool_choices else self

    @property
    def display(self) -> str:
        return self.persona or self.name


@dataclass(frozen=True)
class Ship:
    folder: Path
    name: str
    operator: str
    workspace: Path
    pools: dict[str, Pool]
    sessions: dict[str, Session]
    seats: dict[str, SeatSpec]

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
    """The ship folder: ``--ship``, else ``QD_SHIP`` in the environment."""
    chosen = argument or os.environ.get("QD_SHIP")
    if not chosen:
        raise ShipError("no ship named: pass --ship <folder> or set QD_SHIP")
    folder = Path(chosen).expanduser().resolve()
    if not (folder / "ship.toml").is_file():
        raise ShipError(f"{folder} holds no ship.toml")
    return folder


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
    pools = {name: _pool(name, table) for name, table in data.get("pools", {}).items()}
    sessions = {
        name: Session(name, table.get("title", name), bool(table.get("human", False)))
        for name, table in data.get("sessions", {}).items()
    }
    seats = {name: _seat(name, table, folder) for name, table in data.get("seats", {}).items()}
    return Ship(
        folder=folder,
        name=head.get("name", folder.name),
        operator=head.get("operator", "operator"),
        workspace=_path(head.get("workspace", "."), folder),
        pools=pools,
        sessions=sessions,
        seats=seats,
    )


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
        context=table.get("context"), price=price,
    )


def _seat(name: str, table: dict, folder: Path) -> SeatSpec:
    if "sandbox" in table and type(table["sandbox"]) is not bool:
        raise ShipError(f"seat {name}: sandbox must be true or false (a TOML boolean)")
    pool = table.get("pool", [])
    entries = pool if isinstance(pool, list) else [pool]
    pools, choices = [], {}
    defaults = dict(harness=table['harness'], model=table.get('model'), effort=table.get('effort'),
                    settings=dict(table.get('settings', {})))
    for entry in entries:
        if isinstance(entry, str):
            selected, choice = entry, defaults
        elif isinstance(entry, dict):
            if set(entry) - {'pool', 'harness', 'model', 'effort', 'settings'}:
                raise ShipError(f"seat {name}: unknown pool choice fields")
            selected = entry.get('pool')
            if any(not isinstance(entry.get(key), str) or not entry[key].strip()
                   for key in ('harness', 'model')):
                raise ShipError(f"seat {name}: each inline pool choice needs an explicit harness and model")
            if (entry.get('effort') is not None and not isinstance(entry['effort'], str)
                    or not isinstance(entry.get('settings', {}), dict)):
                raise ShipError(f"seat {name}: pool choice effort must be text and settings must be a table")
            choice = dict(harness=entry['harness'], model=entry['model'], effort=entry.get('effort'),
                          settings=dict(entry.get('settings', {})))
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
        remote_control=bool(table.get("remote_control", False)),
        sandbox=table.get("sandbox"),
        settings=dict(table.get("settings", {})),
        pool_choices=choices if any(isinstance(entry, dict) for entry in entries) else {},
    )


SEAT_KEYS = frozenset({"session", "role", "harness", "pool", "model", "effort", "persona", "cwd", "up", "fresh", "remote_control", "sandbox"})
"""The keys of ``[seats.<name>]`` the framework reads; any other key belongs to the program, under ``.settings``."""


def _path(value: str, folder: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (folder / path).resolve()


def _validate(ship: Ship) -> None:
    from . import harness as harnesses  # the adapters import this module, so it is imported here, not above

    for pool in ship.pools.values():
        if pool.kind != "endpoint" and pool.provider not in harnesses.NAMES:
            raise ShipError(f"pool {pool.name}: unsupported {pool.kind} provider {pool.provider!r}; "
                            f"choose one of {', '.join(harnesses.NAMES)}, or declare an endpoint URL")
        if pool.config_dir and pool.api_key:
            raise ShipError(f"pool {pool.name}: declare config_dir (or home) or api_key, not both")
        if pool.endpoint and pool.config_dir:
            raise ShipError(f"pool {pool.name}: an endpoint pool names no config_dir; a key beside the endpoint is api_key")
        if pool.endpoint and not re.match(r"^https?://\S+$", pool.endpoint):
            raise ShipError(f"pool {pool.name}: endpoint must be an http or https URL such as http://127.0.0.1:8000/v1, not {pool.endpoint!r}")
        if pool.context is not None and (isinstance(pool.context, bool) or not isinstance(pool.context, int) or pool.context <= 0):
            raise ShipError(f"pool {pool.name}: context must be a count of tokens, not {pool.context!r}")
    for seat in ship.seats.values():
        if seat.session not in ship.sessions:
            raise ShipError(f"seat {seat.name}: session {seat.session!r} is not declared")
        for pool in seat.pools:
            if pool not in ship.pools:
                raise ShipError(f"seat {seat.name}: pool {pool!r} is not declared")
        if not seat.pools:
            raise ShipError(f"seat {seat.name}: declare a pool")
        harnesses.get(seat.harness)  # the legacy/default program remains a valid declaration
        for pool in seat.pools:
            selected = seat.for_pool(pool)
            harness = harnesses.get(selected.harness)
            if reason := harness.accepts(ship.pools[pool]):
                raise ShipError(f"seat {seat.name}: {reason}")
            if seat.pool_choices and selected.remote_control and not harness.remote_control:
                raise ShipError(f"seat {seat.name}: {harness.name} has no Remote Control")
        if seat.fresh not in (None, "after-order"):
            raise ShipError(f"seat {seat.name}: fresh = {seat.fresh!r}; the one value is \"after-order\"")


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
    if isinstance(value, Path):
        return json.dumps(str(value))
    return json.dumps(str(value))


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
    text = ship.file.read_text()
    changed = edit(text, table, key, value)
    if changed == text:
        return f"{ship.file.name}: {table}.{key} = {literal(value)} already"
    _validate(_from_text(changed, ship.folder))  # the file never holds a declaration qd cannot load
    _write_atomically(ship.file, changed)
    line = f"{ship.file.name}: {table}.{key} = {literal(value)}"
    if ship.is_repository:
        sha = commit(ship.folder, [ship.file.name], f"qd set {table}.{key} = {literal(value)}")
        return f"{line} · committed {sha}"
    return f"{line} · written; {ship.folder.name} is not a repository, so not committed"


def _write_atomically(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


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
