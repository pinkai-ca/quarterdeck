"""The Claude usage reader keeps the reason a read failed, reads every five minutes, and honours refusals.

Synthetic HTTP only: urlopen is replaced by scripted responses, and no provider is called.
The credential and the response bodies carry recognisable secrets that must never reach a
reading's error or the service log.
"""

from datetime import datetime, timedelta, timezone
import email.message
import io
import json
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from quarterdeck import quota, service as servicemod
from quarterdeck.harness import claude
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.service import Service
from quarterdeck.ship import Pool, Session, Ship

TOKEN = "synthetic-secret-token"
ACCOUNT = "account-holder@example.invalid"
GOOD = {"five_hour": None, "seven_day": {"utilization": 42.0, "resets_at": "2099-01-01T00:00:00+00:00"},
        "account": {"email": ACCOUNT}}


class Reply:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def refused(code: int, retry_after: str | None = None):
    headers = email.message.Message()
    headers["Set-Cookie"] = "session=" + TOKEN
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(claude.USAGE_URL, code, "synthetic", headers, io.BytesIO(json.dumps({"account": ACCOUNT}).encode()))


def ok(document=GOOD):
    return Reply(document if isinstance(document, bytes) else json.dumps(document).encode())


class Endpoint:
    """Scripted outcomes for urlopen: a Reply is returned, an exception is raised."""

    def __init__(self, monkeypatch, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0
        monkeypatch.setattr(urllib.request, "urlopen", self)

    def __call__(self, request, timeout=None):
        assert request.full_url == claude.USAGE_URL and timeout
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else ok()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def subscription(tmp_path, name="claude-primary") -> Pool:
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": TOKEN}}))
    return Pool(name, "claude", config_dir=folder)


def assert_no_secret(text: str) -> None:
    assert TOKEN not in text and ACCOUNT not in text and "session=" not in text, text


def test_a_good_read_gives_its_windows_and_no_error(tmp_path, monkeypatch):
    Endpoint(monkeypatch, ok())
    usage = claude.usage(subscription(tmp_path))
    assert usage.error is None and [(w.name, w.used) for w in usage.windows] == [("7d", 0.42)]


@pytest.mark.parametrize("outcome,reason,retry", [
    (refused(429, "120"), "HTTP 429, retry-after 120", 120),
    (refused(429), "HTTP 429", None),
    (refused(401), "HTTP 401", None),
    (refused(503, "Wed, 21 Oct 2015 07:28:00 GMT"), "HTTP 503, retry-after Wed, 21 Oct 2015 07:28:00 GMT", 0),
    (TimeoutError("timed out"), "timeout", None),
    (urllib.error.URLError(TimeoutError("timed out")), "timeout", None),
    (urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")), "network error: ConnectionRefusedError", None),
    (ConnectionResetError(104, "Connection reset by peer"), "network error: ConnectionResetError", None),
    (ok(b"<html>" + ACCOUNT.encode()), "bad JSON", None),
    (ok(b"[]"), "bad JSON: not an object", None),
    (ok({"seven_day": {"utilization": "most", "resets_at": None}}), "bad JSON: utilization not a number", None),
    (ok({"account": {"email": ACCOUNT}}), "no utilization in response", None),
], ids=["429-retry", "429", "401", "503-date", "timeout", "url-timeout", "refused", "reset", "html", "array", "shape", "empty"])
def test_a_failed_read_keeps_its_reason_and_no_secret(tmp_path, monkeypatch, outcome, reason, retry):
    Endpoint(monkeypatch, outcome)
    usage = claude.usage(subscription(tmp_path))
    assert usage is not None and usage.windows == () and usage.error == reason
    assert usage.retry_after == retry
    assert_no_secret(usage.error)


def test_a_missing_or_tokenless_credential_says_so_without_a_call(tmp_path, monkeypatch):
    endpoint = Endpoint(monkeypatch)
    pool = subscription(tmp_path)
    (pool.config_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {}}))
    assert claude.usage(pool).error == "no access token in the credential file"
    (pool.config_dir / ".credentials.json").unlink()
    assert claude.usage(pool).error == "credential file missing"
    assert endpoint.calls == 0


def test_claude_reads_each_pool_at_most_every_five_minutes():
    assert ClaudeHarness.usage_interval >= 300


@pytest.fixture
def ship_service(tmp_path, monkeypatch):
    pools = {name: subscription(tmp_path, name) for name in ("claude-primary", "claude-secondary")}
    ship = Ship(tmp_path, "example", "admiral", tmp_path, pools, {"crew": Session("crew", "crew")}, {})
    clock = SimpleNamespace(mono=1000.0, wall=datetime.now(timezone.utc))
    monkeypatch.setattr(quota, "time", SimpleNamespace(monotonic=lambda: clock.mono))
    monkeypatch.setattr(servicemod.post, "now", lambda: clock.wall)
    svc = Service(ship, SimpleNamespace())
    yield ship, svc, clock
    svc.ledger.close()


def usage_log(svc) -> list[dict]:
    if not svc.log_path.exists():
        return []
    return [line for line in map(json.loads, svc.log_path.read_text().splitlines()) if line["event"] == "usage"]


def advance(clock, seconds):
    clock.mono += seconds
    clock.wall += timedelta(seconds=seconds)


def test_a_live_seats_pool_is_read_every_five_minutes_and_held_by_retry_after(ship_service, monkeypatch):
    ship, svc, clock = ship_service
    endpoint = Endpoint(monkeypatch, refused(429, "900"), ok(), refused(401), ok(), refused(429, "999999"))
    seat = SimpleNamespace(harness=ClaudeHarness(), pool=ship.pools["claude-primary"])
    launched = SimpleNamespace(pool="claude-primary")

    def tick(seconds=0):
        advance(clock, seconds)
        quota.refresh(svc, seat, True, launched)
        return endpoint.calls

    assert tick() == 1
    assert usage_log(svc)[-1] == {"at": usage_log(svc)[-1]["at"], "event": "usage", "pool": "claude-primary",
                                  "error": "HTTP 429, retry-after 900", "backoff": 900}
    assert "HTTP 429, retry-after 900" in svc.ledger.quota.usage_notes()[0]
    assert tick(300) == 1 and tick(599) == 1, "a refusal's Retry-After holds the pool past the interval"
    assert tick(1) == 2 and svc.ledger.quota.usage_notes() == [] and len(usage_log(svc)) == 1, "a good read logs nothing"
    assert tick(299) == 2 and tick(1) == 3, "the next read waits the five-minute interval"
    assert usage_log(svc)[-1]["error"] == "HTTP 401" and "backoff" not in usage_log(svc)[-1]
    assert tick(300) == 4 and tick(300) == 5
    assert usage_log(svc)[-1]["backoff"] == quota.BACKOFF_MAX, "a refusal's hold is bounded"
    assert tick(quota.BACKOFF_MAX - 1) == 5 and tick(1) == 6
    assert_no_secret(svc.log_path.read_text())


def test_a_destination_read_before_a_move_keeps_its_reason_and_honours_the_hold(ship_service, monkeypatch):
    ship, svc, clock = ship_service
    endpoint = Endpoint(monkeypatch, refused(429, "1200"), ok())
    seat = SimpleNamespace(name="worker", harness=SimpleNamespace(reads_usage=False))
    assert not svc.known_open(seat, "claude-secondary") and endpoint.calls == 1
    assert usage_log(svc)[-1]["error"] == "HTTP 429, retry-after 1200" and usage_log(svc)[-1]["backoff"] == 1200
    failover = [json.loads(line) for line in svc.log_path.read_text().splitlines() if '"failover"' in line]
    assert failover[-1]["skipped"] == "its usage could not be read: HTTP 429, retry-after 1200"
    advance(clock, 301)
    assert not svc.known_open(seat, "claude-secondary") and endpoint.calls == 1, "due by the interval, still held"
    advance(clock, 900)
    assert svc.known_open(seat, "claude-secondary") and endpoint.calls == 2
    advance(clock, 60)
    assert svc.known_open(seat, "claude-secondary") and endpoint.calls == 2, "a current reading is not read again"
    seat_on_it = SimpleNamespace(harness=ClaudeHarness(), pool=ship.pools["claude-secondary"])
    quota.refresh(svc, seat_on_it, True, SimpleNamespace(pool="claude-secondary"))
    assert endpoint.calls == 2, "a destination read counts toward the live seat's interval"
    assert_no_secret(svc.log_path.read_text())
