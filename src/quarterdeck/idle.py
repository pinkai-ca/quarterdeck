"""One ordinary live idle proof, independent of routing and explicit question recovery."""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import NamedTuple

from .harness.types import State


@dataclass(frozen=True)
class Hold:
    dimension: str
    reason: str


NO_RECORD = 'no record yet: nothing has run'


RECORD, RECORD_AND_SCREEN, RECORD_AND_WORD = 'record', 'record and screen', 'record and its own word'


class _Pair(NamedTuple):
    state: State | None
    hold: Hold | None


class Reading(_Pair):
    """The state and its hold, as a pair, and the witness that settled the tool state: the record alone, or
    the record and the screen where the screen was read second and said what the record could not."""

    def __new__(cls, state, hold, witness=RECORD):
        self = super().__new__(cls, state, hold)
        self.witness = witness
        return self

    @property
    def reason(self):
        return self.hold.reason if self.hold else NO_RECORD if self.state is None else 'known idle'


def never_started(seat):
    """An explicit no-record join plus the current owned live pane, never generic absence."""
    try:
        joined = seat.join()
        if joined.record is not None or joined.reason != 'no record yet':
            return False
        launched, pane = seat.launched(), seat.pane()
        latest = seat.join()
        return bool(launched is not None and pane is not None and not pane.dead
                    and (pane.id, pane.pid) == (launched.pane, launched.pid)
                    and latest.record is None and latest.reason == 'no record yet'
                    and seat.launched() == launched)
    except (OSError, AttributeError, TypeError, ValueError):
        return False


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


def check(state: State | None, seat=None, *, explicit=False) -> Hold | None:
    """Unknown is never empty. Only native, timed active-input completion supplies proof."""
    if state is None:
        return None if seat is not None and never_started(seat) else Hold('record', 'it has no joined record')
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


WATCH_ALONE = (('shell', 1),)


def named(names) -> str:
    """What a screen names in its background, by kind and count: labels and numbers only."""
    return ' and '.join(f"{count} {kind}{'' if count == 1 else 's'}" for kind, count in names)


def own_watch_call(seat, state: State) -> bool:
    """The record's own word for a single shell on the screen, where it cannot say the whole tool state: the most
    recently started call it still shows active is the exact own-watch command, and that call fits this seat's
    live registration. The process table proves a shape, never a command: a watch run inside a longer command
    shows the same shape while its watch stage runs, but its call is not the exact command, so it is a later
    active call that is no candidate, and the one shell is not proved. A call whose start the record does not
    give, or two calls started at the same latest moment, prove nothing either."""
    from . import inbox_watch
    tools = state.active_tools
    if not tools or any(tool.started is None for tool in tools):
        return False
    latest = max(tool.started for tool in tools)
    newest = [tool for tool in tools if tool.started == latest]
    return (len(newest) == 1 and newest[0].inbox_watch_started is not None
            and inbox_watch.current(seat, newest[0].inbox_watch_started))


def second(seat, state: State, hold: Hold, *, explicit=False, screen=None):
    """The screen, read second, where the record cannot say whether a tool is open (law: record-first).

    Returns the state, its hold and what the screen showed, in words, or None where it was not read. The screen
    settles the tool state only together with the record: the record shows the turn ended, nothing queued and its
    last input complete, and the door reads the composer at rest. The program's own bottom line then names what
    it runs in its background, in a form measured on the launched version. Nothing named is rest. One shell is
    rest only where it is this seat's registered watch: the record names the exact watch command as the last
    call it still shows active (own_watch_call), and the process table shows the registered watch alone in a
    shell of its own under the launched program. Anything else it names is work, held under its name. A screen
    that does not say settles nothing:
    the record's unknown stands, and the hold says what the screen lacked. A program version with no measured
    bottom line has no second witness in its screen, and the screen is not read for it.
    """
    if hold.dimension != 'tools-unknown' or state is None or state.pending_tools is not None:
        return state, hold, None
    rest = replace(state, pending_tools=(), active_tools=())
    if check(rest, explicit=explicit) is not None:
        return state, hold, None  # the record itself must show everything else at rest
    from . import inbox_watch
    from .door import Door
    from .tmux import TmuxError

    def unknown(lacked):
        return state, Hold('tools-unknown', f"{REASONS['tools-unknown']}: {lacked}"), lacked

    try:
        launched = seat.launched()
        version = getattr(launched, 'version', None)
        if version is None or version not in getattr(seat.harness, 'background_versions', ()):
            return state, hold, None
        if screen is None:
            screen = Door(seat).screen()
    except (OSError, TmuxError):
        # The capture goes through the tmux wrapper: a pane gone between the check and the capture, a server that
        # does not answer or a line it cannot parse is that wrapper's own error. It never leaves the proof and it
        # is never rest: the record's unknown stands.
        return unknown('its screen could not be read')
    if not at_rest(screen):
        return unknown('its screen is not the composer at rest')
    names = seat.harness.background(screen, version)
    if names is None:
        return unknown("its screen's bottom line is not a measured form")
    if names == ():
        return rest, None, 'nothing in the background'
    if names == WATCH_ALONE and own_watch_call(seat, state) and inbox_watch.alone(seat):
        return rest, None, 'only its inbox watch in the background'
    saw = f'{named(names)} in the background'
    if names == WATCH_ALONE:
        saw += ', not proved to be its inbox watch alone'
    return state, Hold('tools-pending', f'tools pending: its screen shows {saw}'), saw


def at_rest(screen) -> bool:
    """The door's own reading of a composer at rest: its box read and empty, and nothing else on the screen."""
    return not (screen is None or screen.box != '' or screen.working is not False or screen.queued
                or screen.unknown or screen.interruption or screen.starting or screen.fresh)


BOX = Hold('box', 'the box is not proved empty and idle')


def release(seat, state: State | None, hold: Hold | None, asked) -> Hold | None:
    """The seat's own word, where neither its record nor its screen settles what it has running.

    ``asked`` is when the seat itself, proved as its own request is proved, said that what it has running may
    be lost. Returns the hold that still stands, or None. Only a hold of the tool state is the seat's to lift,
    since only what it started is its own to give up. The word covers what runs at the end of the turn it was
    given in and no more: the record must show that turn ended, timed, spanning the moment of asking, with no
    input since, an error-free empty queue and the input's completion, and the door must read the composer at
    rest. A word given in an earlier turn than the one that last ended lifts nothing, and neither does one
    whose time is unknown.
    """
    if (hold is None or hold.dimension not in ('tools-unknown', 'tools-pending') or state is None
            or asked is None or asked.tzinfo is None):
        return hold
    if check(replace(state, pending_tools=(), active_tools=())) is not None:
        return hold  # the turn, the queue (an unresolved one too) and the completion are never the seat's to release
    ending = state.completed_turns[-1] if state.completed_turns else None
    if (ending is None or not ending.timed or ending.started is None or ending.ended is None
            or not ending.started <= asked <= ending.ended
            or state.last_input_at is None or state.last_input_at > asked):
        return hold  # the turn that last ended is not the one the word was given in
    from .door import Door
    from .tmux import TmuxError
    try:
        screen = Door(seat).screen()
    except (OSError, TmuxError):
        return BOX  # a capture that fails is never an empty box, and its error never leaves the proof
    return None if at_rest(screen) else BOX


def reading(seat, *, explicit=False, relaunch=False):
    """Read the shared proof; only relaunch may request a launch-scoped adapter view."""
    from . import move_hold
    binding = move_hold.generation(seat)
    state = None
    witness = RECORD
    try:
        record = seat.record()
        state = seat.harness.state(record) if record is not None else None
        if state is not None:
            state = execution(seat, state)
        published_hold = check(state, seat=seat if state is None else None, explicit=explicit)
        reader = getattr(seat.harness, 'state_for_relaunch', None)
        if (relaunch and state is not None and state.pending_tools is None and reader is not None
                and binding is not None and binding['harness'] == getattr(seat.harness, 'name', None)):
            launched_at = None
            try:
                parsed = datetime.fromisoformat(binding['at'])
                if parsed.tzinfo is not None:
                    launched_at = parsed
            except (TypeError, ValueError):
                pass
            state = execution(seat, reader(record, launched_at))
        hold = check(state, seat=seat if state is None else None, explicit=explicit)
        if hold is not None and hold.dimension == 'tools-unknown':
            # The screen says the same of the launch-scoped view and of the ordinary one: what runs now.
            state, hold, saw = second(seat, state, hold, explicit=explicit)
            if hold is None or hold.dimension == 'tools-pending':
                witness = RECORD_AND_SCREEN
                if published_hold is not None and published_hold.dimension == 'tools-unknown':
                    published_hold = hold
    except OSError:
        hold = published_hold = Hold('record', 'its record is unreadable')
    if move_hold.generation(seat) != binding:
        state = None
        hold = published_hold = Hold('identity', REASONS['identity'])
        witness = RECORD
    # A relaunch allowance must not clear the ordinary move/status hold.
    move_hold.publish(seat, binding, published_hold)
    return Reading(state, hold, witness)
