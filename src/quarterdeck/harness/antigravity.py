"""Antigravity (``agy``), best effort on 1.5.2.

Two things set this program apart, and the adapter says so rather than pretending. It has
no home switch: its state lives under ``~/.gemini`` and no variable moves it, so a seat
runs in the user's Antigravity state, with only its settings passed for the session
(``--settings``). And its conversation record is a SQLite file of protobuf blobs the
framework does not read, so a delivery is confirmed by the transcript's echo and the
watch reads working from the screen; the Ledger gets no tokens from it. The brief goes as
the first message (``--prompt-interactive``); a conversation resumes by its id.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..ship import Pool, SeatSpec
from .base import (Box, Control, Credential, Harness, Interruption, Keys, Launch, Screen, State, Turn,
                   program_version)

STATE_DIR = Path.home() / ".gemini" / "antigravity-cli"
RULE = re.compile(r"^─{20,}\s*$")
STATUS_LINE = re.compile(r"(?:^|\s{2,})([A-Z][\w .-]*?) · (low|medium|high|xhigh|max)\s*$")
WORKING = re.compile(r"esc to cancel|^[⠁-⣿]\s+\w+ing", re.M)
QUEUED_PLACEHOLDER = "Press up to edit queued messages"
QUEUED_LINE = re.compile(r"^▸ (.*?)\s*\(alt\+m to send immediately\)\s*$")


class AntigravityHarness(Harness):
    name = "antigravity"
    home_variable = None
    keys = Keys(submit="Enter", send_now=("M-m",), interrupt="Escape", empty=None)
    brief_channel = "first-message"
    record_confirms = False

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        """Our settings for the session, passed on the command line; nothing of the program's own is written."""
        home.mkdir(parents=True, exist_ok=True)
        settings = {"trustedWorkspaces": [str(seat.cwd)] if seat.cwd else []}
        if ship_home and (ship_home / "settings.json").is_file():
            settings = {**json.loads((ship_home / "settings.json").read_text()), **settings}
        settings.update(seat.settings)
        (home / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
        return None

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        argv = ["agy", "--dangerously-skip-permissions", "--settings", str(home / "settings.json")]
        if seat.model:
            argv += ["--model", seat.model]
        if seat.effort:
            argv += ["--effort", seat.effort]
        if resume:
            argv += ["--conversation", resume]
        opening = "\n\n".join(filter(None, [brief.read_text().strip() if not resume else None, first]))
        if opening:
            argv += ["--prompt-interactive", opening]
        return Launch(tuple(argv), {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"})

    def version(self) -> str | None:
        return program_version(("agy", "--version"))

    # -- record: the conversation databases, joined by creation time, otherwise opaque ---------

    def records(self, home: Path) -> list[Path]:
        files = [p for p in (STATE_DIR / "conversations").glob("*.db") if p.is_file()]
        return sorted(files, key=lambda p: p.stat().st_ctime)

    def first_timestamp(self, record: Path) -> datetime | None:
        return datetime.fromtimestamp(record.stat().st_ctime, timezone.utc)

    def session_id(self, record: Path) -> str | None:
        return record.stem

    def state(self, record: Path) -> State:
        try:
            last_write = datetime.fromtimestamp(record.stat().st_mtime, timezone.utc)
        except FileNotFoundError:
            last_write = None
        return State(last_write=last_write)

    def turns(self, record: Path) -> list[Turn]:
        return []

    # -- screen ---------------------------------------------------------------------------

    def screen(self, text: str) -> Screen:
        lines = text.rstrip("\n").split("\n")
        rules = [i for i, line in enumerate(lines) if RULE.match(line)]
        box, queued = None, 1 if QUEUED_PLACEHOLDER in text else 0  # the placeholder sits on the status line here
        if len(rules) >= 2:
            body = [l[2:] if l.startswith(("> ", "  ")) else l.lstrip(">") for l in lines[rules[-2] + 1:rules[-1]]]
            while body and not body[-1].strip():
                body.pop()
            box = "\n".join(body)
        shows = {}
        status = STATUS_LINE.search(lines[-1]) if lines else None
        if status:
            shows = {"model": status.group(1).strip(), "effort": status.group(2)}
        return Screen(text, box=box, working=bool(WORKING.search(text)), interruption=self.recognize(text) if box is None else None,
                      queued=queued, shows=shows, echoes=self.echoes(lines[:rules[-2]] if len(rules) >= 2 else lines))

    @staticmethod
    def echoes(lines: list[str]) -> tuple[str, ...]:
        """Inputs the transcript shows: ``> `` blocks above the box with their indented lines, and a
        queued message's ``▸ …(alt+m to send immediately)`` line, cut short, as an echo ending in an ellipsis."""
        found: list[list[str]] = []
        block: list[str] | None = None
        for line in lines:
            queued = QUEUED_LINE.match(line)
            if line.startswith("> "):
                block = [line[2:]]
                found.append(block)
            elif queued:
                found.append([queued.group(1).rstrip(".") + "…"])
                block = None
            elif block is not None and line.startswith("  ") and line.strip():
                block.append(line[2:])
            else:
                block = None
        return tuple("\n".join(b) for b in found)

    def box(self, text: str) -> Box:
        return Box(text)

    def controls(self) -> tuple[Control, ...]:
        return (
            Control("model", launch="--model <value>", session=None, confirmed_by="screen"),
            Control("effort", launch="--effort <value>", session=None, confirmed_by="screen"),
        )

    def interruptions(self) -> tuple[Interruption, ...]:
        return (
            Interruption("trust dialog", r"Do you trust the contents of this project\?", key="Enter"),
            Interruption("login", r"Sign in|Log in to|Authenticate", key=None),
        )
