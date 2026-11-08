"""Claude Code: the record in ``projects/<slug>/<session>.jsonl`` under its home.

An entry per step. A typed prompt is a ``user`` entry whose content is a string; a tool
result is a ``user`` entry whose content lists ``tool_result`` blocks. Each ``assistant``
entry carries the model, the usage of its API message (repeated on every entry of the
same message) and the stop reason that ends a turn. Queued messages appear as
``queue-operation`` entries the moment they are queued.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .json_record import JsonLinesReader

from ..ship import Pool, SeatSpec
from .base import (Box, ship_template, trimmed, Input, Control, Credential, Harness, Interruption, Keys, Launch, RecordError, Screen, State, Tokens,
                   ToolCall, Turn, Usage, Window, credential, entries, key_environment, merged, program_version, same_name, when)

ENDS_TURN = {"end_turn", "max_tokens", "stop_sequence"}
LOCAL_TAGS = ("<command-name>", "<local-command", "<bash-input>", "<bash-stdout>", "<bash-stderr>")
INTERRUPTED = "[Request interrupted"
PASTED = re.compile(r'^\s*<pasted_content id="[^"]*">\n?(.*?)\n?</pasted_content(?: id="[^"]*")?>\s*$', re.S)


def unwrap(text: str) -> str:
    """A long paste is recorded inside ``<pasted_content>`` tags, queued or submitted, with blank lines around
    them when submitted; the message is what is inside."""
    match = PASTED.match(text)
    return match.group(1) if match else text


def records(home: Path) -> list[Path]:
    files = [p for p in (home / "projects").glob("*/*.jsonl") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def first_timestamp(record: Path) -> datetime | None:
    return next((when(e["timestamp"]) for e in entries(record) if e.get("timestamp")), None)


def session_id(record: Path) -> str | None:
    return next((e["sessionId"] for e in entries(record) if e.get("sessionId")), None)


def prompt_text(entry: dict) -> str | None:
    """The text of a typed prompt, or None for a tool result or a local command's echo."""
    if entry.get("type") != "user" or entry.get("isSidechain"):
        return None
    content = entry.get("message", {}).get("content")
    if isinstance(content, list):
        if any(block.get("type") == "tool_result" for block in content):
            return None
        content = "\n".join(block.get("text", "") for block in content if block.get("type") == "text")
    if not isinstance(content, str) or content.startswith(LOCAL_TAGS):
        return None
    return unwrap(content)


def inputs(record: Path) -> list[Input]:
    found, queued = [], []
    for row in entries(record):
        if row.get("isSidechain"):
            continue
        try:
            stamp = when(row.get("timestamp"))
            text = prompt_text(row)
            if text is not None:
                found.append(Input(text, stamp))
            if row.get("type") != "queue-operation":
                continue
            text = unwrap(row.get("content") or "")
            operation = row.get("operation")
            matching = next((i for i, item in enumerate(queued) if item.text.strip() == text.strip()), None)
            if operation == "enqueue":
                queued.append(Input(text, stamp, "queued"))  # stale and untimed entries still occupy FIFO positions
            elif operation in ("remove", "dequeue") and matching is not None:
                entry = queued.pop(matching)
                found.append(Input(entry.text, entry.at, "consumed", stamp))
            else:
                queued = [item for item in queued if item.text.strip() != text.strip()]
        except (TypeError, ValueError, AttributeError, KeyError):
            continue
    return found + queued


def usage_tokens(usage: dict) -> Tokens:
    return Tokens(
        read_cached=usage.get("cache_read_input_tokens", 0) or 0,
        read_uncached=usage.get("input_tokens", 0) or 0,
        cache_written=usage.get("cache_creation_input_tokens", 0) or 0,
        written=usage.get("output_tokens", 0) or 0,
    )


class _Reader(JsonLinesReader):
    kinds = ('assistant', 'queue-operation', 'system', 'user')
    label = "Claude Code"

    def __init__(self, record: Path):
        self.sidechain_ends = set()
        self.subagent_turns = 0
        super().__init__(record)

    def take(self, entry: dict) -> None:
        kind = entry.get("type")
        if kind == "queue-operation":
            self.queue(entry)
        elif entry.get("isSidechain"):
            message = entry.get("message", {})
            if kind == "assistant" and message.get("stop_reason") in ENDS_TURN and message.get("id") not in self.sidechain_ends:
                self.sidechain_ends.add(message.get("id"))
                self.subagent_turns += 1
        elif kind == "user":
            self.user(entry)
        elif kind == "assistant":
            self.assistant(entry)
        elif kind == "system" and entry.get("subtype") == "turn_duration" and self.open is not None:
            self.end(when(entry.get("timestamp")))  # the one entry that says the turn is over

    def queue(self, entry: dict) -> None:
        """A message queued mid-turn; removed when the program takes it, often into the running turn itself."""
        text = unwrap(entry.get("content") or "")
        if entry.get("operation") == "enqueue":
            self.queued.append(text)
        elif entry.get("operation") in ("remove", "dequeue") and text in self.queued:
            self.queued.remove(text)
            self.consumed.append(text)

    def user(self, entry: dict) -> None:
        text = prompt_text(entry)
        content = entry.get("message", {}).get("content")
        if isinstance(content, str) and content.startswith("<local-command-stdout>"):
            if match := SET_MODEL.search(content):
                self.model = match.group(1)  # the display name, until the next assistant entry names the id
            if match := SET_EFFORT.search(content):
                self.effort = match.group(1)
            return
        if text is not None and text.startswith(INTERRUPTED):
            if self.open is not None:
                self.end(when(entry.get("timestamp")))
            return
        if text is not None:
            self.last_input, self.last_input_at = text, when(entry.get("timestamp"))
            if text in self.queued:
                self.queued.remove(text)
            if self.open is not None:
                self.end(self.last_input_at)  # a prompt while a turn is open ends it: nothing else will
            self.start_turn({"started": self.last_input_at, "input": text, "usage": {}, "tools": [],
                         "reply": "", "model": None, "subagents": self.subagent_turns})
            return
        for block in entry.get("message", {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                self.close_tool(block.get("tool_use_id"), when(entry.get("timestamp")), bool(block.get("is_error")))

    def assistant(self, entry: dict) -> None:
        """One entry per content block, all carrying the message's usage and stop reason.

        No stop reason ends the turn: the program may take a queued message into the same
        turn after an ``end_turn`` block. Only ``turn_duration``, an interruption or the
        next prompt does.
        """
        message = entry.get("message", {})
        self.model = message.get("model") or self.model
        self.effort = entry.get("effort") or self.effort
        if self.open is None:
            return
        self.open["model"] = self.model
        if message.get("usage"):
            self.open["usage"][message.get("id") or entry.get("requestId") or entry.get("uuid")] = message["usage"]
        for block in message.get("content") or []:
            if block.get("type") == "tool_use":
                self.pending[block.get("id")] = (block.get("name", "?"), when(entry.get("timestamp")))
            elif block.get("type") == "text" and block.get("text"):
                self.open["reply"] = (self.open["reply"] + "\n\n" + block["text"]).strip()

    def end(self, at: datetime | None) -> None:
        turn = self.close_turn()
        tokens = sum((usage_tokens(u) for u in turn["usage"].values()), Tokens())
        self.turns.append(
            Turn(
                index=len(self.turns),
                started=turn["started"] or at,
                ended=at or turn["started"],
                input=turn["input"],
                reply=turn["reply"],
                model=turn["model"],
                tokens=tokens,
                tools=tuple(turn["tools"]),
                subagent_turns=self.subagent_turns - turn["subagents"],
            )
        )

state = _Reader.read_state
turns = _Reader.read_turns
read = _Reader.read



# -- the adapter -----------------------------------------------------------------------------

RULE = re.compile(r"^─{10,}")
BOX_START = re.compile(r"^❯[  ]?(.*)$")
MODE_LINE = re.compile(r"[○◐●◑] (\w+) · /effort")
WORKING = re.compile(r"esc to interrupt|^[*✻✶✳✢·⠂⠄⠆] \S+… \(\d+s", re.M)
SET_MODEL = re.compile(r"Set model to `?([^`]+?)`? and saved")
SET_EFFORT = re.compile(r"Set effort level to `?(\w+)`?")
QUEUED_PLACEHOLDER = "Press up to edit queued messages"
SETTINGS = {
    "env": {"DISABLE_AUTOUPDATER": "1"},
    "promptSuggestionEnabled": False,
    "includeCoAuthoredBy": False,
    "autoMemoryEnabled": False,  # a seat keeps nothing of its own between conversations: what must survive is a post or a ship file
    # No approval prompts: a seat runs its program open, and a ship that wants a sandbox asks for one seat by seat. The
    # dialog that asks a person to accept that mode once is answered here, in the settings the program reads it from.
    "skipDangerousModePermissionPrompt": True,
    "permissions": {"allow": ["Bash(qd *)", "Bash(qd:*)"], "defaultMode": "bypassPermissions"},  # law: can-post
}


def pool_account(pool: Pool) -> dict | None:
    """The account record beside a subscription's credential, and nothing else of the pool's state.

    Probed on 2.1.317: a home that does not yet know whose credential it holds opens Remote Control
    only on its second launch, so a seat's state is given the record before its first.
    """
    if pool.config_dir is None:
        return None
    try:
        account = json.loads((pool.config_dir / ".claude.json").read_text()).get("oauthAccount")
    except (OSError, json.JSONDecodeError):
        return None
    return account if isinstance(account, dict) else None


class ClaudeHarness(Harness):
    """Claude Code, proven on 2.1.317."""

    name = "claude"
    program = "claude"
    home_variable = "CLAUDE_CONFIG_DIR"
    keys = Keys(submit="Enter", send_now=("C-x", "C-s"), interrupt="Escape", empty=("C-c",))
    brief_channel = "system-prompt"
    reads_usage = True
    credential_file = ".credentials.json"
    headline_window = (None, "5h")
    remote_control = True

    def freshness(self, path: Path) -> tuple[float, float] | None:
        try:
            oauth = json.loads(path.read_text()).get("claudeAiOauth")
            if isinstance(oauth, dict) and oauth.get("refreshToken"):
                expiry = oauth.get("expiresAt") or 0
                return (float(expiry) / 1000 if expiry > 1e11 else float(expiry), path.stat().st_mtime)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return None

    def grant_expiry(self, path: Path) -> datetime | None:
        try:
            millis = (json.loads(path.read_text()).get("claudeAiOauth") or {}).get("refreshTokenExpiresAt")
            if isinstance(millis, (int, float)):
                return datetime.fromtimestamp(millis / 1000, timezone.utc)
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            pass
        return None

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        home.mkdir(parents=True, exist_ok=True)
        template = ship_template(ship_home, "settings.json")
        settings = merged(merged(SETTINGS, template), seat.settings)  # a ship's or a seat's keys never drop the launch's own
        (home / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
        state_file = home / ".claude.json"
        state = json.loads(state_file.read_text()) if state_file.is_file() else {}
        # The trust dialog and the one that asks a person to accept running without approval prompts, both answered
        # before the first launch: a fresh home opens without a dialog.
        state.update({"hasCompletedOnboarding": True, "bypassPermissionsModeAccepted": True, "theme": state.get("theme", "dark")})
        if account := pool_account(pool):
            state["oauthAccount"] = account  # whose credential this is, known before the first launch
        cwd = str(seat.cwd) if seat.cwd else None
        if cwd:
            state.setdefault("projects", {}).setdefault(cwd, {})["hasTrustDialogAccepted"] = True
        state_file.write_text(json.dumps(state, indent=2) + "\n")
        return credential(pool, home / self.credential_file, "ANTHROPIC_API_KEY")

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        argv = [self.program, "--setting-sources", "user", "--append-system-prompt-file", str(brief), "--name", seat.display]
        if seat.remote_control:
            argv += ["--remote-control", seat.display]  # the same session from the operator's phone, under the seat's name
        if seat.model:
            argv += ["--model", seat.model]
        if seat.effort:
            argv += ["--effort", seat.effort]
        if resume:
            argv += ["--resume", resume]
        if first:
            argv.append(first)
        env = {self.home_variable: str(home), **key_environment(pool, "ANTHROPIC_API_KEY")}
        return Launch(tuple(argv), env)

    inputs = staticmethod(inputs)
    records = staticmethod(records)
    first_timestamp = staticmethod(first_timestamp)
    session_id = staticmethod(session_id)
    reader = staticmethod(read)

    def screen(self, text: str) -> Screen:
        lines = text.rstrip("\n").split("\n")
        box, queued = None, 1 if "ctrl+x ctrl+s to send now" in text else 0
        for index, line in enumerate(lines):
            start = BOX_START.match(line)
            if start and index and RULE.match(lines[index - 1]):
                body = [start.group(1)]
                for following in lines[index + 1:]:
                    if RULE.match(following):
                        break
                    body.append(following[2:] if following.startswith("  ") else following)
                body = trimmed(body)
                box = "\n".join(body).replace(" ", " ")
                if box.strip() == QUEUED_PLACEHOLDER:
                    box, queued = "", max(queued, 1)
                break
        mode = MODE_LINE.search(text)
        interruption = self.recognize(text) if box is None else None  # a dialog replaces the box; prose never counts
        return Screen(text, box=box, working=bool(WORKING.search(text)), interruption=interruption,
                      queued=queued, shows={"effort": mode.group(1)} if mode else {})

    def box(self, text: str) -> Box:
        """A paste folds when it is long or has three newlines or more (2.1.317). The placeholder counts the
        newlines, a trailing one too, and names no count at all for a paste without any."""
        newlines = text.count("\n")
        lines = rf" \+{newlines} lines" if newlines else ""
        return Box(text, folded=rf"\[Pasted text #\d+{lines}\]")

    def controls(self) -> tuple[Control, ...]:
        return (
            Control("model", launch="--model <value>", session="/model {value}", confirmed_by="record"),
            Control("effort", launch="--effort <value>", session="/effort {value}", confirmed_by="screen"),
        )

    def interruptions(self) -> tuple[Interruption, ...]:
        return (
            Interruption("switch-model confirmation", r"Switch model\?", key="Enter"),
            Interruption("login", r"Select login method|Paste code here if prompted|Browser didn't open", key=None),
            Interruption("trust dialog", r"Do you trust the files in this folder\?", key=None),
            Interruption("permission prompt", r"Do you want to proceed\?|Allow once|Allow always", key=None),
        )


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_WINDOWS = {"five_hour": "5h", "seven_day": "7d"}


def usage(pool: Pool) -> Usage | None:
    """A subscription pool's utilization windows from the OAuth usage endpoint; None when it cannot be read."""
    source = pool.config_dir / ".credentials.json" if pool.config_dir else None
    if source is None or not source.exists():
        return None
    try:
        token = json.loads(source.read_text()).get("claudeAiOauth", {}).get("accessToken")
        request = urllib.request.Request(USAGE_URL, headers={
            "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    windows = []
    for key, name in USAGE_WINDOWS.items():
        window = data.get(key) or {}
        if "utilization" in window:
            resets = when(window.get("resets_at")) if window.get("resets_at") else None
            windows.append(Window(name, float(window["utilization"]) / 100.0, resets))
    return Usage(tuple(windows), datetime.now(timezone.utc)) if windows else None


ClaudeHarness.usage = staticmethod(usage)
