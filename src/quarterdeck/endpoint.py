"""Endpoint pools: a model served at an OpenAI-compatible URL, asked what it serves.

An endpoint pool has no credential and no quota wall. A local server is paid for by the machine
that runs it, and a remote one by the key named beside the URL, which reaches the program by an
environment variable and never lands in a file. What the endpoint serves is read from its
``/models`` listing when a seat home is built, so the ship file need not repeat it: vLLM states
each model's context length there; a server that does not is given the pool's ``context`` or a
conservative default, and the home says which.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from . import key_proof, keyfile
from . import durable
from .harness.key_routes import ZAI_CHAT, ZAI_MODELS, zai_model, zai_route
from .ship import Pool, ShipError
from .pricing import save_price, saved_price

KEY_VARIABLE = "QD_ENDPOINT_KEY"  # the variable a keyed endpoint's program reads; the key itself is never written
OPENAI_RESPONSES_BASE = "https://api.openai.com/v1"
GOOGLE_GENERATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"
GOOGLE_OPENAI_BASE = f"{GOOGLE_GENERATIVE_BASE}/openai"
DEFAULT_CONTEXT = 32768
DEFAULT_OUTPUT = 8192
TIMEOUT = 5.0
ZAI_LISTING_BYTES = 256 * 1024
ZAI_LISTING_ROWS = 256
CHOICE_FILE = "model.json"  # written in the seat home when it is built, read at launch


def openai_responses(url: str | None) -> bool:
    """Select the native API only for OpenAI's own endpoint."""
    return isinstance(url, str) and url.removesuffix("/") == OPENAI_RESPONSES_BASE


def google_openai(url: str | None) -> bool:
    return isinstance(url, str) and url.removesuffix("/") == GOOGLE_OPENAI_BASE


@dataclass(frozen=True)
class Served:
    """One listed model and any context length or creation time the listing states."""

    id: str
    context: int | None = None
    created: int | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def models(url: str, timeout: float = TIMEOUT, *, api_key: str | None = None,
           allow_redirects: bool = True) -> list[Served]:
    """Read ``/models``; False uses direct transport without inherited proxies or redirects.

    Existing callers retain urllib's normal transport. Setup uses the direct form only
    for its person's loopback server, without a key, before accepting a declaration.
    """
    headers = {"User-Agent": "quarterdeck"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers.update(key_proof.listing_headers(url))
    request = urllib.request.Request(f"{url.rstrip('/')}/models", headers=headers)
    zai = request.full_url == ZAI_MODELS
    try:
        # urllib carries the auth header across a redirect, so a call with a key follows none.
        opening = (key_proof.keyed_opener(request.full_url).open if api_key and allow_redirects else
                   urllib.request.urlopen if allow_redirects else
                   urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open)
        with opening(request, timeout=timeout) as response:
            if zai:
                raw = response.read(ZAI_LISTING_BYTES + 1)
                if len(raw) > ZAI_LISTING_BYTES:
                    raise ShipError("Z.ai model listing exceeds the byte limit")
                data = json.loads(raw)
            else:
                data = json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            if api_key and key_proof.zai_exhausted(request.full_url, error):
                raise ShipError(key_proof.ZAI_EXHAUSTED) from None
            raise ShipError(f"{url} answered {error.code} to /models") from None
    except (OSError, ValueError, http.client.HTTPException, RecursionError) as error:
        # Header validation errors can quote the Authorization value. Never echo
        # exception payloads from an authenticated request into doctor or a post.
        detail = type(error).__name__ if api_key else (getattr(error, "reason", None) or error)
        raise ShipError(f"{url} does not answer: {detail}") from None
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ShipError(f"{url}/models answered with something other than a model listing")
    if zai and len(rows) > ZAI_LISTING_ROWS:
        raise ShipError("Z.ai model listing exceeds the row limit")
    served = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            if zai and (not zai_model(row["id"]) or (api_key and api_key in row["id"])):
                continue
            length = row.get("max_model_len")  # vLLM states it; the OpenAI shape has no such field
            created = row.get("created")
            served.append(Served(row["id"], length if isinstance(length, int) and length > 0 else None,
                                 created if type(created) is int and created > 0 else None))
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
        durable.replace_file(path, json.dumps(data, indent=2) + "\n")
        save_price(path, price)

    @classmethod
    def read(cls, home: Path) -> "Choice":
        try:
            data = json.loads((home / CHOICE_FILE).read_text())
            return cls(**data, price=saved_price(home / CHOICE_FILE))
        except (OSError, ValueError, TypeError):
            raise ShipError(f"{home} holds no model choice; a seat home is built before its launch") from None


def choose(pool: Pool, wanted: str | None) -> Choice:
    """The model a seat runs on its endpoint pool: the one the ship names, found in the listing, or the first listed."""
    if not pool.endpoint:
        raise ShipError(f"pool {pool.name} names no endpoint")
    zai = zai_route(pool) == ZAI_CHAT
    if zai and not zai_model(wanted):
        raise ShipError(f"pool {pool.name}: Z.ai needs an explicit model ID for this account")
    try:
        served = models(pool.endpoint, api_key=_secret(pool))
    except ShipError as error:
        if zai:
            raise ShipError(f"pool {pool.name}: {error}; could not discover models for launch") from None
        raise ShipError(f"pool {pool.name}: {error}; start the server, or point the pool at one that answers") from None
    if not served:
        raise ShipError(f"pool {pool.name}: {pool.endpoint} serves no model")
    if wanted is None:
        model = served[0]
    else:
        model = next((m for m in served if m.id == wanted), None)
        if (model is None and wanted and "/" not in wanted and pool.provider == "google"
                and google_openai(pool.endpoint)):
            # Gemini's compatible endpoint can list resource names but expects a bare ID in requests.
            model = next((replace(m, id=wanted) for m in served if m.id == f"models/{wanted}"), None)
        if model is None:
            if zai:
                raise ShipError(f"pool {pool.name}: Z.ai does not serve the declared model")
            names = ", ".join(m.id for m in served)
            raise ShipError(f"pool {pool.name}: {pool.endpoint} does not serve {wanted!r}; it serves {names}")
    context = pool.context or model.context or DEFAULT_CONTEXT
    if pool.output is not None and pool.output >= context:
        raise ShipError(f"pool {pool.name}: output must be smaller than the selected model's context ({context:,}), leaving room for input")
    notes = []
    if not pool.context and not model.context:
        notes.append(f"the endpoint states no context length; {DEFAULT_CONTEXT} assumed, or declare context = N in the pool")
    if zai:
        latest = newest_by_created(served)
        notes.append(f"newest by listed creation time: {latest}" if latest else "newest by listed creation time unknown")
    output = pool.output if pool.output is not None else min(DEFAULT_OUTPUT, max(context // 4, 1024))
    return Choice(pool.name, model.id, context, output, note=" · ".join(notes) or None)


def newest_by_created(served: list[Served]) -> str | None:
    """A listing establishes one newest model only when its creation dates are complete and unambiguous."""
    if not served or any(model.created is None for model in served):
        return None
    newest = max(model.created for model in served)
    names = {model.id for model in served if model.created == newest}
    return names.pop() if len(names) == 1 else None


def key(pool: Pool) -> dict[str, str]:
    """The variable a keyed endpoint's program reads, holding the key's reference and never the key; empty for an
    endpoint that takes none. A launch takes the name out and the launcher fills it (key_launch)."""
    return {KEY_VARIABLE: pool.api_key} if pool.api_key else {}


def _secret(pool: Pool) -> str | None:
    """The key itself, for the one call to the endpoint's own listing; None for an endpoint that takes none."""
    if not pool.api_key:
        return None
    try:
        return keyfile.value(pool)
    except keyfile.KeyProblem as problem:
        raise ShipError(str(problem)) from None


def describe(pool: Pool) -> str:
    """What an endpoint pool serves, for ``qd doctor``; a ShipError says why it cannot be read."""
    served = models(pool.endpoint or "", api_key=_secret(pool))
    names = ", ".join(f"{m.id} ({m.context:,} tokens)" if m.context else m.id for m in served) or "nothing"
    output = f"; output {pool.output:,} tokens (declared)" if pool.output is not None else ''
    return f"{pool.endpoint} answers; serves {names}{output}"
