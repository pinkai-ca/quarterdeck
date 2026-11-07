"""The fake harness's records, written in the shape of each real program.

The control plane reads them with the real adapters' readers, so the fake drill exercises
the same code the real seats are read with.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .harness import claude, codex


def now() -> datetime:
    return datetime.now(timezone.utc)


def tokens_for(text: str, reply: str, turn: int) -> dict[str, int]:
    """Deterministic numbers in the four classes, so a test can predict the Ledger."""
    return {
        "read_uncached": len(text) // 4 + 100,
        "read_cached": 1000 * (turn + 1),
        "cache_written": 200,
        "written": len(reply) // 4 + 50,
    }


class _Writer:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, entry: dict) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")


class ClaudeShape(_Writer):
    """``projects/<slug>/<session>.jsonl``: an entry per step, usage on every assistant entry."""

    def __init__(self, home: Path, cwd: Path, session: str, model: str):
        slug = str(cwd).replace("/", "-")
        super().__init__(home / "projects" / slug / f"{session}.jsonl")
        self.session, self.cwd, self.model = session, str(cwd), model
        self.parent: str | None = None
        self.turn = len(claude.turns(self.path)) if self.path.exists() else 0
        self.text = self.effort = None

    def entry(self, kind: str, message: dict, **extra) -> None:
        uid = str(uuid.uuid4())
        self.write({"type": kind, "uuid": uid, "parentUuid": self.parent, "sessionId": self.session,
                    "timestamp": now().isoformat(), "cwd": self.cwd, "version": "fake", "isSidechain": False,
                    "message": message, **extra})
        self.parent = uid

    def begin(self, text: str, model: str, effort: str) -> None:
        self.text, self.model, self.effort = text, model, effort
        self.entry("user", {"role": "user", "content": text})

    def assistant(self, blocks: list[dict], stop: str, reply: str = "") -> None:
        t = tokens_for(self.text or "", reply, self.turn)
        usage = {"input_tokens": t["read_uncached"], "cache_creation_input_tokens": t["cache_written"],
                 "cache_read_input_tokens": t["read_cached"], "output_tokens": t["written"]}
        message = {"id": f"msg_{uuid.uuid4().hex[:12]}", "role": "assistant", "model": self.model,
                   "content": blocks, "stop_reason": stop, "usage": usage}
        self.entry("assistant", message, effort=self.effort)

    def tool_start(self, name: str, input_: str) -> None:
        self.call = f"toolu_{uuid.uuid4().hex[:12]}"
        self.assistant([{"type": "tool_use", "id": self.call, "name": name, "input": {"command": input_}}], "tool_use")

    def tool_end(self, output: str, failed: bool) -> None:
        self.entry("user", {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": self.call, "content": output, "is_error": failed}]})

    def tool(self, name: str, input_: str, output: str, failed: bool) -> None:
        self.tool_start(name, input_)
        self.tool_end(output, failed)

    def end(self, reply: str) -> None:
        self.assistant([{"type": "text", "text": reply}], "end_turn", reply)
        self.turn += 1

    def abort(self) -> None:
        self.entry("user", {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]})
        self.turn += 1

    def queued(self, text: str) -> None:
        self.write({"type": "queue-operation", "operation": "enqueue", "content": text,
                    "sessionId": self.session, "timestamp": now().isoformat()})

    def command(self, text: str, output: str) -> None:
        name, _, args = text[1:].partition(" ")
        self.entry("user", {"role": "user", "content":
                   f"<command-name>/{name}</command-name>\n<command-message>{name}</command-message>\n<command-args>{args}</command-args>"})
        self.entry("user", {"role": "user", "content": f"<local-command-stdout>{output}</local-command-stdout>"})

    def turns_before(self) -> int:
        return self.turn


class CodexShape(_Writer):
    """``sessions/Y/M/D/rollout-<time>-<id>.jsonl``: events and response items with a payload."""

    def __init__(self, home: Path, cwd: Path, session: str, model: str):
        stamp = now()
        existing = [p for p in codex.records(home) if p.name.endswith(f"{session}.jsonl")]
        path = existing[0] if existing else (home / "sessions" / stamp.strftime("%Y/%m/%d")
                                             / f"rollout-{stamp.strftime('%Y-%m-%dT%H-%M-%S')}-{session}.jsonl")
        super().__init__(path)
        self.session, self.cwd, self.model = session, str(cwd), model
        self.turn_id = ""
        self.total = {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                      "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0}
        self.started = now()
        self.text = ""
        if not existing:
            self.line("session_meta", {"id": session, "timestamp": stamp.isoformat(), "cwd": self.cwd,
                                       "originator": "qd-fakeharness", "cli_version": "fake", "model_provider": "fake"})

    def line(self, kind: str, payload: dict) -> None:
        self.write({"timestamp": now().isoformat(), "type": kind, "payload": payload})

    def begin(self, text: str, model: str, effort: str) -> None:
        self.text, self.model, self.turn_id, self.started = text, model, str(uuid.uuid4()), now()
        self.line("turn_context", {"turn_id": self.turn_id, "cwd": self.cwd, "model": model, "effort": effort})
        self.line("event_msg", {"type": "task_started", "turn_id": self.turn_id, "model_context_window": 258400})
        self.line("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]})

    def count(self, reply: str = "") -> None:
        t = tokens_for(self.text, reply, len(codex.turns(self.path)))
        last = {"input_tokens": t["read_uncached"] + t["read_cached"], "cached_input_tokens": t["read_cached"],
                "cache_write_input_tokens": t["cache_written"], "output_tokens": t["written"],
                "reasoning_output_tokens": 0, "total_tokens": sum(t.values())}
        self.total = {k: self.total[k] + last[k] for k in self.total}
        self.line("event_msg", {"type": "token_count", "info": {"total_token_usage": dict(self.total),
                                                                "last_token_usage": last}})

    def tool_start(self, name: str, input_: str) -> None:
        self.call = f"call_{uuid.uuid4().hex[:12]}"
        self.line("response_item", {"type": "function_call", "name": "shell" if name == "Bash" else name,
                                    "arguments": json.dumps({"command": input_}), "call_id": self.call})
        self.count()

    def tool_end(self, output: str, failed: bool) -> None:
        self.line("response_item", {"type": "function_call_output", "call_id": self.call,
                                    "output": json.dumps({"output": output, "metadata": {"exit_code": int(failed)}})})

    def tool(self, name: str, input_: str, output: str, failed: bool) -> None:
        self.tool_start(name, input_)
        self.tool_end(output, failed)

    def end(self, reply: str) -> None:
        self.line("response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": reply}]})
        self.count(reply)
        ms = int((now() - self.started).total_seconds() * 1000)
        self.line("event_msg", {"type": "task_complete", "turn_id": self.turn_id, "last_agent_message": reply, "duration_ms": ms})

    def abort(self) -> None:
        self.line("event_msg", {"type": "turn_aborted", "turn_id": self.turn_id, "reason": "interrupted"})

    def queued(self, text: str) -> None:
        pass

    def command(self, text: str, output: str) -> None:
        pass

    def turns_before(self) -> int:
        return len(codex.turns(self.path))
