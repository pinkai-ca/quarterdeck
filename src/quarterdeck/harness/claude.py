"""Claude Code: the record in ``projects/<slug>/<session>.jsonl`` under its home.

An entry per step. A typed prompt is a ``user`` entry whose content is a string; a tool
result is a ``user`` entry whose content lists ``tool_result`` blocks. Each ``assistant``
entry carries the model, the usage of its API message (repeated on every entry of the
same message) and the stop reason that ends a turn. Queued messages appear as
``queue-operation`` entries the moment they are queued.
"""

from __future__ import annotations

from .base import SetupMetadata
from .capabilities import DefaultMetadata, EffortCapability, SuggestedValue

import copy
import email.utils
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

from .json_record import JsonLinesReader, input_reader, input_rows, records_matching
from .types import AUTHENTICATION, Inputs, TurnFailure
from .claude_background import BackgroundBash, user_notice

from ..ship import Pool, SeatSpec
from .base import (Box, ship_template, trimmed, Input, Control, Credential, Harness, Interruption, Keys, Launch, Screen, Tokens,
                   Turn, Usage, Window, credential, entries, key_environment, merged, when)

ENDS_TURN = {"end_turn", "max_tokens", "stop_sequence"}
LOCAL_TAGS = ("<command-name>", "<local-command", "<bash-input>", "<bash-stdout>", "<bash-stderr>")
INTERRUPTED = "[Request interrupted"
PASTE_FORM_VERSIONS = frozenset({"2.1.317"})  # versions measured to record a paste in the form recorded_paste gives
PASTE_TAG, PASTE_TAG_ESCAPED = "<pasted_content", "<\\pasted_content"  # a literal opening tag, as the record escapes it
INPUT_IGNORED = ('api-request', 'api-request-blob', 'attachment', 'bridge-session', 'custom-title',
                 'agent-name', 'mode', 'permission-mode', 'session-owner', 'atis-latch', 'last-prompt',
                 'file-history-delta', 'file-history-snapshot', 'tools-used', 'api-request-shape', 'cost-state')
PASTED = re.compile(r'^\s*<pasted_content id="[^"]*">\n?(.*?)\n?</pasted_content(?: id="[^"]*")?>\s*$', re.S)


def unwrap(text: str) -> str:
    """A long paste is recorded inside ``<pasted_content>`` tags, queued or submitted, with blank lines around
    them when submitted; the message is what is inside."""
    match = PASTED.match(text)
    return match.group(1) if match else text


def draft_return_text(entry: dict) -> str | None:
    """Only readable whole content can identify a native return-to-draft target."""
    content = entry.get('content')
    text = unwrap(content) if isinstance(content, str) else None
    return text if text is not None and text.strip() else None


def draft_return_slot(slots, text: str | None) -> int | None:
    """An older possible member cannot be skipped to identify a newer equal queued item."""
    if text is not None:
        for index, (item, phase) in enumerate(slots):
            if phase in ('queued', 'pending') and item.strip() == text.strip():
                return index if phase == 'queued' else None
    return None


def records(home: Path) -> list[Path]:
    return records_matching(home, "projects/*/*.jsonl")


def first_timestamp(record: Path) -> datetime | None:
    return next((when(e["timestamp"]) for e in entries(record) if e.get("timestamp")), None)


def session_id(record: Path) -> str | None:
    return next((e["sessionId"] for e in entries(record) if e.get("sessionId")), None)


def prompt_text(entry: dict) -> str | None:
    """Typed text, excluding tool results, local echoes, compaction and measured native notices.

    A compaction writes its summary as a user row marked ``isCompactSummary``; nothing typed it,
    and an idle compaction is followed by no reply, so counting it would open a turn nothing closes.
    """
    if (entry.get("type") != "user" or entry.get("isSidechain")
            or entry.get("isCompactSummary") is True or user_notice(entry)):
        return None
    content = entry.get("message", {}).get("content")
    if isinstance(content, list):
        if any(block.get("type") == "tool_result" for block in content):
            return None
        content = "\n".join(block.get("text", "") for block in content if block.get("type") == "text")
    if not isinstance(content, str) or content.startswith(LOCAL_TAGS):
        return None
    return unwrap(content)


def compactions(record: Path):
    from .compactions import History, read

    def interpret(rows):
        history, seen = History(), {}
        native_turn = None
        for row in rows:
            if 'isSidechain' in row and type(row['isSidechain']) is not bool:
                raise ValueError('unknown root history identity')
            if row.get('isSidechain'):
                continue
            kind = row.get('type')
            if kind not in (*_Reader.kinds, *INPUT_IGNORED):
                raise ValueError('unknown native history')
            key = row.get('uuid')
            if key is not None:
                if not isinstance(key, str) or key in seen:
                    raise ValueError('duplicate native history identity')
                parent = row.get('logicalParentUuid') or row.get('parentUuid')
                if parent is not None and parent not in seen:
                    raise ValueError('missing native parent history')
                if history.conversation is None:
                    if kind != 'user' or prompt_text(row) is None or 'parentUuid' not in row or row['parentUuid'] is not None:
                        raise ValueError('native origin missing')
                    history.conversation = row['sessionId']
                    history.origin = when(row.get('timestamp'))
                    if not isinstance(history.conversation, str) or not history.conversation:
                        raise ValueError('native identity missing')
                if row.get('sessionId') != history.conversation:
                    raise ValueError('mixed native conversation')
                seen[key] = parent
            elif kind in ('user', 'assistant') or row.get('subtype') == 'compact_boundary':
                raise ValueError('native identity missing')
            if kind == 'system' and row.get('subtype') == 'compact_boundary':
                metadata = row.get('compactMetadata')
                if not isinstance(metadata, dict) or metadata.get('trigger') not in ('auto', 'manual'):
                    raise ValueError('unknown compaction boundary')
                history.count += 1
                history.work(row)
            elif kind == 'system' and row.get('subtype') == 'turn_duration':
                ancestor = key
                while ancestor is not None and ancestor != native_turn:
                    ancestor = seen.get(ancestor)
                history.complete = native_turn is not None and ancestor == native_turn
            elif kind == 'user' and prompt_text(row) is not None:
                history.work(row, turn=True)
                native_turn = key
            elif kind == 'queue-operation' and row.get('operation') == 'enqueue':
                history.work(row)
            elif kind == 'assistant':
                for block in row.get('message', {}).get('content', []):
                    if block.get('type') == 'tool_use':
                        history.work(dict(row=key, call=block), call=block.get('id'))
        return history

    return read(record, interpret, _Reader)


@input_reader(lambda row: row.get('type') not in ('assistant', 'system', *INPUT_IGNORED))
def inputs(record: Path) -> list[Input]:
    found, queued = Inputs(), []
    for line, row in input_rows(record, _Reader.kinds, _Reader.label, found):
        kind = row.get('type')
        if kind in INPUT_IGNORED:
            continue  # known non-input metadata does not establish the program format
        if kind not in _Reader.kinds:
            found.skip(line, 'unknown', 'unknown row kind')
            continue
        if row.get("isSidechain"):
            continue
        try:
            stamp = when(row.get("timestamp"))
            if kind == 'user':
                content = row.get('message', {}).get('content')
                if not isinstance(content, (str, list)):
                    raise TypeError
                if isinstance(content, list) and not any(block.get('type') in ('text', 'tool_result') for block in content):
                    found.skip(line, 'user', 'no known prompt text')
                    continue
                if isinstance(content, list) and any(block.get('type') == 'text' and not isinstance(block.get('text'), str) for block in content):
                    raise TypeError
            text = prompt_text(row)
            if text is not None:
                matching = next((i for i, (item, _) in enumerate(queued) if item.text.strip() == text.strip()), None)
                if matching is None:
                    found.append(Input(text, stamp))
                else:
                    entry, phase = queued[matching]
                    if stamp is not None and (entry.at is None or stamp >= entry.at):
                        queued.pop(matching)
                    else:
                        queued[matching] = entry, 'pending' if phase != 'echo' else phase
                    found.append(Input(entry.text, entry.at, 'consumed', stamp))
            if row.get("type") != "queue-operation":
                continue
            operation = row.get("operation")
            if operation == 'popAll':
                text = draft_return_text(row)
                matching = draft_return_slot(((item.text, phase) for item, phase in queued), text)
                if matching is None:
                    found.skip(line, 'queue-operation', 'popAll identity unresolved')
                else:
                    entry, _ = queued[matching]
                    # Native popAllEditable returns this item to the composer draft.
                    # Keep acceptance identity for a later real submission; no receipt now.
                    queued[matching] = entry, 'draft'
                continue
            if operation == 'dequeue' and 'content' not in row:
                # No identity is stated: retain FIFO slots, hold until readable input.
                queued = [(entry, 'pending' if phase == 'queued' else phase) for entry, phase in queued]
                continue
            content = row['content']
            if not isinstance(content, str):
                raise TypeError
            text = unwrap(content)
            if operation not in ('enqueue', 'remove', 'dequeue'):
                found.skip(line, 'queue-operation', 'unknown queue operation')
                queued = [(entry, 'pending' if entry.text.strip() == text.strip() and phase not in ('echo', 'draft') else phase)
                          for entry, phase in queued]
                continue
            matching = next((i for i, (item, phase) in enumerate(queued)
                             if phase not in ('echo', 'draft') and item.text.strip() == text.strip()), None)
            if operation == "enqueue":
                queued.append((Input(text, stamp, "queued"), 'queued'))  # stale/untimed slots still occupy FIFO
            elif operation in ("remove", "dequeue") and matching is not None:
                entry, _ = queued[matching]
                queued[matching] = entry, 'echo'  # a later user echo must retain this original acceptance time
                found.append(Input(entry.text, entry.at, "consumed", stamp))
        except (TypeError, ValueError, AttributeError, KeyError):
            found.skip(line, kind, 'malformed input row')
    found.extend(entry for entry, phase in queued if phase == 'queued')
    return found


# The program's own notices, as measured in Claude Code 2.1.317 records. A background task's end is
# written as a user row whose origin kind is "task-notification", as a queued command in that mode,
# or as queue content beginning with the notification tag. Nothing else is declared a notice.
NOTICE_ORIGINS = frozenset({'task-notification'})
NOTICE_TAG = '<task-notification>'


def _notice_text(text: str) -> bool:
    return text.lstrip().startswith(NOTICE_TAG)


def _row_text(content) -> str | None:
    """A user row's whole text, or None where it holds something other than text blocks."""
    if isinstance(content, str):
        return unwrap(content)
    if isinstance(content, list) and content and all(isinstance(block, dict) and block.get('type') == 'text'
                                                     and isinstance(block.get('text'), str) for block in content):
        return unwrap('\n'.join(block['text'] for block in content))
    return None


def person_inputs(record: Path, excluded: tuple[str, ...]) -> list[datetime]:
    """When the person typed, from the record alone, oldest first.

    Every input row counts as the person's but the program's declared notices, a compaction summary
    and a line in ``excluded`` (compared whole, so only exactly that text is left out). An input row
    the reader cannot classify or date raises ValueError: the caller reads the quiet time as unknown.
    A queued input counts both when it was typed and when it was taken in, so the later time wins:
    an unrecognized row can only make the person seem more recent, never quieter.
    """
    class Evidence:
        def __init__(self):
            self.problems = []

        def skip(self, line, kind, reason):
            self.problems.append(reason)

    evidence = Evidence()
    rows = input_rows(record, _Reader.kinds, _Reader.label, evidence)
    if evidence.problems:
        raise ValueError('record has unreadable rows')
    found = []

    def person(row, text):
        if text is not None and text.strip() in excluded:
            return
        stamp = when(row.get('timestamp'))
        if stamp is None:
            raise ValueError('an input row has no time')
        found.append(stamp)

    for _, row in rows:
        if 'isSidechain' in row and type(row['isSidechain']) is not bool:
            raise ValueError('an input row has an unknown history identity')
        if row.get('isSidechain'):
            continue
        kind = row.get('type')
        if kind == 'user':
            message = row.get('message')
            if not isinstance(message, dict):
                raise ValueError('an input row is malformed')
            content = message.get('content')
            if isinstance(content, list) and any(isinstance(block, dict) and block.get('type') == 'tool_result'
                                                 for block in content):
                continue  # a tool's result, not an input
            if row.get('isCompactSummary') is True:
                continue
            origin = row.get('origin')
            if isinstance(origin, dict) and origin.get('kind') in NOTICE_ORIGINS:
                continue
            person(row, _row_text(content))
        elif kind == 'queue-operation' and row.get('operation') == 'enqueue':
            content = row.get('content')
            if not isinstance(content, str):
                raise ValueError('a queued input is malformed')
            if not _notice_text(content):
                person(row, unwrap(content))
        elif kind == 'attachment':
            attachment = row.get('attachment')
            if not isinstance(attachment, dict) or attachment.get('type') != 'queued_command':
                continue  # context the program attaches, not an input
            origin = attachment.get('origin')
            if (attachment.get('commandMode') == 'task-notification'
                    or isinstance(origin, dict) and origin.get('kind') in NOTICE_ORIGINS):
                continue
            prompt = attachment.get('prompt')
            text = unwrap(prompt) if isinstance(prompt, str) else None
            if text is not None and _notice_text(text):
                continue
            stamp = when(attachment.get('timestamp')) or when(row.get('timestamp'))
            if text is not None and text.strip() in excluded:
                continue
            if stamp is None:
                raise ValueError('an input row has no time')
            found.append(stamp)
    return sorted(found)


def usage_tokens(usage: dict) -> Tokens:
    return Tokens.from_usage(usage, ("cache_read_input_tokens", "input_tokens", "cache_creation_input_tokens", "output_tokens"))



class _Reader(JsonLinesReader):
    kinds = ('assistant', 'queue-operation', 'system', 'user')
    label = "Claude Code"

    def __init__(self, record: Path, snapshot=None):
        self.orphan_auth = None
        self.sidechain_ends = set()
        self.subagent_turns = 0
        self.queue_slots = []  # text/phase FIFO, including returned drafts and consumed echo identities
        self.background = BackgroundBash()
        self.walls = []
        self.notice_input = None  # accounting candidate only: a notice cannot open a turn
        super().__init__(record, snapshot)

    def state(self):
        state = replace(self.background.state(super().state()), walls=tuple(self.walls))
        return replace(state, failure=self.orphan_auth) if self.open is None and self.orphan_auth else state

    def take(self, entry: dict) -> None:
        kind = entry.get("type")
        if kind == "assistant":
            from .claude_limits import refused_at_limit
            uuid = entry.get("uuid")
            wall = refused_at_limit(entry, f"limit:{uuid}" if isinstance(uuid, str) and uuid else f"limit:{len(self.walls)}",
                                    when(entry.get("timestamp")))
            if wall is not None:
                self.walls.append(wall)  # the provider refused work at this limit, whichever branch asked
        if not entry.get('isSidechain'):
            self.background.observe(entry, when(entry.get('timestamp')))
        if kind == "queue-operation":
            self.queue(entry)
        elif entry.get("isSidechain"):
            self.tools_known = False  # this stream cannot prove all child/background execution ended
            message = entry.get("message", {})
            if kind == "assistant" and message.get("stop_reason") in ENDS_TURN and message.get("id") not in self.sidechain_ends:
                self.sidechain_ends.add(message.get("id"))
                self.subagent_turns += 1
        elif kind == "user":
            self.user(entry)
        elif kind == "assistant":
            self.assistant(entry)
        elif kind == "system" and entry.get("subtype") == "turn_duration":
            self.notice_input = None
            if self.open is not None:
                self.end(when(entry.get("timestamp")), native_end=True)  # the one entry that says the turn is over
        elif kind not in ('system', *INPUT_IGNORED):
            self.tools_known = False

    def queue(self, entry: dict) -> None:
        """A message queued mid-turn; removed when the program takes it, often into the running turn itself."""
        if entry.get('isSidechain'):
            return
        if entry.get('operation') == 'popAll':
            text = draft_return_text(entry)
            matching = draft_return_slot(self.queue_slots, text)
            if matching is None:
                self.queue_error = 'popAll identity unresolved'
            else:
                item, _ = self.queue_slots[matching]
                self.queue_slots[matching] = item, 'draft'
                self.queued.remove(item)  # return one selected item to draft, never to consumed
            return
        text = unwrap(entry.get("content") or "")
        if entry.get("operation") == "enqueue":
            self.queued.append(text)
            self.queue_slots.append((text, 'queued'))
        elif entry.get("operation") in ("remove", "dequeue") and text in self.queued:
            self.queued.remove(text)
            self.consumed.append(text)
            matching = next((i for i, (item, phase) in enumerate(self.queue_slots)
                             if phase not in ('echo', 'draft') and item == text), None)
            if matching is not None:
                self.queue_slots[matching] = text, 'echo'
        elif entry.get('operation') == 'dequeue' and 'content' not in entry:
            self.queue_slots = [(item, 'pending' if phase == 'queued' else phase) for item, phase in self.queue_slots]
        elif entry.get('operation') not in ('enqueue', 'remove', 'dequeue'):
            self.queue_slots = [(item, 'pending' if item.strip() == text.strip() and phase not in ('echo', 'draft') else phase)
                                for item, phase in self.queue_slots]

    def content_known(self, content, role: str) -> bool:
        """Classify native blocks for execution proof, independently of accounting."""
        if role == 'user' and isinstance(content, str):
            return True
        if not isinstance(content, list):
            return False
        for block in content:
            if not isinstance(block, dict):
                return False
            kind = block.get('type')
            if kind == 'text':
                known = isinstance(block.get('text'), str)
            elif role == 'assistant' and kind in ('thinking', 'redacted_thinking'):
                known = isinstance(block.get('thinking' if kind == 'thinking' else 'data'), str)
            elif role == 'assistant' and kind == 'tool_use':
                call, name = block.get('id'), block.get('name')
                known = (isinstance(call, str) and bool(call) and call not in self.live_tools
                         and isinstance(name, str) and bool(name) and isinstance(block.get('input'), dict))
            elif role == 'user' and kind == 'tool_result':
                call = block.get('tool_use_id')
                known = (isinstance(call, str) and call in self.live_tools
                         and isinstance(block.get('is_error', False), bool)
                         and (isinstance(block.get('content', ''), str)
                              or self.content_known(block.get('content'), 'result')))
            elif role in ('user', 'result') and kind in ('image', 'document'):
                source = block.get('source')
                known = isinstance(source, dict) and (
                    source.get('type') == 'base64' and isinstance(source.get('data'), str)
                    or source.get('type') == 'url' and isinstance(source.get('url'), str)
                    or kind == 'document' and source.get('type') == 'text' and isinstance(source.get('data'), str)
                    or kind == 'document' and source.get('type') == 'content'
                    and (isinstance(source.get('content'), str) or self.content_known(source.get('content'), 'document')))
            elif role == 'document' and kind == 'image':
                known = self.content_known([block], 'result')
            elif role in ('user', 'result') and kind == 'search_result':
                known = (isinstance(block.get('source'), str) and isinstance(block.get('title'), str)
                         and self.content_known(block.get('content'), 'document'))
            elif role == 'result' and kind == 'tool_reference':
                known = isinstance(block.get('tool_name'), str)
            else:
                known = False
            if not known:
                return False
        return True

    def user(self, entry: dict) -> None:
        message = entry.get('message', {})
        if message.get('role', 'user') != 'user' or not self.content_known(message.get('content'), 'user'):
            self.tools_known = False
        text = prompt_text(entry)
        content = entry.get("message", {}).get("content")
        if user_notice(entry):
            if self.open is None:
                self.notice_input = content
            return
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
            self.notice_input = None
            self.orphan_auth = None
            self.last_input, self.last_input_at = text, when(entry.get("timestamp"))
            matching = next((i for i, (item, _) in enumerate(self.queue_slots) if item.strip() == text.strip()), None)
            if matching is not None:
                item, phase = self.queue_slots.pop(matching)
                if phase not in ('echo', 'draft') and item in self.queued:
                    self.queued.remove(item)
            if self.open is not None:
                self.end(self.last_input_at, completed=False)  # a later prompt bounds accounting, not terminal evidence
            self.start_turn({"started": self.last_input_at, "input": text, "usage": {}, "tools": [],
                         "reply": "", "model": None, "subagents": self.subagent_turns})
            return
        for block in entry.get("message", {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                self.background.result(block.get('tool_use_id'), entry, block, when(entry.get('timestamp')))
                self.close_tool(block.get("tool_use_id"), when(entry.get("timestamp")), bool(block.get("is_error")))

    def assistant(self, entry: dict) -> None:
        """One entry per content block, all carrying the message's usage and stop reason.

        No stop reason ends the turn: the program may take a queued message into the same
        turn after an ``end_turn`` block. Only ``turn_duration``, an interruption or the
        next prompt does.
        """
        message = entry.get("message", {})
        if (self.open is None and self.notice_input is not None
                and message.get('role') == 'assistant'
                and self.content_known(message.get('content'), 'assistant')):
            text, self.notice_input = self.notice_input, None
            self.orphan_auth = None
            self.start_turn({"started": when(entry.get('timestamp')), "input": text,
                             "usage": {}, "tools": [], "reply": "", "model": None,
                             "subagents": self.subagent_turns})
        # Claude Code 2.1.317: a synthetic assistant API error, then turn_duration.
        # Classify the native code, never login words quoted in a user's text.
        content = message.get('content')
        if self.open is not None:
            if entry.get('isApiErrorMessage') is True:
                self.open['api_error'] = True
            elif (entry.get('isApiErrorMessage', False) is False and entry.get('isSidechain') is False
                    and entry.get('error') is None and isinstance(message.get('model'), str) and message['model']
                    and message.get('role') == 'assistant' and self.content_known(content, 'assistant')
                    and any(b.get('type') == 'text' and b.get('text', '').strip() for b in content)):
                self.open['normal_output'] = True
        if (entry.get('isApiErrorMessage') is True and entry.get('error') == 'authentication_failed'
                and entry.get('isSidechain') is False
                and message.get('role') == 'assistant' and message.get('stop_reason') == 'stop_sequence'
                and isinstance(content, list) and content
                and all(isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str)
                        for b in content)):
            failure = TurnFailure(AUTHENTICATION, when(entry.get('timestamp')),
                                  kind=AUTHENTICATION, code='authentication_failed')
            if self.open is None:
                self.orphan_auth = failure
            else:
                self.open['auth_failure'] = failure
        if message.get('role', 'assistant') != 'assistant' or not self.content_known(message.get('content'), 'assistant'):
            self.tools_known = False
        self.model = message.get("model") or self.model
        self.effort = entry.get("effort") or self.effort
        if self.open is None:
            if any(block.get('type') == 'tool_use' for block in message.get('content') or []):
                self.tools_known = False  # an out-of-turn call is not absent execution
            return
        self.open["model"] = self.model
        key = message.get("id") or entry.get("requestId") or entry.get("uuid") or len(self.open["usage"])
        measured = usage_tokens(message.get("usage"))
        if key not in self.open["usage"] or measured.complete:
            self.open["usage"][key] = measured
        for block in message.get("content") or []:
            if block.get("type") == "tool_use":
                name = block.get('name', '?')
                arguments = block.get('input')
                if (block.get('id') in self.live_tools or name in ('Task', 'Agent')
                        or arguments is not None and not isinstance(arguments, dict)
                        or name != 'Bash' and isinstance(arguments, dict) and arguments.get('run_in_background')):
                    self.tools_known = False  # delegation remains opaque; measured Bash has its own lifetime
                self.background.call(block.get('id'), name, arguments, when(entry.get('timestamp')), entry.get('version'))
                self.open_call(block.get('id'), name, when(entry.get('timestamp')))
            elif block.get("type") == "text" and block.get("text"):
                self.open["reply"] = (self.open["reply"] + "\n\n" + block["text"]).strip()

    def end(self, at: datetime | None, *, completed: bool = True, native_end: bool = False) -> None:
        self.notice_input = None
        turn = self.close_turn()
        tokens = sum(turn["usage"].values(), Tokens()) if turn["usage"] else Tokens.unknown()
        self.record_turn(
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
                failure=turn.get('auth_failure'),
                successful=(False if turn.get('api_error') else
                            True if native_end and turn.get('normal_output') else None),
            ), at if completed else None
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

AUTO_DEFAULT_TITLE = 'Make auto mode your default permission mode?'
AUTO_DEFAULT_DESCRIPTION = ('Auto mode lets Claude handle permission prompts automatically. Claude checks each tool call '
                            'for risky actions and prompt injection before executing, runs the ones it assesses as '
                            'lower-risk, and blocks the rest.')


def auto_default_offer(text: str) -> Interruption | None:
    """2.1.317's exact widget, including which of its two rows is selected."""
    lines = text.splitlines()
    titles = [i for i, line in enumerate(lines) if line.strip() == AUTO_DEFAULT_TITLE]
    if len(titles) != 1:
        return None
    at = titles[0]
    if not at or not RULE.match(lines[at-1]) or not re.search(r'Claude Code v2\.1\.317[ \t]*$', '\n'.join(lines[:at]), re.MULTILINE):
        return None
    body = [line.strip() for line in lines[at+1:] if line.strip()]
    if len(body) < 3 or ' '.join(body[:-2]) != AUTO_DEFAULT_DESCRIPTION:
        return None
    yes, no = body[-2:]
    if yes == '❯ Yes, set auto mode as my default permission mode' and no == 'No, keep bypass permissions':
        return Interruption('auto-mode default offer: select No', re.escape(AUTO_DEFAULT_TITLE), 'Down')
    if yes == 'Yes, set auto mode as my default permission mode' and no == '❯ No, keep bypass permissions':
        return Interruption('auto-mode default offer: confirm No', re.escape(AUTO_DEFAULT_TITLE), 'Enter')
    return None


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


class ClaudeBox(Box):
    """2.1.317 may paint two spaces beyond the text area, including on blank rows."""

    def matches(self, shown: str | None, columns: int | None = None, slack: int = 0) -> bool:
        if shown is None or not columns or columns < 1:
            return super().matches(shown, columns, slack)
        if self.folded and re.fullmatch(self.folded, shown.strip()):
            return True
        from .types import _cells
        rows = []
        for row in shown.split('\n'):
            width = _cells(row)
            if width is not None:
                content = _cells(row.rstrip(' '))
                if width > columns + 2 or content is not None and content > columns:
                    return False
                if width > columns:
                    # Only ASCII spaces can occupy the two measured non-text columns. The content
                    # bound above proves these final cells are padding, not part of the input.
                    row = row[:len(row) - (width - columns)]
            rows.append(row)  # unsupported cell layouts retain the shared matcher's existing behavior
        return super().matches('\n'.join(rows), columns, slack)


class ClaudeHarness(Harness):
    def human_wake_box_margin(self, pane_width: int) -> int:
        # 2.1.317: fixture non-text width 4, plus the conservative allowance 4.
        return 8

    def box_columns(self, pane_width: int) -> int:
        return pane_width - 4  # Claude Code 2.1.317: 76 text columns at pane width 80

    def check_tool_evidence(self, record: Path) -> dict:
        from .check_evidence import json_tools, claude_tools
        return json_tools(record, claude_tools)

    reads_queue_error = True
    compactions = staticmethod(compactions)
    """Claude Code, proven on 2.1.317."""

    efforts = EffortCapability()  # CLI vocabulary is not measured
    defaults = DefaultMetadata(model_default=True, model=SuggestedValue("claude-opus-5-5", "named"),
                               effort=SuggestedValue("max", "named"))

    name = "claude"
    display_name = 'Claude Code'
    setup = SetupMetadata(key_env="ANTHROPIC_API_KEY", fresh_after_compactions=2)
    native_provider = "anthropic"
    program = "claude"
    sign_in_command = ("claude", "auth", "login")
    sign_in_home_variable = "CLAUDE_CONFIG_DIR"
    sign_in_note = ("Current docs place file-backed credentials in CLAUDE_CONFIG_DIR; macOS may use Keychain instead. "
                    "File-backed pool readiness is still decided by doctor, not this hint.")
    sign_in_sources = ("https://code.claude.com/docs/en/cli-reference",
                       "https://code.claude.com/docs/en/authentication")
    home_variable = "CLAUDE_CONFIG_DIR"
    keys = Keys(submit="Enter", send_now=("C-x", "C-s"), interrupt="Escape")
    # From the installed 2.1.317 and 2.1.323, the same in both: the environment it builds for what it runs (CLAUDECODE, the session id, the
    # child-session and attended marks, its pid) and the session's private messaging socket and token. At its
    # start it reads CLAUDECODE, CLAUDE_CODE_CHILD_SESSION, CLAUDE_CODE_SESSION_ATTENDED and the session id as
    # a parent's. Measured on 2.1.317: with TMUX absent, an inherited CLAUDE_CODE_CHILD_SESSION turns transcript
    # saving off, so the seat keeps no record. With exactly these removed, 2.1.323 kept it (2.1.317 was measured
    # with every inherited CLAUDE_CODE_* removed). Its preferences, sign-in and privacy names are not among them.
    inherited_markers = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                         "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_PID",
                         "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN")

    def recorded_paste(self, text: str, record: Path, since: datetime) -> str:
        """Measured on 2.1.317 alone (13 November), two changes the program makes to a pasted payload as it records it:

        - a TAB is recorded as four spaces wherever it stands, in the queued entry, its removal and its
          attachment alike;
        - inside its own ``<pasted_content id=…>`` wrapper, a literal opening tag of the text, ``<pasted_content``,
          is escaped as ``<\\pasted_content``. Only the opening form is measured: a closing tag in the text has
          no form, so its message stays unconfirmed.

        Nothing else of the text changes. Queue rows carry no version, so the version is the program's own
        ``version`` field on the record's rows from ``since``: every one must name a measured version. None,
        or any other, leaves the payload as typed.
        """
        if "\t" not in text and PASTE_TAG not in text:
            return text
        versions = {entry.get("version") for entry in entries(record)
                    if "version" in entry and (moment := when(entry.get("timestamp"))) is not None and moment >= since}
        if not versions or not versions <= PASTE_FORM_VERSIONS:
            return text
        return text.replace("\t", "    ").replace(PASTE_TAG, PASTE_TAG_ESCAPED)
    reads_usage = True
    model_scoped_usage = True

    usage_interval = 300  # the account's other callers share the endpoint; a minute's reads were refused
    credential_file = ".credentials.json"
    # refreshTokenExpiresAt ends the grant that renews access; only the human's login replaces it.
    grant_kind = "fixed"
    headline_window = (None, "5h")
    headline_fallback = (None, "7d")  # the provider reports no five-hour figure for some accounts
    remote_control = True
    human_tools = ('AskUserQuestion',)
    human_tool_policy = "AskUserQuestion is denied by the native permissions policy."

    def person_inputs(self, record: Path, excluded: tuple[str, ...]) -> list[datetime]:
        return person_inputs(record, excluded)

    def usage_scope(self, group: str | None, model: str | None) -> bool | None:
        from .claude_limits import FAMILIES, family
        if group is None:
            return True
        known = family(model)
        return group == known if known and group in FAMILIES.values() else None

    def is_headline(self, group: str | None, window: str) -> bool:
        return group is not None or super().is_headline(group, window)

    def attribution_inputs(self, record: Path) -> list[Input]:
        from .claude_watch import inputs as watch_inputs
        return self.inputs(record) + watch_inputs(record)

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
        self.check_defaults(seat, ship_home)
        home.mkdir(parents=True, exist_ok=True)
        template = ship_template(ship_home, "settings.json")
        settings = copy.deepcopy(merged(merged(SETTINGS, template), seat.settings))  # policy cannot mutate shared defaults
        if not seat.human_session:
            permissions = settings.setdefault('permissions', {})
            permissions['deny'] = list(dict.fromkeys([*permissions.get('deny', []), 'AskUserQuestion']))
        (home / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
        state_file = home / ".claude.json"
        state = json.loads(state_file.read_text()) if state_file.is_file() else {}
        # The trust dialog and the one that asks a person to accept running without approval prompts, both answered
        # before the first launch: a fresh home opens without a dialog.
        state.update({"hasCompletedOnboarding": True, "bypassPermissionsModeAccepted": True,
                      # 2.1.317 writes this after No/keep bypass. A fresh-home startup-only
                      # probe proves it prevents that offer without enabling auto mode.
                      "hasSeenAutoDefaultNudge": True, "theme": state.get("theme", "dark")})
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

    def startup_ready(self, home: Path | None) -> bool:
        if home is None:
            return False
        try:
            state = json.loads((home / '.claude.json').read_text())
            return isinstance(state, dict) and state.get('hasSeenAutoDefaultNudge') is True
        except (OSError, ValueError):
            return False  # the pre-offer composer is not readiness; no elapsed-time shortcut

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
        interruption = (auto_default_offer(text) or self.recognize(text)) if box is None else None  # prose never counts
        return Screen(text, box=box, working=bool(WORKING.search(text)), interruption=interruption,
                      queued=queued, shows={"effort": mode.group(1)} if mode else {})

    def box(self, text: str) -> Box:
        """A paste folds when it is long or has three newlines or more (2.1.317). The placeholder counts the
        newlines, a trailing one too, and names no count at all for a paste without any."""
        newlines = text.count("\n")
        lines = rf" \+{newlines} lines" if newlines else ""
        return ClaudeBox(text, folded=rf"\[Pasted text #\d+{lines}\]")

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


def usage(pool: Pool) -> Usage | None:
    """A subscription pool's utilization windows from the OAuth usage endpoint.

    A failed read is a Usage with no windows whose error says what came back: the HTTP
    status (with any Retry-After, kept also as ``retry_after``), a timeout, a network error
    or bad JSON. The reason never carries the token, another header or the response body.
    """
    now = datetime.now(timezone.utc)
    source = pool.config_dir / ".credentials.json" if pool.config_dir else None
    if source is None or not source.exists():
        return Usage((), now, "credential file missing", called=False)
    try:
        token = json.loads(source.read_text()).get("claudeAiOauth", {}).get("accessToken")
    except (OSError, ValueError, AttributeError):
        return Usage((), now, "credential file unreadable", called=False)
    if not token:
        return Usage((), now, "no access token in the credential file", called=False)
    request = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        raw = _printable(error.headers.get("Retry-After") if error.headers else None)
        return Usage((), now, f"HTTP {error.code}" + (f", retry-after {raw}" if raw else ""), retry_after=_retry_after(raw, now))
    except urllib.error.URLError as error:
        reason = error.reason
        return Usage((), now, "timeout" if isinstance(reason, TimeoutError) else f"network error: {type(reason).__name__}")
    except TimeoutError:
        return Usage((), now, "timeout")
    except OSError as error:
        return Usage((), now, f"network error: {type(error).__name__}")
    try:
        data = json.loads(body)
    except ValueError:
        return Usage((), now, "bad JSON")
    if not isinstance(data, dict):
        return Usage((), now, "bad JSON: not an object")
    from .claude_limits import snapshot
    return snapshot(data, now)


def _printable(value: str | None) -> str | None:
    """A header value fit for a reason: printable, short, or nothing."""
    if not value:
        return None
    value = value.strip()[:40]
    return value if value.isprintable() else None


def _retry_after(value: str | None, now: datetime) -> float | None:
    """Retry-After as seconds from now: delta-seconds or an HTTP-date; None when absent or unreadable."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - now).total_seconds())


ClaudeHarness.usage = staticmethod(usage)
