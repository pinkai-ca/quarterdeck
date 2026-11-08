"""The only module that runs tmux.

Every target is a pane id (``%12``), never a name or a title: names are display, and the
programs rewrite titles themselves (law: identity).
"""

from __future__ import annotations

import itertools
import os
import secrets
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

HISTORY_LINES = 50_000


class TmuxError(Exception):
    pass


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
    ]
)


def _session(name: str) -> str:
    """An exact session target: tmux matches a bare name by prefix."""
    return f"={name}:"


def _parse(line: str) -> Pane:
    id_, session, window, pid, width, height, in_mode, dead, status = line.split("\t")
    return Pane(
        id=id_,
        session=session,
        window=window,
        pid=int(pid),
        width=int(width),
        height=int(height),
        in_mode=in_mode == "1",
        dead=dead == "1",
        exit_status=int(status) if status else None,
    )


class Tmux:
    """A tmux server: the user's by default, a private one (``-L``) in tests."""

    def __init__(self, socket: str | None = None):
        self.socket = socket
        self._buffers = itertools.count()

    @classmethod
    def default(cls) -> "Tmux":
        """The user's server, or the one ``QD_TMUX_SOCKET`` names (the drill uses its own)."""
        return cls(os.environ.get("QD_TMUX_SOCKET") or None)

    def run(self, *args: str, stdin: str | None = None) -> str:
        command = ["tmux"] + (["-L", self.socket] if self.socket else []) + list(args)
        done = subprocess.run(command, input=stdin, capture_output=True, text=True)
        if done.returncode:
            raise TmuxError(f"tmux {args[0]}: {done.stderr.strip() or done.returncode}")
        return done.stdout

    # -- sessions and windows ----------------------------------------------------------

    def running(self) -> bool:
        try:
            self.run("list-sessions")
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
        ident = self.run("new-session", "-d", "-P", "-F", "#{session_id}", "-e", f"QD_SESSION_OWNER={token}",
                         "-s", name, "-n", "qd").strip()
        identity = SessionIdentity(ident, token)
        try:
            self.run("set-option", "-t", ident, "history-limit", str(HISTORY_LINES))
            self.run("set-option", "-t", ident, "renumber-windows", "on")
            for key, value in (env or {}).items():
                self.run("set-environment", "-t", ident, key, value)
        except BaseException:
            self.kill_session_id(identity)
            raise
        return identity

    def kill_session_id(self, identity: SessionIdentity) -> None:
        """A server restart can reuse IDs: both the ID and creation token must match."""
        if not self.running() or identity.id not in self.run("list-sessions", "-F", "#{session_id}").splitlines():
            return
        environment = self.run("show-environment", "-t", identity.id).splitlines()
        if f"QD_SESSION_OWNER={identity.token}" in environment:
            self.run("kill-session", "-t", identity.id)

    def kill_session(self, name: str) -> None:
        self.run("kill-session", "-t", _session(name))

    def kill_server(self) -> None:
        try:
            self.run("kill-server")
        except TmuxError:
            pass

    def new_window(
        self, session: str, name: str, cwd: Path, command: list[str], env: dict[str, str] | None = None
    ) -> Pane:
        """Open ``command`` in a new window of ``session``; the window stays when it fails.

        tmux runs the command through the user's login shell, whose profile resets PATH,
        so a PATH the program needs travels in the command itself, and every other
        variable in ``env`` reaches it as given. The command line is short: tmux refuses
        one over 16 KiB, so a seat's long command goes in a script the command names.
        """
        args = ["new-window", "-d", "-P", "-F", _FORMAT, "-t", _session(session), "-n", name, "-c", str(cwd)]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        # Prepare a blocking pane before running a command that may exit immediately.
        pane = _parse(self.run(*args, "cat").rstrip("\n"))
        self.run("set-option", "-w", "-t", pane.id, "remain-on-exit", "failed")
        args = ["respawn-pane", "-k", "-t", pane.id, "-c", str(cwd)]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        # Read the new PID in the same command queue, before an immediate successful
        # exit can remove the pane. Its pane id and retention setting are preserved.
        pane = _parse(self.run(*args, shlex.join(command), ";", "display-message", "-p", "-t", pane.id,
                               _FORMAT).rstrip("\n"))
        self._drop_placeholder(session)
        return pane

    def _drop_placeholder(self, session: str) -> None:
        for pane in self.panes(session):
            if pane.window == "qd":
                self.run("kill-window", "-t", pane.id)

    def kill_window(self, pane_id: str) -> None:
        self.run("kill-window", "-t", pane_id)

    # -- panes -------------------------------------------------------------------------

    def panes(self, session: str | None = None) -> list[Pane]:
        if not self.running():
            return []
        args = ["list-panes", "-F", _FORMAT] + (["-s", "-t", _session(session)] if session else ["-a"])
        return [_parse(line) for line in self.run(*args).splitlines()]

    def pane(self, pane_id: str) -> Pane | None:
        return next((p for p in self.panes() if p.id == pane_id), None)

    def capture(self, pane_id: str, history: int = 0) -> str:
        """Join only terminal-marked soft wraps; preserve application newlines."""
        return self.run("capture-pane", "-p", "-J", "-t", pane_id, "-S", str(-history))

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

    # -- the status line ---------------------------------------------------------------

    def status_line(self, session: str, command: str, interval: int = 5) -> None:
        """Have tmux itself refresh the ship's one line on every session, on its own timer."""
        self.run("set-option", "-t", _session(session), "status-interval", str(interval))
        self.run("set-option", "-t", _session(session), "status-right-length", "160")
        self.run("set-option", "-t", _session(session), "status-right", f"#({command})")
