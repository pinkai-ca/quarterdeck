"""Follow selected sessions with record/post activity and read-only screen frames."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import select
import signal
import sys
from threading import Event, Thread
import time
import tty

from . import fleet_post, follow_geometry, follow_view, post, post_dir, post_index, record_follow, servicectl
from .seat import Seat, read_seat
from .ship import SeatSpec, ShipError
from .tmux import Tmux, TmuxError

FRAME = 1 / 30
SCREEN_INTERVAL = 0.12
RECORD_INTERVAL = 0.4
GEOMETRY_INTERVAL = 0.5
RESTORE_SECONDS = 5.0
MIN_FIT_HEIGHT = 14


def sessions(ship, names):
    """Default to every declared session. Accept the same names and titles as ordinary attach."""
    if not names:
        return tuple(ship.sessions.values())
    selected = {}
    for name in names:
        session = ship.sessions.get(name) or next((s for s in ship.sessions.values() if s.title == name), None)
        if session is None:
            choices = ', '.join(f'{key} ({value.title})' for key, value in ship.sessions.items())
            raise ShipError(f'no session {name!r}; this ship has {choices}')
        selected[session.name] = session
    return tuple(selected.values())


class Arrivals:
    """Watch the canonical store. Only newly published names cause message reads."""

    def __init__(self, ship, selected):
        self.runtime = ship.runtime
        self.folder = post.Store(ship.runtime).dir
        self.selected = selected
        self.forward = ship.forward
        self.changes = None
        self.seen = set()
        self.started = time.time_ns()
        self.initial = self._inventory(initial=True) - self.seen

    def _inventory(self, *, initial=False):
        try:
            descriptor = post_dir.open_directory(self.folder, create=False)
        except FileNotFoundError:
            if self.changes is not None:
                self.changes.close()
                self.changes = None
            return set()
        try:
            info = os.fstat(descriptor)
            identity = info.st_dev, info.st_ino
            if self.changes is None or self.changes.identity != identity or self.changes.fd is None:
                if self.changes is not None:
                    self.changes.close()
                self.changes = post_index.DirectoryChanges(descriptor, identity)
            with os.scandir(f'/proc/self/fd/{descriptor}') as entries:
                names = set()
                for entry in entries:
                    if entry.name.startswith('.') or not entry.name.endswith('.json'):
                        continue
                    if initial:
                        try:
                            before = entry.stat(follow_symlinks=False).st_ctime_ns < self.started
                        except FileNotFoundError:
                            continue
                        if before:
                            self.seen.add(entry.name)
                    names.add(entry.name)
                return names
        finally:
            os.close(descriptor)

    def close(self):
        if self.changes is not None:
            self.changes.close()

    @property
    def fd(self):
        return self.changes.fd if self.changes is not None else None

    def recipients(self):
        changed = self.changes.names() if self.changes is not None else None
        if changed is None:
            changed = self._inventory()
        changed.update(self.initial)
        self.initial.clear()
        recipients = set()
        for name in changed - self.seen:
            if not post_dir._plain_name(name) or name.startswith('.') or not name.endswith('.json'):
                continue
            identifier = name.removesuffix('.json')
            try:
                if fleet_post.context_only(self.runtime, identifier):
                    self.seen.add(name)
                    continue
                raw = post_dir.read_bytes(self.folder / name, limit=post.DROP_MAX_BYTES)
                message = post.Message.from_json(raw.decode('utf-8'))
                if message.id != identifier:
                    continue
                message = fleet_post.local_view(self.runtime, message, data=raw)
            except (OSError, ShipError, ValueError, TypeError, KeyError):
                continue
            self.seen.add(name)
            if message.kind in post.KINDS and message.kind != 'ack' and message.to not in self.forward:
                if message.to in self.selected:
                    recipients.add(message.to)
        return recipients


@dataclass(frozen=True)
class Observation:
    spec: SeatSpec
    turn_open: bool | None = None
    launch: tuple[str, int, str] | None = None
    model: str | None = None


def observe(ship, specs, tmux, *, stopped=None):
    """Read only records already joined by the framework. Missing evidence supplies no turn state."""
    seen = {}
    for spec in specs:
        if stopped is not None and stopped.is_set():
            break
        seen[spec.name] = Observation(spec)
        try:
            seat = read_seat(ship, spec, tmux)
            seen[spec.name] = Observation(seat.spec)
            if not isinstance(seat, Seat):
                continue
            launch = seat.launched()
            if launch is None:
                seen[spec.name] = Observation(seat.spec, False)
                continue
            pane = seat.pane()
            if pane is None or pane.dead:
                if seat.launched() == launch:
                    seen[spec.name] = Observation(seat.spec, False)
                continue
            binding = launch.pane, launch.pid, launch.at
            seen[spec.name] = Observation(seat.spec, launch=binding)
            record = seat.lookup_record()
            if record is None:
                continue
            state = record_follow.state(seat, record)
            if seat.launched() == launch:
                seen[spec.name] = Observation(seat.spec, state.turn_open, binding, state.model)
        except (OSError, ShipError, TmuxError, ValueError):
            continue
    return seen


class Records:
    def __init__(self, ship, specs, tmux):
        self.stopped = Event()
        self.current = {}, time.monotonic()
        self.thread = Thread(target=self._run, args=(ship, specs, tmux), name='qd-follow-records', daemon=True)
        self.thread.start()

    def _run(self, ship, specs, tmux):
        while not self.stopped.is_set():
            self.current = observe(ship, specs, tmux, stopped=self.stopped), time.monotonic()
            self.stopped.wait(RECORD_INTERVAL)

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=0.2)


@contextmanager
def terminal(stdin, stdout):
    """Own only this terminal. Ctrl-C uses its ordinary signal, and TERM/HUP use the same unwind."""
    descriptor = stdin.fileno()
    previous = tty.tcgetattr(descriptor)

    def interrupted(*_):
        raise KeyboardInterrupt

    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGHUP, signal.SIGTERM)}
    tty.setcbreak(descriptor)
    try:
        for sig in handlers:
            signal.signal(sig, interrupted)
        stdout.write('\033[?1049h')
        stdout.flush()
        yield descriptor
    finally:
        tty.tcsetattr(descriptor, tty.TCSANOW, previous)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        try:
            stdout.write('\033[0m\033[?25h\033[?7h\033[?1049l')
            stdout.flush()
        except BrokenPipeError:
            pass


def open_cast(path):
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        return os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n')
    except OSError as error:
        raise ShipError(f'cannot create cast {path}: {error.strerror}') from None


class Viewer:
    def __init__(self, ship, tmux, selected, intent, *, fit=False):
        self.ship, self.tmux, self.intent, self.fit = ship, tmux, intent, fit
        self.sessions = {session.title for session in selected}
        keys = {session.name for session in selected}
        self.specs = {name: spec for name, spec in ship.seats.items() if spec.session in keys}
        columns = tuple(tuple(name for name, spec in self.specs.items() if spec.session == session.name)
                        for session in selected)
        self.room = follow_view.Room(columns, {name: follow_view.Activity() for name in self.specs})
        self.geometry_due = float('-inf')
        self.screen_due = float('-inf')
        self.screens = {}
        self.widths = None

    def running(self):
        return bool(self.tmux.session_details(self.sessions, owner=str(self.ship.folder)))

    def display(self, name):
        """Display is a separate read. A screen never updates the activity or layout."""
        try:
            seat = read_seat(self.ship, self.specs[name], self.tmux)
            if isinstance(seat, Seat):
                frame = seat.read_control_frame()
                if frame is not None and frame.pane.session == seat.session.title:
                    return frame.text
        except (OSError, ShipError, TmuxError, ValueError):
            pass
        return ''

    def _size(self, now, sizes, rows, observed):
        if self.intent is None or now < self.geometry_due:
            return
        self.geometry_due = now + GEOMETRY_INTERVAL
        if self.widths is None:
            self.widths = sizes
        sizes_to_apply = {}
        for column, width, target in zip(self.room.columns, sizes if self.fit else self.widths, self.room.target):
            for name in column:
                reading = observed.get(name)
                if reading is None or reading.launch is None:
                    continue
                height = None
                if self.fit:
                    full = max(1, rows - len(column))
                    height = min(full, max(MIN_FIT_HEIGHT, target.get(name, full)))
                elif self.ship.session_of(self.specs[name]).human:
                    continue
                sizes_to_apply[name] = reading.launch, width, height
        self.intent.update(sizes_to_apply)

    def paint(self, now, size, observed):
        cols, rows = size
        sizes = follow_view.widths(cols, len(self.room.columns))
        if min(sizes) < 16 or rows < max(map(len, self.room.columns)):
            line, _, _ = follow_view._clip('Enlarge the terminal to see every seat.', cols)
            return (line,)
        plans = self.room.update(now, rows)
        self._size(now, sizes, rows, observed)
        activity = self.room.activity
        titles = {}
        for name, declaration in self.specs.items():
            reading = observed.get(name, Observation(declaration))
            spec = reading.spec
            titles[name] = f'{spec.display} · {self.ship.name} · {spec.harness} · {reading.model or spec.model or "default"}'
        showing = [name for plan in plans for name, height in plan.items() if name is not None and height]
        if now >= self.screen_due:
            self.screen_due = now + SCREEN_INTERVAL
            self.screens = {name: self.display(name) for name in showing}
        else:
            for name in showing:
                if name not in self.screens:
                    self.screens[name] = self.display(name)
        return follow_view.compose(self.room.columns, plans, sizes, titles, self.screens, activity, now)

    def run(self, descriptor, stdout, cast):
        with ExitStack() as stack:
            arrivals = Arrivals(self.ship, self.specs)
            stack.callback(arrivals.close)
            records = Records(self.ship, tuple(self.specs.values()), self.tmux)
            stack.callback(records.close)
            frames = follow_view.TerminalFrames(stdout, cast)
            stack.callback(frames.close)
            terminal_fd = stdout.fileno()
            check_running = float('-inf')
            observed, stamp = {}, float('-inf')
            while True:
                now = time.monotonic()
                if now >= check_running:
                    check_running = now + 1.0
                    if not self.running():
                        return
                for name in arrivals.recipients():
                    self.room.activity[name].arrived = now
                latest, sampled_at = records.current
                if sampled_at != stamp:
                    observed, stamp = latest, sampled_at
                    for name, reading in observed.items():
                        self.room.activity[name].observe(reading.turn_open, stamp)
                size = os.get_terminal_size(terminal_fd)
                size = (size.columns or 80, size.lines or 24)
                frames.paint(size, self.paint(now, size, observed))
                events = [descriptor] if arrivals.fd is None else [descriptor, arrivals.fd]
                ready, _, _ = select.select(events, (), (), FRAME)
                if descriptor in ready:
                    data = os.read(descriptor, 1024)
                    if not data or b'q' in data or b'\x03' in data:
                        return


def run(ship, names, *, record: Path | None = None, fit=False):
    selected = sessions(ship, names)
    tmux = Tmux.for_ship(ship, strict=True, command_timeout=follow_geometry.COMMAND_TIMEOUT)
    disposable = follow_geometry.disposable(ship, tmux)
    if fit and not disposable:
        raise ShipError('--fit requires a framework-created throwaway ship')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ShipError('qd attach --follow requires an interactive terminal')
    with ExitStack() as cleanup:
        intent = None
        if disposable:
            if not servicectl.alive(ship.runtime):
                raise ShipError('follow on a throwaway uses its service for window sizing; qd up starts it')
            cleanup.enter_context(follow_geometry.lease(ship.runtime))
            intent = follow_geometry.ViewerIntent(ship.runtime)
        viewer = Viewer(ship, tmux, selected, intent, fit=fit)
        if not viewer.running():
            raise ShipError('no selected session is running; qd up brings the ship up')
        try:
            with ExitStack() as stack:
                cast = stack.enter_context(open_cast(record)) if record is not None else None
                descriptor = stack.enter_context(terminal(sys.stdin, sys.stdout))
                viewer.run(descriptor, sys.stdout, cast)
        except (KeyboardInterrupt, BrokenPipeError):
            pass
        except OSError as error:
            raise ShipError(f'follow view: {error}') from None
        finally:
            if intent is not None and not intent.finish(RESTORE_SECONDS):
                print('qd: follow window restoration awaits the throwaway service', file=sys.stderr, flush=True)
