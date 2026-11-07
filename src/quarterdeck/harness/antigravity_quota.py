"""Antigravity quota facts: the quota errors in an owned record, the banner on the screen, and the program's own
``/usage`` command, run as every launch runs the program: found on the PATH, proven by ``qd check``, never pinned."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .limits import Usage, Wall, Window
from .antigravity_wire import _bytes, _proto, _str

PROGRAM = "agy"
ARGV = ("-p", "/usage", "--output-format", "json")  # on 1.5.2 this answers without opening a conversation
BANNER = re.compile(r"Individual quota reached", re.I)
RESET = re.compile(r"Resets in\s+(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?(?=[.\s]|$)", re.I)
ERROR_ID = re.compile(r"Error ID\s*:?\s*([\w-]+)", re.I)


def banner(text: str) -> str | None:
    match = BANNER.search(text)
    if match is None:
        return None
    # Terminal wrapping changes whitespace, not event identity. Stop at UI boundaries
    # and cap the scan so unrelated transcript/input text cannot supply a distant reset.
    lines = []
    for line in text[match.start():match.start() + 2048].splitlines()[:6]:
        stripped = line.strip()
        if lines and (not stripped or stripped.startswith((">", "─", "━", "╭", "╰", "│"))):
            break
        lines.append(stripped)
        if ERROR_ID.search(" ".join(lines)):
            break
    return " ".join(lines)


def wall(text: str, key: str, at: datetime | None) -> Wall | None:
    text = banner(text)
    if text is None:
        return None
    match = RESET.search(text)
    seconds = sum(int(v or 0) * n for v, n in zip(match.groups(), (86400, 3600, 60, 1))) if match and any(match.groups()) else None
    try:
        reset = at + timedelta(seconds=seconds) if at is not None and seconds is not None else None
    except OverflowError:
        reset = None
    error_id = ERROR_ID.search(text)
    return Wall(key, at, reset, error_id=error_id.group(1) if error_id else None,
                signature=hashlib.sha256(text.encode()).hexdigest())


def record_wall(index: int, payload: dict, at: datetime | None) -> Wall | None:
    raw = _bytes(_proto(_bytes(payload, 24)), 3)  # CortexStepErrorMessage.error
    details = _proto(raw)  # CortexErrorDetails: user_error_message=1, full_error=3
    text = "\n".join(filter(None, (_str(details, 1), _str(details, 3))))
    return wall(text, f"step:{index}", at)


def _timestamp(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def unknown(reason: str, at: datetime) -> Usage:
    return Usage((Window("7d", None, None, group="Gemini 4 Argon", bucket=None, error=reason),), at, reason)


def parse_usage(doc, at: datetime) -> Usage:
    if not isinstance(doc, dict) or doc.get("status") != "SUCCESS":
        return unknown("usage command did not return SUCCESS", at)
    command = doc.get("command")
    if not isinstance(command, dict) or command.get("name") != "usage":
        return unknown("usage command result missing", at)
    counters = doc.get("usage")
    keys = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
    if (doc.get("conversation_id") != "" or type(doc.get("num_turns")) is not int or doc["num_turns"] != 0 or
            not isinstance(counters, dict) or any(type(counters.get(k)) is not int or counters[k] != 0 for k in keys)):
        return unknown("usage command unexpectedly reported a conversation or model work", at)
    data = command.get("data")
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return unknown("usage groups unavailable", at)
    windows, errors, identities = [], [], set()
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("name"), str) or not isinstance(group.get("buckets"), list):
            errors.append("malformed usage group")
            continue
        for bucket in group["buckets"]:
            if not isinstance(bucket, dict) or not isinstance(bucket.get("window"), str):
                errors.append("malformed usage bucket")
                continue
            scope, period = group["name"], bucket["window"]
            headline = scope == "Gemini 4 Argon" and period == "weekly"
            name = "7d" if headline else f"{scope} / {period}"
            fraction = bucket.get("remaining_fraction")
            valid = type(fraction) in (int, float) and 0 <= fraction <= 1 and math.isfinite(fraction)
            reset = _timestamp(bucket.get("reset_time"))
            if not valid or reset is None:
                errors.append(f"{name}: fraction or reset unavailable")
            if name in identities:
                errors.append(f"{name}: ambiguous duplicate bucket")
                windows = [replace(w, used=None, resets_at=None, error="ambiguous duplicate bucket") if w.name == name else w for w in windows]
                continue
            identities.add(name)
            windows.append(Window(name, 1 - fraction if valid else None, reset, headline, scope,
                                  bucket.get("id") if isinstance(bucket.get("id"), str) else None,
                                  bucket.get("name") if isinstance(bucket.get("name"), str) else None,
                                  None if valid and reset is not None else "fraction or reset unavailable"))
    if "7d" not in identities:
        windows.insert(0, Window("7d", None, None, group="Gemini 4 Argon", bucket=None, error="Gemini 4 Argon weekly unavailable"))
        errors.append("Gemini 4 Argon weekly unavailable")
    return Usage(tuple(windows), at, "; ".join(errors) or None)


def fetch_usage() -> Usage:
    """The program's own usage command, run with the program the PATH gives, as every launch runs it."""
    at = datetime.now(timezone.utc)
    program = shutil.which(PROGRAM)
    if program is None:
        return unknown(f"{PROGRAM} is not on the PATH qd runs with", at)
    try:
        result = subprocess.run([program, *ARGV], env=dict(os.environ, AGY_CLI_DISABLE_AUTO_UPDATE="true"),
                                capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        return unknown("usage command timed out", at)
    except OSError as error:
        return unknown(f"usage unavailable: {type(error).__name__}", at)
    if result.returncode:
        return unknown(f"usage command failed (exit {result.returncode})", at)
    try:
        return parse_usage(json.loads(result.stdout), at)
    except ValueError:
        return unknown("usage command printed no JSON", at)
