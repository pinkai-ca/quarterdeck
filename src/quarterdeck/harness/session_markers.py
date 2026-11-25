"""What each program sets in the environment of every command it runs, measured, for post_sender.

The markers are the sanctioned verdicts of work/captures/seat-session-markers.md (Spock, 25 Nov 2026, names only,
on the pinned versions: Claude Code 2.1.331, Codex 0.166.0, OpenCode 1.19.5 and pi 0.79.0), each a name the program
itself adds to every command observed; generic ones (AI_AGENT, AGENT) are left out. OpenCode and pi were measured on
one command each. The homes are the variable each adapter's launch sets to the seat's own home (its
``home_variable``, and OpenCode's config home beside it), and HOME, which a jail sets to the seat's home. Grok Build
has no entry until it is measured: its seat is found by its pane, or inside a jail by the jail.

Kept here, beside the adapters, and in no reader's stamp set (reader_stamp): no record reader needs it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Program:
    """The names that mark the program itself, and the names whose value is its home or config home."""

    markers: tuple[str, ...]
    homes: tuple[str, ...]


PROGRAMS: dict[str, Program] = {
    'claude': Program(markers=('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID'), homes=('CLAUDE_CONFIG_DIR', 'HOME')),
    'codex': Program(markers=('CODEX_SESSION_ID', 'CODEX_THREAD_ID'), homes=('CODEX_HOME', 'HOME')),
    'opencode': Program(markers=('OPENCODE',), homes=('XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'HOME')),
    'pi': Program(markers=('PI_CODING_AGENT',), homes=('PI_CODING_AGENT_DIR', 'HOME')),
}

# The variables a program sets, on every command it runs, to its own pane process's id, joined to a launch record by
# its start time like an ancestor. Claude Code's CLAUDE_PID is its own process, the pane process of a crew seat.
# OpenCode's OPENCODE_PID is not here: a hand's pane process is the relay, and the program runs in the jail's own
# process ids.
PROCESS_MARKERS = ('CLAUDE_PID',)
