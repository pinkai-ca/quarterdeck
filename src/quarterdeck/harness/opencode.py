"""OpenCode: a SQLite database per launch, ``data-<n>/opencode/opencode.db`` in the seat home.

OpenCode keeps every session of a data directory in one database, so the seat home gives each
launch a data directory of its own, named by ``XDG_DATA_HOME``, and the record is the database
written there; a resumed launch names the directory holding the session it continues. The
``session`` table dates the record; the ``message`` table holds the turn structure, a user message
and the assistant messages that answer it, each with its model, tokens, cost and completion time;
the ``part`` table holds the text, the tool calls with their timing and exit, and the step
boundaries. The database is read from a snapshot of its bytes and its write-ahead log, never
opened in place (law: one-folder). Proven on 1.19.5 against a local vLLM endpoint.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .. import endpoint
from ..ship import Pool, SeatSpec, ShipError
from .base import (Box, Control, Credential, Harness, Interruption, Keys, Launch, RecordError, Screen, State, Tokens,
                   ToolCall, Turn, merged, program_version)

from .opencode_record import (DATA_PREFIX, RECORD, _Rows, _launch_number, data_dir_of, first_timestamp,
                              inputs, read, records, session_id, state, turns)

CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "autoupdate": False,  # versions change when the operator says, and the status table says so
    "share": "disabled",  # a seat's conversation is the ship's, never a public link
}


# -- the adapter --------------------------------------------------------------------------------

BAR_TEXT = re.compile(r"^\s*┃  (.*)$")
BAR_BARE = re.compile(r"^\s*┃\s*$")
STATUS = re.compile(r"^\s*┃  (\S+) (\S+) · (.+?)\s*$")  # "Build auto · Qwen3.8 27B local-a": the agent, the permission mode, the model and pool as shown
RULE = re.compile(r"^\s*╹▀{3,}")
PLACEHOLDER = re.compile(r'^Ask anything(?:…|\.\.\.) "[^"]*"\s*$')  # a fresh box's hint, with a question that varies
WORKING = re.compile(r"\besc interrupt\b")
QUEUED_TAG = re.compile(r"^\s*┃\s+QUEUED\s*$")
COMPACT = re.compile(r"ctrl\+p cmd\s*$", re.M)


def _bar(line: str) -> str | None:
    """The text of one box row; the empty string for a bare bar; None for a line that is not a bar."""
    if BAR_BARE.match(line):
        return ""
    found = BAR_TEXT.match(line)
    return found.group(1) if found else None


class OpenCodeHarness(Harness):
    """OpenCode, proven on 1.19.5. Runs on an endpoint pool; the model changes only at launch."""

    name = "opencode"
    home_variable = "XDG_DATA_HOME"
    keys = Keys(submit="Enter", send_now=None, interrupt="Escape Escape", empty=None)  # Escape once opens nothing; twice, quickly, interrupts
    brief_channel = "instructions"

    def accepts(self, pool: Pool) -> str | None:
        if pool.kind != "endpoint":
            return f"pool {pool.name} names no endpoint, and {self.name} runs on an endpoint pool: declare endpoint = \"http://host:port/v1\""
        return None

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        home.mkdir(parents=True, exist_ok=True)
        runtime = home.parents[2]  # runtime/seats/<seat>/home
        choice = endpoint.choose(pool, seat.model)
        secret = endpoint.key(pool)
        provider = {
            "npm": "@ai-sdk/openai-compatible",
            "name": pool.name,
            "options": {"baseURL": pool.endpoint, "apiKey": f"{{env:{endpoint.KEY_VARIABLE}}}" if secret else "local"},
            "models": {choice.model: {"name": choice.model, "tool_call": True, "limit": {"context": choice.context, "output": choice.output}}},
        }
        config = {**CONFIG, "model": f"{pool.name}/{choice.model}", "provider": {pool.name: provider},
                  "instructions": [str(runtime / "brief" / f"{seat.name}.md")]}  # the brief, and no instruction file of the user's
        template = ship_home / "opencode.json" if ship_home else None
        layered = json.loads(template.read_text()) if template and template.is_file() else {}
        config = merged(merged(config, layered), seat.settings)
        folder = home / "config" / "opencode"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "opencode.json").write_text(json.dumps(config, indent=2) + "\n")
        choice.write(home)
        return Credential(variable=endpoint.KEY_VARIABLE) if secret else None

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        choice = endpoint.Choice.read(home)
        data = self._data_dir(home, resume, seat.name)
        argv = ["opencode", "--auto", "--model", f"{pool.name}/{choice.model}"]  # --auto: no approval prompt (law: can-post)
        if resume:
            argv += ["--session", resume]
        if first:
            argv += ["--prompt", first]
        env = {"XDG_CONFIG_HOME": str(home / "config"), self.home_variable: str(data), "XDG_CACHE_HOME": str(home / "cache"),
               "XDG_STATE_HOME": str(home / "state"), "OPENCODE_DISABLE_AUTOUPDATE": "1", **endpoint.key(pool)}
        return Launch(tuple(argv), env)

    @staticmethod
    def _data_dir(home: Path, resume: str | None, seat_name: str) -> Path:
        """A fresh launch gets the next numbered data directory; a resumed one, the directory holding its session."""
        existing = records(home)
        if resume:
            for record in reversed(existing):
                try:
                    if any(row[0] == resume for row in _Rows(record).sessions):
                        return data_dir_of(record)
                except OSError:
                    continue
            raise ShipError(f"{seat_name}: no record in {home} holds the session {resume}; qd fresh {seat_name} starts a new one")
        number = max((_launch_number(r) for r in existing), default=0) + 1
        return home / f"{DATA_PREFIX}{number:03d}"

    def version(self) -> str | None:
        return program_version(("opencode", "--version"))

    records = staticmethod(records)
    first_timestamp = staticmethod(first_timestamp)
    session_id = staticmethod(session_id)
    state = staticmethod(state)
    turns = staticmethod(turns)
    read = staticmethod(read)

    def screen(self, text: str) -> Screen:
        """The box is the bar block straight above the status row, a pad row above and below its text."""
        lines = text.rstrip("\n").split("\n")
        rule_at = next((i for i in range(len(lines) - 1, 0, -1) if RULE.match(lines[i]) and STATUS.match(lines[i - 1])), None)
        working = bool(WORKING.search(text))
        queued = sum(1 for line in lines if QUEUED_TAG.match(line))
        interruption = self.recognize(text)
        if rule_at is None or interruption is not None:
            unknown = None
            if rule_at is None and interruption is None:
                unknown = "the compact layout: widen the pane" if COMPACT.search(text) else "no box drawn as 1.19.5 draws it"
            return Screen(text, box=None, working=working, interruption=interruption, unknown=unknown, queued=queued)
        status_at = rule_at - 1
        i = status_at - 1
        while i >= 0 and _bar(lines[i]) is not None and not QUEUED_TAG.match(lines[i]):
            i -= 1
        top = i + 1
        if i >= 0 and QUEUED_TAG.match(lines[i]) and top < status_at and _bar(lines[top]) == "":
            top += 1  # the queued block's own closing pad
        rows = [_bar(line) for line in lines[top:status_at]]
        if len(rows) < 2 or rows[0] != "" or rows[-1] != "":
            return Screen(text, box=None, working=working, unknown="no box drawn as 1.19.5 draws it", queued=queued)
        body = [row or "" for row in rows[1:-1]]
        while body and not body[-1].strip():
            body.pop()
        box = "" if len(body) == 1 and PLACEHOLDER.match(body[0]) else "\n".join(body)
        return Screen(text, box=box, working=working, queued=queued)

    def box_columns(self, pane_width: int) -> int | None:
        return pane_width - 7  # two cells of margin, the bar, two more cells, and a margin at the right

    def box(self, text: str) -> Box:
        """Probed on 1.19.5: a paste of three lines or more, or a long one, folds to a count of its lines."""
        lines = text.rstrip("\n").count("\n") + 1
        return Box(text, folded=rf"\[Pasted ~{lines} lines\]")

    def controls(self) -> tuple[Control, ...]:
        return (Control("model", launch="--model <pool>/<value>", session=None, confirmed_by="record"),)

    def interruptions(self) -> tuple[Interruption, ...]:
        return (Interruption("model picker", r"┃\s*/models\s+Switch model", key="Escape"),)
