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
from typing import Callable

from . import claude_record, record_file
from .claude_limits import REFUSED_WINDOWS
from .json_record import JsonLinesReader, input_reader, records_matching
from .claude_record import entries as entries
from .claude_record import input_rows, inspect_entries
from .types import AUTHENTICATION, Inputs, State, TurnFailure, ControlAnswer, ControlContext, ControlInterruption
from .claude_limits import ExtraUsage, extra_usage_at_refusal, limit_wall, rejected_limit_info
from .claude_background import (notification as background_notification, BackgroundBash, BRIDGE_EVENT_ID, LINKED_VERSION,
                                STOPPED_RESUME,
                                AbsorbedWatch, BashRelaunchQueue, TaskNoticeQueue, WatchCompletionQueue,
                                bridge_link_event, bridge_pointer, bridge_status, linked_notice_origin,
                                linked_root, native_task_id, user_notice)
from .claude_children import Children, fingerprint as children_fingerprint

from .. import durable, seat_files
from ..ship import Pool, ProgramPin, SeatSpec, ShipError
from .base import (Box, ship_template, trimmed, record_fingerprint, Input, Control, Credential, Harness, Interruption, Keys, Launch, Screen, Tokens,
                   Turn, Usage, Window, credential, key_environment, merged, program_version, when)

ENDS_TURN = {"end_turn", "max_tokens", "stop_sequence"}
LOCAL_TAGS = ("<command-name>", "<local-command", "<bash-input>", "<bash-stdout>", "<bash-stderr>")
INTERRUPTED = "[Request interrupted"
PASTE_FORM_VERSIONS = frozenset({"2.1.317"})  # record-wide paste projections, including queue carriers
TYPED_PASTE_VERSION = "2.1.331"
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
TYPED_PASTE = re.compile(r'\n*<pasted_content id="([^"\n]+)">\n.*\n</pasted_content id="\1">\n*', re.S)
LEADING_PASTE_TAB = re.compile(r'^\t(?=[^\t\n])', re.M)
PASTED_OPENING_LINE = re.compile(r'^<pasted_content(?= id="[^"\n]+">$)', re.M)
DATA_FLOW_ROWS = ('data-flow-records', 'data-flow-incomplete', 'derived-from')
RESPONSE_ID = re.compile(r'msg_(?:staging_|local_)?[A-Za-z0-9]{1,64}')
TOOL_USE_ID = re.compile(r'toolu_[A-Za-z0-9]{1,64}')
COMPACT_SNAPSHOT_TOOLS = ('Agent', 'Bash', 'TaskStop', 'Read', 'SendUserFile', 'Write')  # last three: 2.1.331
# Measured on 2.1.331 person's seats (sanitized shapes): a tool event restated in a compaction snapshot. Remote
# Control events stay with their measured joins: a link at startup, an input bound to its typed root.
SNAPSHOT_EVENT_TOOLS = ('Read',)
REMOTE_CONTROL_EVENTS = ('RemoteControlLinked', 'RemoteControlInput')
MEMORY_WRITE_ID = re.compile(r'[-0-9_a-z]{55}')


def ai_title(entry: dict) -> bool:
    """The exact title bookkeeping shape measured on Claude Code 2.1.325."""
    return (set(entry) == {'type', 'aiTitle', 'sessionId'}
            and isinstance(entry['aiTitle'], str) and isinstance(entry['sessionId'], str)
            and bool(entry['sessionId']))


def _data_flow_response(entry) -> bool:
    if not isinstance(entry, dict) or not {'response_id'} <= entry.keys() <= {'response_id', 'mark_sent_by', 'listed'}:
        return False
    response = entry['response_id']
    sender = entry.get('mark_sent_by')
    listed = entry.get('listed', [])
    return (isinstance(response, str) and RESPONSE_ID.fullmatch(response) is not None
            and ('mark_sent_by' not in entry or isinstance(sender, str) and bool(sender))
            and isinstance(listed, list)
            and all(isinstance(item, str) and RESPONSE_ID.fullmatch(item) is not None for item in listed))


def _data_flow_snapshot_tool(entry) -> bool:
    return (isinstance(entry, dict) and entry.keys() == {'tool_name', 'tool_use_id'}
            and isinstance(entry['tool_name'], str) and bool(entry['tool_name'])
            and isinstance(entry['tool_use_id'], str) and TOOL_USE_ID.fullmatch(entry['tool_use_id']) is not None)


def _data_flow_event(entry, names=None) -> bool:
    return (isinstance(entry, dict) and entry.keys() == {'event_id', 'tool_name'}
            and isinstance(entry['tool_name'], str) and bool(entry['tool_name'])
            and (names is None or entry['tool_name'] in names)
            and isinstance(entry['event_id'], str) and BRIDGE_EVENT_ID.fullmatch(entry['event_id']) is not None)


def _data_flow_memory_write(entry, *, id_pattern=MEMORY_WRITE_ID) -> bool:
    """The program's own memory write, measured on 2.1.331: an event naming what it touched, never input."""
    touched = entry.get('touched') if isinstance(entry, dict) else None
    return (isinstance(entry, dict) and entry.keys() == {'event_id', 'touched'}
            and isinstance(entry['event_id'], str) and BRIDGE_EVENT_ID.fullmatch(entry['event_id']) is not None
            and isinstance(touched, dict) and touched.keys() == {'action', 'id', 'kind'}
            and touched['action'] == 'write' and touched['kind'] == 'memory'
            and isinstance(touched['id'], str) and id_pattern.fullmatch(touched['id']) is not None)


MIXED_REMOTE_INPUT = 'a Remote Control input of unknown time, unbound in its data-flow row'


def mixed_remote_input(row) -> bool:
    """The measured 2.1.331 row that carries a Remote Control input event beside response entries. Its event has no
    time and no established link to any typed row, so it is held under this name, never dated or dismissed."""
    items = row.get('records')
    return (row.get('type') == 'data-flow-records' and row.keys() == {'type', 'v', 'sessionId', 'records'}
            and type(row['v']) is int and row['v'] == 1 and isinstance(row['sessionId'], str) and bool(row['sessionId'])
            and isinstance(items, list) and len(items) >= 2
            and sum(_data_flow_event(item, ('RemoteControlInput',)) for item in items) == 1
            and all(_data_flow_response(item) or _data_flow_event(item, ('RemoteControlInput',)) for item in items))


def _derived_session(entry, session: str) -> bool:
    return (isinstance(entry, dict) and entry.keys() == {'type', 'id', 'via'}
            and entry['type'] == 'claude_code_session' and isinstance(entry['id'], str) and bool(entry['id'])
            and (entry['via'] in ('resume', 'branch') or entry['via'] == 'compact' and entry['id'] == session))


def _data_flow_snapshot_item(item, *, for_compaction: bool) -> bool:
    if _data_flow_snapshot_tool(item):
        return for_compaction or item['tool_name'] in COMPACT_SNAPSHOT_TOOLS
    if _data_flow_event(item):
        name = item['tool_name']
        if for_compaction:
            return name not in REMOTE_CONTROL_EVENTS
        return name in SNAPSHOT_EVENT_TOOLS
    return (_data_flow_response(item) or _data_flow_memory_write(item)
            or for_compaction and _data_flow_update_event(item))


def data_flow_metadata(row, *, for_compaction: bool = False) -> bool:
    """Root transcript bookkeeping from the native v1 writers and sanitized row shapes."""
    kind = row.get('type')
    if (kind not in DATA_FLOW_ROWS or type(row.get('v')) is not int or row['v'] != 1
            or not isinstance(row.get('sessionId'), str) or not row['sessionId']):
        return False
    fields = row.keys() - {'type', 'v', 'sessionId'}
    if kind == 'data-flow-records':
        items = row.get('records')
        snapshot = fields == {'records', 'snapshot'} and row['snapshot'] is True
        if not ((fields == {'records'} or snapshot) and isinstance(items, list)):
            return False
        if snapshot:
            # A compaction snapshot restates what the conversation already carried; it is not new input.
            return all(_data_flow_snapshot_item(item, for_compaction=for_compaction) for item in items)
        return (all(_data_flow_response(item) or _data_flow_memory_write(item) for item in items)
                or for_compaction and _compaction_data_flow_update(items))
    if kind == 'data-flow-incomplete':
        codes = row.get('codes')
        return (fields == {'codes'} and isinstance(codes, list) and bool(codes)
                and all(type(code) is int and 1 <= code <= 255 for code in codes))
    sessions = row.get('sessions')
    return (fields == {'sessions'} and isinstance(sessions, list) and bool(sessions)
            and all(_derived_session(link, row['sessionId']) for link in sessions))


_BRIDGE_STARTUP_METADATA = ('custom-title', 'agent-name', 'mode', 'permission-mode', 'session-owner')


class _RemoteControlStartup:
    """The 2.1.331 interactive bridge's root link event, before any conversational rows."""

    def __init__(self, session):
        self.phase = 'metadata'
        self.session = session
        self.linked = False

    def take(self, row) -> bool:
        if self.phase is None:
            return False
        phase, self.phase = self.phase, None
        if row.get('sessionId') != self.session:
            return False
        kind = row.get('type')
        if phase == 'metadata':
            if kind in _BRIDGE_STARTUP_METADATA:
                self.phase = phase
            elif bridge_pointer(row):
                self.phase = 'status'
        elif phase == 'status' and bridge_status(row, None):
            self.phase = 'snapshot'
        elif phase == 'snapshot':
            self.linked = bridge_link_event(row, snapshot=True) is not None
            return self.linked
        return False


_REMOTE_INPUT_ATTACHMENTS = (
    'deferred_tools_delta', 'agent_listing_delta', 'mcp_instructions_delta',
    'skill_listing', 'total_tokens_reminder', 'session_context',
)
_REQUEST_HASH = re.compile(r'[0-9a-f]{64}')
_REQUEST_KEYS = {'type', 'sessionId', 'id', 'timestamp', 'version', 'querySource', 'shapeHash',
                 'params', 'keep', 'tail', 'cacheControl', 'requestDigest'}


class _RemoteTypedInput:
    """Join the .331 undated input delta to a typed root and its contiguous request write."""

    def __init__(self, session):
        self.session = session
        self.at = None
        self.chain = []
        self.blobs = {}
        self.pending = False

    def take(self, row, at):
        if self.chain and self.attachment(row, at):
            self.chain.append(row['uuid'])
            self.at = at
            return
        if len(self.chain) == 1 + len(_REMOTE_INPUT_ATTACHMENTS) and self.event(row):
            self.chain = []
            self.pending = True
            return 'event'
        if self.pending and self.blob(row):
            self.blobs[row['hash']] = None
            return
        result = None
        if self.pending:
            result = 'bound' if self.request(row, at) else 'unbound'
        self.pending = False
        self.blobs.clear()
        self.chain = [row['uuid']] if self.typed(row, at) else []
        self.at = at if self.chain else None
        return result

    def typed(self, row, at):
        if (row.get('type') != 'user' or not linked_root(row, self.session)
                or row.get('version') != LINKED_VERSION or row.get('dataFlowOn') is not True
                or row.get('origin') != {'kind': 'human'} or row.get('promptSource') != 'typed'
                or row.get('turnOrigin') != 'human' or row.keys() & {'isMeta', 'isCompactSummary', 'agentId'}
                or at is None or not native_task_id(row.get('uuid')) or not native_task_id(row.get('promptId'))
                or row['promptId'] != row.get('turnPromptId')):
            return False
        message = row.get('message')
        return (isinstance(message, dict) and message.get('role') == 'user'
                and isinstance(message.get('content'), str) and bool(message['content'].strip()))

    def attachment(self, row, at):
        index = len(self.chain) - 1
        detail = row.get('attachment')
        return (index < len(_REMOTE_INPUT_ATTACHMENTS) and row.get('type') == 'attachment'
                and linked_root(row, self.session) and 'agentId' not in row
                and row.get('version') == LINKED_VERSION and row.get('dataFlowOn') is True
                and row.get('parentUuid') == self.chain[-1] and native_task_id(row.get('uuid'))
                and row['uuid'] not in self.chain and at is not None and at >= self.at
                and isinstance(detail, dict) and detail.get('type') == _REMOTE_INPUT_ATTACHMENTS[index]
                and (index != len(_REMOTE_INPUT_ATTACHMENTS) - 1 or row.get('session_id') == self.session))

    def event(self, row):
        records = row.get('records')
        if (row.keys() != {'type', 'v', 'sessionId', 'records'} or row['type'] != 'data-flow-records'
                or row['sessionId'] != self.session or type(row['v']) is not int or row['v'] != 1
                or not isinstance(records, list) or len(records) != 1):
            return False
        event = records[0]
        return (isinstance(event, dict) and event.keys() == {'tool_name', 'event_id'}
                and event['tool_name'] == 'RemoteControlInput' and isinstance(event['event_id'], str)
                and BRIDGE_EVENT_ID.fullmatch(event['event_id']) is not None)

    def blob(self, row):
        if (row.keys() != {'type', 'sessionId', 'hash', 'message'} or row['type'] != 'api-request-blob'
                or row['sessionId'] != self.session or not self.valid_hash(row['hash'])
                or row['hash'] in self.blobs or len(self.blobs) >= claude_record.ROW_BYTES // 67):
            return False
        message = row['message']
        return (isinstance(message, dict) and message.get('role') in ('user', 'system', 'assistant')
                and isinstance(message.get('content'), list))

    @staticmethod
    def valid_hash(value):
        return isinstance(value, str) and _REQUEST_HASH.fullmatch(value) is not None

    def request(self, row, at):
        keep, tail = row.get('keep'), row.get('tail')
        if (row.get('type') != 'api-request' or row.get('sessionId') != self.session
                or row.get('version') != LINKED_VERSION or row.get('querySource') != 'repl_main_thread'
                or at is None or at < self.at or type(keep) is not int or keep < 0
                or row.keys() != (_REQUEST_KEYS | {'base'} if keep else _REQUEST_KEYS)
                or not native_task_id(row['id']) or keep and not native_task_id(row['base'])
                or not isinstance(row['params'], dict) or not isinstance(row['cacheControl'], list)
                or not self.valid_hash(row['shapeHash']) or not self.valid_hash(row['requestDigest'])
                or not isinstance(tail, list) or not tail or not all(map(self.valid_hash, tail))):
            return False
        # The writer emits only uncached blobs, in the same order as its request's changed wire messages.
        hashes = iter(tail)
        return all(digest in hashes for digest in self.blobs)


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
            or entry.get("isCompactSummary") is True or linked_notice_origin(entry) or user_notice(entry)):
        return None
    content = entry.get("message", {}).get("content")
    if isinstance(content, list):
        if any(block.get("type") == "tool_result" for block in content):
            return None
        content = "\n".join(block.get("text", "") for block in content if block.get("type") == "text")
    if not isinstance(content, str) or content.startswith(LOCAL_TAGS):
        return None
    return unwrap(content)


@dataclass(frozen=True)
class TypedPaste(Input):
    """The measured wrapped paste written as a new human turn, with no joined queue slot."""


def _accepted_input(row: dict, text: str, stamp: datetime | None) -> Input:
    message = row['message']
    content = message.get('content')
    prompt = row.get('promptId')
    if (row.get('version') == TYPED_PASTE_VERSION and row.get('isSidechain') is False
            and row.get('isMeta') is not True and row.get('origin') == {'kind': 'human'}
            and row.get('promptSource') == 'typed' and row.get('turnOrigin') == 'human'
            and isinstance(prompt, str) and prompt and prompt == row.get('turnPromptId')
            and message.get('role') == 'user' and isinstance(content, str)
            and TYPED_PASTE.fullmatch(content)):
        return TypedPaste(text, stamp)
    return Input(text, stamp)


def _reactive_auto_boundary(row: dict, metadata: dict) -> bool:
    """The measured automatic boundary forms saved by the interactive 2.1.331 writer."""
    row_fields = {'type', 'subtype', 'content', 'level', 'parentUuid', 'logicalParentUuid', 'isSidechain',
                  'uuid', 'timestamp', 'userType', 'entrypoint', 'cwd', 'sessionId', 'version',
                  'gitBranch', 'slug', 'dataFlowOn', 'compactMetadata'}
    if ((row.keys() != row_fields and (row.keys() != row_fields | {'isMeta'} or row['isMeta'] is not False))
            or row['version'] != '2.1.331' or row['content'] != 'Conversation compacted' or row['level'] != 'info'
            or row['parentUuid'] is not None or row['isSidechain'] is not False or row['dataFlowOn'] is not True
            or row['userType'] != 'external' or row['entrypoint'] != 'cli' or when(row['timestamp']) is None
            or not all(isinstance(row[field], str) and row[field]
                       for field in ('uuid', 'logicalParentUuid', 'cwd', 'gitBranch', 'slug'))):
        return False

    metadata_fields = {'trigger', 'preTokens', 'postTokens', 'cumulativeDroppedTokens', 'durationMs',
                       'preservedSegment', 'preservedMessages', 'preCompactOrganizations'}
    cover_start = 'preCompactOrganizationsCoverStart'
    if cover_start in metadata and metadata[cover_start] is not True:
        return False
    fields = metadata.keys() - {cover_start}
    discovered_tools = {'preCompactDiscoveredTools'}
    if fields == metadata_fields:
        if cover_start not in metadata:
            return False
    elif 'isMeta' in row:
        if fields != metadata_fields | discovered_tools:
            return False
    elif fields != metadata_fields | discovered_tools | {'precomputed'} or metadata['precomputed'] is not True:
        return False

    if 'preCompactDiscoveredTools' in metadata:
        tools = metadata['preCompactDiscoveredTools']
        if not isinstance(tools, list) or not tools or not all(isinstance(tool, str) for tool in tools):
            return False
    if (not all(type(metadata[field]) is int and metadata[field] >= 0
                       for field in ('preTokens', 'postTokens', 'cumulativeDroppedTokens', 'durationMs'))
            or metadata['cumulativeDroppedTokens'] < max(0, metadata['preTokens'] - metadata['postTokens'])
            or not _compaction_ids(metadata['preCompactOrganizations'])):
        return False
    segment, messages = metadata['preservedSegment'], metadata['preservedMessages']
    if (not isinstance(segment, dict) or segment.keys() != {'headUuid', 'anchorUuid', 'tailUuid'}
            or not isinstance(messages, dict) or messages.keys() != {'anchorUuid', 'uuids', 'allUuids'}
            or not _compaction_ids(messages['uuids']) or not _compaction_ids(messages['allUuids'])):
        return False
    anchor, kept = messages['anchorUuid'], messages['uuids']
    return (isinstance(anchor, str) and bool(anchor) and anchor == segment['anchorUuid']
            and anchor != row['uuid'] and anchor not in messages['allUuids']
            and segment['headUuid'] == kept[0] and segment['tailUuid'] == kept[-1]
            and set(kept) <= set(messages['allUuids']))


def _compaction_ids(values) -> bool:
    return (isinstance(values, list) and bool(values)
            and all(isinstance(value, str) and value for value in values)
            and len(set(values)) == len(values))


_SHORT_MEMORY_WRITE_ID = re.compile(r'[-0-9_a-z]{42}')
_DATA_FLOW_SOURCE_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')


def _data_flow_update_event(event) -> bool:
    if _data_flow_memory_write(event, id_pattern=_SHORT_MEMORY_WRITE_ID):
        return True
    if (not isinstance(event, dict) or event.keys() != {'event_id', 'source'}
            or not isinstance(event['event_id'], str) or BRIDGE_EVENT_ID.fullmatch(event['event_id']) is None):
        return False
    source = event['source']
    if not isinstance(source, dict) or source.keys() != {'type', 'id'}:
        return False
    source_id = source['id']
    return ((source['type'] == 'claude_code_session' and isinstance(source_id, str)
             and _DATA_FLOW_SOURCE_ID.fullmatch(source_id) is not None)
            or (source['type'] == 'unknown' and source_id is None))


def _compaction_data_flow_update(items) -> bool:
    """Measured .331 updates that contain no compaction boundary, even when other readers hold them."""
    if not 1 <= len(items) <= 2:
        return False
    events = [item for item in items if not (_data_flow_response(item) and item.keys() == {'listed', 'response_id'})]
    if len(events) != 1:
        return False
    event = events[0]
    return _data_flow_update_event(event) or len(items) == 2 and _data_flow_event(event, ('Read',))


def compactions(record: Path):
    from .compactions import History, read

    class Interpret:
        """The compaction history, fed rows in turn and kept with the record, so an append feeds only its new
        rows (compactions.read); a row it refuses refuses the history, which then stays refused."""

        def __init__(self):
            self.history, self.seen, self.native_turn = History(), {}, None
            self.metadata_session = None
            self.version = None

        def work(self, row, *, turn=False):
            self.history.work(row, turn=turn)
            if isinstance(row, claude_record.ProjectedRow):
                # The guarded stream hashed the complete row, including image bytes.
                self.history.frontier[-1] = row.identity
                if turn:
                    self.history.turn = row.identity

        def feed(self, rows):
            for row in rows:
                kind = row.get('type')
                if kind in DATA_FLOW_ROWS:
                    if not data_flow_metadata(row, for_compaction=True):
                        raise ValueError('unknown native data-flow metadata')
                    session = row['sessionId']
                    if not isinstance(session, str) or not session:
                        raise ValueError('native metadata identity missing')
                    if (self.history.conversation or self.metadata_session) not in (None, session):
                        raise ValueError('mixed native conversation')
                    self.metadata_session = session
                    continue
                if 'isSidechain' in row and type(row['isSidechain']) is not bool:
                    raise ValueError('unknown root history identity')
                if row.get('isSidechain'):
                    continue
                if kind == 'ai-title' and not ai_title(row):
                    raise ValueError('unknown title bookkeeping')
                if kind not in (*_Reader.kinds, *INPUT_IGNORED):
                    raise ValueError('unknown native history')
                key = row.get('uuid')
                version = row.get('version')
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
                        if self.metadata_session not in (None, self.history.conversation):
                            raise ValueError('mixed native conversation')
                    if row.get('sessionId') != self.history.conversation:
                        raise ValueError('mixed native conversation')
                    self.seen[key] = parent
                    if isinstance(version, str) and version:
                        self.version = version
                elif kind in ('user', 'assistant') or row.get('subtype') == 'compact_boundary':
                    raise ValueError('native identity missing')
                if kind == 'system' and row.get('subtype') == 'compact_boundary':
                    metadata = row.get('compactMetadata')
                    if not isinstance(metadata, dict) or metadata.get('trigger') not in ('auto', 'manual'):
                        raise ValueError('unknown compaction boundary')
                    if (self.version == '2.1.331' and metadata['trigger'] == 'auto'
                            and not _reactive_auto_boundary(row, metadata)):
                        raise ValueError('unknown automatic compaction boundary')
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


class _ClaudeInputs(Inputs):
    def __init__(self):
        super().__init__()
        self.remote_events: dict[int, bool] = {}  # line -> whether the event reports undated person input


@input_reader(lambda row: (row.get('type') not in ('assistant', 'system', *INPUT_IGNORED)
                           and not data_flow_metadata(row))
              or (row.get('type') == 'api-request' and row.get('version') == LINKED_VERSION
                  and row.get('querySource') == 'repl_main_thread')
              or row.get('type') == 'ai-title' and not ai_title(row)
              or (row.get('type') == 'attachment' and isinstance(row.get('attachment'), dict)
                  and row['attachment'].get('type') == 'queued_command')
              or (row.get('type') == 'system' and row.get('subtype') in ('turn_duration', 'bridge_status',
                                                                      'compact_boundary')
                  and row.get('version') == LINKED_VERSION), parse=claude_record.parse)
def inputs(record: Path, snapshot) -> list[Input]:
    found, queued = _ClaudeInputs(), []
    dequeue = _TextlessDequeue()
    background = BackgroundBash(record)
    task_queue = TaskNoticeQueue(background)
    bash_relaunch = BashRelaunchQueue(background)
    watch_completion = WatchCompletionQueue(background)
    startup = _RemoteControlStartup(record.stem)
    remote_input = _RemoteTypedInput(record.stem)
    remote_line = None
    for line, row in input_rows(record, _Reader.kinds, _Reader.label, found, parse=lambda _: snapshot):
        if (not snapshot.ends_newline and line == snapshot.line_count
                and (bash_relaunch.pending or bash_relaunch.after_link is not None or remote_input.pending)):
            continue  # a partially appended row cannot finish either join
        kind = row.get('type')
        at = when(row.get('timestamp'))
        remote_disposition = remote_input.take(row, at)
        if remote_disposition == 'event':
            remote_line = line
        elif remote_disposition == 'bound':
            found.remote_events[remote_line] = False
        elif remote_disposition == 'unbound':
            found.skip(remote_line, 'data-flow-records', 'unknown native data-flow metadata')
        queue_empty = not any(phase in ('queued', 'pending') for _, phase in queued)
        private_relaunch = bash_relaunch.take(row, at, queue_empty=queue_empty)
        if startup.take(row):
            continue
        watch_disposition = watch_completion.take(row, at, queue_empty=queue_empty,
                                                  task_queue_empty=task_queue.pending is None)
        row = dequeue.take(row)
        if private_relaunch or watch_disposition != 'ordinary':
            if private_relaunch and kind == 'data-flow-records':
                found.remote_events[line] = bash_relaunch.undated_remote_input
            if 'version' in row:
                task_queue.version = row['version']
            continue
        if kind in DATA_FLOW_ROWS:
            if remote_disposition != 'event' and not data_flow_metadata(row):
                found.skip(line, kind, MIXED_REMOTE_INPUT if mixed_remote_input(row)
                           else 'unknown native data-flow metadata')
            continue
        private, _ = task_queue.take(row, at, complete=snapshot.ends_newline or line != snapshot.line_count)
        if private:
            continue  # the native task queue produces no receipt for a person
        if kind == 'ai-title' and not ai_title(row):
            found.skip(line, 'ai-title', 'unknown title bookkeeping')
            continue
        if row.get('isSidechain'):
            if kind not in (*INPUT_IGNORED, *_Reader.kinds):
                found.skip(line, 'unknown', 'unknown row kind')
            continue
        try:
            stamp = at
            background.observe(row, stamp)
            background.observe_tools(row, stamp)
            if kind in INPUT_IGNORED:
                continue  # known non-input metadata does not establish the program format
            if kind not in _Reader.kinds:
                found.skip(line, 'unknown', 'unknown row kind')
                continue
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
                        found.append(_accepted_input(row, text, stamp))
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
    if remote_input.pending:
        found.skip(remote_line, 'data-flow-records', 'unknown native data-flow metadata')
    if (task_queue.pending is not None or task_queue.unknown or bash_relaunch.pending or bash_relaunch.unknown
            or watch_completion.pending is not None or watch_completion.unknown):
        found.skip(0, 'queue-operation', 'task notification queue unresolved')
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
    snapshot = claude_record.parse(record)
    rows = input_rows(record, _Reader.kinds, _Reader.label, evidence, parse=lambda _: snapshot)
    if evidence.problems:
        raise ValueError('record has unreadable rows')
    found = []
    startup = _RemoteControlStartup(Path(record).stem)
    remote_events = None
    undated_remote = False

    def person(row, text):
        if text is not None and text.strip() in excluded:
            return
        stamp = when(row.get('timestamp'))
        if stamp is None:
            raise ValueError('an input row has no time')
        found.append(stamp)

    for line, row in rows:
        if startup.take(row):
            continue
        kind = row.get('type')
        if kind in DATA_FLOW_ROWS and not data_flow_metadata(row):
            if remote_events is None:
                # Use the receipt reader's exact same row and task joins on this observation.
                remote_events = inputs.__wrapped__(record, snapshot).remote_events
            if line not in remote_events:
                raise ValueError(MIXED_REMOTE_INPUT if mixed_remote_input(row) else 'unknown native data-flow metadata')
            undated_remote |= remote_events[line]
            continue
        if 'isSidechain' in row and type(row['isSidechain']) is not bool:
            raise ValueError('an input row has an unknown history identity')
        if row.get('isSidechain'):
            continue
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
    if undated_remote:
        # The bridge status supplies only a lower bound. Later row position supplies no upper bound.
        raise ValueError('an input row has no time')
    return sorted(found)


def usage_tokens(usage: dict) -> Tokens:
    return Tokens.from_usage(usage, ("cache_read_input_tokens", "input_tokens", "cache_creation_input_tokens", "output_tokens"))


QUEUE_COMPLETION_VERSIONS = frozenset({'2.1.317'})  # crew enqueue/receipt/turn_duration measurements

# The 2.1.331 interactive finalizer omits turn_duration on simple terminal API failures.
TERMINAL_ERROR_VERSION = '2.1.331'
# The refusal witness links the assistant back to the root through these context attachments.
USAGE_REFUSAL_ATTACHMENTS = (
    'environment', 'model', 'deferred_tools_delta', 'agent_listing_delta', 'skill_listing',
    'session_context', 'date', 'remote_session_change', 'prompt_snapshot',
)


SYNTHETIC_MODEL = '<synthetic>'


RESUME_PLACEHOLDER = 'No response requested.'


def _synthetic_model(entry: dict, message: dict) -> bool:
    """The program's own placeholder reply, which names no model that ran, in its two measured forms: the API error
    envelope (2.1.317, 2.1.330) and the 2.1.331 resume placeholder (not an error, made by the program, its fixed
    text). Each is a stop-sequence reply with no output tokens."""
    usage, content = message.get('usage'), message.get('content')
    if not (message.get('model') == SYNTHETIC_MODEL and message.get('role') == 'assistant'
            and message.get('stop_reason') == 'stop_sequence' and isinstance(usage, dict)
            and type(usage.get('output_tokens')) is int and usage['output_tokens'] == 0
            and isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
            and content[0].get('type') == 'text' and isinstance(content[0].get('text'), str)):
        return False
    if entry.get('isApiErrorMessage') is True:
        return True
    return (entry.get('version') == LINKED_VERSION and entry.get('isApiErrorMessage') is False
            and entry.get('madeBy') == 'other' and content[0]['text'] == RESUME_PLACEHOLDER)


def _terminal_api_error(entry: dict, message: dict) -> bool:
    """The native synthetic envelope of a failed response with no tool call."""
    content = message.get('content')
    return (entry.get('version') == TERMINAL_ERROR_VERSION
            and entry.get('isSidechain') is False
            and entry.get('isApiErrorMessage') is True
            and entry.get('failedResponseHadNoToolCall') is True
            and type(entry.get('apiErrorStatus')) is int
            and all(entry.get(key, False) is False for key in
                    ('isMeta', 'isVirtual', 'apiErrorIsTransient', 'truncatedAfterOutput', 'streamWatchdogGaveUp'))
            and entry.get('streamCutCause') is None
            and message.get('model') == SYNTHETIC_MODEL and message.get('role') == 'assistant'
            and message.get('type') == 'message'
            and message.get('stop_reason') == 'stop_sequence' and message.get('stop_sequence') == ''
            and isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
            and content[0].get('type') == 'text'
            and isinstance(content[0].get('text'), str) and bool(content[0]['text'].strip()))


def _usage_limit_refusal(entry: dict, chain: list[str]) -> bool:
    uuid = entry.get('uuid')
    if (entry['apiErrorStatus'] != 429 or not chain or entry.get('parentUuid') != chain[-1]
            or not isinstance(uuid, str) or not uuid or uuid in chain):
        return False
    info = rejected_limit_info(entry)
    if info is None:
        return False
    window = info.get('rateLimitType')
    return (isinstance(window, str) and window in REFUSED_WINDOWS and type(info.get('resetsAt')) is int
            and info.get('isUsingOverage') is False and info.get('dailyCapPaused', False) is False)


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
    absorbed_watch: AbsorbedWatch | None = None
    input_hold: str | None = None  # an input the record holds by name, of unknown time; no screen lifts it


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
        if snapshot is None:
            snapshot = self.parse_snapshot(record)
        self.unterminated = snapshot.last_row[1] if not snapshot.ends_newline and snapshot.last_row else None
        self.orphan_auth = None
        self.sidechain_ends = set()
        self.subagent_turns = 0
        self.textless_dequeue = _TextlessDequeue()
        self.queue_slots = []  # FIFO, including returned drafts and consumed echo identities
        self.queue_version = None
        self.background = BackgroundBash(record)
        self.task_queue = TaskNoticeQueue(self.background)
        self.bash_relaunch = BashRelaunchQueue(self.background)
        self.watch_completion = WatchCompletionQueue(self.background)
        self.children = Children(record)
        self.metadata_session = None
        self.remote_control_startup = _RemoteControlStartup(record.stem)
        self.remote_input = _RemoteTypedInput(record.stem)
        self.mixed_remote = False  # the measured mixed Remote Control input row: a named hold for the record's life
        self.walls = []
        self.extra_usage = []
        self.notice_input = None  # accounting candidate only: a notice cannot open a turn
        self.notice_unanswered, self.notice_at = False, None  # a notice no assistant row has followed, and its time
        super().__init__(record, snapshot)

    def state(self, *, launched_at=None):
        state = replace(self.children.project(super().state(), self.background, INPUT_IGNORED, ai_title,
                                              launched_at=launched_at), walls=tuple(self.walls))
        startup = self.remote_control_startup
        if (self.remote_input.pending
                or self.metadata_session is not None and self.children.sessions != {self.metadata_session}
                or startup.linked and self.children.sessions - {startup.session}):
            state = replace(state, pending_tools=None)
        if state.queue_error is None and any(slot.uncertain and slot.phase in ('queued', 'pending')
                                             for slot in self.queue_slots):
            state = replace(state, queue_error='unknown queue operation')
        if (self.task_queue.pending is not None or self.task_queue.unknown
                or self.bash_relaunch.pending or self.bash_relaunch.unknown
                or self.watch_completion.pending is not None or self.watch_completion.unknown):
            named = (self.task_queue.causes == {'stopped'} and self.task_queue.pending is None
                     and not (self.bash_relaunch.pending or self.bash_relaunch.unknown
                              or self.watch_completion.pending is not None or self.watch_completion.unknown))
            state = replace(state, pending_tools=None, queue_error=state.queue_error or (
                STOPPED_RESUME if named else 'task notification queue unresolved'))
        if self.answering_notice():
            state = replace(state, turn_open=None)
        if self.open is None and self.orphan_auth:
            state = replace(state, failure=self.orphan_auth)
        absorbed = None if self.unterminated is not None else self.watch_completion.absorbed_watch()
        held = MIXED_REMOTE_INPUT if self.mixed_remote else None
        if self.extra_usage or absorbed is not None or held is not None:
            return _ClaudeState(**vars(state), extra_usage=tuple(self.extra_usage), absorbed_watch=absorbed,
                                input_hold=held)
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
        if (entry is self.unterminated
                and (self.bash_relaunch.pending or self.bash_relaunch.after_link is not None
                     or self.remote_input.pending)):
            self.tools_known = False
            return
        kind = entry.get("type")
        at = when(entry.get('timestamp'))
        remote_disposition = self.remote_input.take(entry, at)
        if remote_disposition == 'unbound':
            self.tools_known = False
        private_relaunch = self.bash_relaunch.take(entry, at, queue_empty=not self.queued and self.open is None)
        if self.remote_control_startup.take(entry):
            return  # bridge status already joins this metadata to the root before its first turn
        watch_disposition = self.watch_completion.take(entry, at, queue_empty=not self.queued,
                                                        task_queue_empty=self.task_queue.pending is None)
        if kind in DATA_FLOW_ROWS:
            if private_relaunch or remote_disposition == 'event':
                return
            if not data_flow_metadata(entry) or self.metadata_session not in (None, entry['sessionId']):
                self.tools_known = False
                if mixed_remote_input(entry) and entry['sessionId'] == self.remote_input.session:
                    self.mixed_remote = True
            else:
                self.metadata_session = entry['sessionId']
            return
        entry = self.textless_dequeue.take(entry)
        self.children.observe(entry)
        if not entry.get('isSidechain') and 'version' in entry:
            self.queue_version = entry['version']
        if private_relaunch or watch_disposition != 'ordinary':
            if 'version' in entry:
                self.background.version = self.task_queue.version = entry['version']
            if watch_disposition == 'turn':
                self.start_notice_turn(at)
            return
        private, evidence = self.task_queue.take(entry, at, complete=entry is not self.unterminated)
        if private:
            if evidence is not None:
                notice, enqueued_at = evidence
                if notice.kind == 'bash':
                    self.background.delivered_to_child(notice, enqueued_at, self.children.compacted_at)
                    if kind == 'user':
                        self.start_notice_turn(at)  # a recorded callback can own later root tool calls
                else:
                    self.children.notice(notice, enqueued_at)
                    self.start_notice_turn(at)
            return
        if kind == 'ai-title' and not ai_title(entry):
            self.tools_known = False
            return
        if kind == "assistant":
            info = rejected_limit_info(entry)
            uuid = entry.get("uuid")
            refusal_at = at if info is not None else None
            wall = limit_wall(info, f"limit:{uuid}" if isinstance(uuid, str) and uuid else f"limit:{len(self.walls)}", refusal_at)
            if wall is not None:
                self.walls.append(wall)  # the provider refused work at this limit, whichever branch asked
            if (reading := extra_usage_at_refusal(info, refusal_at)) is not None:
                self.extra_usage.append(reading)
        if not entry.get('isSidechain'):
            self.background.observe(entry, at)
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
            self.usage_refusal_attachment(entry)
            if (text := queued_notice(entry)) is not None:
                self.consume_queued(text, receipt=entry)
        elif kind == "assistant":
            self.assistant(entry)
        elif kind == "system" and entry.get("subtype") == "turn_duration":
            self.notice_input = None
            self.notice_unanswered = False
            if self.open is not None:
                self.end(when(entry.get("timestamp")), native_end=True)
                self.complete_queue(entry)
        elif kind not in ('system', *INPUT_IGNORED):
            self.tools_known = False

    def start_notice_turn(self, at):
        if self.open is None:
            self.start_turn(dict(started=at, input='', usage={}, tools=[],
                                 reply='', model=None, subagents=self.subagent_turns))
        else:
            self.tools_known = False  # this native notice belongs to a separate root turn

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
            if match := set_model_match(content, entry.get('version')):
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
            session = entry.get('sessionId')
            if (entry.get('version') == TERMINAL_ERROR_VERSION and entry.get('isSidechain') is False
                    and entry.get('isMeta', False) is False and message.get('role') == 'user'
                    and self.last_input_at is not None and isinstance(session, str) and session):
                self.open['terminal_error_session'] = session
                uuid = entry.get('uuid')
                if isinstance(uuid, str) and uuid and text.strip() and entry.get('isVirtual', False) is False:
                    self.open['usage_refusal_chain'] = [uuid]
            return
        for block in entry.get("message", {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                self.background.result(block.get('tool_use_id'), entry, block, when(entry.get('timestamp')),
                                       linked=linked_root(entry, self.record.stem))
                self.children.result(block.get('tool_use_id'), entry, block, when(entry.get('timestamp')))
                self.close_tool(block.get("tool_use_id"), when(entry.get("timestamp")), bool(block.get("is_error")))

    def usage_refusal_attachment(self, entry: dict) -> None:
        """Extend the bounded lineage from a typed root through measured context rows."""
        if self.open is None or not (chain := self.open.get('usage_refusal_chain')):
            return
        uuid, attachment = entry.get('uuid'), entry.get('attachment')
        if (len(chain) <= len(USAGE_REFUSAL_ATTACHMENTS)
                and entry.get('version') == TERMINAL_ERROR_VERSION and entry.get('isSidechain') is False
                and entry.get('isMeta', False) is False and entry.get('isVirtual', False) is False
                and entry.get('sessionId') == self.open['terminal_error_session']
                and entry.get('parentUuid') == chain[-1] and isinstance(uuid, str) and uuid and uuid not in chain
                and isinstance(attachment, dict) and attachment.get('type') in USAGE_REFUSAL_ATTACHMENTS):
            chain.append(uuid)
        else:
            del self.open['usage_refusal_chain']

    def assistant(self, entry: dict) -> None:
        """One entry per content block, all carrying the message's usage and stop reason.

        A stop reason alone cannot end a turn: the program may take a queued message into
        the same turn after an ``end_turn`` block. A measured terminal API error can end
        the simple turn that produced it without a duration row.
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
        if not _synthetic_model(entry, message):
            self.model = message.get("model") or self.model  # a native error envelope names no model that ran
        self.effort = entry.get("effort") or self.effort
        if self.open is None:
            if any(block.get('type') == 'tool_use' for block in message.get('content') or []):
                self.tools_known = False  # an out-of-turn call is not absent execution
            return
        terminal_at = self._terminal_error_end(entry, message)
        self.open["model"] = self.model
        key = message.get("id") or entry.get("requestId") or entry.get("uuid") or len(self.open["usage"])
        measured = usage_tokens(message.get("usage"))
        if key not in self.open["usage"] or measured.complete:
            self.open["usage"][key] = measured
        for block in message.get("content") or []:
            if block.get("type") == "tool_use":
                name = block.get('name', '?')
                arguments = block.get('input')
                linked = linked_root(entry, self.record.stem)
                child = self.children.call(block.get('id'), name, arguments, when(entry.get('timestamp')),
                                           entry.get('version'), linked=linked)
                if (block.get('id') in self.live_tools or name in ('Task', 'Agent') and not child
                        or arguments is not None and not isinstance(arguments, dict)
                        or name != 'Bash' and not child and isinstance(arguments, dict) and arguments.get('run_in_background')):
                    self.tools_known = False  # delegation remains opaque; measured Bash has its own lifetime
                self.background.call(block.get('id'), name, arguments, when(entry.get('timestamp')),
                                     entry.get('version'), linked=linked, row_uuid=entry.get('uuid'))
                self.open_call(block.get('id'), name, when(entry.get('timestamp')))
            elif block.get("type") == "text" and block.get("text"):
                self.open["reply"] = (self.open["reply"] + "\n\n" + block["text"]).strip()
        if terminal_at is not None:
            self.end(terminal_at, native_end=True)

    def _terminal_error_end(self, entry: dict, message: dict) -> datetime | None:
        session = self.open.get('terminal_error_session')
        if (session is None or entry.get('sessionId') != session
                or self.open['usage'] or self.open['tools'] or self.live_tools
                or not _terminal_api_error(entry, message)):
            return None
        overload = entry['apiErrorStatus'] == 529 and entry.get('error') == 'server_error' and entry.get('apiError') is None
        if not overload and not _usage_limit_refusal(entry, self.open.get('usage_refusal_chain', [])):
            return None
        at = when(entry.get('timestamp'))
        if at is not None and at >= self.open['started']:
            # Check background evidence separately. Unknown rows may independently limit
            # execution knowledge, and queued input still needs its own receipt.
            background = self.background.state(State(pending_tools=()))
            if background.pending_tools == ():
                return at
        return None

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
#            way to the count of shells, and of one monitor beside them. One feedback draft can appear beside
#            the cycling hint at rest or beside a single shell. A helper agent adds a hint beside the cycling hint
#            and a panel of its own below the line (a throwaway seat's capture);
#            for some thirty seconds after it ended, and for the first seconds after a launch, a hint alone
#   2.1.325  the same two bodies, each followed by the name of the session's published page; a helper agent
#            running in the background adds a panel of its own below the line, gone when the helper ends
#   2.1.331  the mover witness saw the cycling hint with no shell after restart and the manage hint
#            with one shell (its inbox watch) before and after manual compaction.
# Any other bottom line, or one that is not the screen's last, says nothing here.
BOTTOM_LINES = {
    "2.1.317": re.compile(r"  ⏵⏵ bypass permissions on(?: \(shift\+tab to cycle\)(?: · 1 feedback draft)?"
                          r"| · (?P<shells>[1-9]\d*) (?P<noun>shells?)"
                          r"(?:(?P<monitor>, 1 monitor)|(?<= · 1 shell) · 1 feedback draft)?)"),
    "2.1.325": re.compile(r"  ⏵⏵ bypass permissions on(?: \(shift\+tab to cycle\)"
                          r"| · (?P<shells>[1-9]\d*) (?P<noun>shells?)) · ⧉ \S+"),
    LINKED_VERSION: re.compile(r"  ⏵⏵ bypass permissions on(?: \(shift\+tab to cycle\)"
                               r"| · (?P<shells>1) (?P<noun>shell) · ↓ to manage)"),
}
SET_MODEL = re.compile(r"Set model to `?([^`]+?)`? and saved")
SESSION_MODEL = re.compile(r"Set model to `?([^`\r\n]+?)`? for this session only")
SET_EFFORT = re.compile(r"Set effort level to `?(\w+)`?")
QUEUED_PLACEHOLDER = "Press up to edit queued messages"
# Exact native IDs established in assistant rows. ModelPicker/account catalogs may override display names:
# a display-only command-output line never establishes identity or record confirmation.
MODEL_IDENTITIES = frozenset({'claude-haiku-5-5', 'claude-sonnet-5-5', 'claude-opus-5-5'})
CONTROL_VERSION = '2.1.331'
CONTROL_VERSIONS = frozenset({'2.1.317', CONTROL_VERSION})
MODEL_QUESTION = 'Switch model?'
NAMED_CONTROL_EFFORTS = frozenset({'low', 'medium', 'high', 'xhigh', 'max'})


def set_model_match(content: str, version: str | None):
    return SET_MODEL.search(content) or (SESSION_MODEL.search(content) if version == CONTROL_VERSION else None)


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


def catalog_display(data: dict, target: str, *, launched: datetime, now: datetime,
                    unique_name: bool = False) -> tuple[str, int, int] | None:
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
        if unique_name and sum(row.get('name') == name for row in rows) != 1:
            return None
        return name, fetched, stale
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def model_display(seat, target, launched):
    """A per-seat fresh catalog precondition, never a global display-name alias."""
    try:
        if launched is None or launched.version not in CONTROL_VERSIONS:
            return None
        candidate = launched.version == CONTROL_VERSION
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
            bound = catalog_display(data, target, launched=launched.when, now=datetime.now(timezone.utc),
                                    unique_name=candidate)
            if bound is not None:
                display, fetched, stale = bound
                candidates.append(dict(display=display, catalog=str(path), fetched=fetched, stale=stale,
                                       digest=hashlib.sha256(raw).hexdigest(),
                                       settings=hashlib.sha256(settings_raw).hexdigest()))
                if candidate:
                    candidates[-1].update(version=launched.version, target=target)
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
    """The established 2.1.317 result, also used by the synthetic adapter."""
    return command_result(record, origin, 'model', value, display)


def command_result(record: Path, origin: dict, name: str, value: str, display: str,
                   *, version: str | None = None) -> str | None:
    """A new exact local command/result triple linked by native parents."""
    try:
        before = record.stat()
        rows = control_suffix(record, origin)
        if rows is None:
            return None
        triples = []
        command_tag = f'<command-name>/{name}</command-name>'
        for index, row in enumerate(rows):
            content = row.get('message', {}).get('content')
            # Any second same-kind command makes ownership ambiguous, even if its target
            # happens to agree. A missing caveat/result cannot be filled from older history.
            if row.get('type') != 'user' or not isinstance(content, str) or command_tag not in content:
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
            if (not isinstance(introduction, str) or not introduction.startswith('<local-command-caveat>')
                    or arguments != [value] or not isinstance(output, str)
                    or not output.startswith('<local-command-stdout>')):
                return None
            if version == CONTROL_VERSION:
                session = row.get('sessionId')
                command = (f'{command_tag}\n            <command-message>{name}</command-message>\n'
                           f'            <command-args>{value}</command-args>')
                if (session != record.stem or content != command
                        or any(item.get('type') in ('user', 'queue-operation') for item in rows[:index - 1])
                        or any(item.get('sessionId') != session or item.get('version') != version
                               or item['message'].get('role') != 'user' for item in group)
                        or not introduction.endswith('</local-command-caveat>')
                        or not candidate_control_output(output, name, display)):
                    return None
            elif SET_MODEL.findall(output) != [display]:
                return None
            triples.append(group)
        after = record.stat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            return None
        return value if len(triples) == 1 else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def candidate_control_output(output: str, name: str, display: str) -> bool:
    end = '</local-command-stdout>'
    if not output.endswith(end):
        return False
    body = output[len('<local-command-stdout>'):-len(end)]
    if name == 'model':
        labels = (display, f'`{display}`')
        return body in {f'Set model to {label}{suffix}' for label in labels
                        for suffix in (' and saved as your default for new sessions', ' for this session only')}
    return re.fullmatch(
        rf'Set effort level to {re.escape(display)} '
        r'\((?:saved as your default for new sessions|this session only)\): [^\r\n]+', body) is not None


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


def zai_settings(template, settings) -> dict:
    """WebFetch's domain lookup stays off, and no session is mirrored to claude.ai (2.1.331 user settings)."""
    for layer in (template, settings):
        if layer.get('skipWebFetchPreflight', True) is not True:
            raise ShipError('Z.ai Claude requires skipWebFetchPreflight to be true')
        if layer.get('autoUploadSessions', False) is not False:
            raise ShipError('Z.ai Claude refuses autoUploadSessions')
    return dict(skipWebFetchPreflight=True)


class ClaudeHarness(Harness):
    def __init__(self, program_pin: ProgramPin | None = None):
        self.program_pin = program_pin

    def with_program_pin(self, pin: ProgramPin) -> ClaudeHarness:
        return type(self)(pin)

    def selected_program(self) -> Path | None:
        """The verified runtime copy, if declared. Otherwise Claude resolves through PATH."""
        return self.program_pin.verified() if self.program_pin is not None else None

    def version(self, home: Path | None = None) -> str | None:
        program = self.selected_program() or self.program
        return program_version(("env", "DISABLE_AUTOUPDATER=1", str(program), "--version"))

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
        if value in MODEL_IDENTITIES or (version == CONTROL_VERSION and value == 'claude-fable-5-2'):
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
                model = self.model_identity(message['model'], version=row.get('version'))
            elif row.get('type') == 'user':
                content = message.get('content')
                if (isinstance(content, str) and content.startswith('<local-command-stdout>')
                        and set_model_match(content, row.get('version'))):
                    model = None
        return model if record_fingerprint(record) == before else None

    def model_control_origin(self, seat, record: Path | None, value: str) -> dict | None:
        if record is None:
            return None
        try:
            launched = seat.launched()
        except (OSError, ValueError):
            return None
        if launched is None or self.model_identity(value, version=launched.version) is None:
            return None
        display = model_display(seat, value, launched)
        return control_origin(record, display) if display is not None else None

    def model_control_result(self, seat, record: Path, value: str, origin: dict) -> str | None:
        launched = seat.launched()
        if launched is None or self.model_identity(value, version=launched.version) is None:
            return None
        if launched.version == CONTROL_VERSION:
            display = origin.get('display', {})
            label = display.get('display')
            if display.get('version') == CONTROL_VERSION and display.get('target') == value and isinstance(label, str):
                # The native command can save the model and invalidate its catalog cache. The
                # admission-time display remains the binding for the exact appended result.
                return command_result(record, origin, 'model', value, label, version=CONTROL_VERSION)
            return None
        current_display = model_display(seat, value, launched)
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

    @staticmethod
    def request_bound_effort(version: str | None) -> bool:
        return version == CONTROL_VERSION

    def effort_control_origin(self, seat, record: Path | None, value: str) -> dict | None:
        if record is None or value not in NAMED_CONTROL_EFFORTS:
            return None
        return control_origin(record, dict(version=CONTROL_VERSION, target=value))

    def effort_control_result(self, seat, record: Path, value: str, origin: dict) -> str | None:
        if origin.get('display') != dict(version=CONTROL_VERSION, target=value):
            return None
        return command_result(record, origin, 'effort', value, value, version=CONTROL_VERSION)

    def bound_control_current(self, seat, record: Path, control: Control, value: str, origin: dict) -> bool | None:
        launch = seat.launched()
        if launch is None:
            return None
        if launch.version == CONTROL_VERSION:
            display = (model_display(seat, value, launch) if control.name == 'model'
                       else dict(version=launch.version, target=value))
            if display is None or display != origin.get('display'):
                return None
        return self.model_control_current(record, origin)

    def control_context(self, seat, control: Control, value: str, screen: Screen,
                        *, version: str | None = None, question: str | None = None) -> ControlContext | None:
        target = self.control_target(control, value, version)
        if target is None or question is None:
            return None
        launch = seat.launched()
        if launch is None or launch.version != version:
            return None
        display = model_display(seat, value, launch)
        if display is None or (frame := seat.read_control_frame()) is None:
            return None
        geometry = frame.pane.width, frame.pane.height
        if (frame.cursor_visible is not False or (frame.pane.id, frame.pane.pid) != (launch.pane, launch.pid)
                or version != CONTROL_VERSION and geometry != (120, 40)):
            return None
        rows = styled_rows(frame.text)
        if rows is None or len(rows) != frame.pane.height:
            return None
        plain = '\n'.join(row[0] for row in rows) + '\n'
        if hashlib.sha256(screen.text.encode()).hexdigest() != question or self.screen(plain).box is not None:
            return None
        if version == CONTROL_VERSION:
            # A styled capture can retain trailing background cells omitted by the plain capture.
            if [row.rstrip(' ') for row, _ in rows] != [row.rstrip(' ') for row in screen.text.splitlines()]:
                return None  # physical/joined content or row count disagreement, including wraps
            return self.candidate_control_context(target, display, frame, rows, question)
        if plain.rstrip('\n') != screen.text.rstrip('\n'):
            return None
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
        titles = [index for index, row in enumerate(rows) if row[0].strip() == MODEL_QUESTION]
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

    def control_target(self, control: Control, value: str, version: str | None) -> str | None:
        if control.name == 'model' and version in CONTROL_VERSIONS:
            return self.model_identity(value, version=version)
        return None

    @staticmethod
    def candidate_control_context(target, display, frame, rows, question) -> ControlContext | None:
        from .types import _cells

        width = frame.pane.width
        label = display['display']
        yes = f'Yes, switch to {label}'
        dialog = ('▔' * width, f'  {MODEL_QUESTION}',
                  '  Your next response will be slower and use more tokens', '',
                  f'  This conversation is cached for the current model. Switching to {label}'
                  ' means the full history gets re-read.',
                  '', f'  ❯ 1. {yes}', '    2. No, go back', '', '  Enter to confirm · Esc to cancel')
        if (tuple(row.rstrip(' ') for row, _ in rows[-len(dialog):]) != dialog
                or any((cells := _cells(row)) is None or cells > width for row in dialog)):
            return None
        styled = frame.text.splitlines()
        selected, number = '\x1b[38;2;177;185;249m', '\x1b[38;2;153;153;153m'
        focus = f'  {selected}❯\x1b[39m {number}1. {selected}{yes}\x1b[39m'
        if (styled[-4].rstrip(' ') != focus
                or styled[-3].rstrip(' ') != f'    {number}2. \x1b[39mNo, go back'):
            return None
        scope = (frame.pane.id, frame.pane.pid, frame.pane.width, frame.pane.height,
                 display.get('catalog'), display.get('digest'), display.get('settings'), question,
                 hashlib.sha256(frame.text.encode()).hexdigest())
        return ControlContext(CONTROL_VERSION, target, label, screen_reader_off=True, focused=True,
                              composer_hidden=True, cursor_visible=False, yes_highlighted=True, scope=scope)

    def control_answer(self, control: Control, value: str, screen: Screen,
                       *, version: str | None = None, context: ControlContext | None = None) -> ControlAnswer | None:
        if (not isinstance(screen.interruption, ControlInterruption)
                or screen.interruption.control != control.name):
            return None
        target = self.control_target(control, value, version)
        if (not isinstance(context, ControlContext) or context.version != version or context.target != target
                or target is None or context.screen_reader_off is not True or context.focused is not True
                or context.composer_hidden is not True or context.cursor_visible is not False
                or context.yes_highlighted is not True
                or not context.scope or not context.display or len(context.display) > 200
                or not all(char.isprintable() for char in context.display)):
            return None
        if sum(line.strip(' \u00a0') == MODEL_QUESTION for line in screen.text.splitlines()) != 1:
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
        return ControlAnswer(control.name, value, label, ('Enter',))

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
    sign_in_note = ("Current Claude Code docs place file-backed credentials in CLAUDE_CONFIG_DIR. "
                    "Use qd doctor to check whether the declared pool is ready.")
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
        if "\t" not in text and PASTE_TAG not in text or self._paste_versions(record, since) != PASTE_FORM_VERSIONS:
            return text
        return text.replace("\t", "    ").replace(PASTE_TAG, PASTE_TAG_ESCAPED)

    def paste_ignored(self, text: str, record: Path, since: datetime) -> frozenset[str]:
        """Measured on 2.1.317 (13 November): the program drops a zero-width space (U+200B) from a paste, in the
        queued entry, its removal and its attachment alike. Its sanitiser treats the other format characters of
        ``PASTE_HIDDEN_FORMAT`` the same way, removing each unless its context keeps it, so the receipt removes
        exactly these from both sides before comparing; nothing else is ignored. Only for a payload that holds
        one, and under the same version scope as ``recorded_paste``.
        """
        if PASTE_HIDDEN_FORMAT.isdisjoint(text) or self._paste_versions(record, since) != PASTE_FORM_VERSIONS:
            return frozenset()
        return PASTE_HIDDEN_FORMAT

    def recorded_input_projection(self, forms: tuple[str, ...], record: Path,
                                  since: datetime) -> Callable[[Input], tuple[str, ...]] | None:
        """The 2.1.331 wrapped idle paste only, with its measured one-way projection."""
        projected = tuple(PASTED_OPENING_LINE.sub(lambda _: PASTE_TAG_ESCAPED,
                          LEADING_PASTE_TAB.sub('    ', form)).replace('\u200b', '') for form in forms)
        if projected == forms or self._paste_versions(record, since) != {TYPED_PASTE_VERSION}:
            return None
        return lambda item: projected if isinstance(item, TypedPaste) else ()

    @staticmethod
    def _paste_versions(record: Path, since: datetime) -> set[str | None]:
        return {version if isinstance(version := entry["version"], str) else None
                for entry in inspect_entries(record)
                if "version" in entry and (moment := when(entry.get("timestamp"))) is not None and moment >= since}
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

    person_quiet_reasons = (MIXED_REMOTE_INPUT,)  # held by name in the quiet-time reading

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

    def inbox_watch_absorption(self, record, cwd, launch, state, proof, attempted_at):
        from .claude_watch import artifact_matches

        absorption = getattr(state, 'absorbed_watch', None)
        return (launch.version == LINKED_VERSION and absorption is not None and launch.when <= absorption.called_at
                and all(absorption.acknowledged_at <= at <= absorption.enqueued_at for at in attempted_at)
                and artifact_matches(cwd, record, absorption, proof))

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
        settings = merged(merged(defaults, template), seat.settings)
        settings = copy.deepcopy(merged(settings, {"env": SETTINGS["env"]}))  # the updater stays off
        if pool.provider == 'zai' and pool.paid_by_key:
            settings.update(zai_settings(template, seat.settings))
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
        return credential(pool, home / self.credential_file, "ANTHROPIC_API_KEY", adapter=self)

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        program = self.selected_program() or self.program
        argv = [str(program), "--setting-sources", "user", "--append-system-prompt-file", str(brief), "--name", seat.display]
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
        env = {self.home_variable: str(home), "DISABLE_AUTOUPDATER": "1", **key_environment(pool, "ANTHROPIC_API_KEY")}
        if pool.provider == 'zai' and pool.paid_by_key:
            env.update(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
                       CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL='1')
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
    # The new footer reports its shell count. It cannot settle unknown record evidence.
    background_rest_versions = background_versions - {LINKED_VERSION}

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
            ControlInterruption("change-effort confirmation", r"(?m)^  Change effort level\? *$", control="effort"),
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
