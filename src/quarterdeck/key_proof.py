"""qd doctor's proof that a pool's key works, by one call that spends nothing.

The call goes only to the provider's fixed address, or to the endpoint the pool itself declares. The key travels in a
header, never in a URL. A redirect is refused, since urllib would carry the header to wherever it points.
Z.ai's documented 429 body is read only for its bounded business code. No response or exception text is kept.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
from typing import Literal
import urllib.error
import urllib.parse
import urllib.request

from . import keyfile
from .harness.key_routes import ZAI_MODELS, zai_route

TIMEOUT = 10.0
ZAI_ERROR_BYTES = 16 * 1024
ZAI_EXHAUSTED = 'Z.ai refused the key for insufficient balance or no resource package (HTTP 429, code 1113)'


@dataclass(frozen=True)
class Address:
    url: str
    header: str
    bearer: bool = False
    extra: tuple[tuple[str, str], ...] = ()


ADDRESSES = {
    'anthropic': Address('https://api.anthropic.com/v1/models', 'x-api-key',
                         extra=(('anthropic-version', '2023-06-01'),)),
    'openai': Address('https://api.openai.com/v1/models', 'Authorization', bearer=True),
    'xai': Address('https://api.x.ai/v1/models', 'Authorization', bearer=True),
    'google': Address('https://generativelanguage.googleapis.com/v1beta/models', 'x-goog-api-key'),
}
OPENROUTER_HOST = 'openrouter.ai'  # its model list is public and proves nothing; its own key route does
ANTHROPIC_HOST = 'api.anthropic.com'
ANTHROPIC_VERSION = ('anthropic-version', '2023-06-01')  # its /v1/models answers 400 to a Bearer key without it


def listing_headers(url: str) -> dict[str, str]:
    """What an endpoint's model listing needs beside its key, by the exact host of the declared address."""
    return dict((ANTHROPIC_VERSION,)) if urllib.parse.urlsplit(url).hostname == ANTHROPIC_HOST else {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def keyed_opener(url: str) -> urllib.request.OpenerDirector:
    """Follows no redirect. Over plain http a proxy would read the key's header, so that call goes direct; over https
    a proxy sees only CONNECT and is kept."""
    handlers: list = [_NoRedirect()]
    if urllib.parse.urlsplit(url).scheme != 'https':
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def address(pool) -> tuple[Address | None, bool]:
    """Where this pool's key is proven, and whether a good answer proves it: an endpoint's listing may be public."""
    if zai_route(pool) is not None:
        return Address(ZAI_MODELS, 'Authorization', bearer=True), False
    if pool.endpoint:
        base = pool.endpoint.rstrip('/')
        if urllib.parse.urlsplit(base).hostname == OPENROUTER_HOST:
            return Address(f'{base}/key', 'Authorization', bearer=True), True
        return Address(f'{base}/models', 'Authorization', bearer=True, extra=tuple(listing_headers(base).items())), False
    return ADDRESSES.get(pool.provider), True


def zai_exhausted(url: str, error: urllib.error.HTTPError) -> bool:
    """Only the exact Z.ai listing's HTTP 429 with the documented business code establishes exhaustion."""
    if url != ZAI_MODELS or error.code != 429:
        return False
    try:
        raw = error.read(ZAI_ERROR_BYTES + 1)
    except (OSError, ValueError, http.client.HTTPException):
        return False
    return zai_balance_response(raw)


def zai_balance_response(raw: bytes | str) -> bool:
    if len(raw) > ZAI_ERROR_BYTES:
        return False
    try:
        if isinstance(raw, str) and len(raw.encode('utf-8')) > ZAI_ERROR_BYTES:
            return False
        body = json.loads(raw)
    except (ValueError, RecursionError):
        return False
    detail = body.get('error') if isinstance(body, dict) else None
    code = detail.get('code') if isinstance(detail, dict) else None
    return code == '1113' or type(code) is int and code == 1113


def status(found: Address, key: str, timeout: float = TIMEOUT) -> int | Literal['zai-exhausted'] | None:
    """HTTP status, a classified Z.ai exhaustion, or None when no answer came."""
    headers = {'User-Agent': 'quarterdeck', found.header: f'Bearer {key}' if found.bearer else key, **dict(found.extra)}
    opener = keyed_opener(found.url)
    outcome = None
    try:
        with opener.open(urllib.request.Request(found.url, headers=headers), timeout=timeout) as answer:
            outcome = answer.status
    except urllib.error.HTTPError as error:
        with error:
            outcome = 'zai-exhausted' if zai_exhausted(found.url, error) else error.code
    except (OSError, ValueError, http.client.HTTPException):
        outcome = None  # a header's validation error can quote its value: nothing of it is kept
    return outcome


def prove(pool) -> tuple[bool, str]:
    """Whether the doctor passes this pool's key, and its words. Holds nothing of the key or of the answer."""
    found, conclusive = address(pool)
    if found is None:
        return True, 'this provider has no call that proves a key without spending'
    try:
        key = keyfile.value(pool)
    except keyfile.KeyProblem as problem:
        return False, str(problem)
    answer = status(found, key)
    if answer is None:
        return False, f'could not reach {urllib.parse.urlsplit(found.url).hostname}'
    if answer == 'zai-exhausted':
        return False, ZAI_EXHAUSTED
    if 200 <= answer < 300:
        if zai_route(pool) is not None:
            return True, 'Z.ai model listing answered; inference unverified'
        return True, 'key works' if conclusive else 'answers with this key; it may not need one'
    if answer in (401, 403):
        return False, 'key refused by the provider'
    if 300 <= answer < 400:
        return False, 'the provider answered with a redirect, which is not followed with a key'
    return False, f'could not prove the key: the provider answered {answer}'
