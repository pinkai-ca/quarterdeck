"""Building a seat's home: a pool's credential brought into it, a key pool's environment, and the ship's settings layered over a program's own."""

from __future__ import annotations

import os
import json
import tomllib
from pathlib import Path

from .. import seat_files
from ..ship import Pool, ShipError
from .types import Credential


def credential(pool: Pool, link: Path, variable: str, *, copy: bool = False) -> Credential | None:
    """How a pool's credential reaches a seat home: a key by environment, a subscription by one link, or by one copy
    for a program that writes its refresh through the link into the pool's own file (``copy``).

    A refreshed file in the link's place is left alone: the service reseeds the pool from it.
    """
    if pool.api_key:
        if _newer(pool, link, None):
            raise ShipError(f"{link} holds a detached subscription credential and is not deleted: it may hold the only "
                            f"live token. Pool {pool.name} uses an API key and has no shared credential file; "
                            "move it aside before trying again")
        seat_files.unlink(link)  # a key pool shares no file: whatever a former pool left here goes
        return Credential(variable=variable)
    if pool.config_dir is None:
        return None
    source = pool.config_dir / link.name
    if copy:
        return _copy(pool, link, source)
    if _newer(pool, link, source):
        raise ShipError(f"{link} holds a credential newer than pool {pool.name}'s and is not deleted: it may hold the only "
                        f"live token. If it is {pool.name}'s, put it in {source}; otherwise move it aside; then try again")
    # A link to a former pool, or a refreshed file the service has reseeded from, is replaced by a link renamed over
    # its name: never followed, whatever the jail planted there.
    seat_files.symlink(link, source)
    return Credential(link=(link, source))


def _copy(pool: Pool, path: Path, source: Path) -> Credential:
    """The home's own copy of the pool's sign-in, made while no program of this home runs (a build or a launch).

    A copy newer than the pool's is this home's own refresh and stays, for the service to put in the pool's place. A link
    left from before, an older or unreadable copy, or none at all becomes a copy of the pool's file, put in one step at
    mode 600. A copy as fresh as the pool's is left as it is. With no pool file there is nothing to copy.
    """
    from . import native_for  # the adapters import this module

    reader = native_for(pool.provider)
    if path.is_symlink():
        seat_files.unlink(path)
    elif path.exists() and (_newer(pool, path, source)
                            or (source.exists() and reader.freshness(path) is not None
                                and reader.freshness(path) == reader.freshness(source))):
        return Credential(link=(path, source))
    if source.exists():
        put_copy(source, path)
    return Credential(link=(path, source))


def put_copy(source: Path, path: Path) -> None:
    """A copy of ``source`` at ``path`` in one step: its bytes and times kept, so it reads exactly as fresh, mode 600."""
    seat_files.copy(source, path, mode=0o600)  # a temporary renamed over the name: a planted link is never followed


def _newer(pool: Pool, link: Path, source: Path | None) -> bool:
    """A readable detached refresh: newer than the pool's own, or with no shared file to compare."""
    if link.is_symlink() or not link.exists():
        return False
    from . import native_for  # the adapters import this module

    reader = native_for(pool.provider)
    detached = reader.freshness(link)
    current = reader.freshness(source) if source is not None and source.exists() else None
    return detached is not None and (current is None or detached > current)


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
