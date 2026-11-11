"""Measured usage windows and source-identified quota events, with explicit unknowns."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Window:
    name: str
    used: float | None
    resets_at: datetime | None
    blocks: bool = True
    group: str | None = None
    bucket: str | None = None
    bucket_name: str | None = None
    error: str | None = None
    exhausted: bool = False  # An explicit native wall, independent of the reported fraction.
    uncertainty: str | None = None  # A complete measurement with unresolved scope/period evidence.


@dataclass(frozen=True)
class Usage:
    windows: tuple[Window, ...]
    read_at: datetime
    error: str | None = None
    retry_after: float | None = None  # seconds a refusal asked to wait; held in memory, never stored
    called: bool = True  # False when the reader made no provider call (a record reader, no credential or token)
    diagnostics: tuple[str, ...] = ()  # Safe field-name/type observations, never native values.


@dataclass(frozen=True)
class Wall:
    key: str
    at: datetime | None
    resets_at: datetime | None
    reason: str = "individual quota reached"
    error_id: str | None = None
    signature: str = ""
