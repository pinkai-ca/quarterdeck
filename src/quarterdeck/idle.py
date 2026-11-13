"""One ordinary live idle proof, independent of routing and explicit question recovery."""

from dataclasses import dataclass

from .harness.types import State


@dataclass(frozen=True)
class Hold:
    dimension: str
    reason: str


REASONS = {
    'record': 'its record is unavailable',
    'identity': 'its launch or watch changed while reading',
    'turn-unknown': 'turn state unknown',
    'turn-open': 'a turn is open',
    'queue-unknown': 'queue state unknown',
    'queue-pending': 'input is queued',
    'tools-unknown': 'tool state unknown',
    'tools-pending': 'tools are pending',
    'completion': 'its last input has no recorded completion',
}


def tool_reason(state: State) -> str | None:
    """Tool detail is operational only; never render arguments, results or native identities."""
    if state.pending_tools == ():
        return None
    if state.pending_tools is None:
        return REASONS['tools-unknown']
    details = []
    observed = iter(state.active_tools)
    for name in state.pending_tools[:3]:
        # Tool names are bounded single-line labels, not source text or command arguments.
        label = ''.join(c for c in name if c.isascii() and (c.isalnum() or c in '._-'))[:64] or 'tool'
        item = next((item for item in observed if item.name == name), None)
        since = item.started.isoformat() if item is not None and item.started is not None else 'unknown'
        details.append(f'{label} since {since}')
    if len(state.pending_tools) > 3:
        details.append(f'{len(state.pending_tools) - 3} more')
    return 'tools pending: ' + '; '.join(details)


def check(state: State) -> Hold | None:
    """Unknown is never empty. Only native, timed active-input completion supplies proof."""
    if state.turn_open is not False:
        key = 'turn-open' if state.turn_open is True else 'turn-unknown'
        return Hold(key, REASONS[key])
    if state.queue_error or state.queued is None:
        return Hold('queue-unknown', REASONS['queue-unknown'])
    if state.queued:
        return Hold('queue-pending', REASONS['queue-pending'])
    if state.pending_tools != ():
        key = 'tools-unknown' if state.pending_tools is None else 'tools-pending'
        return Hold(key, tool_reason(state))
    if not any(turn.timed and turn.started is not None and turn.ended is not None
               and state.last_input_at is not None
               and turn.started <= state.last_input_at <= turn.ended
               for turn in state.completed_turns):
        return Hold('completion', REASONS['completion'])
    return None
