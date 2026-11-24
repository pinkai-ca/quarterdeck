"""Bounded native Claude reads, with guarded raw record identity."""

from hashlib import sha256
import codecs
from dataclasses import dataclass, replace
import json
import os
from functools import partial
import sys
import time
from pathlib import Path
import re

from . import json_record, record_file
from ..record_location import resolve


CHUNK = 64 * 1024
ROW_BYTES = 256 * 1024
COMPATIBILITY_BOUND = 128 * 1024 * 1024  # the full reader never follows a future streaming cap increase
ROW_STATE_BYTES = 1024  # kept reader, compaction and input index allowance per physical line
DEPTH = 96
ATOM_BYTES = 8192
OMIT_TYPE = 'file-history-snapshot'
OMIT_KEY = 'snapshot'
SPECIAL = re.compile(rb'["\\\x00-\x1f]')
ATOM_END = re.compile(rb'[ \t,{}\[\]":]')
OTHER_LINES = re.compile(r'[\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]')
SPLIT_LINES = re.compile(r'\r\n|[\n\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]')
HEX_DIGITS = b'0123456789abcdefABCDEF'
MALFORMED = (None, 'json', 0)
IMAGE_TYPES = frozenset(('image/png', 'image/jpeg', 'image/webp', 'image/gif'))
BASE64 = re.compile(rb'[A-Za-z0-9+/]*={0,2}')
BASE64_BYTES = re.compile(rb'[A-Za-z0-9+/=]*')
WATCH_HEADER = 'qd >'
BACKGROUND_HEADER = 'Command running in background with ID: '
WATCH_HEADERS = (WATCH_HEADER, BACKGROUND_HEADER)
WATCH_TEXT = tuple(marker.encode() for marker in WATCH_HEADERS)
WATCH_WINDOW = max(map(len, WATCH_TEXT)) - 1
PAYLOAD_PATHS = {
    ('message', 'content', None, 'source', 'data'): 'image',
    ('message', 'content', None, 'content'): 'text',
    ('message', 'content', None, 'content', None, 'source', 'data'): 'image',
    ('message', 'content', None, 'content', None, 'text'): 'text',
}
PAYLOAD_PARENTS = {path[:index] for path in PAYLOAD_PATHS for index in range(len(path))}
MAX_PAYLOADS = 256
MIRROR = 'toolUseResult'
# These top-level fields are read by the task, monitor and child interpreters.
# An unexpected type is evidence too, including any values nested inside it.
MIRROR_EVIDENCE = frozenset(('taskId', 'agentId', 'isAsync', 'status', 'backgroundTaskId',
                             'task_type', 'message', 'task_id', 'outputFile'))
IMAGE_FIELDS = frozenset(('data', 'base64'))
IMAGE_FORMATS = IMAGE_TYPES | {'png', 'jpeg', 'jpg', 'webp', 'gif'}
SCHEMA_KEYS = MIRROR_EVIDENCE | IMAGE_FIELDS | frozenset((
    MIRROR, OMIT_KEY, 'type', 'role', 'content', 'text', 'source', 'media_type',
    'mimeType', 'file', 'originalSize', 'size', 'stdout', 'stderr', 'output',
    'tool_use_id', 'is_error', 'sessionId', 'uuid', 'parentUuid', 'isSidechain',
    'timestamp', 'version', 'usage', 'attachment',
))
DIAGNOSTIC_KEY_BYTES = 6 * max(map(len, SCHEMA_KEYS))
REFUSAL_LAYOUTS = frozenset(('newline', 'splitlines'))
REFUSAL_REASONS = frozenset((
    'not-a-user-row', 'unknown-block-type', 'image-media-type', 'image-not-base64',
    'image-padding', 'text-watch-marker', 'text-escape', 'duplicate-key', 'too-many-payloads', 'budget',
))
REFUSAL_TYPES = IMAGE_FORMATS | {
    'image', 'text', 'tool_result', 'tool_use', 'thinking', 'redacted_thinking',
    'document', 'search_result', 'base64',
}
ASSISTANT_RESULTS = frozenset((
    'tool_result', 'mcp_tool_result', 'web_search_tool_result', 'web_fetch_tool_result',
    'tool_search_tool_result', 'code_execution_tool_result', 'bash_code_execution_tool_result',
    'text_editor_code_execution_tool_result', 'advisor_tool_result',
))
REFUSAL_SHAPE_REASONS = ('not-a-user-row', 'unknown-block-type')
REFUSAL_ROW_TYPE = re.compile(r'^[a-z][a-z_-]{0,31}$')
REFUSAL_ROLES = frozenset(('user', 'assistant'))
REFUSAL_BLOCK_TYPES = ASSISTANT_RESULTS | {'image', 'text'}


def safe_type(value, allowed=REFUSAL_TYPES):
    return value if isinstance(value, str) and value in allowed else '?'


@dataclass(frozen=True)
class RowRefusal:
    """Fixed labels and a value's encoded byte count from one guarded observation."""

    path: tuple[str, ...]
    size: int
    layout: str | None = None
    reason: str | None = None
    value_type: str | None = None
    row_type: str | None = None
    message_role: str | None = None
    block_type: str | None = None
    content_is_list: bool | None = None

    def with_shape(self, row_type, message_role, block_type, content_is_list):
        label = row_type if isinstance(row_type, str) and REFUSAL_ROW_TYPE.fullmatch(row_type) else '?'
        return replace(self, row_type=label,
                       message_role=safe_type(message_role, REFUSAL_ROLES),
                       block_type=safe_type(block_type, REFUSAL_BLOCK_TYPES), content_is_list=content_is_list)

    @classmethod
    def read(cls, observation):
        path, size = observation.get('path'), observation.get('size')
        if (not isinstance(path, list) or len(path) > DEPTH
                or not all(isinstance(part, str) and (part == '?' or part in SCHEMA_KEYS) for part in path)
                or type(size) is not int or size < 0):
            return None
        layout, reason = observation.get('layout'), observation.get('reason')
        if (isinstance(layout, str) and layout in REFUSAL_LAYOUTS
                and isinstance(reason, str) and reason in REFUSAL_REASONS):
            found = cls(tuple(path), size, layout, reason, safe_type(observation.get('value_type')))
            content_is_list = observation.get('content_is_list')
            if reason in REFUSAL_SHAPE_REASONS and type(content_is_list) is bool:
                found = found.with_shape(observation.get('row_type'), observation.get('message_role'),
                                         observation.get('block_type'), content_is_list)
            return found
        return cls(tuple(path), size)  # older observations have no layout or reason


@dataclass(frozen=True)
class RefusedProjectionUsage(record_file.ProjectionUsage):
    row_refusal: RowRefusal | None = None


def refusal_usage(stamp, limit, *, compatibility=False):
    if limit.row_refusal is not None:
        return RefusedProjectionUsage(stamp, exhausted=limit.resource, compatibility=compatibility,
                                      row_refusal=limit.row_refusal)
    return record_file.ProjectionUsage(stamp, exhausted=limit.resource, compatibility=compatibility)


class Unknown(record_file.RecordError):
    """Required native evidence exceeds the reader's retained-data budget."""


class ProjectionLimit(Unknown):
    """A guarded read exhausted a projection resource."""

    def __init__(self, reason, resource='native projection', *, limit=None, unit=None, row_refusal=None):
        super().__init__(reason)
        self.resource = resource
        self.limit = limit
        self.unit = unit
        self.row_refusal = row_refusal


class CompatibilityRefusal(ProjectionLimit):
    """A per-row or shape refusal eligible for the bounded whole-file reader."""


def _row_limit(reason, row_refusal=None):
    if row_refusal is not None and row_refusal.reason is None:
        row_refusal = replace(row_refusal, reason='budget', value_type='?')
    return CompatibilityRefusal(reason, record_file.ROW_RETAINED_BYTES, limit=ROW_BYTES, unit='bytes',
                                row_refusal=row_refusal)


class PhysicalSeparators(Exception):
    """An older text layout needs Unicode splitlines boundaries."""


class Payload(str):
    """A validated string projected to empty text, carrying its original UTF-8 byte count."""

    __slots__ = ('size', 'image_bytes')

    def __new__(cls, size, image_bytes=None):
        found = super().__new__(cls, '')
        found.size = size
        found.image_bytes = image_bytes
        return found

    def __reduce__(self):
        return type(self), (self.size, self.image_bytes)


class ProjectedRow(dict):
    __slots__ = ('identity',)


def path_shape(path):
    return tuple(None if isinstance(part, int) else part for part in path)


def schema_path(path):
    return tuple(part if part in SCHEMA_KEYS else '?' for part in path if isinstance(part, str))


def mirror_path(path):
    return bool(path and path[0] == MIRROR and (len(path) == 1 or path[1] not in MIRROR_EVIDENCE))


@dataclass(slots=True)
class Frame:
    state: str
    path: tuple | None
    schema: tuple
    start: int
    key: str | None = None
    label: str = '?'
    index: int = 0
    keys: set | None = None

    def __post_init__(self):
        if self.path is not None:
            self.keys = set()


class PayloadString:
    """Validate an oversized value without retaining its body."""

    def __init__(self, kind, start):
        self.kind, self.start = kind, start
        self.inline = bytearray()
        self.size = self.escapes = self.padding = 0
        self.suffix = b''
        self.nonbase64 = self.bad_padding = False
        self.text_escape = self.watch_marker = False

    def feed(self, data):
        self.size += len(data)
        if self.inline is not None:
            if self.size <= CHUNK:
                self.inline.extend(data)
            else:
                self.inline = None
        if self.kind != 'text':
            if not BASE64.fullmatch(data):
                if BASE64_BYTES.fullmatch(data):
                    self.bad_padding = True
                else:
                    self.nonbase64 = True
            elif self.padding and data.strip(b'='):
                self.bad_padding = True
            self.padding += data.count(b'=')
        if self.kind != 'image':
            checked = self.suffix + data
            # A Unicode escape could spell part of a watch marker without containing its literal bytes.
            self.text_escape = self.text_escape or b'\\u' in checked
            self.watch_marker = self.watch_marker or any(marker in checked for marker in WATCH_TEXT)
            self.suffix = checked[-WATCH_WINDOW:]

    def value(self, kind, refusal, value_type):
        if kind == 'image':
            if self.nonbase64:
                raise projection_error(refusal, 'image-not-base64', value_type)
            if self.bad_padding or self.size % 4 or self.padding > 2:
                raise projection_error(refusal, 'image-padding', value_type)
            return Payload(self.size, self.size // 4 * 3 - self.padding)
        if self.watch_marker:
            raise projection_error(refusal, 'text-watch-marker', value_type)
        if self.text_escape:
            raise projection_error(refusal, 'text-escape', value_type)
        return Payload(self.size - self.escapes)


def at_path(row, path):
    for part in path:
        row = row[part]
    return row


def native_image(block):
    if not isinstance(block, dict):
        return False
    source = block.get('source')
    return (block.get('type') == 'image' and isinstance(source, dict)
            and source.get('type') == 'base64' and isinstance(source.get('media_type'), str)
            and source['media_type'] in IMAGE_TYPES)


def native_result(block, result_types=('tool_result',)):
    return (isinstance(block.get('type'), str) and block['type'] in result_types
            and isinstance(block.get('tool_use_id'), str)
            and bool(block['tool_use_id']) and type(block.get('is_error', False)) is bool)


def text_or_image(block):
    return isinstance(block, dict) and (
        block.get('type') == 'text' and isinstance(block.get('text'), str)
        or native_image(block) and isinstance(block['source'].get('data'), str))


def known_content(block):
    if text_or_image(block):
        return True
    if not native_result(block):
        return False
    content = block.get('content', '')
    return isinstance(content, str) or isinstance(content, list) and all(map(text_or_image, content))


def projection_error(refusal=None, reason='budget', value_type=None, *, row=None, path=()):
    if refusal is not None:
        refusal = replace(refusal, reason=reason, value_type=safe_type(value_type))
        if row is not None and reason in REFUSAL_SHAPE_REASONS:
            message = row.get('message')
            role = message.get('role') if isinstance(message, dict) else None
            content = message.get('content') if isinstance(message, dict) else None
            content_is_list = isinstance(content, list)
            block_type = None
            if content_is_list and path[:2] == ('message', 'content') and len(path) > 2:
                index = path[2]
                if isinstance(index, int) and index < len(content) and isinstance(content[index], dict):
                    block_type = content[index].get('type')
            refusal = refusal.with_shape(row.get('type', 'missing'), role, block_type, content_is_list)
    return _row_limit('required JSON row exceeds the retained-byte budget', refusal)


def check_native_image(block, refusal, row, path):
    if block.get('type') != 'image':
        raise projection_error(refusal, 'unknown-block-type', block.get('type'), row=row, path=path)
    source = block.get('source')
    if not isinstance(source, dict) or source.get('type') != 'base64':
        raise projection_error(refusal, 'image-not-base64', 'image')
    media_type = source.get('media_type')
    if not isinstance(media_type, str) or media_type not in IMAGE_TYPES:
        raise projection_error(refusal, 'image-media-type', media_type)
    return media_type


def mirror_image_kind(row, path, result_has_image):
    """Validate typed and Read-style image carriers before withholding their encoded data."""
    if len(path) == 1:
        return 'image' if result_has_image else 'text'
    if path[-1] not in IMAGE_FIELDS:
        return 'text'
    parent = at_path(row, path[:-1])
    if (path[-1] == 'base64'
            or path[-2:-1] == ('source',) and native_image(at_path(row, path[:-2]))
            or parent.get('type') in ('base64', 'image') or any(
                isinstance(parent.get(key), str) and parent[key] in IMAGE_FORMATS
                for key in ('type', 'media_type', 'mimeType'))):
        return 'image'
    ancestor_is_image = any(isinstance(item := at_path(row, path[:depth]), dict) and item.get('type') == 'image'
                            for depth in range(1, len(path) - 1))
    if ancestor_is_image or result_has_image:
        # The envelope identifies an image, and this candidate is one of its data fields.
        return 'image'
    return 'text'


def project_payloads(row, payloads, descriptor, retained_bytes):
    """Prove candidates from their completed envelopes, independent of JSON key order."""
    message = row.get('message')
    first_path, first_capture = next(iter(payloads.items()))
    first_refusal = RowRefusal(schema_path(first_path), first_capture.size)
    row_type = row.get('type')
    role = message.get('role') if isinstance(message, dict) else None
    if (role not in ('user', 'assistant') or (row_type in ('user', 'assistant') and row_type != role)
            or not isinstance(message.get('content'), list)):
        raise projection_error(first_refusal, 'not-a-user-row', row=row, path=first_path)
    if not all(isinstance(block, dict) for block in message['content']):
        raise projection_error(first_refusal, 'unknown-block-type', row=row, path=first_path)
    if row_type != 'user':
        return project_images(row, payloads, descriptor, ROW_BYTES - retained_bytes)
    results = [block for block in message['content'] if native_result(block)]
    result_has_image = any(isinstance(block.get('content'), list)
                           and any(map(native_image, block['content'])) for block in results)
    spans = {}
    for path, capture in payloads.items():
        refusal = RowRefusal(schema_path(path), capture.size)
        if capture.kind == 'mirror':
            if not results:
                raise projection_error(refusal, 'unknown-block-type', row=row, path=path)
            kind = mirror_image_kind(row, path, result_has_image)
            at_path(row, path[:-1])[path[-1]] = capture.value(kind, refusal, 'image' if kind == 'image' else '?')
            continue
        block = at_path(row, path[:3])
        value_type = block.get('type')
        if path[3] == 'source':
            value_type = check_native_image(block, refusal, row, path)
        elif not native_result(block):
            raise projection_error(refusal, 'unknown-block-type', block.get('type'), row=row, path=path)
        elif len(path) > 4:
            child = at_path(row, path[:5])
            value_type = child.get('type')
            if capture.kind == 'image':
                value_type = check_native_image(child, refusal, row, path)
            elif value_type != 'text':
                raise projection_error(refusal, 'unknown-block-type', value_type, row=row, path=path)
        value = capture.value(capture.kind, refusal, value_type)
        at_path(row, path[:-1])[path[-1]] = value
        spans[id(value)] = capture
    for block in message['content']:
        if not known_content(block):
            raise projection_error(first_refusal, 'unknown-block-type', block.get('type'), row=row, path=first_path)
        if not native_result(block) or not isinstance(block.get('content'), list):
            continue
        content = block['content']
        if (any(isinstance(child.get('text'), Payload) for child in content)
                and all(child['type'] == 'text' for child in content)
                and any(any(marker in child['text'] for marker in WATCH_HEADERS) for child in content)):
            # Empty text replacing a leading block must not expose a later watch header.
            raise projection_error(first_refusal, 'text-watch-marker', 'text')
    if not any(block.get('type') == 'tool_result' for block in message['content']):
        return with_identity(row, spans, descriptor)
    return row


def project_images(row, payloads, descriptor, remaining):
    """Replace only verified message images and restore the other candidates exactly."""
    message = row['message']
    result_types = ASSISTANT_RESULTS if message['role'] == 'assistant' else ('tool_result',)
    spans = {}
    for path, capture in payloads.items():
        refusal = RowRefusal(schema_path(path), capture.size)
        block = message['content'][path[2]] if capture.kind != 'mirror' else {}
        try:
            value = verified_image_payload(row, path, block, capture, refusal, result_types)
        except ProjectionLimit:
            # Text and mirror fields may carry evidence in these rows.
            extra = capture.size - 2  # the encoded string replaces the four bytes of null
            if extra > remaining:
                raise
            raw = b''.join(payload_bytes(descriptor, capture))
            value = json.loads(b'"' + raw + b'"')
            remaining -= extra
        else:
            spans[id(value)] = capture
        at_path(row, path[:-1])[path[-1]] = value
    # These compaction frontier entries identify the full row, including images.
    if spans and (row.get('type') == 'system' and row.get('subtype') == 'compact_boundary'
                  or row.get('type') == 'queue-operation' and row.get('operation') == 'enqueue'):
        return with_identity(row, spans, descriptor)
    return row


def verified_image_payload(row, path, block, capture, refusal, result_types):
    if capture.kind != 'image' or path[3] != 'source' and not native_result(block, result_types):
        raise projection_error(refusal, 'unknown-block-type', block.get('type'), row=row, path=path)
    image = block if path[3] == 'source' else at_path(row, path[:5])
    media_type = check_native_image(image, refusal, row, path)
    return capture.value('image', refusal, media_type)


def with_identity(row, spans, descriptor):
    row = ProjectedRow(row)
    hasher = sha256()
    for part in canonical_parts(row, spans, descriptor):
        hasher.update(part)
    row.identity = hasher.hexdigest()
    return row


def payload_bytes(descriptor, capture):
    start, left = capture.start, capture.size
    while left:
        data = os.pread(descriptor, min(CHUNK, left), start)
        if not data:
            raise Unknown('record ended while rereading a payload')
        start += len(data)
        left -= len(data)
        yield data


def canonical_parts(value, spans, descriptor):
    """Hash full rows exactly as sorted compact JSON, streaming the original ASCII image bytes."""
    if isinstance(value, Payload):
        yield b'"'
        yield from payload_bytes(descriptor, spans[id(value)])
        yield b'"'
    elif isinstance(value, (dict, list)):
        mapping = isinstance(value, dict)
        yield b'{' if mapping else b'['
        children = sorted(value) if mapping else value
        for index, child in enumerate(children):
            if index:
                yield b','
            if mapping:
                yield json.dumps(child).encode() + b':'
                child = value[child]
            yield from canonical_parts(child, spans, descriptor)
        yield b'}' if mapping else b']'
    else:
        yield json.dumps(value).encode()


class GiantRow:
    """Validate a giant JSON row while bounding snapshots and candidate payload strings."""

    def __init__(self, descriptor, start):
        self.frames = []
        self.descriptor, self.offset = descriptor, start
        self.payload = None
        self.payloads = {}
        self.duplicate_key = False
        self.output = bytearray()
        self.snapshot = None
        self.snapshot_size = 0
        self.in_snapshot = False
        self.key_bytes = None
        self.value_schema = ()
        self.string_size = 0
        self.string_exhausted = False
        self.refused_frame = None
        self.atom = None
        self.string = False
        self.escape = 0
        self.done = False

    def withheld_payload(self):
        return (self.string_exhausted or self.refused_frame is not None or self.payloads
                or self.payload is not None and self.payload[1].inline is None)

    def emit(self, data, *, scalar=False):
        if self.in_snapshot:
            self.snapshot_size += len(data)
            if self.snapshot is not None:
                if self.snapshot_size <= ROW_BYTES:
                    self.snapshot.extend(data)
                else:
                    self.snapshot = None
        elif self.refused_frame is not None:
            return True  # the first over-budget container is already being measured
        else:
            if len(self.output) + len(data) > ROW_BYTES:
                if not scalar:
                    self.refused_frame = self.frames[-1]
                return False
            self.output.extend(data)
        return True

    def value(self):
        if not self.frames:
            raise ValueError('JSON value in an unexpected position')
        frame = self.frames[-1]
        if frame.state in ('array first', 'array value'):
            frame.state = 'array next'
            part = frame.index
            frame.index += 1
            self.value_schema = frame.schema
        elif frame.state == 'value':
            frame.state = 'object next'
            part = frame.key
            self.value_schema = (*frame.schema, frame.label)
        else:
            raise ValueError('JSON value in an unexpected position')
        return None if frame.path is None else (*frame.path, part)

    def end_value(self):
        if len(self.frames) == 1:
            self.in_snapshot = False

    def punct(self, char, offset):
        if self.done:
            raise ValueError('data after root JSON object')
        if char in b'{[':
            if not self.frames and char != ord('{'):
                raise CompatibilityRefusal('large row does not start with a native JSON object')
            path = self.value() if self.frames else ()
            if path is not None and not mirror_path(path) and path_shape(path) not in PAYLOAD_PARENTS:
                path = None
            if len(self.frames) >= DEPTH:
                raise CompatibilityRefusal('JSON nesting exceeds the validation budget')
            self.frames.append(Frame('object first' if char == ord('{') else 'array first', path,
                                     self.value_schema, offset))
        elif not self.frames:
            raise ValueError('root JSON object required')
        elif char == ord(':'):
            frame = self.frames[-1]
            if frame.state != 'colon':
                raise ValueError('unexpected JSON colon')
            frame.state = 'value'
            self.emit(bytes((char,)))
            if len(self.frames) == 1 and frame.key == OMIT_KEY:
                self.emit(b'null')
                self.in_snapshot = True
                self.snapshot = bytearray()
                self.snapshot_size = 0
            return
        elif char == ord(','):
            frame = self.frames[-1]
            if frame.state == 'object next':
                frame.state = 'key'
            elif frame.state == 'array next':
                frame.state = 'array value'
            else:
                raise ValueError('unexpected JSON comma')
        else:
            wanted = ('object first', 'object next') if char == ord('}') else ('array first', 'array next')
            if self.frames[-1].state not in wanted:
                raise ValueError('incomplete JSON value')
            self.emit(bytes((char,)))
            closed = self.frames.pop()
            if self.refused_frame is closed:
                raise projection_error(RowRefusal(closed.schema, offset + 1 - closed.start))
            self.end_value()
            self.done = not self.frames
            return
        self.emit(bytes((char,)))

    def string_start(self, start):
        if not self.frames:
            raise ValueError('root JSON object required')
        frame = self.frames[-1]
        self.string_size = 0
        self.string_exhausted = False
        if frame.state in ('key', 'object first'):
            self.value_schema = (*frame.schema, '?')
            if self.refused_frame is None:
                self.key_bytes = bytearray()
            frame.state = 'colon'
        else:
            path = self.value()
            if path is not None and self.refused_frame is None:
                kind = 'mirror' if mirror_path(path) else PAYLOAD_PATHS.get(path_shape(path))
                if kind is not None:
                    self.payload = path, PayloadString(kind, start)
        self.string = True
        if self.payload is None:
            self.string_exhausted = not self.emit(b'"', scalar=True)

    def string_data(self, data):
        self.string_size += len(data)
        if self.key_bytes is not None:
            frame = self.frames[-1]
            bound = ROW_BYTES if frame.path is not None else DIAGNOSTIC_KEY_BYTES
            if len(self.key_bytes) + len(data) <= bound:
                self.key_bytes.extend(data)
            else:
                self.key_bytes = None
        if self.payload is None:
            if not self.string_exhausted:
                self.string_exhausted = not self.emit(data, scalar=True)
        elif data:
            self.payload[1].feed(data)

    def string_end(self):
        frame = self.frames[-1]
        if self.payload is not None:
            path, capture = self.payload
            self.payload = None
            if capture.inline is not None:
                self.string_exhausted = not self.emit(b'"' + capture.inline + b'"', scalar=True)
            else:
                if len(self.payloads) >= MAX_PAYLOADS:
                    raise projection_error(RowRefusal(self.value_schema, self.string_size), 'too-many-payloads')
                self.string_exhausted = not self.emit(b'null', scalar=True)
                self.payloads[path] = capture
        elif not self.string_exhausted:
            self.string_exhausted = not self.emit(b'"', scalar=True)
        if frame.state == 'colon':
            frame.key, frame.label = None, '?'
            if self.key_bytes is not None:
                key = json.loads(b'"' + self.key_bytes + b'"')
                frame.label = key if key in SCHEMA_KEYS else '?'
                if frame.path is not None:
                    frame.key = key
                    if key in frame.keys:
                        self.duplicate_key = True
                    frame.keys.add(key)
                self.key_bytes = None
            self.value_schema = (*frame.schema, frame.label)
        else:
            self.end_value()
        if self.string_exhausted:
            raise projection_error(RowRefusal(self.value_schema, self.string_size))
        self.string = False

    def atom_end(self):
        data = self.atom
        value = json.loads(data.decode())
        if isinstance(value, (str, list, dict)):
            raise ValueError('invalid JSON atom')
        if not self.emit(data, scalar=True):
            raise projection_error(RowRefusal(self.value_schema, len(data)))
        self.end_value()
        self.atom = None

    def feed(self, data):
        offset = self.offset
        self.offset += len(data)
        at = 0
        while at < len(data):
            if self.string:
                if self.escape:
                    char = data[at]
                    if self.escape == -1:
                        if char not in b'"\\/bfnrtu':
                            raise ValueError('invalid JSON escape')
                        self.escape = 4 if char == ord('u') else 0
                        if self.payload is not None:
                            self.payload[1].escapes += 1
                    else:
                        if char not in HEX_DIGITS:
                            raise ValueError('invalid JSON Unicode escape')
                        self.escape -= 1
                    self.string_data(data[at:at + 1])
                    at += 1
                    continue
                special = SPECIAL.search(data, at)
                if special is None:
                    self.string_data(data[at:])
                    return
                char = data[special.start()]
                if char == ord('"'):
                    self.string_data(data[at:special.start()])
                    self.string_end()
                elif char == ord('\\'):
                    self.string_data(data[at:special.end()])
                    self.escape = -1
                else:
                    raise ValueError('unescaped control byte in JSON string')
                at = special.end()
            elif self.atom is not None:
                stop = ATOM_END.search(data, at)
                end = stop.start() if stop else len(data)
                if len(self.atom) + end - at > ATOM_BYTES:
                    raise CompatibilityRefusal('JSON atom exceeds the validation budget')
                self.atom.extend(data[at:end])
                at = end
                if stop:
                    self.atom_end()
            else:
                char = data[at]
                if char in b' \t':
                    at += 1
                elif char == ord('"'):
                    self.string_start(offset + at + 1)
                    at += 1
                elif char in b'{}[]:,':
                    self.punct(char, offset + at)
                    at += 1
                else:
                    self.value()
                    self.atom = bytearray()

    def finish(self):
        if self.atom is not None:
            self.atom_end()
        if not self.output and not self.frames:
            return None, None, 0  # only spaces and tabs occurred
        if not self.done or self.string or self.frames:
            raise ValueError('incomplete JSON object')
        row = json.loads(self.output.decode())
        if OMIT_KEY not in row and not self.payloads:
            raise CompatibilityRefusal('large row had no discardable value')
        if OMIT_KEY in row:
            if row.get('type') == OMIT_TYPE:
                del row[OMIT_KEY]
            elif self.snapshot is None:
                raise _row_limit('required snapshot value exceeds the retained-byte budget',
                                 RowRefusal((OMIT_KEY,), self.snapshot_size))
            else:
                row[OMIT_KEY] = json.loads(self.snapshot.decode())
        if self.payloads:
            if self.duplicate_key:
                path, capture = next(iter(self.payloads.items()))
                raise projection_error(RowRefusal(schema_path(path), capture.size), 'duplicate-key')
            row = project_payloads(row, self.payloads, self.descriptor, len(self.output))
        return row, None, retained_size(row, len(self.payloads) * 16)


def retained_size(row, metadata=0):
    data = json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode('utf-8', errors='surrogatepass')
    size = len(data) + metadata
    if size > ROW_BYTES:
        raise _row_limit('required projection exceeds the per-row budget', RowRefusal((), size))
    return size


def decoded_size(row):
    """Conservatively charge containers, keys and values, including each repeated reference."""
    pending, size = [row], 64  # kept row pair and its slot
    while pending:
        value = pending.pop()
        size += sys.getsizeof(value)
        if isinstance(value, Payload):
            size += sys.getsizeof(value.size) + sys.getsizeof(value.image_bytes)
        if isinstance(value, ProjectedRow):
            size += sys.getsizeof(value.identity)
        if isinstance(value, dict):
            pending.extend(value)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return size


def projection_usage(snapshot):
    entry = json_record._entry(snapshot)
    if entry.claude_full:
        if (refusal := getattr(entry, 'claude_row_refusal', None)) is not None:
            return RefusedProjectionUsage(snapshot.stamp, compatibility=True, row_refusal=refusal)
        return record_file.ProjectionUsage(snapshot.stamp, compatibility=True)
    if snapshot.raw_digest is None:
        return None
    return record_file.ProjectionUsage(snapshot.stamp, snapshot.line_count, snapshot.projected_bytes,
                                       snapshot.retained_cost)


class Line:
    def __init__(self, descriptor, start):
        self.small = bytearray()
        self.giant = None
        self.malformed = False
        self.descriptor, self.start = descriptor, start

    def feed(self, data):
        if self.malformed:
            return
        if self.giant is None and len(self.small) + len(data) <= ROW_BYTES:
            self.small.extend(data)
            return
        try:
            if self.giant is None:
                self.giant = GiantRow(self.descriptor, self.start)
                self.giant.feed(self.small)
                self.small.clear()
            self.giant.feed(data)
        except ValueError:
            if self.giant.withheld_payload():
                raise projection_error() from None
            self.malformed = True

    def finish(self):
        if self.malformed:
            return MALFORMED
        if self.giant is not None:
            try:
                return self.giant.finish()
            except (ValueError, RecursionError):
                if self.giant.withheld_payload():
                    raise projection_error() from None
                return MALFORMED
        if not self.small.strip():
            return None, None, 0
        text = self.small.decode('utf-8')
        if not text.isascii() and not text.strip():
            return None, None, 0  # matches str.splitlines rows containing only Unicode whitespace
        try:
            row = json.loads(text)
        except RecursionError:
            raise CompatibilityRefusal('JSON nesting exceeds the validation budget') from None
        except (ValueError, UnicodeError):
            return MALFORMED
        if not isinstance(row, dict):
            return None, 'non-object', 0
        size = len(self.small)
        if row.get('type') == OMIT_TYPE and OMIT_KEY in row:
            del row[OMIT_KEY]
            size = retained_size(row)
        return row, None, size


def scan(descriptor, size, cached, *, splitlines=False):
    """Yield after each 64 KiB read; return a snapshot after all required rows."""
    hasher = sha256()
    offset = number = retained = decoded = 0
    rows_bound = record_file.CLAUDE_STREAM_ROWS
    retained_bound = record_file.CLAUDE_STREAM_BYTES
    memory_bound = record_file.CLAUDE_STREAM_MEMORY
    rows, skipped = [], []
    proof = None
    if cached is not None and 0 < cached.prefix_bytes <= size:
        left = cached.prefix_bytes
        while left:
            data = read_piece(descriptor, left)
            hasher.update(data)
            left -= len(data)
            yield len(data)
        if hasher.digest() == cached.prefix_sha256.digest():
            offset, number = cached.prefix_bytes, cached.complete_lines
            rows = cached.rows[:cached.row_count]
            skipped = [item for item in cached.skipped if item[0] <= number]
            retained = cached.retained_bytes
            decoded = cached.decoded_bytes
            proof = offset, hasher.hexdigest()
        else:
            cached = None
            os.lseek(descriptor, 0, os.SEEK_SET)
            hasher = sha256()
    else:
        cached = None

    prefix_bytes, prefix = offset, hasher.copy()
    decoder = codecs.getincrementaldecoder('utf-8')()
    line = Line(descriptor, offset)
    # The UTF-8 decoder can hold a partial code point while the splitter holds a CR.
    # Track bytes consumed by the splitter independently of the descriptor's read offset.
    split_at = offset
    trailing_cr = ends_lf = False

    def finish_line():
        nonlocal retained, number, decoded
        number += 1
        if number > rows_bound:
            raise ProjectionLimit('native record exceeds its physical line budget', 'physical lines',
                                  limit=rows_bound, unit='lines')
        row, diagnostic, weight = line.finish()
        retained += weight
        if retained > retained_bound:
            raise ProjectionLimit('native record exceeds its retained-byte budget', 'retained bytes',
                                  limit=retained_bound, unit='bytes')
        if row is not None:
            decoded += decoded_size(row)
        if decoded + number * ROW_STATE_BYTES > memory_bound:
            raise ProjectionLimit('native record exceeds its retained memory budget', 'retained memory',
                                  limit=memory_bound, unit='bytes')
        if row is not None:
            rows.append((number, row))
        elif diagnostic is not None:
            skipped.append((number, diagnostic, 'invalid JSON' if diagnostic == 'json' else 'expected JSON object'))

    while offset < size:
        data = read_piece(descriptor, size - offset)
        text = decoder.decode(data)
        if splitlines:
            ends_lf = data.endswith(b'\n')
            if text:
                if trailing_cr:
                    text = '\r' + text
                trailing_cr = text.endswith('\r')
                if trailing_cr:
                    text = text[:-1]
                text_at = 0
                for boundary in SPLIT_LINES.finditer(text):
                    part = text[text_at:boundary.start()].encode('utf-8')
                    line.feed(part)
                    finish_line()
                    split_at += len(part) + len(boundary[0].encode('utf-8'))
                    line = Line(descriptor, split_at)
                    text_at = boundary.end()
                part = text[text_at:].encode('utf-8')
                line.feed(part)
                split_at += len(part)
            hasher.update(data)
        else:
            if OTHER_LINES.search(text):
                raise PhysicalSeparators
            parts = data.split(b'\n')
            next_start = offset
            for part in parts[:-1]:
                line.feed(part)
                finish_line()
                next_start += len(part) + 1
                line = Line(descriptor, next_start)
            if len(parts) > 1:
                boundary = len(data) - len(parts[-1])
                hasher.update(data[:boundary])
                prefix_bytes, prefix = offset + boundary, hasher.copy()
                hasher.update(parts[-1])
            else:
                hasher.update(data)
            line.feed(parts[-1])
        offset += len(data)
        yield len(data)
    decoder.decode(b'', final=True)
    if trailing_cr:
        finish_line()
        line = Line(descriptor, size)
    complete_lines, complete_rows, complete_bytes, complete_decoded = number, len(rows), retained, decoded
    has_tail = line.start < size if splitlines else size != prefix_bytes
    if has_tail:
        finish_line()
    if not rows and skipped:
        raise Unknown('no JSON object lines in this record: not a record this reader can read')
    return json_record._Snapshot(
        '', (), rows, skipped, None, 0, complete_lines, prefix_bytes=prefix_bytes,
        prefix_sha256=prefix, total_bytes=size, row_count=complete_rows,
        last_row=rows[-1] if rows else None,
        last_complete_row=rows[complete_rows - 1] if complete_rows else None,
        ends_newline=ends_lf if splitlines else size > 0 and size == prefix_bytes, line_count=number,
        raw_digest=hasher.hexdigest(), previous_prefix=proof, retained_bytes=complete_bytes,
        projected_bytes=retained, decoded_bytes=complete_decoded,
        retained_cost=decoded + number * ROW_STATE_BYTES,
    ), cached


def read_piece(descriptor, remaining):
    data = os.read(descriptor, min(CHUNK, remaining))
    if not data:
        raise Unknown('record ended during read')
    return data


def _consume(work):
    while True:
        try:
            next(work)
        except StopIteration as done:
            return done.value


def _same_stamp(descriptor, source, stamp):
    try:
        named, info = record_file.open_record(source)
    except FileNotFoundError:
        return False
    try:
        return json_record._stamp(info) == json_record._stamp(record_file.restat(descriptor)) == stamp
    finally:
        os.close(named)


def parse(record):
    """Cache only bounded projected rows, keyed by the original record and reader."""
    with json_record._read_lock:
        return _parse(record)


def inspect_entries(record):
    """Iterate one guarded observation for a reader that only inspects native rows."""
    with json_record._read_lock:
        for _, row in _parse(record).rows:
            yield row


def _parse(record):
    key = Path(record)
    cached = json_record._snapshots.pop(key, None)
    if cached is not None and cached.raw_digest is None and not cached.claude_full:
        cached = None
    for attempt in range(3):
        source = resolve(record)
        started = time.time_ns()
        descriptor, info = record_file.open_record(source)
        try:
            checked = time.time_ns()
            stamp = json_record._stamp(info)
            raw_bound = record_file.bound_of(source, 'claude')
            if info.st_size > raw_bound:
                raise record_file.past_bound(source, info.st_size, raw_bound)
            if (attempt == 0 and cached is not None and cached.stamp == stamp and cached.certified_at is not None
                    and cached.source == source and cached.certified_at <= started <= checked):
                json_record._snapshots[key] = cached
                if cached.claude_full and not json_record._KEEP_ROWS:
                    return json_record._view(source, cached, None, json_record._partial(cached), cached.row_count)
                return cached
            previous = (cached if cached is not None and not cached.claude_full
                        and cached.source == source and cached.stamp[:2] == stamp[:2] else None)
            failure = full = limit = None
            try:
                try:
                    entry, reused = _scan_layout(descriptor, info.st_size, previous)
                except CompatibilityRefusal as error:
                    bound = min(COMPATIBILITY_BOUND, raw_bound)
                    if info.st_size > bound:
                        raise
                    # A traceback would retain the abandoned projection while
                    # the legacy reader allocates its own buffer and rows.
                    limit = CompatibilityRefusal(str(error), error.resource, limit=error.limit, unit=error.unit,
                                                  row_refusal=error.row_refusal)
                if limit is not None:
                    full = record_file.read_from(descriptor, info, source, bound)
            except (OSError, UnicodeError) as error:
                failure = error
            if not _same_stamp(descriptor, source, stamp) or resolve(record) != source:
                continue
            if isinstance(failure, UnicodeError):
                raise record_file.RecordError('record is not UTF-8 JSON lines') from failure
            if failure is not None:
                if isinstance(failure, ProjectionLimit) and limit is None:
                    failure.projection_usage = refusal_usage(stamp, failure)
                elif limit is not None:
                    failure.projection_usage = refusal_usage(stamp, limit, compatibility=True)
                raise failure
        finally:
            os.close(descriptor)

        finished = time.time_ns()
        certified = started if max(stamp[-2:]) + json_record._TIMESTAMP_TICK_NS < started <= checked <= finished else None
        if full is not None:
            try:
                return _compatible(key, source, full, stamp, certified, cached, limit)
            except OSError as error:
                error.projection_usage = refusal_usage(stamp, limit, compatibility=True)
                raise
        entry.stamp, entry.source, entry.certified_at = stamp, source, certified
        if reused is not None:
            if entry.raw_digest == reused.raw_digest:
                json_record.refresh_stamp(reused, entry.stamp, entry.certified_at)
                entry = reused
            elif reused.ends_newline and entry.ends_newline:
                json_record._carry(reused, entry, entry.rows[reused.row_count:], entry.skipped[len(reused.skipped):])
        json_record._store(key, entry, cached is not None)
        return entry
    raise record_file.RecordError('record changed while being read')


def _scan_layout(descriptor, size, previous):
    layout = 'newline'
    try:
        try:
            return _consume(scan(descriptor, size, previous))
        except PhysicalSeparators:
            # Preserve str.splitlines() for older layouts without reusing a prefix.
            layout = 'splitlines'
            os.lseek(descriptor, 0, os.SEEK_SET)
        return _consume(scan(descriptor, size, None, splitlines=True))
    except ProjectionLimit as error:
        if error.row_refusal is not None:
            error.row_refusal = replace(error.row_refusal, layout=layout)
        raise


def _compatible(record, source, data, stamp, certified, cached, limit):
    """Apply the original full reader to a stable, under-bound record."""
    whole = cached if cached is not None and cached.claude_full and cached.source == source else None
    try:
        with json_record.passing():
            if json_record._KEEP_ROWS:
                parsed = json_record._parse_text(record, data, stamp, certified, whole)
            else:
                parsed = json_record._parse_bytes(source, record, data, stamp, certified, whole)
    except OSError:
        json_record._snapshots.pop(record, None)
        raise
    except (ValueError, RecursionError):
        json_record._snapshots.pop(record, None)
        raise limit from None
    entry = json_record._entry(parsed)
    entry.claude_full = True
    entry.claude_row_refusal = limit.row_refusal
    entry.source = source
    json_record._store(record, entry, cached is not None)
    return parsed


entries = partial(json_record.entries, parse=parse)
object_lines = partial(json_record.object_lines, parse=parse)
input_rows = partial(json_record.input_rows, parse=parse)
