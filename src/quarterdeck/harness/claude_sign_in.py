"""Claude sign-in evidence for pool status and subscription usage."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path

from .limits import Usage


@dataclass(frozen=True)
class ExpiredAccess(Usage):
    """A local usage preflight that matched a specific renewable access generation."""

    identity: tuple[str, str] | None = None


@dataclass(frozen=True, repr=False)
class _Snapshot:
    """One file generation for both of the adapter's path-based readers."""

    text: str
    info: os.stat_result

    def read_text(self) -> str:
        return self.text

    def stat(self) -> os.stat_result:
        return self.info


@dataclass(frozen=True)
class SignIn:
    access_expiry: float
    refresh_usable: bool
    grant_end: datetime | None
    has_access: bool

    def access_expired(self, now: datetime) -> bool:
        return self.access_expiry <= now.timestamp()

    def can_renew(self, now: datetime) -> bool:
        return self.refresh_usable and (self.grant_end is None or self.grant_end > now)


def observe(adapter, source: Path) -> SignIn | None:
    """Read once and return evidence only when the adapter can read the deadlines."""
    if getattr(adapter, "name", None) != "claude":
        return None
    try:
        with source.open() as file:
            snapshot = _Snapshot(file.read(), os.fstat(file.fileno()))
    except (OSError, ValueError):
        return None
    metadata = adapter.sign_in_metadata(snapshot)
    if metadata is None:
        return None
    expiry, _, end = metadata
    refresh_usable = adapter.freshness(snapshot) is not None
    # Match the ordinary usage reader's test for an access token.
    has_access = bool(json.loads(snapshot.read_text())["claudeAiOauth"].get("accessToken"))
    return SignIn(expiry, refresh_usable, end, has_access)


def subscription(pool, program=None) -> bool:
    from . import native_name

    return (pool.kind == "subscription" and pool.config_dir is not None
            and native_name(pool.provider) == "claude" and program in (None, "claude"))


def on_another_program(launch) -> bool:
    """A launch that recorded another program never ran on a Claude subscription; one that recorded none may have."""
    from .claude import ClaudeHarness

    return getattr(launch, "harness", None) not in (None, ClaudeHarness.name)


def location(pool):
    """The same subscription through two declarations shares one canonical credential location."""
    from . import native_for

    if not subscription(pool):
        return None
    reader = native_for(pool.provider)
    return (pool.config_dir / reader.credential_file).resolve()


def renewable_identity(adapter, source: Path, sign_in: SignIn, now: datetime):
    if (not sign_in.can_renew(now) or not sign_in.access_expired(now)
            or adapter.renewal_failed(source) is True):
        return None
    account = hashlib.sha256(os.fsencode(source)).hexdigest()
    expiry = float(sign_in.access_expiry) or 0.0  # both signs of zero name the same expired generation
    return account, expiry.hex()
