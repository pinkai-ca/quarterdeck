"""Codex 0.166.0 token_count rate_limits: one snapshot, dated by its record event.

The slots are not durations: primary can be 7d with no secondary. limit_id names
an allowance, not an account; callers bind current records to saved launch pools.
"""

from datetime import datetime, timezone
import math
from pathlib import Path

from .json_record import entries
from .limits import Usage, Window

WINDOWS = {300: '5h', 10080: '7d'}


def timestamp(value) -> datetime | None:
    try:
        at = datetime.fromisoformat(value)
        return at if at.tzinfo is not None and at.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def unknown(reason: str, now: datetime) -> Usage:
    return Usage(tuple(Window(name, None, None, error=reason) for name in WINDOWS.values()), now, reason)


def snapshot(data: dict, at: datetime) -> Usage:
    windows = []
    for slot in ('primary', 'secondary'):
        value = data.get(slot)
        if not isinstance(value, dict):
            continue
        minutes = value.get('window_minutes')
        if type(minutes) is not int or minutes not in WINDOWS:
            continue
        used, reset = value.get('used_percent'), value.get('resets_at')
        error = None
        if type(used) not in (int, float) or not 0 <= used <= 100 or not math.isfinite(used):
            error = 'reported percentage unavailable or invalid'
        try:
            reset = datetime.fromtimestamp(reset, timezone.utc) if type(reset) is int else None
        except (ValueError, OverflowError, OSError):
            reset = None
        if reset is None or reset <= at:
            error = error or 'reported reset unavailable or not after observation'
        windows.append(Window(WINDOWS[minutes], used / 100 if not error else None, reset,
                              bucket='codex', error=error))
    for name in WINDOWS.values():
        if not any(w.name == name for w in windows):
            windows.append(Window(name, None, None, error='window absent or duration unrecognized in latest snapshot'))
    return Usage(tuple(windows), at)


def read(sources: list[tuple[Path | None, datetime]], now: datetime) -> Usage:
    """Newest eligible observation, never newest valid windows assembled across seats.

    Missing/unplaceable evidence cannot certify an older observation as current.
    Pre-launch entries and explicit other allowance buckets are not eligible.
    """
    latest = None
    candidates = []
    for path, launched_at in sources:
        if path is None:
            return unknown('joined usage record unavailable', now)
        try:
            record = entries(path)
        except OSError:
            return unknown('joined usage record unreadable', now)
        for entry in record:
            payload = entry.get('payload')
            if (entry.get('type') != 'event_msg' or not isinstance(payload, dict)
                    or payload.get('type') != 'token_count' or 'rate_limits' not in payload):
                continue
            at = timestamp(entry.get('timestamp'))
            if at is not None and at < launched_at:
                continue
            data = payload['rate_limits']
            if isinstance(data, dict) and isinstance(data.get('limit_id'), str) and data['limit_id'] != 'codex':
                continue
            if at is None or at > now:
                return unknown('usage observation time unavailable or in the future', now)
            if latest is None or at > latest:
                latest, candidates = at, [data]
            elif at == latest:
                candidates.append(data)
    if latest is None:
        return unknown('no post-launch Codex usage snapshot in joined records', now)
    if any(data != candidates[0] for data in candidates[1:]):
        return unknown('conflicting usage snapshots at the same observation time', now)
    data = candidates[0]
    if not isinstance(data, dict) or data.get('limit_id') != 'codex':
        return unknown('latest Codex usage snapshot unavailable or bucket unknown', now)
    return snapshot(data, latest)
