"""pi: one JSON-lines file per session under ``sessions/`` in the seat home.

The first line is the session, with its id and working directory; ``model_change`` and
``thinking_level_change`` lines carry the model and the thinking level; ``message`` lines carry the
conversation: a user message, the assistant messages that answer it, each with its model, usage,
cost and stop reason, and the tool results between them. A message typed while a turn runs is
steered into it after the current tool call, and enters the record only then. Proven on 0.74.2
against a local vLLM endpoint.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .. import endpoint
from ..pricing import Price, amount
from .endpoint import EndpointHarness
from .costs import CostStep, costed, model_name
from .json_record import JsonLinesReader

from ..ship import Pool, SeatSpec, ShipError
from .base import (Box, ship_template, trimmed, Input, Control, Credential, Harness, Interruption, Keys, Launch, RecordError, Screen, State, Tokens,
                   ToolCall, Turn, entries, merged, program_version, when)

SETTINGS = {
    "defaultProjectTrust": "always",  # the workspace is the ship's: no trust dialog
    "quietStartup": True,
    "enableInstallTelemetry": False,
}
UNMEASURED_STOPS = {"aborted", "error"}
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")


# -- the record ---------------------------------------------------------------------------------


def records(home: Path) -> list[Path]:
    folder = home / "sessions"
    return sorted(p for p in folder.glob("*.jsonl") if p.is_file()) if folder.is_dir() else []


def _session(record: Path) -> dict | None:
    return next((e for e in entries(record) if e.get("type") == "session"), None)


def first_timestamp(record: Path) -> datetime | None:
    session = _session(record)
    return when(session.get("timestamp")) if session else next((when(e["timestamp"]) for e in entries(record) if e.get("timestamp")), None)


def session_id(record: Path) -> str | None:
    session = _session(record)
    return session.get("id") if session else None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content or [] if isinstance(block, dict) and block.get("type") == "text")


def _tokens(message: dict) -> Tokens:
    usage = message.get("usage")
    if isinstance(usage, dict) and message.get("stopReason") in UNMEASURED_STOPS and not usage.get("totalTokens"):
        return Tokens.unknown()  # the program initializes an aborted step with zero counters before any response
    return Tokens.from_usage(usage, ("cacheRead", "input", "cacheWrite", "output"), total="totalTokens")


class _Reader(JsonLinesReader):
    kinds = ('session', 'message', 'model_change')
    label = "pi"

    def take(self, entry: dict) -> None:
        kind, at = entry.get("type"), when(entry.get("timestamp"))
        if kind == "model_change":
            self.model = entry.get("modelId") or self.model
        elif kind == "thinking_level_change":
            self.effort = entry.get("thinkingLevel") or self.effort
        elif kind == "message" and isinstance(entry.get("message"), dict):
            self.message(entry["message"], at)

    def message(self, message: dict, at: datetime | None) -> None:
        role = message.get("role")
        if role == "user":
            text = _text(message.get("content"))
            self.last_input, self.last_input_at = text, at
            if self.open is not None:
                self.consumed.append(text)  # steered into the running turn after its current tool call
            else:
                self.start_turn({"started": at, "input": text, "tokens": Tokens(), "tools": [], "reply": "", "costs": []})
        elif role == "assistant":
            if self.open is None:
                self.start_turn({"started": at, "input": "", "tokens": Tokens(), "tools": [], "reply": "", "costs": []})
            step_model = model_name(message.get("model"))
            self.model = step_model or self.model
            tokens = _tokens(message)
            self.open["tokens"] = self.open["tokens"] + tokens
            usage = message.get("usage")
            costs = usage.get("cost") if isinstance(usage, dict) else None
            cost = costs.get("total") if isinstance(costs, dict) else None
            self.open["costs"].append(CostStep(step_model, tokens, amount(cost)))
            for block in message.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    self.open["reply"] = block["text"]
                elif block.get("type") == "toolCall":
                    name = " ".join(str(block.get("name") or "").split()) or "?"  # a malformed call may carry a name with line breaks
                    self.pending[block.get("id")] = (name, at)
            if message.get("stopReason") != "toolUse":
                self.end(at)
        elif role == "toolResult":
            self.close_tool(message.get("toolCallId"), at, bool(message.get("isError")), message.get("toolName") or "?")

    def end(self, at: datetime | None) -> None:
        turn = self.close_turn()
        self.turns.append(costed(Turn(len(self.turns), turn["started"] or at, at or turn["started"], turn["input"], turn["reply"],
                                     self.model, turn["tokens"], tuple(turn["tools"]), cost_steps=tuple(turn["costs"]))))

state = _Reader.read_state
turns = _Reader.read_turns
read = _Reader.read


def inputs(record: Path) -> list[Input]:
    """Every user message with the time it entered the record, for the delivery receipt."""
    found = []
    opened = False
    for entry in entries(record):
        try:
            message = entry.get("message")
            if entry.get("type") == "message" and isinstance(message, dict) and message.get("role") == "user":
                at = when(entry.get("timestamp"))
                found.append(Input(_text(message.get("content")), at, "consumed" if opened else "accepted", at if opened else None))
                opened = True
            elif entry.get("type") == "message" and isinstance(message, dict) and message.get("role") == "assistant":
                opened = message.get("stopReason") == "toolUse"
        except (TypeError, ValueError, AttributeError, KeyError):
            continue
    return found


# -- the adapter --------------------------------------------------------------------------------

RULE = re.compile(r"^─{20,}\s*$")
# "↑1.6k ↓19 1.6%/98k (auto)    qwen3.8-27b • medium"; a model declared without reasoning shows no thinking level
FOOTER = re.compile(r"^(?P<stats>\S.*?)\s{2,}(?P<model>\S+)(?: • (?P<level>\w+))?\s*$")
WORKING = re.compile(r"^\s*[⠁-⣿] Working\.\.\.", re.M)
QUEUED_HINT = "Alt+Up to edit all queued messages"
STEERING = re.compile(r"^\s*Steering: (.*?)\s*$", re.M)


class PiHarness(EndpointHarness):
    """pi, proven on 0.74.2. Runs on an endpoint pool; model and thinking level change only at launch."""

    name = "pi"
    program = "pi"
    home_variable = "PI_CODING_AGENT_DIR"
    keys = Keys(submit="Enter", send_now=None, interrupt="Escape")
    wrap_slack = 1  # probed on 0.74.2: a word wraps one column short of where a word without spaces breaks

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        agent, sessions = home / "agent", home / "sessions"
        agent.mkdir(parents=True, exist_ok=True)
        sessions.mkdir(parents=True, exist_ok=True)
        if seat.effort and seat.effort not in THINKING_LEVELS:
            raise ShipError(f"{seat.name}: effort {seat.effort!r} is not a pi thinking level; the levels are {', '.join(THINKING_LEVELS)}")
        choice, secret = self.endpoint_home(home, seat, pool)
        model = {"id": choice.model, "name": choice.model, "reasoning": seat.effort is not None, "input": ["text"],
                 "contextWindow": choice.context, "maxTokens": choice.output}
        provider = {"baseUrl": pool.endpoint, "api": "openai-completions",
                    "apiKey": endpoint.KEY_VARIABLE if secret else "local",  # a variable's name, read by the program; never the key
                    "models": [model]}
        models = {"$schema": "https://pi.dev/schemas/models.schema.json", "providers": {pool.name: provider}}
        layered = ship_template(ship_home, "models.json")
        custom = layered.get("providers", {}).get(pool.name, {}).get("models", [])
        matching = [m for m in custom if isinstance(m, dict) and m.get("id") == choice.model]
        if len(matching) > 1:
            raise ShipError(f"pool {pool.name}: template repeats the selected model; price target is ambiguous")
        if matching:
            # The generic list merge appends. Merge this known model identity instead
            # of generating two conflicting definitions for the program to choose from.
            provider["models"] = []
            matching[0].update(merged(model, matching[0]))
        models = merged(models, layered)
        price = Price.parse(pool.price) if pool.price is not None else None
        selected = [m for m in models["providers"][pool.name]["models"] if m["id"] == choice.model]
        if len(selected) != 1:
            raise ShipError(f"pool {pool.name}: generated model does not identify one price target")
        selected[0].pop("cost", None)  # omission is supported; null rates are not
        if price:
            selected[0]["cost"] = {"input": price.input, "output": price.output,
                                    "cacheRead": price.cache_read, "cacheWrite": price.cache_write}
        settings = merged(SETTINGS, ship_template(ship_home, "settings.json"))
        settings = merged(settings, seat.settings)
        (agent / "models.json").write_text(json.dumps(models, indent=2) + "\n")
        (agent / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
        return self.endpoint_ready(home, choice, secret, price)

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        choice = endpoint.Choice.read(home)
        argv = [self.program, "--provider", pool.name, "--model", choice.model, "--session-dir", str(home / "sessions"),
                "--append-system-prompt", str(brief), "--no-context-files",  # the brief, and no instruction file of the user's
                "--no-extensions", "--no-skills", "--no-prompt-templates"]  # nothing of the project's runs or instructs a seat
        if seat.effort:
            argv += ["--thinking", seat.effort]
        if resume:
            record = next((r for r in reversed(records(home)) if session_id(r) == resume), None)
            if record is None:
                raise ShipError(f"{seat.name}: no record in {home} holds the session {resume}; qd fresh {seat.name} starts a new one")
            argv += ["--session", str(record)]
        if first:
            argv.append(first)
        env = {self.home_variable: str(home / "agent"), "PI_SKIP_VERSION_CHECK": "1", **endpoint.key(pool)}
        return Launch(tuple(argv), env, choice.price)

    inputs = staticmethod(inputs)
    records = staticmethod(records)
    first_timestamp = staticmethod(first_timestamp)
    session_id = staticmethod(session_id)
    reader = staticmethod(read)

    def screen(self, text: str) -> Screen:
        """The box is between two rules above the working directory and the footer with the model and thinking level."""
        lines = text.rstrip("\n").split("\n")
        working = bool(WORKING.search(text))
        queued = 1 if QUEUED_HINT in text else 0
        echoes = tuple(m.group(1) for m in STEERING.finditer(text))
        footer_at = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)  # the program draws top-down: rows below it stay blank
        footer = FOOTER.match(lines[footer_at]) if footer_at is not None else None
        shows = {}
        if footer:
            shows = {"model": footer.group("model"), **({"effort": footer.group("level")} if footer.group("level") else {})}
        interruption = self.recognize(text)
        if footer is None or interruption is not None:
            unknown = None if footer or interruption else "no footer drawn as 0.74.2 draws it"
            return Screen(text, box=None, working=working, interruption=interruption, unknown=unknown, queued=queued, shows=shows, echoes=echoes)
        bottom = next((i for i in range(footer_at - 1, -1, -1) if RULE.match(lines[i])), None)
        if bottom is None or footer_at - bottom > 3:  # the working directory sits between the bottom rule and the footer
            return Screen(text, box=None, working=working, unknown="no box drawn as 0.74.2 draws it", queued=queued, shows=shows, echoes=echoes)
        top = next((i for i in range(bottom - 1, -1, -1) if RULE.match(lines[i])), None)
        if top is None:
            return Screen(text, box=None, working=working, unknown="no box drawn as 0.74.2 draws it", queued=queued, shows=shows, echoes=echoes)
        body = lines[top + 1:bottom]
        body = trimmed(body)
        return Screen(text, box="\n".join(body), working=working, queued=queued, shows=shows, echoes=echoes)

    def box_columns(self, pane_width: int) -> int | None:
        return pane_width - 1  # the box spans the pane from the first column; the last column stays clear

    def box(self, text: str) -> Box:
        """Probed on 0.74.2: a short paste shows whole; a long one folds to its count of lines, or of characters when few."""
        return Box(text, folded=rf"\[paste #\d+ (?:\+{text.count(chr(10)) + 1} lines|{len(text)} chars)\]")

    def controls(self) -> tuple[Control, ...]:
        return (
            Control("model", launch="--model <value>", session=None, confirmed_by="record"),
            Control("effort", launch="--thinking <value>", session=None, confirmed_by="record"),
        )

    def interruptions(self) -> tuple[Interruption, ...]:
        return (Interruption("model picker", r"Only showing models from configured providers", key="Escape"),)
