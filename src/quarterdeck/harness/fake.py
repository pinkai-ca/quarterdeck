"""The fake harness adapter: the reference implementation of the interface.

It launches ``qd-fakeharness`` and reads what it writes. A seat's ``settings`` are the
fake program's own keys (``as``, ``queues``, ``dialog``, ``steps``, ``step_seconds``),
passed verbatim as ``QD_FAKE_<KEY>`` at launch.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from ..ship import Pool, SeatSpec
from . import claude, codex
from .base import Box, Control, Credential, Harness, Interruption, Keys, Launch, Screen, State, Turn

RULE = re.compile(r"^─{8,}\s*$")
STATUS = re.compile(r"^fake (\w+) · model (\S+) · effort (\S+) · (idle|working)(?: · queued (\d+))?")
DIALOG = re.compile(r"^┌ ([^─]+?) ─")


class FakeHarness(Harness):
    name = "fake"
    home_variable = "QD_FAKE_HOME"
    keys = Keys(submit="Enter", send_now=("C-x", "C-s"), interrupt="Escape", empty=("C-u",))
    brief_channel = "system-prompt"

    @staticmethod
    def shape_of(seat: SeatSpec) -> str:
        return str(seat.settings.get("as", "claude"))

    def queues_while_working(self, seat: SeatSpec) -> bool:
        return str(seat.settings.get("queues", "1")) not in ("0", "False", "false")

    # -- launch ------------------------------------------------------------------------

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        home.mkdir(parents=True, exist_ok=True)
        (home / "settings.json").write_text(json.dumps(seat.settings, indent=2) + "\n")
        return None

    def launch(
        self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None
    ) -> Launch:
        argv = [sys.executable, "-m", "quarterdeck.fakeharness", "--as", self.shape_of(seat), "--brief", str(brief)]
        if seat.model:
            argv += ["--model", seat.model]
        if seat.effort:
            argv += ["--effort", seat.effort]
        if resume:
            argv += ["--resume", resume]
        if first:
            argv += ["--first", first]
        env = {self.home_variable: str(home)}
        for key, value in seat.settings.items():
            if key != "as":
                env[f"QD_FAKE_{key.upper()}"] = "1" if value is True else "0" if value is False else str(value)
        return Launch(tuple(argv), env)

    def version(self) -> str:
        return "fake"

    # -- record ------------------------------------------------------------------------

    @staticmethod
    def _reader(record: Path):
        return codex if "sessions" in record.parts else claude

    def records(self, home: Path) -> list[Path]:
        files = claude.records(home) + codex.records(home)
        return sorted(files, key=lambda p: p.stat().st_mtime)

    def first_timestamp(self, record: Path) -> datetime | None:
        return self._reader(record).first_timestamp(record)

    def session_id(self, record: Path) -> str | None:
        return self._reader(record).session_id(record)

    def state(self, record: Path) -> State:
        return self._reader(record).state(record)

    def turns(self, record: Path) -> list[Turn]:
        return self._reader(record).turns(record)

    def read(self, record: Path) -> tuple[State, list[Turn]]:
        return self._reader(record).read(record)

    # -- screen ------------------------------------------------------------------------

    def screen(self, text: str) -> Screen:
        lines = text.rstrip("\n").split("\n")
        rules = [i for i, line in enumerate(lines) if RULE.match(line)]
        interruption = self.recognize(text)
        unknown = None
        for line in lines:
            match = DIALOG.match(line)
            if match and interruption is None:
                unknown = match.group(1).strip()
        if len(rules) < 2:
            return Screen(text, box=None, interruption=interruption, unknown=unknown)
        top, bottom = rules[-2], rules[-1]
        box = "\n".join(line[2:] for line in lines[top + 1 : bottom])
        status = STATUS.match(lines[bottom + 1]) if bottom + 1 < len(lines) else None
        shows = {"model": status.group(2), "effort": status.group(3)} if status else {}
        return Screen(
            text,
            box=box,
            working=bool(status and status.group(4) == "working"),
            interruption=interruption,
            unknown=unknown,
            queued=int(status.group(5)) if status and status.group(5) else 0,
            shows=shows,
        )

    def box(self, text: str) -> Box:
        """The fake folds any paste with a newline, counting newlines as Claude Code does, trailing ones too."""
        newlines = text.count("\n")
        return Box(text, folded=rf"\[Pasted text #\d+ \+{newlines} lines\]" if newlines else None)

    def controls(self) -> tuple[Control, ...]:
        return (
            Control("model", launch="--model <value>", session="/model {value}", confirmed_by="screen"),
            Control("effort", launch="--effort <value>", session="/effort {value}", confirmed_by="screen"),
        )

    def interruptions(self) -> tuple[Interruption, ...]:
        return (Interruption("daybreak notice", r"┌ Daybreak ", key="Escape"),)
