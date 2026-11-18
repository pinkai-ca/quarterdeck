"""Operational status-line text with category counts when alert names exceed the budget."""

from unicodedata import east_asian_width

WIDTH = 160
SEPARATOR = " · "


def _columns(text: str) -> int:
    """Budget conservatively for wide characters and combining sequences."""
    return sum(2 if east_asian_width(char) in ('W', 'F') else 1 for char in text)


def status_line(facts, alerts: list[str]) -> str:
    """Preserve every operational category when the detailed alert text overflows."""
    text = SEPARATOR.join(alerts)
    if _columns(text) > WIDTH:
        text = _abbreviated_status(facts)
    notices = getattr(facts, 'operational_notices', ())
    if not notices:
        return text
    room = WIDTH - _columns(text) - _columns(SEPARATOR)
    notice_text = SEPARATOR.join(notices)
    if _columns(notice_text) > room:
        notice_text = '1 tool notice' if len(notices) == 1 else f'{len(notices)} tool notices'
    return text + SEPARATOR + notice_text if _columns(notice_text) <= room else text


def _abbreviated_status(facts) -> str:
    parts = ["SERVICE DOWN" if facts.service == "down" else f"service {facts.service}"]
    if retirement := getattr(facts, 'retirement', None):
        parts.append(retirement)
    parts += getattr(facts, 'ledger_recovery', ())  # preserve the evidence path on overflow
    parts += [line for line in getattr(facts, 'server_recovery', ()) if line.startswith('service stop barrier')]
    if accounting := getattr(facts, 'accounting', None):
        parts.append(accounting)  # keep its time and safe cause even when other alerts overflow
    if startup_delivery := getattr(facts, 'startup_delivery', None):
        parts.append(startup_delivery)
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
    # A held move must keep its seat and missing dimension visible even on overflow.
    parts += getattr(facts, 'move_holds', ())
    # A seat down because its program is still ending, or its relaunch was refused for that, keeps its name.
    parts += getattr(facts, 'ending', ())
    # A crew qd raise started keeps its ship and the desk's order that clears it.
    parts += getattr(facts, 'raises', ())
    # A seat stopped at its write bound keeps its name and the word that lifts it.
    parts += getattr(facts, 'write_bounds', ())
    # A ship-folder git that has not answered keeps its words: every rule change is told to its editor too.
    parts += getattr(facts, 'brief_git', ())
    # A stray sandbox the sweep ended keeps its seat, its pid and its time for the day.
    parts += getattr(facts, 'jail_sweeps', ())
    # A temporary seat that outlived its run keeps its name and its cure until it is gone.
    parts += getattr(facts, 'leftover_seats', ())
    # A sign-in failure must still name its seat and failed cure when categories overflow.
    parts += getattr(facts, 'sign_in_failures', ())
    # A short pane's delivery obstruction must retain its seat and remedy on overflow.
    parts += getattr(facts, 'pane_obstructions', ())
    parts += getattr(facts, 'seat_holds', ())
    # A link that stops the post must keep the path it names.
    parts += getattr(facts, 'post_held', ())
    parts += getattr(facts, 'post_waits', ())
    parts += getattr(facts, 'pool_forecasts', ())
    # A held delivery's available cure must survive the abbreviated status line.
    parts += [line for line in facts.pending if '; qd submit ' in line]
    parts += getattr(facts, 'request_waits', ())
    if getattr(facts, 'request_waits', ()):
        # New request details must not push the original operational words out.
        parts += [line for line in facts.pending if line.startswith('request claimed: ')]
        parts += facts.unknown
    # A ship still on tmux's default server can be stopped by any bare kill-server: kept in short.
    if getattr(facts, 'cutover', ''):
        parts.append("tmux: default server, qd down then qd up")
    # Check failures keep their program, time and safe reason even in the overflow form.
    parts += facts.check_failed
    parts += facts.check_unknown
    parts += getattr(facts, 'model_mismatches', ())  # a no-systems mismatch must retain its seat and difference
    parts.append("details: qd status")
    return SEPARATOR.join(parts)
