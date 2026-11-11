"""Antigravity (``agy``), proven on 1.5.2.

The program has no home switch: its state lives under ``~/.gemini/antigravity-cli``, so a seat passes
its settings on the command line and joins the one conversation that appears after its launch, and
only a record proven to be the seat's own is ever opened (law: one-record). A conversation is a
SQLite database in WAL mode holding protobuf blobs. It is read from its bytes into an in-memory
snapshot, so nothing is written beside the program's files (law: one-folder). The fields read are
listed in docs/ANTIGRAVITY.md.
"""

from __future__ import annotations

from dataclasses import replace
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..ship import Pool, SeatSpec
from .antigravity_quota import banner, fetch_usage, unknown
from .antigravity_record import OWNED, _CACHE, _parse_usage, first_timestamp, inputs, read, records, session_id, state, turns
from .base import Box, Control, Credential, Harness, Interruption, Join, Keys, Launch, Screen, ship_template, trimmed
from .limits import Usage

STATE_DIR = Path.home() / ".gemini" / "antigravity-cli"
BEFORE = "conversations.before"  # written in the seat home at launch: the conversations that already existed

RULE = re.compile(r"^─{20,}\s*$")
BG_TASK = re.compile(r"^\s*● \[\d{2}:\d{2}:\d{2}\] .+$")  # a background task's line, drawn in a box of its own
STATUS_LINE = re.compile(r"(?:^|\s{2,})([A-Z]\w*(?: [\w().-]+)*) · (low|medium|high|xhigh|max)(?: · \d+ task\(s\) · /tasks)?\s*$")
WORKING = re.compile(r"esc to cancel|^[⠁-⣿]\s+\w+ing", re.M)
QUEUED_PLACEHOLDER = "Press up to edit queued messages"
QUEUED_LINE = re.compile(r"^▸ (.*?)\s*\(alt\+m to send immediately\)\s*$")


class AntigravityHarness(Harness):
    human_tool_policy = "Antigravity: no supported native question-tool exclusion proved; interactive asks still need a human."
    name = "antigravity"
    native_provider = "google"
    program = "agy"
    home_variable = None  # no home switch: the seat runs in the program's own state directory
    reads_usage = True
    usage_interval = 300
    background_usage = True
    headline_window = ("Gemini 4 Argon", "7d")
    screen_walls = True
    record_limits = ("cost is unknown unless the record states it; effort and child-turn counts are not exposed; "
                     "a step without a completion time leaves its duration unknown")
    keys = Keys(submit="Enter", send_now=("M-m",), interrupt="Escape")

    def usage(self, pool: Pool) -> Usage:
        if pool.config_dir is not None:
            return replace(unknown("a Google account directory is not read; /usage reads the sign-in the program itself holds",
                                   datetime.now(timezone.utc)), called=False)
        return fetch_usage()

    def shared_usage_key(self, pool: Pool) -> tuple | None:
        return (self.name, "default-sign-in") if pool.config_dir is None else None

    def quota_wall(self, text: str, at: datetime):
        from .antigravity_quota import wall
        return wall(text, "individual-quota", at)

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        """Our settings for the session, passed on the command line; nothing of the program's own is written."""
        home.mkdir(parents=True, exist_ok=True)
        settings = {"trustedWorkspaces": [str(seat.cwd)] if seat.cwd else []}
        settings = {**ship_template(ship_home, "settings.json"), **settings}
        settings.update(seat.settings)
        (home / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
        return None

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        argv = [self.program, "--dangerously-skip-permissions", "--settings", str(home / "settings.json")]
        if seat.model:
            argv += ["--model", seat.model]
        if seat.effort:
            argv += ["--effort", seat.effort]
        if resume:
            argv += ["--conversation", resume]
        opening = "\n\n".join(filter(None, [brief.read_text().strip() if not resume else None, first]))
        if opening:
            argv += ["--prompt-interactive", opening]
        if not resume:  # a new conversation is about to appear: note which ones are there already
            (home / BEFORE).write_text("".join(f"{p.name}\n" for p in sorted(self._conversations())))
        return Launch(tuple(argv), {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"})

    # -- record: the one conversation that appeared after the launch ---------------------------

    @staticmethod
    def _conversations() -> list[Path]:
        return [p for p in (STATE_DIR / "conversations").glob("*.db") if p.is_file()]

    def _appeared(self, home: Path) -> list[Path]:
        """The conversations the shared folder gained since the launch noted what was there."""
        noted = set((home / BEFORE).read_text().split()) if (home / BEFORE).is_file() else set()
        return sorted((p for p in self._conversations() if p.name not in noted), key=lambda p: p.stat().st_mtime)

    def join(self, home: Path, launched_at: datetime, excluded: tuple[Path, ...] = ()) -> Join:
        """The one conversation that appeared since the launch; two are reported, never guessed."""
        appeared = [p for p in self._appeared(home) if p not in excluded]
        if not appeared:
            return Join(None, "no record yet")
        if len(appeared) > 1:
            return Join(None, f"ambiguous: {', '.join(p.name for p in appeared)} appeared since the launch")  # law: one-record
        return Join(appeared[0])

    inputs = staticmethod(inputs)
    records = staticmethod(records)
    first_timestamp = staticmethod(first_timestamp)
    session_id = staticmethod(session_id)
    reader = staticmethod(read)

    # -- screen ---------------------------------------------------------------------------

    def screen(self, text: str) -> Screen:
        lines = text.rstrip().split("\n")
        rules = [i for i, line in enumerate(lines) if RULE.match(line)]
        box, queued = None, 1 if QUEUED_PLACEHOLDER in text else 0  # the placeholder sits on the status line here
        top = rules[-2] if len(rules) >= 2 else len(lines)
        if len(rules) >= 2:
            top, bottom = rules[-2], rules[-1]
            tasks = lines[top + 1:bottom]
            if len(rules) >= 3 and tasks and all(BG_TASK.match(line) for line in tasks):
                top, bottom = rules[-3], rules[-2]  # the lowest box holds background tasks; the input box is above it
            span = lines[top + 1:bottom]
            if span and (span[0].rstrip() == ">" or span[0].startswith("> ")):
                body = [line[2:] if line.startswith(("> ", "  ")) else line.lstrip(">") for line in span]
                body = trimmed(body)
                box = "\n".join(body)
                if box.rstrip(" ") == QUEUED_PLACEHOLDER:
                    box, queued = "", max(queued, 1)
        status = STATUS_LINE.search(lines[-1]) if lines else None
        shows = {"model": status.group(1).strip(), "effort": status.group(2)} if status else {}
        return Screen(text, box=box, working=bool(WORKING.search(text)), interruption=self.recognize(text) if box is None else None,
                      queued=queued, shows=shows, echoes=self.echoes(lines[:top]), quota=banner(text))

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
            elif block is not None and (line.startswith("  ") or not line.strip()) and not line.lstrip().startswith("⎿"):
                block.append(line[2:] if line.startswith("  ") else "")  # a paragraph break continues the echo; ⎿ is the program's own line
            else:
                block = None
        return tuple("\n".join(b).strip("\n") for b in found)

    def box(self, text: str) -> Box:
        """A long paste folds two ways (1.5.2): one of few lines to ``[Pasted text #N M chars]``, M the length less
        one trailing newline; one of many lines to ``[Pasted text #N +k lines]``, k the newlines. The box takes either."""
        chars, newlines = len(text.removesuffix("\n")), text.count("\n")
        return Box(text, folded=rf"\[Pasted text #\d+ (?:{chars} chars|\+{newlines} lines)\]")

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
