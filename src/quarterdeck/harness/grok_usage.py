"""Grok's own ACP billing reader; no provider HTTP client or token handling."""

from __future__ import annotations

import json
import math
import os
import selectors
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .json_record import when
from .limits import Usage, Window


def decode(result: object, at: datetime) -> Usage:
    """Only an explicit weekly meter establishes a window; absence is unknown."""
    config = result.get("config") if isinstance(result, dict) else None
    tier = result.get("subscription_tier") if isinstance(result, dict) else None
    # The tier is a live billing fact apart from the weekly window; its value, a plan name, is never named or shown.
    diagnostics = (("native subscription tier reported",) if isinstance(tier, str) and tier.strip()
                   else ("native subscription tier unavailable",))
    period = config.get("currentPeriod") if isinstance(config, dict) else None
    if not isinstance(period, dict) or period.get("type") != "USAGE_PERIOD_TYPE_WEEKLY":
        return Usage((), at, "billing did not report a recognized weekly period", diagnostics=diagnostics)
    start, end = when(period.get("start")), when(period.get("end"))
    if start is None or end is None or end <= start:
        return Usage((Window("7d", None, end),), at, "billing weekly period is incomplete", diagnostics=diagnostics)
    percent = config.get("creditUsagePercent")
    try:
        valid = (type(percent) in (int, float) and math.isfinite(percent) and percent >= 0)
    except OverflowError:
        valid = False
    error = None if valid else "billing percentage absent or unreadable"
    return Usage((Window("7d", percent / 100 if valid else None, end, error=error),), at, error,
                 diagnostics=diagnostics)


def billing(program: str, home: Path, env: dict[str, str], timeout: float = 20) -> Usage:
    """Initialize, then read billing without opening a session, within one deadline.

    The caller supplies an owned home linked through the ordinary credential machinery.
    The process group is private; even a child holding stdout cannot outlive the deadline.
    Native errors are summarized without retaining account data in a public reading.
    """
    at = datetime.now(timezone.utc)
    deadline = time.monotonic() + timeout
    proc = None
    try:
        proc = subprocess.Popen(
            [program, "agent", "--no-leader", "stdio"], cwd=home,
            env={**os.environ, **env, "GROK_HOME": str(home), "GROK_DISABLE_AUTOUPDATER": "1"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        requests = (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": 1, "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}},
            {"jsonrpc": "2.0", "id": 2, "method": "_x.ai/billing", "params": {}},
        )
        pending, buffered = 0, b""
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            proc.stdin.write((json.dumps(requests[0]) + "\n").encode())
            proc.stdin.flush()
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                buffered += chunk
                if len(buffered) > 1024 * 1024:
                    return Usage((), at, "billing protocol response exceeded its bound")
                while b"\n" in buffered:
                    line, buffered = buffered.split(b"\n", 1)
                    try:
                        reply = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(reply, dict) or type(reply.get("id")) is not int or reply["id"] != pending + 1:
                        continue
                    if "error" in reply:
                        return Usage((), at, "native billing protocol refused the reading")
                    if pending == 1:
                        return decode(reply.get("result"), at)
                    if not isinstance(reply.get("result"), dict):
                        return Usage((), at, "native billing initialization was unreadable")
                    pending = 1
                    proc.stdin.write((json.dumps(requests[1]) + "\n").encode())
                    proc.stdin.flush()
        return Usage((), at, "native billing deadline reached or child closed without a reply")
    except (OSError, ValueError):
        return Usage((), at, "native billing process unavailable")
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    stream.close()
