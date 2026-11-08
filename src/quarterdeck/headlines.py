"""Compact cached headlines; full readings and alert details remain in status/JSON."""

from datetime import timezone

from .usage_cache import parse

WIDTH = 160
SEPARATOR = " · "


def compact(item: dict) -> str:
    row = item["reading"]
    if row is None:
        return f"{item['pool']} {item.get('window', '')} unknown reset ?" if item["pool"] else item["reason"]
    label = f"{row.pool} {row.window}"
    if not row.complete:
        return f"{label} unknown reset ?"
    reset = parse(row.resets_at).astimezone(timezone.utc).strftime("%m-%dT%H:%MZ")
    value = f"{row.used:.0%}"
    if not item["current"]:
        age = int(item["age_seconds"] // 60)
        value += f" LAST KNOWN {age}m"
    return f"{label} {value} reset {reset}"


def status_line(facts, alerts: list[str]) -> str:
    """Never trade an alert for a headline. Overflow is named, not silently clipped."""
    parts = list(alerts)
    usage = [compact(item) for item in facts.usage]
    if not usage or len(SEPARATOR.join(parts + usage)) <= WIDTH:
        return SEPARATOR.join(parts + usage)
    # Long names/reasons or many simultaneous alerts: retain every affected category
    # as an explicit count and direct the reader to the complete status reading.
    if len(SEPARATOR.join(parts + [f"{len(usage)} usage: qd status"])) > WIDTH:
        parts = ["SERVICE DOWN" if facts.service == "down" else f"service {facts.service}"]
        counts = [(facts.waiting, "waiting"), (facts.queued, "queued"), (facts.blind, "blind"),
                  (len(facts.needs_human), "need human"), (len(facts.unarmed), "unarmed"),
                  (len(facts.walls), "walls"), (len(facts.pending), "pending"),
                  (len(facts.disks), "disk alerts"), (len(facts.login), "login needed")]
        parts += [f"{count} {label}" for count, label in counts if count]
        parts.append("details: qd status")
    suffix = "" if "details: qd status" in parts else ": qd status"
    for index, value in enumerate(usage):
        remaining = len(usage) - index - 1
        tail = [f"+{remaining} usage{suffix}"] if remaining else []
        if len(SEPARATOR.join(parts + [value] + tail)) > WIDTH:
            parts.append(f"+{remaining + 1} usage{suffix}")
            break
        parts.append(value)
    return SEPARATOR.join(parts)
