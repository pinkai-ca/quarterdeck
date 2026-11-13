"""The only module that runs tmux.

Every target is a pane id (``%12``), never a name or a title: names are display, and the
programs rewrite titles themselves (law: identity).
"""

from __future__ import annotations

import itertools
import os
import secrets
import shlex
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from .tmux_socket import Namespace, OwnedSocket, OwnershipError, ShutdownPending, OWNER, locked, private_endpoint

HISTORY_LINES = 50_000
SHIP_SOCKET = ("tmux", "server")  # under the ship's runtime: the ship's own server, never tmux's default
SOCKET_LIMIT = 108  # bytes in a Unix socket's path (sun_path), its terminating NUL included
CUTOVER = "qd down, then qd up, moves it to its own; the default server itself is left running"


def socket_bytes(path) -> int:
    """A socket path's length as the kernel counts it: its bytes (UTF-8 here) and the terminating NUL."""
    return len(os.fsencode(path)) + 1


def check_socket_length(path, what: str) -> None:
    if socket_bytes(path) > SOCKET_LIMIT:
        raise TmuxError(f"{what} {path} would be {socket_bytes(path)} bytes with its terminating NUL, and a socket's "
                        f"path holds {SOCKET_LIMIT}; nothing was started: move the ship to a shorter folder")


class TmuxError(Exception):
    pass


class TmuxFormatError(TmuxError):
    """A line tmux printed for one of qd's formats that is not in that format; the line is in the message."""


class PrivateTmuxError(TmuxError):
    """Private ownership or lock uncertainty is never evidence of a missing server/pane."""


@dataclass(frozen=True)
class SessionIdentity:
    id: str
    token: str


@dataclass(frozen=True)
class Pane:
    id: str
    session: str
    window: str
    pid: int
    width: int
    height: int
    in_mode: bool
    dead: bool
    exit_status: int | None
    dead_time: int | None = None  # when the pane's program died, in whole seconds since the epoch, as tmux reports it


@dataclass(frozen=True)
class SessionView:
    """One reading of a session, for showing a window there: what it shows, who is scrolling, when its clients last typed."""

    windows: dict[str, tuple[bool, int, int]]  # window id -> (active, width, height)
    pane_windows: dict[str, str]  # pane id -> its window id
    scrolling: bool  # some pane of the session is in a mode (copy or view): a human is reading back
    inputs: tuple[int, ...]  # each attached client's last input, epoch seconds

    @property
    def active(self) -> str | None:
        return next((ident for ident, (active, _, _) in self.windows.items() if active), None)


_FORMAT = "\t".join(
    [
        "#{pane_id}",
        "#{session_name}",
        "#{window_name}",
        "#{pane_pid}",
        "#{pane_width}",
        "#{pane_height}",
        "#{pane_in_mode}",
        "#{pane_dead}",
        "#{pane_dead_status}",
        "#{pane_dead_time}",
    ]
)


def _session(name: str) -> str:
    """An exact session target: tmux matches a bare name by prefix."""
    return f"={name}:"


def _parse(line: str) -> Pane:
    try:
        fields = line.split("\t")
        died = fields.pop() if len(fields) == 10 else ""  # a nine-field line is the format before the death time
        id_, session, window, pid, width, height, in_mode, dead, status = fields
        return Pane(id=id_, session=session, window=window, pid=int(pid), width=int(width), height=int(height),
                    in_mode=in_mode == "1", dead=dead == "1", exit_status=int(status) if status else None,
                    dead_time=int(died) if died else None)
    except ValueError:
        raise TmuxFormatError(f"tmux printed a pane line not in qd's pane format: {line!r}") from None


def _identity(line: str, pane_id: str) -> Pane | None:
    """One pane's identity line: tmux prints every field empty for a pane gone, which reads as gone.

    A non-empty line with no tab, or one whose fields do not parse, is not qd's format: it raises with the
    line, and never reads as a pane gone, which would bring a live seat down in qd's eyes.
    """
    if line.partition("\t")[0] == "" and set(line) <= {"\t"}:
        return None
    pane = _parse(line)
    return pane if pane.id == pane_id else None


def _transport(args):
    """Only known endpoint-neutral transport queues share a namespace stability lock."""
    commands = {'list-sessions', 'list-panes', 'list-windows', 'list-clients',
                'display-message', 'capture-pane', 'show-option', 'show-options', 'show-environment',
                'load-buffer', 'paste-buffer', 'send-keys'}
    command = True
    for arg in args:
        if command:
            if arg not in commands:
                return False
            command = False
        elif arg == ';':
            command = True
        elif ';' in arg or '#(' in arg:
            return False
    return not command


def ship_socket(ship) -> Path:
    """The ship's own server: a fixed path under its runtime, so qd finds it from the ship folder alone."""
    return Path(ship.runtime).joinpath(*SHIP_SOCKET)


def _check_socket_path(path: Path, *, create: bool) -> None:
    """Refuse a socket place qd did not make for this user: a link, another owner, open to others, too long to start.

    Nothing found is changed or removed; only a missing folder is made, and only when the server is to start,
    which is also when the path's length is refused: no server can be listening on a path too long for one.
    """
    folder = path.parent
    if create:
        check_socket_length(path, "the ship's tmux socket")
        folder.parent.mkdir(parents=True, exist_ok=True)
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            pass
    check_private_folder(folder)
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != os.getuid():
        raise TmuxError(f"{path} is not this user's socket; qd changes nothing of it: move it aside")


def check_private_folder(folder: Path) -> None:
    """A folder of this user's alone, as qd made it: no link, this user's, mode 700. Missing is fine."""
    try:
        info = folder.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise TmuxError(f"{folder} is not a plain folder; qd keeps its tmux sockets only in a folder of its own")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise TmuxError(f"{folder} belongs to another user or is open to others (mode {stat.S_IMODE(info.st_mode):o}); "
                        "qd changes nothing of it: make it this user's, mode 700, or move it aside")


def _user_socket(path: Path) -> bool:
    try:
        info = path.lstat()
    except (OSError, ValueError):
        return False
    return stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()


def _default_sockets() -> list[Path]:
    """Where tmux's default server is for this process, as tmux itself finds it: the socket TMUX names, and
    the default socket under TMUX_TMPDIR, or /tmp when that is unset."""
    found = []
    if inherited := os.environ.get("TMUX", "").split(",")[0]:
        found.append(Path(inherited))
    found.append(Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}" / "default")
    sockets = []
    for path in found:
        if _user_socket(path) and path not in sockets:
            sockets.append(path)
    return sockets


def ship_sessions(tmux: "Tmux", ship) -> list[str]:
    """This ship's sessions on a server: a title the ship declares, whose QD_SHIP is this ship's folder."""
    try:
        titles = tmux.sessions()
    except TmuxError:
        return []
    return [title for title in (s.title for s in ship.sessions.values())
            if title in titles and tmux.session_environment(title, "QD_SHIP") == str(ship.folder)]


class Tmux:
    """A tmux server: a ship's own by its socket, or an owned absolute endpoint in tests.

    ``ship=True`` marks a ship's server: the commands name its socket with ``-S`` and keep the
    person's tmux configuration. ``legacy`` names the default server a ship ran on before it had
    its own; qd reaches that one only to read, to bring down and to finish what runs there, and
    never starts or stops it (``-N``).
    """

    def __init__(self, socket: str | None = None, *, _owned=None, ship: bool = False, legacy: bool = False):
        self.socket = socket
        self._buffers = itertools.count()
        self._owned = _owned
        self.held = None
        self._closed = False
        self.ship = ship or legacy
        self.legacy = legacy

    @property
    def _private(self) -> bool:
        return bool(self.socket and Path(self.socket).is_absolute()) and not self.ship

    @classmethod
    def private(cls, fallback: Path) -> "Tmux":
        """A creator-owned short namespace, outside tmux's shared socket directory."""
        try:
            owned = OwnedSocket(Namespace(fallback), root_owner=True)
            return cls(str(owned.path), _owned=owned)
        except (OSError, OwnershipError) as error:
            raise TmuxError(str(error)) from error

    def private_peer(self, suffix='-replacement') -> "Tmux":
        if self._owned is None or self._closed:
            raise TmuxError('only a private namespace creator may delegate a peer')
        try:
            owned = OwnedSocket(self._owned.namespace, suffix)
            return Tmux(str(owned.path), _owned=owned)
        except OwnershipError as error:
            raise TmuxError(str(error)) from error

    def attach_argv(self, session: str) -> list[str]:
        """The words that attach a person's terminal to ``session`` on this server."""
        address = ['-S' if Path(self.socket).is_absolute() else '-L', self.socket] if self.socket else []
        if self.socket and Path(self.socket).is_absolute() and not (self.ship and not self.legacy):
            address += ['-N']
        return ['tmux', *address, 'attach-session', '-t', _session(session)]

    def attach_command(self, session: str) -> str:
        return shlex.join(self.attach_argv(session))

    @classmethod
    def for_ship(cls, ship) -> "Tmux":
        """The server a ship runs on, chosen explicitly and never through TMUX.

        - Its own: the socket under its runtime, whenever that holds the ship's sessions or no other
          server does.
        - tmux's default server, while that holds the ship's sessions and its own holds none: a ship not
          moved yet keeps running there, and says so, until one ``qd down`` and ``qd up``.
        - ``QD_TMUX_SOCKET`` naming another path is honoured only for a creator's private namespace (the
          tests and the drill), or for the server that holds this ship's sessions while its own holds none,
          which is what a seat launched before the move is told. Anything else is refused by name: qd never
          runs one ship on another's server.
        """
        own = ship_socket(ship)
        _check_socket_path(own, create=False)
        server = cls(str(own), ship=True)
        named = os.environ.get("QD_TMUX_SOCKET")
        if named and named != str(own):
            if private_endpoint(named):
                return cls(named)
            if Path(named).is_absolute() and _user_socket(Path(named)):
                old = cls(named, legacy=True)
                if ship_sessions(old, ship) and not (own.exists() and ship_sessions(server, ship)):
                    return old
            raise TmuxError(f"QD_TMUX_SOCKET names {named}, which is not this ship's server {own}, not a private "
                            "test server and not a server holding this ship's sessions; qd will not run this ship "
                            "there. Unset QD_TMUX_SOCKET, or run qd for this ship outside another ship's seat")
        if own.exists() and ship_sessions(server, ship):
            return server  # moved: the default server is not read again on every call
        for old in cls.defaults(own):
            if ship_sessions(old, ship):
                return old
        return server

    def launchable(self) -> None:
        """Refuse, before anything of a launch is made, a ship server whose socket could never be started."""
        if self.ship and not self.legacy:
            check_socket_length(self.socket, "the ship's tmux socket")
            _check_socket_path(Path(self.socket), create=False)

    @classmethod
    def defaults(cls, own: Path) -> list["Tmux"]:
        """tmux's default servers, each reached by its explicit path and never started (``-N``)."""
        return [cls(str(path), legacy=True) for path in _default_sockets() if path != own]

    def split(self, ship) -> list[str]:
        """This ship's sessions on a default server while the ship's own server also holds some: a ship in two places."""
        if not self.ship or self.legacy:
            return []
        return [f"{title} on {old.socket}" for old in Tmux.defaults(Path(self.socket)) for title in ship_sessions(old, ship)]

    def run(self, *args: str, stdin: str | None = None) -> str:
        if self._private and self._owned is None and not _transport(args):
            raise PrivateTmuxError('private observer generic commands require the typed session/door API')
        return self._invoke(*args, stdin=stdin)

    def _invoke(self, *args: str, stdin: str | None = None) -> str:
        """Validated dispatcher for typed operations; generic observers use transport only."""
        if self.ship and not self.legacy and args and args[0] == 'new-session':
            _check_socket_path(Path(self.socket), create=True)
        if self._private:
            try:
                if self._closed:
                    raise OwnershipError('private namespace is closed')
                if not args or args[0].startswith('-'):
                    raise OwnershipError('private transport requires a native command, not address options')
                if self._owned is None and args[0] in ('kill-server', 'start-server'):
                    raise OwnershipError('private observer has no server startup or cleanup authority')
                with locked(self.socket, self._owned.namespace if self._owned else None,
                            exclusive=not _transport(args)) as held:
                    start = False
                    if self._owned:
                        if str(self._owned.path) != self.socket:
                            raise OwnershipError('private transport binding changed; retained')
                        present = self._owned.current(held)
                        if args[0] == 'new-session' and (not present or self._owned.ended()):
                            if not self._owned.ended():
                                raise OwnershipError('prior private server still running; retained')
                            self._remove_ended(held)
                            self._owned.reset()
                            start = True
                    # Keep a private server available to bound observers between sessions.
                    # Its creator, rather than the last session, ends this server lifetime.
                    command = (*args, ';', 'set-option', '-s', 'exit-empty', 'off') if start else args
                    result = self._run(*command, stdin=stdin, start=start)
                    if start:
                        self._owned.record(held, lambda: self._run('display-message', '-p', '#{pid}', ';',
                                                                 'show-environment', '-g', OWNER))
                    return result
            except (OSError, ValueError, OwnershipError) as error:
                raise PrivateTmuxError(str(error)) from error
        return self._run(*args, stdin=stdin)

    def _run(self, *args, stdin=None, start=False):
        if self.ship:
            address = ['-S', self.socket] + (['-N'] if self.legacy else [])
        elif self._private:
            address = ['-S', self.socket, '-f', '/dev/null'] + ([] if start else ['-N'])
        else:
            address = ['-L', self.socket] if self.socket else []
        # -u: a client without a UTF-8 locale prints a format's tabs as "_"; -u keeps them whatever the locale.
        command = ['tmux', '-u', *address, *args]
        env = dict(os.environ, **{OWNER: self._owned.nonce}) if start else None
        done = subprocess.run(command, input=stdin, capture_output=True, text=True, env=env)
        if done.returncode:
            raise TmuxError(f"tmux {args[0]}: {done.stderr.strip() or done.returncode}")
        return done.stdout

    # -- sessions and windows ----------------------------------------------------------

    def running(self) -> bool:
        if self._closed:
            return False
        try:
            self.run("list-sessions")
        except PrivateTmuxError:
            raise
        except TmuxError:
            return False
        return True

    def sessions(self) -> list[str]:
        if not self.running():
            return []
        return self.run("list-sessions", "-F", "#{session_name}").splitlines()

    def has_session(self, name: str) -> bool:
        return name in self.sessions()

    def new_session(self, name: str, env: dict[str, str] | None = None) -> SessionIdentity:
        """A detached session, holding its history for days, with the ship in its environment.

        It opens on a placeholder window that the first seat replaces, because the
        history limit must be set before any seat's window exists.
        """
        token = secrets.token_hex(16)
        ident = self._invoke("new-session", "-d", "-P", "-F", "#{session_id}", "-e", f"QD_SESSION_OWNER={token}",
                         "-s", name, "-n", "qd").strip()
        identity = SessionIdentity(ident, token)
        try:
            self._invoke("set-option", "-t", ident, "history-limit", str(HISTORY_LINES))
            self._invoke("set-option", "-t", ident, "renumber-windows", "on")
            for key, value in (env or {}).items():
                self._invoke("set-environment", "-t", ident, key, value)
        except BaseException:
            self.kill_session_id(identity)
            raise
        return identity

    def kill_session_id(self, identity: SessionIdentity) -> None:
        """A server restart can reuse IDs: both the ID and creation token must match."""
        if not self.running() or identity.id not in self.run("list-sessions", "-F", "#{session_id}").splitlines():
            return
        self._invoke("if-shell", "-F", "-t", identity.id, f"#{{==:#{{QD_SESSION_OWNER}},{identity.token}}}",
                 f"kill-session -t {identity.id}")

    def kill_session(self, name: str) -> None:
        self._invoke("kill-session", "-t", _session(name))

    def kill_server(self) -> None:
        if self.ship:
            raise TmuxError("qd never stops a ship's tmux server: qd down ends the ship's sessions, and the server with them")
        if self._private:
            if self._owned is None:
                raise TmuxError('a private transport binding grants no server cleanup authority')
            try:
                with locked(self.socket, self._owned.namespace) as held:
                    present = self._owned.current(held)
                    if present and not self._owned.ended():
                        guard = '#{&&:#{==:#{pid},%s},#{==:#{%s},%s}}' % (self._owned.pid, OWNER, self._owned.nonce)
                        try:
                            self._run('if-shell', '-F', guard, 'kill-server')
                        except TmuxError:
                            if not self._owned.ended():
                                raise
                    self._remove_ended(held)
                return
            except (OSError, ValueError, OwnershipError) as error:
                raise TmuxError(str(error)) from error
        try:
            self.run("kill-server")
        except TmuxError:
            pass

    def _remove_ended(self, held):
        deadline = time.monotonic() + 2
        while True:
            try:
                self._owned.remove_dead(held)
                return
            except ShutdownPending:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.01)

    def close(self) -> bool:
        """Stop/remove only this creator's endpoint; unknown or peer state is retained visibly."""
        if self._closed:
            return True
        if self._owned is None:
            self.held = 'transport binding has no private cleanup authority'
            return False
        try:
            self.kill_server()
            if self._owned.root_owner:
                with locked(self.socket, self._owned.namespace) as held:
                    # A managed replacement may have arrived since shutdown completed.
                    self._owned.remove_dead(held)
                    self._owned.namespace.remove(held)
            self._owned.reset()
            self._closed = True
            return True
        except (OSError, TmuxError, OwnershipError, ValueError) as error:
            self.held = f'{error}; endpoint {self.socket}'
            return False
        except (KeyboardInterrupt, SystemExit) as error:
            self.held = f'private cleanup interrupted; endpoint retained at {self.socket}'
            error.add_note(self.held)
            raise

    def new_window(
        self, session: str, name: str, cwd: Path, command: list[str], env: dict[str, str] | None = None
    ) -> Pane:
        """Open ``command`` in a new window of ``session``; the window stays when it fails.

        tmux runs the command through the user's login shell, whose profile resets PATH,
        so a PATH the program needs travels in the command itself, and every other
        variable in ``env`` reaches it as given. The command line is short: tmux refuses
        one over 16 KiB, so a seat's long command goes in a script the command names.

        When a terminal is attached to the session, the window takes the size of the window
        the session shows before the command starts; tmux would otherwise size a new detached
        window by the most recently active terminal anywhere, which may be another session's.
        """
        size = self._watched_size(session)
        args = ["new-window", "-d", "-P", "-F", _FORMAT, "-t", _session(session), "-n", name, "-c", str(cwd)]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        # Prepare a blocking pane before running a command that may exit immediately.
        pane = _parse(self._invoke(*args, "cat").rstrip("\n"))
        self._invoke("set-option", "-w", "-t", pane.id, "remain-on-exit", "failed")
        args = []
        if size is not None:
            # Sized while only the placeholder runs, before the command can draw or the door read it.
            # resize-window pins the window to manual sizing, so its own option is unset again at once.
            args += ["resize-window", "-t", pane.id, "-x", str(size[0]), "-y", str(size[1]), ";",
                     "set-option", "-wu", "-t", pane.id, "window-size", ";"]
        args += ["respawn-pane", "-k", "-t", pane.id, "-c", str(cwd)]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        # Read the new PID in the same command queue, before an immediate successful
        # exit can remove the pane. Its pane id and retention setting are preserved.
        pane = _parse(self._invoke(*args, shlex.join(command), ";", "display-message", "-p", "-t", pane.id,
                               _FORMAT).rstrip("\n"))
        self._drop_placeholder(session)
        return pane

    def _watched_size(self, session: str) -> tuple[int, int] | None:
        """The size of the window a session shows, when a terminal is attached to it; otherwise None."""
        try:
            view = self.session_view(session)
        except PrivateTmuxError:
            view = None  # a busy or uncertain namespace sizes nothing; the window's own creation then validates it exclusively
        if view is None or not view.inputs or view.active is None:
            return None  # no terminal watching this session, or no certain reading: tmux sizes it as before
        _, width, height = view.windows[view.active]
        return (width, height) if width > 0 and height > 0 else None

    def _drop_placeholder(self, session: str) -> None:
        for pane in self.panes(session):
            if pane.window == "qd":
                self._invoke("kill-window", "-t", pane.id)

    def kill_window(self, pane_id: str, *, expected_pid: int | None = None) -> None:
        if expected_pid is None:
            self._invoke("kill-window", "-t", pane_id)
            return
        if (not pane_id.startswith('%') or not pane_id[1:].isascii() or not pane_id[1:].isdigit()
                or type(expected_pid) is not int or expected_pid <= 0):
            raise TmuxError('invalid window retirement identity')
        refused = 'qd-window-retirement-identity-refused'
        result = self._invoke('if-shell', '-F', '-t', pane_id, f'#{{==:#{{pane_pid}},{expected_pid}}}',
                              f'kill-window -t {pane_id}', f'display-message -p {refused}')
        if result.strip() == refused:
            raise TmuxError(f'pane {pane_id} changed process identity; window retained')

    def session_view(self, session: str) -> SessionView | None:
        """Windows, panes and clients of one session in one command queue; None when any part is unreadable."""
        if any(char in session for char in '\t\r\n'):
            return None
        target = _session(session)
        try:
            lines = self.run('list-windows', '-t', target, '-F', 'w\t#{window_id}\t#{window_active}\t#{window_width}\t#{window_height}',
                             ';', 'list-panes', '-s', '-t', target, '-F', 'p\t#{pane_id}\t#{window_id}\t#{pane_in_mode}',
                             ';', 'list-clients', '-t', target, '-F', 'c\t#{client_activity}').splitlines()
        except PrivateTmuxError:
            raise
        except (TmuxError, OSError, UnicodeError):
            return None
        windows, panes, inputs, scrolling = {}, {}, [], False
        for line in lines:
            fields = line.split('\t')
            try:
                if fields[0] == 'w' and len(fields) == 5:
                    windows[fields[1]] = (fields[2] == '1', int(fields[3]), int(fields[4]))
                elif fields[0] == 'p' and len(fields) == 4:
                    panes[fields[1]] = fields[2]
                    scrolling = scrolling or fields[3] == '1'
                elif fields[0] == 'c' and len(fields) == 2:
                    inputs.append(int(fields[1]))
                else:
                    return None
            except ValueError:
                return None
        if not windows or sum(active for active, _, _ in windows.values()) != 1 or set(panes.values()) - set(windows):
            return None
        return SessionView(windows, panes, scrolling, tuple(inputs))

    def select_window(self, pane_id: str, *, expected_pid: int) -> None:
        """Make this pane's window the one its session shows, only while the pane still runs the expected process.

        Selecting a window never resizes it by itself; the caller refuses a window whose size
        differs from the one shown, since a client viewing it would fit it to itself.
        """
        if (not pane_id.startswith('%') or not pane_id[1:].isascii() or not pane_id[1:].isdigit()
                or type(expected_pid) is not int or expected_pid <= 0):
            raise TmuxError('invalid window selection identity')
        refused = 'qd-window-selection-identity-refused'
        result = self._invoke('if-shell', '-F', '-t', pane_id, f'#{{==:#{{pane_pid}},{expected_pid}}}',
                              f'select-window -t {pane_id}', f'display-message -p {refused}')
        if result.strip() == refused:
            raise TmuxError(f'pane {pane_id} changed process identity; window not shown')

    # -- panes -------------------------------------------------------------------------

    def panes(self, session: str | None = None) -> list[Pane]:
        if not self.running():
            return []
        args = ["list-panes", "-F", _FORMAT] + (["-s", "-t", _session(session)] if session else ["-a"])
        return [_parse(line) for line in self.run(*args).splitlines()]

    def pane(self, pane_id: str) -> Pane | None:
        if not pane_id.startswith('%') or not pane_id[1:].isascii() or not pane_id[1:].isdigit():
            return None
        try:
            identity = self.run('display-message', '-p', '-t', pane_id, _FORMAT).rstrip('\n')
        except PrivateTmuxError:
            raise
        except TmuxError:
            return next((p for p in self.panes() if p.id == pane_id), None)
        return _identity(identity, pane_id)

    def started(self) -> int | None:
        """When the running server began, in whole seconds since the epoch, as tmux reports it; None if unreadable.

        Pane ids start again from %0 in each server lifetime, so this tells an id saved in an
        earlier lifetime from the same id in this one.
        """
        try:
            text = self.run('display-message', '-p', '#{start_time}').strip()
        except PrivateTmuxError:
            raise
        except TmuxError:
            return None
        return int(text) if text.isascii() and text.isdigit() else None

    def read_pane(self, pane_id: str, history: int = 0, *, styles: bool = False) -> tuple[Pane, str] | None:
        """Read current identity and text in one server command queue, never from a cache."""
        if not pane_id.startswith('%') or not pane_id[1:].isascii() or not pane_id[1:].isdigit():
            return None
        try:
            output = self.run('display-message', '-p', '-t', pane_id, _FORMAT, ';',
                              'capture-pane', '-p', '-J', *(['-e'] if styles else []), '-t', pane_id, '-S', str(-history))
        except PrivateTmuxError:
            raise
        except TmuxError:
            if self.pane(pane_id) is None:
                return None
            raise
        identity, separator, text = output.partition('\n')
        pane = _identity(identity, pane_id) if separator else None
        return (pane, text) if pane is not None else None

    def clients_attached(self, sessions: list[str]) -> bool | None:
        """Only a successful complete client listing can establish no presentation demand."""
        if any(any(char in name for char in '\t\r\n') for name in sessions):
            return None
        try:
            lines = self.run('list-clients', '-F', 'client\t#{session_name}').splitlines()
        except (TmuxError, OSError, UnicodeError):
            return None
        names = []
        for line in lines:
            fields = line.split('\t')
            if len(fields) != 2 or fields[0] != 'client' or not fields[1]:
                return None
            names.append(fields[1])
        return any(name in sessions for name in names)

    def capture(self, pane_id: str, history: int = 0, *, styles: bool = False) -> str:
        """Join only terminal-marked soft wraps; preserve application newlines."""
        return self.run("capture-pane", "-p", "-J", *(['-e'] if styles else []), "-t", pane_id, "-S", str(-history))

    def session_environment(self, session: str, name: str) -> str | None:
        """A variable set on the session with ``new_session``; None when unset."""
        try:
            out = self.run("show-environment", "-t", _session(session), name)
        except TmuxError:
            return None
        return out.strip().partition("=")[2] or None

    # -- typing ------------------------------------------------------------------------

    def paste(self, pane_id: str, text: str, bracketed: bool = True) -> None:
        """Paste ``text`` into the pane as one bracketed paste, the way a terminal would.

        tmux brackets a paste itself (``-p``) except while the pane is in copy mode, where
        the paste still reaches the program but without its brackets; there the brackets go
        in with the text, so a multi-line message stays one paste for the reader scrolled up.
        """
        pane = self.pane(pane_id)
        buffer = f"qd-{os.getpid()}-{next(self._buffers)}"  # one buffer per paste: concurrent pastes never cross
        if bracketed and pane is not None and pane.in_mode:
            self.run("load-buffer", "-b", buffer, "-", stdin=f"\x1b[200~{text.replace(chr(10), chr(13))}\x1b[201~")
            self.run("paste-buffer", "-d", "-r", "-b", buffer, "-t", pane_id)
            return
        self.run("load-buffer", "-b", buffer, "-", stdin=text)
        self.run("paste-buffer", "-d", "-b", buffer, "-t", pane_id, *(["-p"] if bracketed else []))

    def keys(self, pane_id: str, *keys: str) -> None:
        self.run("send-keys", "-t", pane_id, *keys)

    def mouse_modes(self, pane_id: str, expected_pid: int) -> dict[str, bool] | None:
        """The mouse reports the pane's program has asked for, bound to this exact process; None if unreadable."""
        raw = self._invoke('display-message', '-p', '-t', pane_id,
                           '#{pane_pid}\t#{mouse_any_flag}\t#{mouse_button_flag}\t#{mouse_all_flag}'
                           '\t#{mouse_standard_flag}\t#{mouse_sgr_flag}').strip()
        parts = raw.split('\t')
        if len(parts) != 6 or not all(part in ('0', '1') for part in parts[1:]):
            return None
        try:
            if int(parts[0]) != expected_pid:
                return None
        except ValueError:
            return None
        return dict(zip(('any', 'button', 'all', 'standard', 'sgr'), (part == '1' for part in parts[1:])))

    def click(self, pane_id: str, x: int, y: int) -> None:
        """One left-button press and its release at the 1-based cell, as SGR mouse reports, sent as raw bytes."""
        for final in ('M', 'm'):
            report = f'\x1b[<0;{x};{y}{final}'.encode()
            self.run('send-keys', '-t', pane_id, '-H', *(f'{byte:02x}' for byte in report))

    def editor_position(self,pane_id: str,expected_pid: int):
        """One cursor/geometry reading bound to this exact native process."""
        raw=self._invoke('display-message','-p','-t',pane_id,
                         '#{pane_pid}\t#{cursor_x}\t#{cursor_y}\t#{pane_width}\t#{pane_height}').strip()
        try:pid,x,y,width,height=map(int,raw.split('\t'))
        except ValueError:return None
        return (x,y,width,height) if pid==expected_pid else None

    def clear_owned_paste(self,pane_id: str,expected_pid: int,position,keys):
        """Refuse a changed cursor, geometry or process in the native key queue."""
        if (not pane_id.startswith('%') or not pane_id[1:].isascii() or not pane_id[1:].isdigit()
                or type(expected_pid) is not int or expected_pid<=0 or len(position)!=4
                or any(type(n) is not int or n<0 for n in position)
                or not keys or any(k not in ('C-a','C-k','BSpace') for k in keys)):
            raise TmuxError('unproved paste clear identity or key path')
        conditions=[f'#{{==:#{{{name}}},{value}}}' for name,value in zip(
            ('pane_pid','cursor_x','cursor_y','pane_width','pane_height'),(expected_pid,*position))]
        condition=conditions[0]
        for item in conditions[1:]:condition=f'#{{&&:{condition},{item}}}'
        refused='qd-paste-clear-identity-refused'
        result=self._invoke('if-shell','-F','-t',pane_id,condition,
                            shlex.join(['send-keys','-t',pane_id,*keys]),f'display-message -p {refused}')
        if result.strip()==refused:raise TmuxError('owned paste changed before clear; no keys sent')

    # -- the status line ---------------------------------------------------------------

    def status_line(self, session: str, command: str, interval: int = 5) -> None:
        """Have tmux itself refresh the ship's one line on every session, on its own timer."""
        self._invoke("set-option", "-t", _session(session), "status-interval", str(interval))
        self._invoke("set-option", "-t", _session(session), "status-right-length", "160")
        self._invoke("set-option", "-t", _session(session), "status-right", f"#({command})")
