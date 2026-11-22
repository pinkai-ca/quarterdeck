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
import hashlib
import json
import math
import re
import shlex
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from pathlib import Path

from . import claude_record, record_file
from .json_record import JsonLinesReader, input_reader, records_matching
from .claude_record import entries as entries
from .claude_record import input_rows, inspect_entries
from .types import AUTHENTICATION, Inputs, State, TurnFailure, ControlAnswer, ControlContext, ControlInterruption
from .claude_limits import ExtraUsage, extra_usage_at_refusal, limit_wall, rejected_limit_info
from .claude_background import notification as background_notification, BackgroundBash, user_notice
from .claude_children import Children, fingerprint as children_fingerprint

from .. import durable, seat_files
from ..ship import Pool, SeatSpec
from .base import (Box, ship_template, trimmed, record_fingerprint, Input, Control, Credential, Harness, Interruption, Keys, Launch, Screen, Tokens,
                   Turn, Usage, Window, credential, key_environment, merged, when)

ENDS_TURN = {"end_turn", "max_tokens", "stop_sequence"}
LOCAL_TAGS = ("<command-name>", "<local-command", "<bash-input>", "<bash-stdout>", "<bash-stderr>")
INTERRUPTED = "[Request interrupted"
PASTE_FORM_VERSIONS = frozenset({"2.1.317"})  # versions measured to record a paste in the form recorded_paste gives
PASTE_TAG, PASTE_TAG_ESCAPED = "<pasted_content", "<\\pasted_content"  # a literal opening tag, as the record escapes it
# The format characters (Unicode category Cf) among those 2.1.317 treats as hidden when it cleans a paste, read from
# the program's own sanitiser: it removes each one or, in a context it names (a joiner inside an emoji or an Indic
# word, a mark beside right-to-left text), keeps it. Its other hidden characters are controls, marks and fillers.
PASTE_HIDDEN_FORMAT = frozenset(chr(code) for first, last in (
    (0x00AD, 0x00AD), (0x061C, 0x061C), (0x180E, 0x180E), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x2064),
    (0x2066, 0x206F), (0xFEFF, 0xFEFF), (0xFFF9, 0xFFFB), (0x13430, 0x1343F), (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A), (0xE0001, 0xE0001), (0xE0020, 0xE007F)) for code in range(first, last + 1))
INPUT_IGNORED = ('api-request', 'api-request-blob', 'attachment', 'bridge-session', 'custom-title',
                 'ai-title', 'agent-name', 'mode', 'permission-mode', 'session-owner', 'atis-latch', 'last-prompt',
                 'file-history-delta', 'file-history-snapshot', 'tools-used', 'api-request-shape', 'cost-state',
                 # Claude Code 2.1.325 artifact publication bookkeeping: no input or execution boundary.
                 'frame-link', 'artifact-comment-monitor', 'artifact-autoreact-ledger')
PASTED = re.compile(r'^\s*<pasted_content id="[^"]*">\n?(.*?)\n?</pasted_content(?: id="[^"]*")?>\s*$', re.S)


def ai_title(entry: dict) -> bool:
    """The exact title bookkeeping shape measured on Claude Code 2.1.325."""
    return (set(entry) == {'type', 'aiTitle', 'sessionId'}
            and isinstance(entry['aiTitle'], str) and isinstance(entry['sessionId'], str)
            and bool(entry['sessionId']))


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
    return next((when(e["timestamp"]) for e in inspect_entries(record) if e.get("timestamp")), None)


def session_id(record: Path) -> str | None:
    return next((e["sessionId"] for e in inspect_entries(record) if e.get("sessionId")), None)


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

    class Interpret:
        """The compaction history, fed rows in turn and kept with the record, so an append feeds only its new
        rows (compactions.read); a row it refuses refuses the history, which then stays refused."""

        def __init__(self):
            self.history, self.seen, self.native_turn = History(), {}, None

        def work(self, row, *, turn=False):
            self.history.work(row, turn=turn)
            if isinstance(row, claude_record.ProjectedRow):
                # The guarded stream hashed the complete row, including image bytes.
                self.history.frontier[-1] = row.identity
                if turn:
                    self.history.turn = row.identity

        def feed(self, rows):
            for row in rows:
                if 'isSidechain' in row and type(row['isSidechain']) is not bool:
                    raise ValueError('unknown root history identity')
                if row.get('isSidechain'):
                    continue
                kind = row.get('type')
                if kind == 'ai-title' and not ai_title(row):
                    raise ValueError('unknown title bookkeeping')
                if kind not in (*_Reader.kinds, *INPUT_IGNORED):
                    raise ValueError('unknown native history')
                key = row.get('uuid')
                if key is not None:
                    if not isinstance(key, str) or key in self.seen:
                        raise ValueError('duplicate native history identity')
                    parent = row.get('logicalParentUuid') or row.get('parentUuid')
                    if parent is not None and parent not in self.seen:
                        raise ValueError('missing native parent history')
                    if self.history.conversation is None:
                        if kind != 'user' or prompt_text(row) is None or 'parentUuid' not in row or row['parentUuid'] is not None:
                            raise ValueError('native origin missing')
                        self.history.conversation = row['sessionId']
                        self.history.origin = when(row.get('timestamp'))
                        if not isinstance(self.history.conversation, str) or not self.history.conversation:
                            raise ValueError('native identity missing')
                    if row.get('sessionId') != self.history.conversation:
                        raise ValueError('mixed native conversation')
                    self.seen[key] = parent
                elif kind in ('user', 'assistant') or row.get('subtype') == 'compact_boundary':
                    raise ValueError('native identity missing')
                if kind == 'system' and row.get('subtype') == 'compact_boundary':
                    metadata = row.get('compactMetadata')
                    if not isinstance(metadata, dict) or metadata.get('trigger') not in ('auto', 'manual'):
                        raise ValueError('unknown compaction boundary')
                    self.history.count += 1
                    self.work(row)
                elif kind == 'system' and row.get('subtype') == 'turn_duration':
                    ancestor = key
                    while ancestor is not None and ancestor != self.native_turn:
                        ancestor = self.seen.get(ancestor)
                    self.history.complete = self.native_turn is not None and ancestor == self.native_turn
                elif kind == 'user' and prompt_text(row) is not None:
                    self.work(row, turn=True)
                    self.native_turn = key
                elif kind == 'queue-operation' and row.get('operation') == 'enqueue':
                    self.work(row)
                elif kind == 'assistant':
                    for block in row.get('message', {}).get('content', []):
                        if block.get('type') == 'tool_use':
                            self.history.work(dict(row=key, call=block), call=block.get('id'))

    return read(record, Interpret, _Reader, parse=claude_record.parse, bound=record_file.bound_of(record, 'claude'))


class _TextlessDequeue:
    """The measured 2.1.317 adjacent queue-operation pair, never a general FIFO guess."""
    def __init__(self):
        self.version = None
        self.previous = None

    def take(self, entry):
        if 'version' in entry:
            self.version = entry['version']
        if entry.get('type') != 'queue-operation':
            return entry
        previous = self.previous
        self.previous = entry, self.version
        if (entry.get('operation') != 'dequeue' or 'content' in entry
                or self.version != '2.1.317' or entry.get('isSidechain')):
            return entry
        if previous is None:
            return entry
        earlier, version = previous
        text = earlier.get('content')
        before, after = when(earlier.get('timestamp')), when(entry.get('timestamp'))
        if (version == self.version and earlier.get('operation') == 'enqueue'
                and not earlier.get('isSidechain') and isinstance(text, str)
                and background_notification(text) is not None
                and before is not None and after is not None
                and 0 < (after - before).total_seconds() <= 1):
            return {**entry, 'content': text}
        return entry


@input_reader(lambda row: row.get('type') not in ('assistant', 'system', *INPUT_IGNORED)
              or row.get('type') == 'ai-title' and not ai_title(row), parse=claude_record.parse)
def inputs(record: Path, snapshot) -> list[Input]:
    found, queued = Inputs(), []
    dequeue = _TextlessDequeue()
    for line, row in input_rows(record, _Reader.kinds, _Reader.label, found, parse=lambda _: snapshot):
        row = dequeue.take(row)
        kind = row.get('type')
        if kind == 'ai-title' and not ai_title(row):
            found.skip(line, 'ai-title', 'unknown title bookkeeping')
            continue
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
            notice = user_notice(row)
            text = row['message']['content'] if notice else prompt_text(row)
            if text is not None:
                matching = next((i for i, (item, _) in enumerate(queued) if item.text.strip() == text.strip()), None)
                if matching is None:
                    if not notice:
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
            if operation == 'enqueue' and 'content' not in row:
                # Measured in Claude Code 2.1.317: activity, but no text identity for a receipt or attribution.
                found.skip(line, 'queue-operation', 'queued input text not recorded')
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
    an unrecognized row can only make the person seem more recent, never quieter. An enqueue without
    a content key records activity at its own time, but cannot identify excluded text or a receipt.
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
        if kind == 'ai-title' and not ai_title(row):
            raise ValueError('unknown title bookkeeping')
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
            if 'content' not in row:
                person(row, None)  # absent text is activity, never a guessed notice or excluded waking line
                continue
            content = row['content']
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


QUEUE_COMPLETION_VERSIONS = frozenset({'2.1.317'})  # crew enqueue/receipt/turn_duration measurements


@dataclass(frozen=True)
class _QueueSlot:
    text: str
    phase: str
    at: datetime | None
    session: str | None
    known: bool
    uncertain: bool = False


def queued_notice(entry: dict) -> str | None:
    """The measured queued_command notice receipt; no execution or input receipt is inferred."""
    attachment = entry.get('attachment')
    at = when(attachment.get('timestamp')) if isinstance(attachment, dict) else None
    if (entry.get('type') != 'attachment' or entry.get('version') not in QUEUE_COMPLETION_VERSIONS
            or entry.get('isSidechain') is not False
            or not isinstance(attachment, dict) or attachment.get('type') != 'queued_command'
            or set(attachment) != {'type', 'commandMode', 'origin', 'prompt', 'runId',
                                   'source_uuid', 'delivery_id', 'timestamp'}
            or attachment.get('commandMode') != 'task-notification'
            or attachment.get('origin') != {'kind': 'task-notification', 'producer': 'session-task'}
            or not all(isinstance(attachment.get(key), str) and attachment[key]
                       for key in ('runId', 'source_uuid', 'delivery_id'))
            or at is None or at.tzinfo is None):
        return None
    text = attachment.get('prompt')
    fields = background_notification(text)
    return text if fields is not None and fields[2] in ('completed', 'failed', 'killed', 'stopped') else None



# A measured notice is the program's own input and opens no turn, so between it and the first assistant row of the
# program's reply to it the record alone would read idle. For that window turn_open reads unknown and the screen
# decides (law: record-first); once an assistant row is written, or the grace has passed, it reads as before.
# Measured on the crew's Claude records of 16 November 2026 (767 records, 2.1.317 and 2.1.325): of 894
# task-notification user rows followed by an assistant row, that row came after a median of 2.9 s, 11.7 s at the
# 99th percentile and 42.4 s at most; a typed prompt's first assistant row (7991) came within 60.5 s at the 99.9th
# percentile. The grace is above every measured notice and at that percentile of typed prompts.
NOTICE_GRACE_SECONDS = 60


def _wall_clock() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _ClaudeState(State):
    extra_usage: tuple[ExtraUsage, ...] = ()


class _Reader(JsonLinesReader):
    parse_snapshot = staticmethod(claude_record.parse)
    kinds = ('assistant', 'queue-operation', 'system', 'user')
    label = "Claude Code"
    clock = staticmethod(_wall_clock)  # the notice grace is judged on this clock alone; a test injects its own
    reading_fingerprint = staticmethod(children_fingerprint)

    @classmethod
    def read_snapshot(cls, record, snapshot):
        state, turns = super().read_snapshot(record, snapshot)
        return replace(state, projection_usage=claude_record.projection_usage(snapshot)), turns

    def __init__(self, record: Path, snapshot=None):
        self.orphan_auth = None
        self.sidechain_ends = set()
        self.subagent_turns = 0
        self.textless_dequeue = _TextlessDequeue()
        self.queue_slots = []  # FIFO, including returned drafts and consumed echo identities
        self.queue_version = None
        self.background = BackgroundBash(record)
        self.children = Children(record)
        self.walls = []
        self.extra_usage = []
        self.notice_input = None  # accounting candidate only: a notice cannot open a turn
        self.notice_unanswered, self.notice_at = False, None  # a notice no assistant row has followed, and its time
        super().__init__(record, snapshot)

    def state(self, *, launched_at=None):
        state = replace(self.children.project(super().state(), self.background, INPUT_IGNORED, ai_title,
                                              launched_at=launched_at), walls=tuple(self.walls))
        if state.queue_error is None and any(slot.uncertain and slot.phase in ('queued', 'pending')
                                             for slot in self.queue_slots):
            state = replace(state, queue_error='unknown queue operation')
        if self.answering_notice():
            state = replace(state, turn_open=None)
        if self.open is None and self.orphan_auth:
            state = replace(state, failure=self.orphan_auth)
        if self.extra_usage:
            return _ClaudeState(**vars(state), extra_usage=tuple(self.extra_usage))
        return state

    def answering_notice(self) -> bool:
        """No turn is open, the program's own notice came within the grace and no assistant row has followed it.

        The program may be answering it: the record alone cannot say the seat is idle. A notice whose time cannot be
        read cannot be shown old, so it reads the same until an assistant row or a turn's end follows it.
        """
        if self.open is not None or not self.notice_unanswered:
            return False
        at = self.notice_at
        if at is None or at.tzinfo is None:
            return True
        return (self.clock() - at).total_seconds() < NOTICE_GRACE_SECONDS

    def take(self, entry: dict) -> None:
        entry = self.textless_dequeue.take(entry)
        kind = entry.get("type")
        self.children.observe(entry)
        if not entry.get('isSidechain') and 'version' in entry:
            self.queue_version = entry['version']
        if kind == 'ai-title' and not ai_title(entry):
            self.tools_known = False
            return
        if kind == "assistant":
            info = rejected_limit_info(entry)
            uuid = entry.get("uuid")
            at = when(entry.get("timestamp")) if info is not None else None
            wall = limit_wall(info, f"limit:{uuid}" if isinstance(uuid, str) and uuid else f"limit:{len(self.walls)}", at)
            if wall is not None:
                self.walls.append(wall)  # the provider refused work at this limit, whichever branch asked
            if (reading := extra_usage_at_refusal(info, at)) is not None:
                self.extra_usage.append(reading)
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
        elif kind == 'attachment':
            if (text := queued_notice(entry)) is not None:
                self.consume_queued(text, receipt=entry)
        elif kind == "assistant":
            self.assistant(entry)
        elif kind == "system" and entry.get("subtype") == "turn_duration":
            self.notice_input = None
            self.notice_unanswered = False
            if self.open is not None:
                self.end(when(entry.get("timestamp")), native_end=True)  # the one entry that says the turn is over
                self.complete_queue(entry)
        elif kind not in ('system', *INPUT_IGNORED):
            self.tools_known = False

    def queue(self, entry: dict) -> None:
        """A message queued mid-turn; removed when the program takes it, often into the running turn itself."""
        if entry.get('isSidechain'):
            return
        if entry.get('operation') == 'popAll':
            text = draft_return_text(entry)
            matching = draft_return_slot(((slot.text, slot.phase) for slot in self.queue_slots), text)
            if matching is None:
                self.queue_error = 'popAll identity unresolved'
            else:
                slot = self.queue_slots[matching]
                self.queue_slots[matching] = replace(slot, phase='draft')
                self.queued.remove(slot.text)  # return one selected item to draft, never to consumed
            return
        text = unwrap(entry.get("content") or "")
        if entry.get("operation") == "enqueue":
            self.queued.append(text)
            at, session = when(entry.get('timestamp')), entry.get('sessionId')
            known = (self.queue_version in QUEUE_COMPLETION_VERSIONS
                     and set(entry) == {'type', 'operation', 'sessionId', 'timestamp', 'content'}
                     and isinstance(session, str) and bool(session) and bool(text.strip())
                     and at is not None and at.tzinfo is not None
                     and (not _notice_text(text) or background_notification(text) is not None))
            self.queue_slots.append(_QueueSlot(text, 'queued', at, session, known))
        elif entry.get("operation") in ("remove", "dequeue") and text in self.queued:
            self.queued.remove(text)
            self.consumed.append(text)
            matching = next((i for i, slot in enumerate(self.queue_slots)
                             if slot.phase not in ('echo', 'draft') and slot.text == text), None)
            if matching is not None:
                self.queue_slots[matching] = replace(self.queue_slots[matching], phase='echo')
        elif entry.get('operation') == 'dequeue' and 'content' not in entry:
            self.queue_slots = [replace(slot, phase='pending') if slot.phase == 'queued' else slot
                                for slot in self.queue_slots]
        elif entry.get('operation') not in ('enqueue', 'remove', 'dequeue'):
            # Uncertainty belongs to these candidates, not every later queue reading.
            # Keep their enqueue identity/time for a later native completion.
            self.queue_slots = [replace(slot, phase='pending', uncertain=True)
                                if slot.text.strip() == text.strip() and slot.phase not in ('echo', 'draft') else slot
                                for slot in self.queue_slots]

    def complete_queue(self, entry: dict) -> None:
        """A native completed turn bounds the pending queue, never delivery or tool evidence."""
        at, session = when(entry.get('timestamp')), entry.get('sessionId')
        if (entry.get('version') not in QUEUE_COMPLETION_VERSIONS or not self.completed_turns
                or not self.completed_turns[-1].timed or at is None or at.tzinfo is None
                or not isinstance(session, str) or not session):
            return
        started = self.completed_turns[-1].started
        if started is None or started.tzinfo is None or started > at:
            return
        kept = []
        for slot in self.queue_slots:
            if (slot.phase in ('queued', 'pending') and slot.known and slot.session == session
                    and slot.at <= at):
                self.queued.remove(slot.text)
            else:
                kept.append(slot)
        self.queue_slots = kept

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

    def consume_queued(self, text: str, *, receipt=None) -> None:
        at = when(receipt['attachment']['timestamp']) if receipt is not None else None
        matching = next((i for i, slot in enumerate(self.queue_slots)
                         if slot.text.strip() == text.strip() and (receipt is None or
                            slot.known and slot.session == receipt.get('sessionId') and slot.at <= at)), None)
        if matching is not None:
            slot = self.queue_slots.pop(matching)
            if slot.phase not in ('echo', 'draft') and slot.text in self.queued:
                self.queued.remove(slot.text)

    def user(self, entry: dict) -> None:
        message = entry.get('message', {})
        if message.get('role', 'user') != 'user' or not self.content_known(message.get('content'), 'user'):
            self.tools_known = False
        text = prompt_text(entry)
        content = entry.get("message", {}).get("content")
        if user_notice(entry):
            self.consume_queued(content)
            if self.open is None:
                self.notice_input = content
                self.notice_unanswered, self.notice_at = True, when(entry.get('timestamp'))
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
            self.notice_unanswered = False
            self.orphan_auth = None
            self.last_input, self.last_input_at = text, when(entry.get("timestamp"))
            self.consume_queued(text)
            if self.open is not None:
                self.end(self.last_input_at, completed=False)  # a later prompt bounds accounting, not terminal evidence
            self.start_turn({"started": self.last_input_at, "input": text, "usage": {}, "tools": [],
                         "reply": "", "model": None, "subagents": self.subagent_turns})
            return
        for block in entry.get("message", {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                self.background.result(block.get('tool_use_id'), entry, block, when(entry.get('timestamp')))
                self.children.result(block.get('tool_use_id'), entry, block, when(entry.get('timestamp')))
                self.close_tool(block.get("tool_use_id"), when(entry.get("timestamp")), bool(block.get("is_error")))

    def assistant(self, entry: dict) -> None:
        """One entry per content block, all carrying the message's usage and stop reason.

        No stop reason ends the turn: the program may take a queued message into the same
        turn after an ``end_turn`` block. Only ``turn_duration``, an interruption or the
        next prompt does.
        """
        message = entry.get("message", {})
        self.notice_unanswered = False  # the program has answered: its reply row decides from here
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
                child = self.children.call(block.get('id'), name, arguments, when(entry.get('timestamp')), entry.get('version'))
                if (block.get('id') in self.live_tools or name in ('Task', 'Agent') and not child
                        or arguments is not None and not isinstance(arguments, dict)
                        or name != 'Bash' and not child and isinstance(arguments, dict) and arguments.get('run_in_background')):
                    self.tools_known = False  # delegation remains opaque; measured Bash has its own lifetime
                self.background.call(block.get('id'), name, arguments, when(entry.get('timestamp')), entry.get('version'))
                self.open_call(block.get('id'), name, when(entry.get('timestamp')))
            elif block.get("type") == "text" and block.get("text"):
                self.open["reply"] = (self.open["reply"] + "\n\n" + block["text"]).strip()

    def end(self, at: datetime | None, *, completed: bool = True, native_end: bool = False) -> None:
        self.notice_input = None
        self.notice_unanswered = False
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
# The working sign is one of the spinner's glyphs, a verb and the turn's running time. Past the first minute the
# time leads with minutes ("(1m 32s", 2.1.317; "(3m 38s", 2.1.325) and past the first hour with hours
# ("(1h 0m 43s", 2.1.325); "✽" is one of the glyphs (2.1.317). All measured on the ship's own seats,
# 15 November 2026.
WORKING = re.compile(r"esc to interrupt|^[*✻✶✳✢✽·⠂⠄⠆] \S+… \((?:\d+h )?(?:\d+m )?\d+s", re.M)
# The composer's bottom line names what the program runs in its background. Each form is matched whole and only
# on the version it was measured on. The original forms were captured on 15 November 2026:
#   2.1.317  with nothing in the background the mode is followed by its cycling hint; with work the hint gives
#            way to the count of shells, and of one monitor beside them. One feedback draft beside the cycling
#            hint is also at rest. A helper agent running in the background adds a hint beside the cycling hint
#            and a panel of its own below the line (a throwaway seat's capture);
#            for some thirty seconds after it ended, and for the first seconds after a launch, a hint alone
#   2.1.325  the same two bodies, each followed by the name of the session's published page; a helper agent
#            running in the background adds a panel of its own below the line, gone when the helper ends
# Any other bottom line, or one that is not the screen's last, says nothing here.
BOTTOM_LINES = {
    "2.1.317": re.compile(r"  ⏵⏵ bypass permissions on(?: \(shift\+tab to cycle\)(?: · 1 feedback draft)?"
                          r"| · (?P<shells>[1-9]\d*) (?P<noun>shells?)(?P<monitor>, 1 monitor)?)"),
    "2.1.325": re.compile(r"  ⏵⏵ bypass permissions on(?: \(shift\+tab to cycle\)"
                          r"| · (?P<shells>[1-9]\d*) (?P<noun>shells?)) · ⧉ \S+"),
}
SET_MODEL = re.compile(r"Set model to `?([^`]+?)`? and saved")
SET_EFFORT = re.compile(r"Set effort level to `?(\w+)`?")
QUEUED_PLACEHOLDER = "Press up to edit queued messages"
# Exact native IDs established for 2.1.317. ModelPicker/account catalogs may override display names:
# a display-only command-output line never establishes identity or record confirmation.
MODEL_IDENTITIES = frozenset({'claude-haiku-5-5', 'claude-sonnet-5-5', 'claude-opus-5-5'})


def screen_reader_boolean(value: str | None) -> bool | None:
    """2.1.317's tri-state env parser; an unrecognized token falls through to settings."""
    if not isinstance(value, str):
        return None
    token = value.strip().lower()
    if token in {'1', 'true', 'yes', 'on'}:
        return True
    if token in {'0', 'false', 'no', 'off'}:
        return False
    return None


def catalog_display(data: dict, target: str, *, launched: datetime, now: datetime) -> tuple[str, int, int] | None:
    """A fresh persisted catalog candidate, not proof of the process's in-memory name or native ID."""
    try:
        if not isinstance(target, str) or not target:
            return None
        if type(data['version']) is not int or data['version'] != 2:
            return None
        fetched, stale = data['fetchedAt'], data['staleAt']
        if (type(fetched) is not int or type(stale) is not int
                or launched.tzinfo is None or now.tzinfo is None):
            return None
        start_ms, now_ms = int(launched.timestamp() * 1000), int(now.timestamp() * 1000)
        if not (0 <= fetched <= now_ms < stale and start_ms <= now_ms and start_ms < stale):
            return None
        rows = data['catalog']['config']['models']
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return None
        matching = [row for row in rows if row.get('id') == target]
        if len(matching) != 1:
            return None
        name = matching[0]['name']
        if not isinstance(name, str) or not name or len(name) > 200 or not all(c.isprintable() for c in name):
            return None
        return name, fetched, stale
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def model_display(seat, target):
    """A per-seat fresh catalog precondition, never a global display-name alias."""
    try:
        launched = seat.launched()
        if launched is None or launched.version != '2.1.317':
            return None
        argv = launched.argv
        if ('--settings' in argv or '--setting-sources' not in argv
                or argv[argv.index('--setting-sources') + 1] != 'user'):
            return None
        settings_path = seat.home / 'settings.json'
        settings_raw = settings_path.read_bytes()
        settings = json.loads(settings_raw)
        if not isinstance(settings, dict) or 'modelPicker' in settings:
            return None
        # Managed settings may override a user's complete model-picker table. Their presence is
        # unsupported here; a missing or ambiguous catalog never falls back to the bundled table.
        if Path('/etc/claude-code').exists():
            return None
        candidates = []
        for path in (seat.home / 'cache' / 'model-catalog').glob('*.json'):
            raw = path.read_bytes()
            data = json.loads(raw)
            bound = catalog_display(data, target, launched=launched.when, now=datetime.now(timezone.utc))
            if bound is not None:
                display, fetched, stale = bound
                candidates.append(dict(display=display, catalog=str(path), fetched=fetched, stale=stale,
                                       digest=hashlib.sha256(raw).hexdigest(),
                                       settings=hashlib.sha256(settings_raw).hexdigest()))
        if settings_path.read_bytes() != settings_raw:
            return None
        return candidates[0] if len(candidates) == 1 else None
    except (OSError, ValueError, TypeError, AttributeError, IndexError):
        return None


def control_suffix(record, origin):
    """Bounded complete rows after one unchanged native prefix, with no raw persistence."""
    try:
        if str(record) != origin['path']:
            return None
        bound = record_file.bound_of(record, 'claude')
        with record_file.Opened(record, bound) as stream:  # never a link, never without end
            before = stream.stat
            offset = origin['offset']
            if (type(offset) is not int or not 0 <= offset <= before.st_size <= bound
                    or (before.st_dev, before.st_ino) != (origin['device'], origin['inode'])):
                return None
            digest, left = hashlib.sha256(), offset
            while left:
                chunk = stream.read(min(left, 65536))
                if not chunk:
                    return None
                digest.update(chunk)
                left -= len(chunk)
            if digest.hexdigest() != origin['prefix']:
                return None
            # A large or partial suffix is unknown, not truncated into an apparent result.
            suffix = stream.read(2 * 1024 * 1024 + 1)
        if len(suffix) > 2 * 1024 * 1024 or suffix and not suffix.endswith(b'\n'):
            return None
        rows = [json.loads(line) for line in suffix.splitlines()]
        if any(not isinstance(row, dict) for row in rows):
            return None
        after = record.stat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            return None
        return rows
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def control_origin(record: Path | None, display: dict, *, launch: dict | None = None) -> dict | None:
    """Complete-row record boundary for an adapter-proved display binding."""
    if record is None:
        if launch is None or display.get('synthetic') is not True:
            return None
        return dict(path=None, device=None, inode=None, offset=0, prefix=hashlib.sha256(b'').hexdigest(),
                    display=display, first_record_launch=dict(launch))
    try:
        digest = hashlib.sha256()
        tail = b''
        bound = record_file.bound_of(record, 'claude')
        with record_file.Opened(record, bound) as stream:  # never a link, never without end
            before = stream.stat
            if before.st_size > bound:
                return None
            for chunk in iter(lambda: stream.read(65536), b''):
                digest.update(chunk)
                tail = chunk[-1:]
        after = record.stat()
        stamp = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if stamp(before) != stamp(after) or after.st_size and tail != b'\n':
            return None  # never begin inside a partial native row
        return dict(path=str(record), device=after.st_dev, inode=after.st_ino, offset=after.st_size,
                    prefix=digest.hexdigest(), display=display)
    except OSError:
        return None


def command_model_result(record: Path, origin: dict, value: str, display: str) -> str | None:
    """A new exact model command/result triple linked by native parents."""
    try:
        before = record.stat()
        rows = control_suffix(record, origin)
        if rows is None:
            return None
        triples = []
        for index, row in enumerate(rows):
            content = row.get('message', {}).get('content')
            if row.get('type') != 'user' or not isinstance(content, str) or '<command-name>' not in content:
                continue
            # Any second local model command makes ownership ambiguous, even if its target
            # happens to agree. A missing caveat/result cannot be filled from older history.
            if '<command-name>/model</command-name>' not in content:
                continue
            if index == 0 or index + 1 >= len(rows):
                return None
            caveat, stdout = rows[index - 1], rows[index + 1]
            group = (caveat, row, stdout)
            prompt = row.get('promptId')
            identities = [item.get('uuid') for item in group]
            if (not isinstance(prompt, str) or not prompt
                    or any(not isinstance(identity, str) or not identity for identity in identities)
                    or len(set(identities)) != 3
                    or any(item.get('type') != 'user' or item.get('promptId') != prompt
                           or item.get('isSidechain') or item.get('isCompactSummary') for item in group)
                    or row.get('parentUuid') != identities[0] or stdout.get('parentUuid') != identities[1]
                    or caveat.get('isMeta') is not True):
                return None
            introduction = caveat.get('message', {}).get('content')
            output = stdout.get('message', {}).get('content')
            arguments = re.findall(r'<command-args>(.*?)</command-args>', content, re.S)
            matches = SET_MODEL.findall(output) if isinstance(output, str) else []
            if (not isinstance(introduction, str) or not introduction.startswith('<local-command-caveat>')
                    or arguments != [value] or not isinstance(output, str)
                    or not output.startswith('<local-command-stdout>')
                    or matches != [display]):
                return None
            triples.append(group)
        after = record.stat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            return None
        return value if len(triples) == 1 else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def styled_rows(text):
    """Decode SGR foreground cells; any other terminal escape leaves the frame unknown."""
    rows, letters, colors, foreground = [], [], [], None
    position = 0
    while position < len(text):
        char = text[position]
        if char == '\x1b':
            match = re.match(r'\x1b\[([0-9;]*)m', text[position:])
            if match is None:
                return None
            codes = [int(code or '0') for code in match[1].split(';')]
            index = 0
            while index < len(codes):
                code = codes[index]
                if code in (0, 39):
                    foreground = None
                elif 30 <= code <= 37 or 90 <= code <= 97:
                    foreground = ('ansi', code)
                elif code in (38, 48):
                    if index + 2 < len(codes) and codes[index + 1] == 5:
                        if code == 38:
                            foreground = ('indexed', codes[index + 2])
                        index += 2
                    elif index + 4 < len(codes) and codes[index + 1] == 2:
                        if code == 38:
                            foreground = ('rgb', *codes[index + 2:index + 5])
                        index += 4
                    else:
                        return None
                elif code not in (1, 2, 3, 4, 7, 9, 22, 23, 24, 27, 29, 49, *range(40, 48), *range(100, 108)):
                    return None
                index += 1
            position += len(match[0])
            continue
        if char == '\n':
            rows.append((''.join(letters), tuple(colors)))
            letters, colors = [], []
        elif char.isprintable() or char == '\u00a0':
            letters.append(char)
            colors.append(foreground)
        else:
            return None
        position += 1
    if letters:
        rows.append((''.join(letters), tuple(colors)))
    return rows


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


LOGIN_QUESTION = Interruption("login", r"Select login method|Paste code here if prompted|Browser didn't open", key=None)
TRUST_QUESTION = Interruption("trust dialog", r"Do you trust the files in this folder\?", key=None)
PERMISSION_QUESTION = Interruption("permission prompt", r"Do you want to proceed\?|Allow once|Allow always", key=None)


class ClaudeHarness(Harness):
    reading_fingerprint = staticmethod(children_fingerprint)
    native_status_stream = True
    box_geometry_known = True  # box_columns below declares the established text area

    def state_for_relaunch(self, record, launched_at):
        # This rare lifecycle read uses a fresh reader, never changes the cached record state.
        return _Reader(record).state(launched_at=launched_at)

    def human_wake_box_margin(self, pane_width: int) -> int:
        # 2.1.317: fixture non-text width 4, plus the conservative allowance 4.
        return 8

    def box_columns(self, pane_width: int) -> int:
        return pane_width - 4  # Claude Code 2.1.317: 76 text columns at pane width 80

    def check_tool_evidence(self, record: Path) -> dict:
        from .check_evidence import json_tools, claude_tools
        return json_tools(record, claude_tools)
    def model_identity(self, value: str | None, *, version: str | None = None) -> str | None:
        if not isinstance(value, str):
            return None
        if value in MODEL_IDENTITIES:
            return value
        return None

    def record_model(self, record: Path) -> str | None:
        # A local-command result replaces State.model with a DISPLAY name. Even a display spelling
        # like a native ID is not identity evidence. Only the next main assistant's model field is.
        before = record_fingerprint(record)
        self.state(record)  # validate this adapter's current native record envelope before walking it
        model = None
        for row in inspect_entries(record):
            if row.get('isSidechain') or row.get('isCompactSummary') is True:
                continue
            message = row.get('message')
            if not isinstance(message, dict):
                continue
            if row.get('type') == 'assistant' and message.get('model') is not None:
                model = self.model_identity(message['model'])
            elif row.get('type') == 'user':
                content = message.get('content')
                if isinstance(content, str) and content.startswith('<local-command-stdout>') and SET_MODEL.search(content):
                    model = None
        return model if record_fingerprint(record) == before else None

    def model_control_origin(self, seat, record: Path | None, value: str) -> dict | None:
        if record is None or self.model_identity(value) is None:
            return None
        display = model_display(seat, value)
        return control_origin(record, display) if display is not None else None

    def model_control_result(self, seat, record: Path, value: str, origin: dict) -> str | None:
        current_display = model_display(seat, value)
        if current_display is None or current_display != origin.get('display'):
            return None
        return command_model_result(record, origin, value, current_display['display'])

    def model_control_current(self, record: Path, origin: dict) -> bool | None:
        rows = control_suffix(record, origin)
        if rows is None:
            return None
        # Local commands are deliberately excluded from the ordinary prompt reader. They still
        # revoke answer currentness. No question-side native command envelope was measured, so a
        # newly written user/queue row here is unknown rather than assumed to be our own command.
        if any(row.get('type') in ('user', 'queue-operation') for row in rows):
            return None
        return True

    def control_context(self, seat, control: Control, value: str, screen: Screen,
                        *, version: str | None = None, question: str | None = None) -> ControlContext | None:
        if control.name != 'model' or version != '2.1.317' or question is None:
            return None
        display = model_display(seat, value)
        if display is None:
            return None
        frame = seat.read_control_frame()
        if (frame is None or frame.cursor_visible is not False
                or (frame.pane.width, frame.pane.height) != (120, 40)):
            return None  # only this complete native geometry/style was measured
        rows = styled_rows(frame.text)
        if rows is None:
            return None
        plain = '\n'.join(row[0] for row in rows) + '\n'
        if (plain.rstrip('\n') != screen.text.rstrip('\n')
                or hashlib.sha256(screen.text.encode()).hexdigest() != question
                or len(rows) != 40 or self.screen(plain).box is not None):
            return None  # physical/joined disagreement (including wrapped frames) stays unknown
        label = f'Yes, switch to {display["display"]}'
        yes, no = rows[37], rows[38]
        if yes[0].rstrip(' ') != f'   ❯ 1. {label}' or no[0].rstrip(' ') != '     2. No, go back':
            return None
        if (yes[1][3] != ('indexed', 153)
                or any(color != ('indexed', 246) for color in yes[1][5:8])
                or any(color != ('indexed', 153) for color in yes[1][8:8 + len(label)])
                or any(color != ('indexed', 246) for color in no[1][5:8])
                or any(color is not None for color in no[1][8:8 + len('No, go back')])):
            return None
        titles = [index for index, row in enumerate(rows) if row[0].strip() == 'Switch model?']
        if len(titles) != 1 or titles[0] >= 37:
            return None
        body = [row[0].strip() for row in rows[titles[0] + 1:37]]
        if body.count('Your next response will be slower and use more tokens') != 1:
            return None  # hook-replaced or otherwise unmeasured question bodies remain unknown
        # Positive measured selection/composer/cursor shape, not an absence-of-warning guess.
        if any('❯\u00a0' in row[0] or row[0].strip() == '─' * 120 for row in rows[36:39]):
            return None
        option_rows = [index for index, row in enumerate(rows)
                       if re.match(r'^\s*(?:❯\s*)?[0-9]+\. ', row[0])]
        if option_rows != [37, 38] or sum('❯' in row[0] for row in rows[titles[0]:]) != 1:
            return None
        scope = (frame.pane.id, frame.pane.pid, frame.pane.width, frame.pane.height,
                 display['catalog'], display['digest'], display['settings'], question,
                 hashlib.sha256(frame.text.encode()).hexdigest())
        return ControlContext(version, value, display['display'], screen_reader_off=True, focused=True,
                              composer_hidden=True, cursor_visible=False, yes_highlighted=True, scope=scope)

    def control_answer(self, control: Control, value: str, screen: Screen,
                       *, version: str | None = None, context: ControlContext | None = None) -> ControlAnswer | None:
        if (control.name != 'model' or version != '2.1.317'
                or not isinstance(screen.interruption, ControlInterruption)
                or screen.interruption.control != 'model'):
            return None
        target = self.model_identity(value, version=version)
        if (not isinstance(context, ControlContext) or context.version != version or context.target != target
                or target is None or context.screen_reader_off is not True or context.focused is not True
                or context.composer_hidden is not True or context.cursor_visible is not False
                or context.yes_highlighted is not True
                or not context.scope or not context.display or len(context.display) > 200
                or not all(char.isprintable() for char in context.display)):
            return None
        if sum(line.strip(' \u00a0') == 'Switch model?' for line in screen.text.splitlines()) != 1:
            return None
        pattern = re.compile(r'^[ \u00a0]*(?P<cursor>❯[ \u00a0]*)?(?P<number>[0-9]+)\. (?P<text>.*?)[ \u00a0]*$')
        options = [match for line in screen.text.splitlines() if (match := pattern.fullmatch(line))]
        label = f'Yes, switch to {context.display}'
        yes = [option for option in options if option['text'] == label and option['number'] == '1']
        no = [option for option in options if option['text'] == 'No, go back' and option['number'] == '2']
        if len(options) != 2 or len(yes) != 1 or len(no) != 1:
            return None
        if not yes[0]['cursor'] or no[0]['cursor']:
            return None
        # 2.1.317 measured Enter only on highlighted, selected Yes, with composer/cursor hidden.
        # No digit action or cursor movement is authorized by that native proof.
        return ControlAnswer('model', value, label, ('Enter',))

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
        if "\t" not in text and PASTE_TAG not in text or not self._paste_measured(record, since):
            return text
        return text.replace("\t", "    ").replace(PASTE_TAG, PASTE_TAG_ESCAPED)

    def paste_ignored(self, text: str, record: Path, since: datetime) -> frozenset[str]:
        """Measured on 2.1.317 (13 November): the program drops a zero-width space (U+200B) from a paste, in the
        queued entry, its removal and its attachment alike. Its sanitiser treats the other format characters of
        ``PASTE_HIDDEN_FORMAT`` the same way, removing each unless its context keeps it, so the receipt removes
        exactly these from both sides before comparing; nothing else is ignored. Only for a payload that holds
        one, and under the same version scope as ``recorded_paste``.
        """
        if PASTE_HIDDEN_FORMAT.isdisjoint(text) or not self._paste_measured(record, since):
            return frozenset()
        return PASTE_HIDDEN_FORMAT

    @staticmethod
    def _paste_measured(record: Path, since: datetime) -> bool:
        versions = {entry.get("version") for entry in inspect_entries(record)
                    if "version" in entry and (moment := when(entry.get("timestamp"))) is not None and moment >= since}
        return bool(versions) and versions <= PASTE_FORM_VERSIONS
    reads_usage = True
    model_scoped_usage = True

    usage_interval = 300  # the account's other callers share the endpoint; a minute's reads were refused
    idle_usage = True  # usage(pool) reads the pool's own credential file: no seat needed
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

    @staticmethod
    def _access_expiry(value: int | float) -> float:
        return float(value) / 1000 if value > 1e11 else float(value)

    def sign_in_metadata(self, path: Path) -> tuple[float, bool, datetime | None] | None:
        """The access deadline, refresh-key presence and optional end, from one metadata reading."""
        try:
            oauth = json.loads(path.read_text()).get("claudeAiOauth")
            if not isinstance(oauth, dict):
                return None
            access = oauth.get("expiresAt")
            end = oauth.get("refreshTokenExpiresAt")
            if (type(access) not in (int, float) or not math.isfinite(access)
                    or end is not None and (type(end) not in (int, float) or not math.isfinite(end))):
                return None
            return self._access_expiry(access), "refreshToken" in oauth, (
                datetime.fromtimestamp(end / 1000, timezone.utc) if end is not None else None)
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            return None

    def freshness(self, path: Path) -> tuple[float, float] | None:
        try:
            oauth = json.loads(path.read_text()).get("claudeAiOauth")
            if isinstance(oauth, dict) and oauth.get("refreshToken"):
                expiry = oauth.get("expiresAt") or 0
                return self._access_expiry(expiry), path.stat().st_mtime
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
        seat_files.make_folder(home)  # never through a link its jail planted
        template = ship_template(ship_home, "settings.json")
        from . import claude_statusline
        defaults = merged(SETTINGS, {"statusLine": {
            "type": "command", "refreshInterval": 30,
            "command": shlex.join([sys.executable, "-I", "-S", str(Path(claude_statusline.__file__).resolve())]),
        }})
        settings = copy.deepcopy(merged(merged(defaults, template), seat.settings))  # policy cannot mutate shared defaults
        if not seat.human_session:
            permissions = settings.setdefault('permissions', {})
            permissions['deny'] = list(dict.fromkeys([*permissions.get('deny', []), 'AskUserQuestion']))
        durable.replace_file(home / "settings.json", json.dumps(settings, indent=2) + "\n")
        state_file = home / ".claude.json"
        # Read without following a link: a jailed home could plant one to an operator's file, which would be
        # merged here and written back where the jail reads it (seat_files.read_text reads a link as nothing).
        state = json.loads(text) if (text := seat_files.read_text(state_file)) else {}
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
        durable.replace_file(state_file, json.dumps(state, indent=2) + "\n")
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

    def dismissal_key_without_activity(self, screen: Screen) -> str | None:
        # This measured startup widget replaces the composer, so the generic
        # Screen reading cannot establish activity. Its exact decline choices
        # remain usable; no other notice or key inherits the exception.
        if (screen.box is not None or screen.unknown is not None or screen.starting
                or screen.fresh is not None):
            return None
        offer = auto_default_offer(screen.text)
        if offer is not None and screen.interruption == offer and offer.key in ('Down', 'Enter'):
            return offer.key
        return None

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

    background_versions = frozenset(BOTTOM_LINES)

    def background(self, screen: Screen, version: str | None) -> tuple[tuple[str, int], ...] | None:
        form = BOTTOM_LINES.get(version)
        lines = screen.text.rstrip().split("\n")
        found = form.fullmatch(lines[-1].rstrip()) if form is not None and screen.box is not None else None
        if found is None:
            return None
        shells, monitor = found["shells"], found.groupdict().get("monitor")
        if shells is None:
            return ()
        if found["noun"] != ("shell" if shells == "1" else "shells"):
            return None
        return (("shell", int(shells)), *((("monitor", 1),) if monitor else ()))

    def box(self, text: str) -> Box:
        """A paste folds when it is long or has three newlines or more (2.1.317). The placeholder counts the
        newlines, a trailing one too, and names no count at all for a paste without any."""
        newlines = text.count("\n")
        lines = rf" \+{newlines} lines" if newlines else ""
        # 2.1.317's literal composer wraps before a whole whitespace-delimited
        # word, including its hyphens; only a word wider than the text area splits.
        return ClaudeBox(text, folded=rf"\[Pasted text #\d+{lines}\]", wrap_hyphens=False)

    def controls(self) -> tuple[Control, ...]:
        return (
            Control("model", launch="--model <value>", session="/model {value}", confirmed_by="record"),
            Control("effort", launch="--effort <value>", session="/effort {value}", confirmed_by="screen"),
        )

    def interruptions(self) -> tuple[Interruption, ...]:
        return (
            ControlInterruption("switch-model confirmation", r"Switch model\?", control="model"),
            LOGIN_QUESTION,
            TRUST_QUESTION,
            PERMISSION_QUESTION,
        )

    def check_account_questions(self) -> tuple[tuple[Interruption, str], ...]:
        return ((LOGIN_QUESTION, 'account-login'), (TRUST_QUESTION, 'account-trust'),
                (PERMISSION_QUESTION, 'account-permission'))


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
