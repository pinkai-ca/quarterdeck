"""One ordinary live idle proof, independent of routing and explicit question recovery."""

from dataclasses import dataclass, replace

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
        if item is not None and item.inbox_watch_started is not None:
            label = 'inbox watch (registration unproved)'
        since = item.started.isoformat() if item is not None and item.started is not None else 'unknown'
        details.append(f'{label} since {since}')
    if len(state.pending_tools) > 3:
        details.append(f'{len(state.pending_tools) - 3} more')
    return 'tools pending: ' + '; '.join(details)


def execution(seat, state: State) -> State:
    """A registered own inbox watch is infrastructure, while all other work stays pending."""
    if state.pending_tools is None or not state.pending_tools:
        return state
    if tuple(tool.name for tool in state.active_tools) != state.pending_tools:
        return state
    candidates = [index for index, tool in enumerate(state.active_tools)
                  if tool.inbox_watch_started is not None]
    if len(candidates) != 1:
        return state
    from . import inbox_watch
    index = candidates[0]
    if not inbox_watch.current(seat, state.active_tools[index].inbox_watch_started):
        return state
    return replace(state,
                   pending_tools=state.pending_tools[:index] + state.pending_tools[index + 1:],
                   active_tools=state.active_tools[:index] + state.active_tools[index + 1:])


def check(state: State, seat=None, *, explicit=False) -> Hold | None:
    """Unknown is never empty. Only native, timed active-input completion supplies proof."""
    if seat is not None:
        state = execution(seat, state)
    if state.turn_open is not False:
        key = 'turn-open' if state.turn_open is True else 'turn-unknown'
        return Hold(key, REASONS[key])
    if state.queue_error and not explicit:
        return Hold('queue-unknown', f'queue unknown: {state.queue_error}')
    if state.queued is None:
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


def reading(seat, *, explicit=False):
    """Read and publish the seat-aware proof without requiring a service object."""
    from . import move_hold
    binding = move_hold.generation(seat)
    state = None
    try:
        record = seat.record()
        state = seat.harness.state(record) if record is not None else None
    except OSError:
        hold = Hold('record', 'its record is unreadable')
    else:
        if state is not None:
            state = execution(seat, state)
        hold = check(state, explicit=explicit) if state is not None else Hold('record', 'it has no joined record')
    if move_hold.generation(seat) != binding:
        state = None
        hold = Hold('identity', REASONS['identity'])
    move_hold.publish(seat, binding, hold)
    return state, hold
