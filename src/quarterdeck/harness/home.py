"""Building a seat's home: a pool's credential brought into it, a key pool's environment, and the ship's settings layered over a program's own."""

from __future__ import annotations

import os
import json
import tomllib
from pathlib import Path

from ..ship import Pool, ShipError
from .types import Credential


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
    if _newer(pool, link, source):
        raise ShipError(f"{link} holds a credential newer than pool {pool.name}'s and is not deleted: it may hold the only "
                        f"live token. If it is {pool.name}'s, put it in {source}; otherwise move it aside; then try again")
    link.unlink(missing_ok=True)  # a link to a former pool, or a refreshed file the service has reseeded from
    link.symlink_to(source)
    return Credential(link=(link, source))


def _newer(pool: Pool, link: Path, source: Path) -> bool:
    """A detached credential the pool's adapter reads as live and fresher than the pool's own."""
    if link.is_symlink() or not link.exists():
        return False
    from . import get  # the adapters import this module

    reader = get(pool.provider)
    detached = reader.freshness(link)
    current = reader.freshness(source) if source.exists() else None
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
