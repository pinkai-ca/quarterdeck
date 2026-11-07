"""The only module that runs tmux.

Every target is a pane id (``%12``), never a name or a title: names are display, and the
programs rewrite titles themselves (law: identity).
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

HISTORY_LINES = 50_000


class TmuxError(Exception):
    pass


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

    @property
    def target(self) -> str:
        return self.id


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

    def new_session(self, name: str, env: dict[str, str] | None = None) -> None:
        """A detached session, holding its history for days, with the ship in its environment.

        It opens on a placeholder window that the first seat replaces, because the
        history limit must be set before any seat's window exists.
        """
        self.run("new-session", "-d", "-s", name, "-n", "qd")
        self.run("set-option", "-t", _session(name), "history-limit", str(HISTORY_LINES))
        self.run("set-option", "-t", _session(name), "renumber-windows", "on")
        for key, value in (env or {}).items():
            self.run("set-environment", "-t", _session(name), key, value)

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
        so a PATH the program needs travels in the command itself (``env PATH=…``), and
        every other variable in ``env`` reaches it as given.
        """
        args = ["new-window", "-d", "-P", "-F", _FORMAT, "-t", _session(session), "-n", name, "-c", str(cwd)]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        pane = _parse(self.run(*args, shlex.join(command)).rstrip("\n"))
        self.run("set-option", "-w", "-t", pane.id, "remain-on-exit", "failed")
        self._drop_placeholder(session)
        return pane

    def _drop_placeholder(self, session: str) -> None:
        for pane in self.panes(session):
            if pane.window == "qd":
                self.run("kill-window", "-t", pane.id)

    def kill_window(self, pane_id: str) -> None:
        self.run("kill-window", "-t", pane_id)

    def rename_window(self, pane_id: str, name: str) -> None:
        self.run("rename-window", "-t", pane_id, name)

    # -- panes -------------------------------------------------------------------------

    def panes(self, session: str | None = None) -> list[Pane]:
        if not self.running():
            return []
        args = ["list-panes", "-F", _FORMAT] + (["-s", "-t", _session(session)] if session else ["-a"])
        return [_parse(line) for line in self.run(*args).splitlines()]

    def pane(self, pane_id: str) -> Pane | None:
        return next((p for p in self.panes() if p.id == pane_id), None)

    def capture(self, pane_id: str, history: int = 0) -> str:
        """The screen as shown, with ``history`` lines of scrollback above it."""
        return self.run("capture-pane", "-p", "-t", pane_id, "-S", str(-history))

    def pane_environment(self, pane_id: str, name: str) -> str | None:
        pane = self.pane(pane_id)
        if pane is None:
            return None
        try:
            out = self.run("show-environment", "-t", _session(pane.session), name)
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
        if bracketed and pane is not None and pane.in_mode:
            self.run("load-buffer", "-b", "qd", "-", stdin=f"\x1b[200~{text.replace(chr(10), chr(13))}\x1b[201~")
            self.run("paste-buffer", "-d", "-r", "-b", "qd", "-t", pane_id)
            return
        self.run("load-buffer", "-b", "qd", "-", stdin=text)
        self.run("paste-buffer", "-d", "-b", "qd", "-t", pane_id, *(["-p"] if bracketed else []))

    def keys(self, pane_id: str, *keys: str) -> None:
        self.run("send-keys", "-t", pane_id, *keys)

    # -- the status line ---------------------------------------------------------------

    def status_line(self, session: str, command: str, interval: int = 5) -> None:
        """Have tmux itself refresh the ship's one line on every session, on its own timer."""
        self.run("set-option", "-t", _session(session), "status-interval", str(interval))
        self.run("set-option", "-t", _session(session), "status-right-length", "160")
        self.run("set-option", "-t", _session(session), "status-right", f"#({command})")
