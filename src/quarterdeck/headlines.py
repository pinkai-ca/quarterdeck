"""Operational status-line text with category counts when alert names exceed the budget."""

WIDTH = 160
SEPARATOR = " · "


def status_line(facts, alerts: list[str]) -> str:
    """Preserve every operational category when the detailed alert text overflows."""
    text = SEPARATOR.join(alerts)
    if len(text) <= WIDTH:
        return text
    parts = ["SERVICE DOWN" if facts.service == "down" else f"service {facts.service}"]
    counts = [(facts.waiting, "waiting"), (facts.queued, "queued"), (facts.blind, "blind"),
              (len(facts.needs_human), "need human"), (len(facts.unarmed), "unarmed"),
              (len(facts.held), "held"), (len(facts.pending), "pending"),
              (len(facts.disks), "disk alerts"), (len(facts.login), "login needed"),
              (len(facts.routes), "on fallback"), (len(facts.unknown), "selection unknown"),
              (len(facts.queue_unknown), "queue unknown"), (len(facts.compactions), "compaction readings"),
              (len(facts.turn_errors), "turn errors"), (facts.unsettled, "unsettled after an error"),
              (len(facts.group_held), "groups held"), (len(facts.cascade_held), "cascades held"),
              (len(facts.structure_waiting), "structure waits")]
    parts += [f"{count} {label}" for count, label in counts if count]
    # A manual-fresh reading with nobody to wake must keep its seat/readiness,
    # even when that operational detail takes the line beyond the normal budget.
    parts += getattr(facts, 'manual_fresh', ())
    parts.append("details: qd status")
    return SEPARATOR.join(parts)
