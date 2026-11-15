"""Where the files the service writes for a seat, and then trusts, are kept: a folder of each seat's own that no jail
sees, ``runtime/trusted/<seat>``.

A seat's own folder, ``runtime/seats/<seat>``, is bound writable into its jail: its home, carried/, its tmux folder
and the watch broker's request and receipts live there, and the jail may write any of them. What the service decides
from lives here instead: the launch record and its price, the pool selection, the record pointer and its registries,
the alarm journals, compaction.json, refusal.json, the holds on a relaunch, a retirement, a move and a sandbox that
did not end, the sweep's record of the stray sandboxes it ended, the launch script
tmux runs outside the jail, and the service's own readings and journals of the seat (model mismatch, turn failure,
person quiet, the human wake, qd press and qd type). Inside every
jail the runtime is an empty fence and this folder, like the locks, is a private empty tmpfs: no jail can read or
write it, so nothing a jail writes can make the service act, on its own seat or another.

Standard library only, and no import of the framework, so that every module can name a file here.
"""

from __future__ import annotations

from pathlib import Path

TRUSTED = 'trusted'

LAUNCH = 'launch.json'
PRICE = 'launch-price.json'
POOL = 'pool'
EXPLICIT = 'pool-explicit'  # beside ``pool``: a person chose that pool for the next launch, which takes it
RECORD = 'record'
READERS = 'record-readers.jsonl'
LAUNCHES = 'record-launches.jsonl'
COMPACTION = 'compaction.json'
REFUSAL = 'refusal.json'
ALARMS = ('human-alert.json', 'unconfirmed-delivery-alert.json', 'selection-alert.json', 'manual-fresh-alert.json',
          'write-bound-floor-alert.json', 'jail-sweep-alert.json')  # every journal of alarms.FILES
RETIRING = 'retiring.json'
REFUSED_RELAUNCH = 'relaunch-refused.json'  # a relaunch refused while the old program was ending, until up succeeds
MOVE_HOLD = 'move-hold.json'
JAIL_HELD = 'jail-held.json'  # a seat's old sandbox that did not end at its stop: the seat is held until it has
JAIL_STOPPING = 'jail-stopping.json'  # the seat's own stop is ending its sandbox now: the sweep leaves it to that stop
JAIL_SWEPT = 'jail-swept.json'  # the stray sandboxes of this seat the sweep ended, within the last day
LAUNCH_SCRIPT = 'launch.sh'  # what tmux runs for the seat's window, outside its jail
MODEL_MISMATCH = 'model-mismatch.json'
TURN_FAILURE = 'turn-failure.json'
PERSON_QUIET = 'person-quiet.json'
PERSON_QUIET_WAKE = 'person-quiet-wake.json'
HUMAN_WAKE = 'human-wake.json'
HUMAN_WAKE_STATUS = 'human-wake-status.json'
HUMAN_WAKE_HISTORY = 'human-wake-history'
ACTS = 'acts.jsonl'
ACTS_NOTICES = 'acts-notices.jsonl'

# Every file that moved here from the seat's own folder. The old copies are never read again: a seat folder that
# still holds one is migrated once (seat_migration), and a jail writing one there later changes nothing.
MOVED = (LAUNCH, PRICE, POOL, EXPLICIT, RECORD, READERS, LAUNCHES, COMPACTION, REFUSAL, *ALARMS, RETIRING,
         REFUSED_RELAUNCH, MOVE_HOLD, LAUNCH_SCRIPT, MODEL_MISMATCH, TURN_FAILURE, PERSON_QUIET, PERSON_QUIET_WAKE,
         HUMAN_WAKE, HUMAN_WAKE_STATUS, HUMAN_WAKE_HISTORY, ACTS, ACTS_NOTICES)


def root(runtime: Path) -> Path:
    """``runtime/trusted``: every seat's trusted folder, hidden inside every jail."""
    return Path(runtime) / TRUSTED


def folder(runtime: Path, seat: str) -> Path:
    """``runtime/trusted/<seat>``: the files the service writes for that seat and trusts."""
    return root(runtime) / seat


def path(runtime: Path, seat: str, name: str) -> Path:
    return folder(runtime, seat) / name


def of_seat_folder(seat_folder: Path) -> Path:
    """The trusted folder of the seat whose own folder (``runtime/seats/<seat>``) is given."""
    seat_folder = Path(seat_folder)
    return folder(seat_folder.parent.parent, seat_folder.name)


def of_home(home: Path) -> Path:
    """The trusted folder of the seat whose home (``runtime/seats/<seat>/home``) is given."""
    return of_seat_folder(Path(home).parent)


def make(runtime: Path, seat: str) -> Path:
    """The seat's trusted folder, made private (0700) with its parent; an existing one is kept as it is."""
    base = root(runtime)
    own = base / seat
    if own.is_dir():
        return own  # made already: nothing is written
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    own.mkdir(mode=0o700, exist_ok=True)
    return own

