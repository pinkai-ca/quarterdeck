"""Claude sign-in evidence for pool status and subscription usage."""

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path


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
