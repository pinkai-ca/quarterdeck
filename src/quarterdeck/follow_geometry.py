"""Temporary follow-view geometry, serialized with the ship's delivery and lifecycle."""

from contextlib import nullcontext
from dataclasses import asdict, dataclass
import json
import os
import secrets
import stat
import time

from . import durable, human_wake, launch_opening, lifecycle, pending, post, record_owner, ship_locks, shutdown
from .harness.capabilities import follow_minimum_size
from .seat import Seat, read_seat
from .ship import ShipError, load
from .tmux import Tmux, TmuxError, WindowGeometry

COMMAND_TIMEOUT = 1.0
DISPOSABLE = '.qd-follow-disposable.json'
UNCERTAIN = frozenset((None, 'typed', 'blind', 'queued', 'pending', 'uncertain'))
CHECK_SECONDS = 0.5
PASS_SECONDS = 0.05
MAX_INTENT = 256 * 1024


def _private_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('disposable parent is not a private directory')
    return [info.st_dev, info.st_ino]


def _disposable_binding(ship, tmux):
    if tmux.legacy or not tmux.socket or ship.is_repository:
        raise ValueError('server is not a disposable ship endpoint')
    info = ship.folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('disposable ship folder is not owned')
    return dict(version=1, parent=_private_directory(ship.folder.parent),
                ship=[info.st_dev, info.st_ino], socket=tmux.socket)


def mark_disposable(ship, tmux):
    """Called by the drill factories as part of creating their new ship and endpoint."""
    target = ship.folder.parent / DISPOSABLE
    durable.replace_file(target, json.dumps(_disposable_binding(ship, tmux)) + '\n')


def disposable(ship, tmux) -> bool:
    try:
        known = shutdown._read(ship.folder.parent / DISPOSABLE, 4096)
        return known is not None and known[0] == _disposable_binding(ship, tmux)
    except (OSError, ValueError, TmuxError):
        return False


def journal_path(runtime):
    return ship_locks.path(runtime, 'follow', 'geometry.json')


def intent_path(runtime):
    return ship_locks.path(runtime, 'follow', 'intent.json')


def _closed_path(runtime):
    return ship_locks.path(runtime, 'follow', 'closed.json')


def lease(runtime):
    return ship_locks.held(ship_locks.path(runtime, 'follow.lock'), 'follow view', wait=0)


def _token(value):
    return isinstance(value, str) and len(value) == 32 and all(ch in '0123456789abcdef' for ch in value)


@dataclass(frozen=True)
class Intent:
    token: str
    pid: int
    start: str
    closing: bool
    targets: dict

    @classmethod
    def read(cls, runtime):
        path = intent_path(runtime)
        if not os.path.lexists(path):
            return None
        try:
            data, _ = shutdown._read(path, MAX_INTENT)
            intent = cls(**data)
            if (not _token(intent.token) or type(intent.pid) is not int or intent.pid <= 1
                    or not isinstance(intent.start, str) or not intent.start.isascii() or not intent.start.isdigit()
                    or type(intent.closing) is not bool or not isinstance(intent.targets, dict)
                    or intent.closing and intent.targets):
                raise ValueError
            for name, values in intent.targets.items():
                (pane, pid, at), width, height = values
                if (not isinstance(name, str) or not isinstance(pane, str) or not pane.startswith('%')
                        or type(pid) is not int or pid <= 0 or not isinstance(at, str) or not at
                        or type(width) is not int or not 1 <= width <= 65535
                        or height is not None and (type(height) is not int or not 1 <= height <= 65535)):
                    raise ValueError
            return intent
        except (OSError, ValueError, TypeError, KeyError):
            if not os.path.lexists(path):
                return None
            raise ShipError('follow geometry intent is unreadable; no window resized') from None


class ViewerIntent:
    """An exclusive disposable viewer publishes intent, never a seat claim or a tmux resize."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.intent = None
        self.token = secrets.token_hex(16)
        self.start = shutdown.process_start(os.getpid())
        if self.start is None:
            raise ShipError('could not identify the follow viewer process')
        ship_locks.prepare(intent_path(runtime))

    def update(self, targets, *, closing=False):
        intent = Intent(self.token, os.getpid(), self.start, closing, targets)
        if not closing and not targets and self.intent is None:
            return
        if intent != self.intent:
            durable.replace_file(intent_path(self.runtime), json.dumps(asdict(intent)) + '\n')
            self.intent = intent

    def finish(self, timeout):
        if self.intent is None:
            return True
        self.update({}, closing=True)
        deadline = time.monotonic() + timeout
        while True:
            done = shutdown._read(_closed_path(self.runtime), 4096)
            if done is not None and done[0] == self.token:
                intent_path(self.runtime).unlink(missing_ok=True)
                _closed_path(self.runtime).unlink(missing_ok=True)
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _delivery_clear(seat, launch, attempts):
    """Do not disturb the geometry an unresolved paste/receipt was read against."""
    if getattr(seat.harness, 'requires_deferred_opening', False) and launch_opening.hold(seat):
        return False
    if seat.session.human:
        wake = human_wake.load(seat)
        if (wake is not None and not human_wake.changed_launch(seat, wake)
                and wake.data['phase'] not in ('prepared', 'cancelled', 'delivered')):
            return False
    deadline = time.monotonic() + CHECK_SECONDS

    def check_budget():
        if time.monotonic() >= deadline:
            raise lifecycle.Busy('follow delivery evidence read exceeded its budget')

    # Read current paste evidence within the claim. Attempts made after the
    # earlier inventory must publish this journal before any paste can begin.
    proofs, errors = pending._journals(seat.ship.runtime, seat.name, progress=check_budget)
    if errors:
        return False
    identity = launch.pane, launch.pid, launch.at
    accepted_queue = set()
    for proof in proofs:
        bind = proof.data['identity']
        if (bind['pane'], bind['pid'], bind['launch']) != identity:
            continue
        phase = proof.data['phase']
        recovering = proof.data.get('recovery', {}).get('mode', 'done') != 'done'
        if (phase == 'queued' and not proof.data.get('screen_queue')
                and not proof.data.get('paste_clear') and not recovering):
            accepted_queue.add(proof.data.get('message'))
        elif (phase not in pending.TERMINAL | {'prepared'}
                or phase == 'uncertain' or pending.holds_screen_queue(proof.data)
                or proof.data.get('paste_clear') and phase != 'withheld'
                or recovering):
            return False
    latest = attempts.get((seat.name, *identity), {})
    return not any(result in UNCERTAIN and not (result == 'queued' and message in accepted_queue)
                   for message, result in latest.items())


@dataclass(frozen=True)
class Original:
    geometry: WindowGeometry
    launch: str
    server_start: str
    fit: bool
    socket: str


class Resize:
    """Only a holder of the view lease may change or replay the geometry journal."""

    def __init__(self, ship, tmux, *, attempts=None):
        self.ship, self.tmux = ship, tmux
        self.attempts = attempts if attempts is not None else post.Attempts(ship.runtime)
        self.path = journal_path(ship.runtime)
        self.owner, self.original = self._read()
        self.applied = {}

    def _read(self):
        try:
            self.path.lstat()
        except FileNotFoundError:
            return None, {}
        try:
            data, _ = shutdown._read(self.path, 256 * 1024)
            owner = data['owner']
            if (data['version'] != 1 or not isinstance(data['seats'], dict)
                    or owner is not None and not _token(owner)):
                raise ValueError
            originals = {}
            for name, row in data['seats'].items():
                geometry = WindowGeometry(**row['geometry'])
                original = Original(geometry, row['launch'], row['server_start'], row['fit'], row['socket'])
                if (not all(isinstance(value, str) and value for value in
                            (original.launch, original.server_start, original.socket))
                        or type(original.fit) is not bool or not os.path.isabs(original.socket)
                        or type(geometry.server) is not int or geometry.server <= 1
                        or any(type(value) is not int or value <= 0
                               for value in (geometry.width, geometry.height, geometry.pid))):
                    raise ValueError
                originals[name] = original
            return owner, originals
        except (OSError, ValueError, TypeError, KeyError):
            raise ShipError('follow geometry journal is unreadable; retained without resizing') from None

    def _save(self, originals):
        if originals:
            ship_locks.prepare(self.path)
            durable.replace_file(self.path, json.dumps(dict(version=1, owner=self.owner, seats={
                name: asdict(value) for name, value in originals.items()})) + '\n')
        else:
            self.path.unlink(missing_ok=True)
        self.original = originals

    def _seat(self, name, fit):
        # Read the current declaration under the seat claim. A deleted/reclassified
        # crew window is never resized using the viewer's stale declaration.
        ship = load(self.ship.folder)
        spec = ship.seats.get(name)
        if spec is None or not disposable(ship, self.tmux) or (not fit and ship.session_of(spec).human):
            return None
        return read_seat(ship, spec, self.tmux)

    @staticmethod
    def _admission(seat):
        # Human wake holds admission around its fixed-line delivery.
        return post.admission(seat.ship.runtime, seat.name, wait=0) if seat.session.human else nullcontext()

    def _forget(self, name):
        self._save({key: entry for key, entry in self.original.items() if key != name})
        self.applied.pop(name, None)

    def _restore(self, name, original, attempts):
        geom = original.geometry
        if shutdown.process_gone(dict(pid=geom.server, start=original.server_start)):
            self._forget(name)
            return
        if original.socket != self.tmux.socket:
            return  # an earlier server still owns this exact unresolved restoration
        pane = self.tmux.pane(geom.pane)
        if pane is None or pane.pid != geom.pid:
            self._forget(name)
            return
        seat = self._seat(name, original.fit)
        if not isinstance(seat, Seat) or not self._owns_session(seat, pane):
            return
        with self._admission(seat):
            launch = seat.launched()
            if (launch is None or (launch.pane, launch.pid, launch.at) != (geom.pane, geom.pid, original.launch)
                    or not pane.dead and (attempts is None or not _delivery_clear(seat, launch, attempts))):
                return
            current = self.tmux.window_geometry(pane, restoring=True)
            if current is None or current.binding != geom.binding:
                return
            height = geom.height if original.fit else None
            if self.tmux.resize_owned_window(geom, geom.width, height, restore=True):
                self._forget(name)

    def _owns_session(self, seat, pane):
        return (pane.session == seat.session.title
                and self.tmux.session_environment(pane.session, 'QD_SHIP', strict=True) == str(seat.ship.folder))

    def _attempt_inventory(self, deadline=None):
        found = {}

        def progress():
            if deadline is not None and time.monotonic() >= deadline:
                raise lifecycle.Busy('follow receipt inventory exceeded its service budget')

        try:
            for attempt in self.attempts.all(progress=progress):
                identity = attempt.seat, attempt.pane, attempt.pid, attempt.launched_at
                found.setdefault(identity, {})[attempt.message] = attempt.result
        except (ShipError, OSError, ValueError, TypeError, KeyError):
            return None  # an unreadable receipt cannot excuse changing its geometry
        return found

    def restore(self, *, deadline=None, first=0):
        if not self.original:
            return ()
        attempts = self._attempt_inventory(deadline)
        ordered = tuple(self.original.items())
        offset = first % len(ordered)
        for index, (name, original) in enumerate(ordered[offset:] + ordered[:offset]):
            if deadline is not None and index and time.monotonic() >= deadline:
                break
            try:
                with lifecycle.claim(self.ship.runtime, name, wait=0):
                    self._restore(name, original, attempts)
            except (ShipError, TmuxError, OSError, ValueError, TypeError, KeyError):
                continue  # the complete original remains for the next bounded pass
        return tuple(self.original)

    def apply(self, targets, *, expected=None, deadline=None):
        """Resize at most once per live launch in normal mode. Fit can follow strip heights."""
        if not targets or (attempts := self._attempt_inventory(deadline)) is None:
            return
        for index, (name, (width, height)) in enumerate(targets.items()):
            if deadline is not None and index and time.monotonic() >= deadline:
                break
            try:
                with lifecycle.claim(self.ship.runtime, name, wait=0):
                    self._apply(name, width, height, attempts, (expected or {}).get(name))
            except (ShipError, TmuxError, OSError, ValueError, TypeError, KeyError):
                continue  # uncertainty leaves the screen cropped and its original geometry recoverable

    def _apply(self, name, width, height, attempts, expected):
        fit = height is not None
        seat = self._seat(name, fit)
        if not isinstance(seat, Seat):
            return
        launch = seat.launched()
        if launch is None:
            return
        wanted = (launch.pane, launch.pid, launch.at), width, height
        if expected is not None and wanted[0] != expected or self.applied.get(name) == wanted:
            return
        if minimum := follow_minimum_size(record_owner.launch_reader(launch)):
            # Keep the requested strip size in wanted. Only the native window gets the floor.
            min_width, min_height = minimum
            width = max(width, min_width)
            if fit:
                height = max(height, min_height)
        if (previous := self.original.get(name)) and previous.launch != launch.at:
            self._restore(name, previous, attempts)
            if name in self.original:
                return
        with self._admission(seat):
            pane = seat.pane()
            if pane is None or pane.dead or not self._owns_session(seat, pane) or seat.launched() != launch:
                return
            geom = self.tmux.window_geometry(pane)
            if geom is None:
                return
            if (not fit and geom.width <= width) or (fit and (geom.width, geom.height) == (width, height)):
                self.applied[name] = wanted
                return
            if not _delivery_clear(seat, launch, attempts):
                return
            start = shutdown.process_start(geom.server)
            if start is None or seat.launched() != launch:
                return
            original = self.original.get(name)
            if original is None:
                original = Original(geom, launch.at, start, fit, self.tmux.socket)
                self._save({**self.original, name: original})
            if original.geometry.binding != geom.binding or original.server_start != start:
                return
            if self.tmux.resize_owned_window(geom, width, height):
                self.applied[name] = wanted


class ServiceWindows:
    """Geometry runs in the disposable's service, after it has handled input."""

    def __init__(self, runtime):
        self.directory = intent_path(runtime).parent
        self.resize = None
        self.first = 0

    def tick(self, ship, tmux, attempts):
        if not disposable(ship, tmux):
            return
        intent = Intent.read(ship.runtime)
        if intent is None and self.resize is None and not journal_path(ship.runtime).exists():
            return
        if self.resize is None:
            bounded = Tmux(tmux.socket, ship=tmux.ship, legacy=tmux.legacy, command_timeout=COMMAND_TIMEOUT)
            self.resize = Resize(ship, bounded, attempts=attempts)
        resize = self.resize
        if intent is not None:
            owner = dict(pid=intent.pid, start=intent.start)
            gone = shutdown.process_gone(owner)
            if not gone and shutdown.process_start(intent.pid) != intent.start:
                return  # an unproved owner supplies no resize or cleanup authority
        else:
            gone = False
        active = intent is not None and not (intent.closing or gone)
        deadline = time.monotonic() + PASS_SECONDS
        self.first += 1
        if resize.original and (not active or resize.owner != intent.token):
            if resize.restore(deadline=deadline, first=self.first):
                return
        if not active:
            if intent is not None:
                self._settled(ship.runtime, intent, gone)
            resize.owner = None
            resize.applied.clear()
            return
        if resize.owner != intent.token:
            resize.owner = intent.token
            resize.applied.clear()
        wanted = [(name, tuple(binding), width, height) for name, (binding, width, height) in intent.targets.items()
                  if name in ship.seats and resize.applied.get(name) != (tuple(binding), width, height)]
        if wanted:
            offset = self.first % len(wanted)
            ordered = wanted[offset:] + wanted[:offset]
            resize.apply({name: (width, height) for name, _, width, height in ordered},
                         expected={name: binding for name, binding, _, _ in ordered}, deadline=deadline)

    @staticmethod
    def _settled(runtime, intent, gone):
        if not gone:
            done = shutdown._read(_closed_path(runtime), 4096)
            if (done is None or done[0] != intent.token) and Intent.read(runtime) == intent:
                durable.replace_file(_closed_path(runtime), json.dumps(intent.token) + '\n')
            return
        try:
            with lease(runtime):
                if Intent.read(runtime) == intent:
                    intent_path(runtime).unlink(missing_ok=True)
                    _closed_path(runtime).unlink(missing_ok=True)
        except ship_locks.Busy:
            pass  # a successor may already be replacing the intent
