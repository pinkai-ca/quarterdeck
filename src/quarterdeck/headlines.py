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
    def priority(item: dict) -> int:
        row = item["reading"]
        if row is None or not row.complete:
            return 2
        return 0 if item["current"] else 1

    # Sorting is stable and local to the overflowing text, never the source reading.
    usage = [compact(item) for item in sorted(facts.usage, key=priority)]
    # Long names/reasons or many simultaneous alerts: retain every affected category
    # as an explicit count and direct the reader to the complete status reading.
    if len(SEPARATOR.join(parts + [f"+{len(usage)} usage: qd status"])) > WIDTH:
        parts = ["SERVICE DOWN" if facts.service == "down" else f"service {facts.service}"]
        counts = [(facts.waiting, "waiting"), (facts.queued, "queued"), (facts.blind, "blind"),
                  (len(facts.needs_human), "need human"), (len(facts.unarmed), "unarmed"),
                  (len(facts.walls), "walls"), (len(facts.pending), "pending"),
                  (len(facts.disks), "disk alerts"), (len(facts.login), "login needed"),
                  (len(facts.routes), "on fallback")]
        counts.append((len(facts.unknown), 'selection unknown'))
        parts += [f"{count} {label}" for count, label in counts if count]
        parts.append("details: qd status")
    suffix = "" if "details: qd status" in parts else ": qd status"
    shown = 0
    for value in usage:
        remaining = len(usage) - shown - 1
        tail = [f"+{remaining} usage{suffix}"] if remaining else []
        if len(SEPARATOR.join(parts + [value] + tail)) > WIDTH:
            continue  # an oversized name must not hide a later fitting reading
        parts.append(value)
        shown += 1
    if shown < len(usage):
        parts.append(f"+{len(usage) - shown} usage{suffix}")
    return SEPARATOR.join(parts)
