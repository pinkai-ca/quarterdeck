"""The fake harness adapter: the reference implementation of the interface.

It launches ``qd-fakeharness`` and reads what it writes. A seat's ``settings`` are the
fake program's own keys (``as``, ``queues``, ``dialog``, ``steps``, ``step_seconds``, and pi-shaped ``end_reason``),
passed verbatim as ``QD_FAKE_<KEY>`` at launch.
"""

from __future__ import annotations

from .capabilities import DefaultMetadata, EffortCapability, SuggestedValue

import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

from ..ship import Pool, SeatSpec
from . import claude, codex, pi
from .base import Box, Input, Control, Credential, Harness, Interruption, Keys, Launch, Screen, State, Turn

RULE = re.compile(r"^─{8,}\s*$")
STATUS = re.compile(r"^fake (\w+) · model (\S+) · effort (\S+) · (idle|working)(?: · queued (\d+))?")
DIALOG = re.compile(r"^┌ ([^─]+?) ─")


class FakeHarness(Harness):
    efforts = EffortCapability("unrestricted", measured_versions=("fake",), complete=True)
    defaults = DefaultMetadata(model_default=True, model=SuggestedValue(None, "program-default"),
                               effort=SuggestedValue(None, "program-default"))
    unclassified_default_overrides = False  # simulator selection reads argv only
    model_selectors = effort_selectors = ()
    selection_templates = ()  # the simulator never reads a ship template

    def human_wake_box_margin(self, pane_width: int) -> int:
        return 2  # synthetic one-row prompt; the simulator does not emulate wrap

    name = "fake"
    native_provider = "synthetic"
    program = sys.executable
    synthetic = True
    home_variable = "QD_FAKE_HOME"
    keys = Keys(submit="Enter", send_now=("C-x", "C-s"), interrupt="Escape")

    def owns_launch(self, argv) -> bool:
        return bool(argv) and (Path(argv[0]).name in ("fake", "qd-fakeharness") or
                              len(argv) >= 3 and tuple(argv[1:3]) == ("-m", "quarterdeck.fakeharness"))

    @staticmethod
    def shape_of(seat: SeatSpec) -> str:
        return str(seat.settings.get("as", "claude"))

    def drill_settings(self, slow_acceptance: bool = False) -> dict:
        return {"step_seconds": "0.05"}

    def queues_while_working(self, seat: SeatSpec) -> bool:
        return str(seat.settings.get("queues", "1")) not in ("0", "False", "false")

    # -- launch ------------------------------------------------------------------------

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        self.check_defaults(seat, ship_home)
        home.mkdir(parents=True, exist_ok=True)
        (home / "settings.json").write_text(json.dumps(seat.settings, indent=2) + "\n")
        return None

    def launch(
        self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None
    ) -> Launch:
        argv = [self.program, "-m", "quarterdeck.fakeharness", "--as", self.shape_of(seat), "--brief", str(brief)]
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
        self._startup_token = uuid.uuid4().hex
        env["QD_FAKE_STARTUP_TOKEN"] = self._startup_token
        return Launch(tuple(argv), env)

    def synthetic_ready(self, home: Path) -> bool:
        token = getattr(self, "_startup_token", None)
        if token is None:
            return False
        try:
            return (home / "synthetic-ready").read_text() == token
        except FileNotFoundError:
            return False

    def version(self, home: Path | None = None) -> str:
        return "fake"

    # -- record ------------------------------------------------------------------------

    @staticmethod
    def _reader(record: Path):
        if record.parent.name == "pi-sessions":
            return pi
        return codex if "sessions" in record.parts else claude

    def records(self, home: Path) -> list[Path]:
        files = claude.records(home) + codex.records(home) + list((home / "pi-sessions").glob("*.jsonl"))
        return sorted(files, key=lambda p: p.stat().st_mtime)

    def first_timestamp(self, record: Path) -> datetime | None:
        return self._reader(record).first_timestamp(record)

    def session_id(self, record: Path) -> str | None:
        return self._reader(record).session_id(record)

    def inputs(self, record: Path) -> list[Input]:
        return self._reader(record).inputs(record)

    def state(self, record: Path) -> State:
        return self._reader(record).state(record)

    def turns(self, record: Path) -> list[Turn]:
        return self._reader(record).turns(record)

    def read(self, record: Path) -> tuple[State, list[Turn]]:
        return self._reader(record).read(record)

    def model_control_origin(self, seat, record: Path | None, value: str) -> dict | None:
        from ..request_queue import launch_binding
        if (self.model_identity(value) is None
                or (self.shape_of(seat.spec) != 'claude' if record is None else self._reader(record) is not claude)):
            return None
        # The simulator owns this literal display/identity contract; no native catalog is inferred.
        return claude.control_origin(record, dict(display=value, synthetic=True), launch=launch_binding(seat))

    def model_control_join(self, seat, record: Path, origin: dict) -> dict | None:
        from ..request_queue import launch_binding
        if origin.get('path') is not None:
            return origin
        launched = seat.launched()
        if (launched is None or origin.get('first_record_launch') != launch_binding(seat)
                or self._reader(record) is not claude or seat.record() != record
                or record in tuple(Path(path) for path in launched.records_before)):
            return None
        try:
            first = self.first_timestamp(record)
            if first is None or first.tzinfo is None or first < launched.when:
                return None
            info = record.stat()
            return {**origin, 'path': str(record), 'device': info.st_dev, 'inode': info.st_ino}
        except (OSError, ValueError, TypeError):
            return None

    def model_control_result(self, seat, record: Path, value: str, origin: dict) -> str | None:
        if (self._reader(record) is not claude or origin.get('display') != dict(display=value, synthetic=True)
                or claude.command_model_result(record, origin, value, value) != value):
            return None
        # Keep the fake's existing public screen confirmation too; its record is now required first.
        found = seat.read_pane()
        return value if found is not None and self.screen(found[1]).shows.get('model') == value else None

    def model_control_current(self, record: Path, origin: dict) -> bool | None:
        rows = claude.control_suffix(record, origin)
        if rows is None or any(row.get('type') in ('user', 'queue-operation') for row in rows):
            return None
        return True

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
        if not box.strip():
            box = ""
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
