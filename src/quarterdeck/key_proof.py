"""qd doctor's proof that a pool's key works, by one call that spends nothing.

The call goes only to the provider's fixed address, or to the endpoint the pool itself declares. The key travels in a
header, never in a URL. A redirect is refused, since urllib would carry the header to wherever it points. Only the
answer's status is read: its body is never read, printed or stored, and no exception's text is kept.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import urllib.error
import urllib.parse
import urllib.request

from . import keyfile

TIMEOUT = 10.0


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
    if pool.endpoint:
        base = pool.endpoint.rstrip('/')
        if urllib.parse.urlsplit(base).hostname == OPENROUTER_HOST:
            return Address(f'{base}/key', 'Authorization', bearer=True), True
        return Address(f'{base}/models', 'Authorization', bearer=True, extra=tuple(listing_headers(base).items())), False
    return ADDRESSES.get(pool.provider), True


def status(found: Address, key: str, timeout: float = TIMEOUT) -> int | None:
    """The answer's status, or None where no answer came. The body stays unread."""
    headers = {'User-Agent': 'quarterdeck', found.header: f'Bearer {key}' if found.bearer else key, **dict(found.extra)}
    opener = keyed_opener(found.url)
    outcome = None
    try:
        with opener.open(urllib.request.Request(found.url, headers=headers), timeout=timeout) as answer:
            outcome = answer.status
    except urllib.error.HTTPError as error:
        outcome = error.code
        error.close()
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
    if 200 <= answer < 300:
        return True, 'key works' if conclusive else 'answers with this key; it may not need one'
    if answer in (401, 403):
        return False, 'key refused by the provider'
    if 300 <= answer < 400:
        return False, 'the provider answered with a redirect, which is not followed with a key'
    return False, f'could not prove the key: the provider answered {answer}'
