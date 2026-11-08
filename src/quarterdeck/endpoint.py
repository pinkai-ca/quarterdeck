"""Endpoint pools: a model served at an OpenAI-compatible URL, asked what it serves.

An endpoint pool has no credential and no quota wall. A local server is paid for by the machine
that runs it, and a remote one by the key named beside the URL, which reaches the program by an
environment variable and never lands in a file. What the endpoint serves is read from its
``/models`` listing when a seat home is built, so the ship file need not repeat it: vLLM states
each model's context length there; a server that does not is given the pool's ``context`` or a
conservative default, and the home says which.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .harness.base import key_environment
from .ship import Pool, ShipError
from .pricing import save_price, saved_price

KEY_VARIABLE = "QD_ENDPOINT_KEY"  # the variable a keyed endpoint's program reads; the key itself is never written
DEFAULT_CONTEXT = 32768
DEFAULT_OUTPUT = 8192
TIMEOUT = 5.0
CHOICE_FILE = "model.json"  # written in the seat home when it is built, read at launch


@dataclass(frozen=True)
class Served:
    """One model an endpoint lists, with the context length it states, where it states one."""

    id: str
    context: int | None = None


def models(url: str, timeout: float = TIMEOUT, *, api_key: str | None = None) -> list[Served]:
    """What an endpoint serves, from its ``/models`` listing; a ShipError names why it cannot be read."""
    headers = {"User-Agent": "quarterdeck"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(f"{url.rstrip('/')}/models", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        raise ShipError(f"{url} answered {error.code} to /models") from None
    except (OSError, ValueError) as error:
        # Header validation errors can quote the Authorization value. Never echo
        # exception payloads from an authenticated request into doctor or a post.
        detail = type(error).__name__ if api_key else (getattr(error, "reason", None) or error)
        raise ShipError(f"{url} does not answer: {detail}") from None
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ShipError(f"{url}/models answered with something other than a model listing")
    served = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            length = row.get("max_model_len")  # vLLM states it; the OpenAI shape has no such field
            served.append(Served(row["id"], length if isinstance(length, int) and length > 0 else None))
    return served


@dataclass(frozen=True)
class Choice:
    """The model a seat runs on an endpoint, and the limits its program is told."""

    pool: str
    model: str
    context: int
    output: int
    note: str | None = None  # why the context is what it is, where the endpoint did not state it
    price: dict | None = None  # the effective stated rates for this model, captured before launch

    def write(self, home: Path) -> None:
        data = asdict(self)
        price = data.pop('price')
        path = home / CHOICE_FILE
        path.write_text(json.dumps(data, indent=2) + "\n")
        save_price(path, price)

    @classmethod
    def read(cls, home: Path) -> "Choice":
        try:
            data = json.loads((home / CHOICE_FILE).read_text())
            data.pop("price", None)
            return cls(**data, price=saved_price(home / CHOICE_FILE))
        except (OSError, ValueError, TypeError):
            raise ShipError(f"{home} holds no model choice; a seat home is built before its launch") from None


def choose(pool: Pool, wanted: str | None) -> Choice:
    """The model a seat runs on its endpoint pool: the one the ship names, found in the listing, or the first listed."""
    if not pool.endpoint:
        raise ShipError(f"pool {pool.name} names no endpoint")
    try:
        served = models(pool.endpoint, api_key=key(pool).get(KEY_VARIABLE))
    except ShipError as error:
        raise ShipError(f"pool {pool.name}: {error}; start the server, or point the pool at one that answers") from None
    if not served:
        raise ShipError(f"pool {pool.name}: {pool.endpoint} serves no model")
    if wanted is None:
        model = served[0]
    else:
        model = next((m for m in served if m.id == wanted), None)
        if model is None:
            names = ", ".join(m.id for m in served)
            raise ShipError(f"pool {pool.name}: {pool.endpoint} does not serve {wanted!r}; it serves {names}")
    context = pool.context or model.context or DEFAULT_CONTEXT
    note = None
    if not pool.context and not model.context:
        note = f"the endpoint states no context length; {DEFAULT_CONTEXT} assumed, or declare context = N in the pool"
    return Choice(pool.name, model.id, context, min(DEFAULT_OUTPUT, max(context // 4, 1024)), note=note)


def key(pool: Pool) -> dict[str, str]:
    """The environment carrying the key named beside an endpoint; empty for an endpoint that takes none."""
    return key_environment(pool, KEY_VARIABLE)


def describe(pool: Pool) -> str:
    """What an endpoint pool serves, for ``qd doctor``; a ShipError says why it cannot be read."""
    served = models(pool.endpoint or "", api_key=key(pool).get(KEY_VARIABLE))
    names = ", ".join(f"{m.id} ({m.context:,} tokens)" if m.context else m.id for m in served) or "nothing"
    return f"{pool.endpoint} answers; serves {names}"
