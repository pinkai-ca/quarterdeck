"""Decode Muse's native usage JSON; never request a service endpoint directly.

The no-allowance envelope is reported by the current installed command. Numeric
allowance decoding is a source-backed draft until subscribed evidence exists.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from .limits import Usage, Window


def reset(value) -> datetime | None:
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def bucket(name: str, value: object) -> Window:
    if not isinstance(value, dict):
        return Window(name, None, None, error="Muse allowance bucket is not an object")
    percent = value.get("used_percent")
    fraction = (percent / 100 if type(percent) in (int, float)
                and math.isfinite(percent) and 0 <= percent <= 100 else None)
    at = reset(value.get("resets_at"))
    return Window(name, fraction, at,
                  error=("Muse allowance percentage or reset unavailable" if fraction is None or at is None else None))


def decode(text: str, at: datetime | None = None) -> Usage:
    """Only present native values establish a fraction, period or reset.

    In particular no_allowance is unknown, never zero used or a quota wall.
    Unrecognized data never appears in the returned diagnostics.
    """
    at = at or datetime.now(timezone.utc)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return Usage((), at, "Muse usage returned unreadable JSON")
    if not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1:
        return Usage((), at, "Muse usage schema unavailable or unsupported")
    allowance = data.get("allowance")
    if allowance is None:
        reason = "no_allowance" if data.get("reason") == "no_allowance" else "allowance unavailable"
        return Usage((), at, f"Muse usage {reason}")
    if not isinstance(allowance, dict):
        return Usage((), at, "Muse allowance is not an object")
    windows = []
    if "window" in allowance:
        current = allowance["window"]
        minutes = current.get("window_duration_mins") if isinstance(current, dict) else None
        if type(minutes) in (int, float) and math.isfinite(minutes) and minutes > 0:
            name = f"{minutes:g}m"
            windows.append(bucket(name, current))
        else:
            windows.append(Window("window", None, None, error="Muse window duration unavailable"))
    if "weekly" in allowance:
        windows.append(bucket("weekly", allowance["weekly"]))
    if not windows:
        return Usage((), at, "Muse allowance shape unrecognized")
    errors = [window.error for window in windows if window.error]
    return Usage(tuple(windows), at, "; ".join(dict.fromkeys(errors)) or None)
