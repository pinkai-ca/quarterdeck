"""Bounded native Claude reads, with guarded raw record identity."""

from hashlib import sha256
import codecs
from dataclasses import dataclass
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
PROJECTED_BYTES = 16 * 1024 * 1024
COMPATIBILITY_BOUND = 128 * 1024 * 1024  # the full reader never follows a future streaming cap increase
ROWS = 65_536
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


class Unknown(record_file.RecordError):
    """Required native evidence exceeds the reader's retained-data budget."""


class ProjectionLimit(Unknown):
    """A record might need the existing bounded full reader."""

    def __init__(self, reason, resource='native projection', *, limit=None, unit=None):
        super().__init__(reason)
        self.resource = resource
        self.limit = limit
        self.unit = unit


def _row_limit(reason):
    return ProjectionLimit(reason, record_file.ROW_RETAINED_BYTES, limit=ROW_BYTES, unit='bytes')


class PhysicalSeparators(Exception):
    """An older text layout needs Unicode splitlines boundaries."""


class Payload(str):
    """A validated string projected to empty text, carrying its original UTF-8 byte count."""

    __slots__ = ('size',)

    def __new__(cls, size):
        found = super().__new__(cls, '')
        found.size = size
        return found

    def __reduce__(self):
        return type(self), (self.size,)


class ProjectedRow(dict):
    __slots__ = ('identity',)


def path_shape(path):
    return tuple(None if isinstance(part, int) else part for part in path)


@dataclass(slots=True)
class Frame:
    state: str
    path: tuple | None
    key: str | None = None
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
        self.unsafe = False

    def feed(self, data):
        self.size += len(data)
        if self.inline is not None:
            if self.size <= CHUNK:
                self.inline.extend(data)
            else:
                self.inline = None
        if self.kind == 'image':
            if (self.padding and data.strip(b'=')) or not BASE64.fullmatch(data):
                self.unsafe = True
            self.padding += data.count(b'=')
        else:
            checked = self.suffix + data
            # A Unicode escape could spell part of a watch marker without containing its literal bytes.
            if b'\\u' in checked or any(marker in checked for marker in WATCH_TEXT):
                self.unsafe = True
            self.suffix = checked[-WATCH_WINDOW:]

    def value(self):
        if self.unsafe or self.kind == 'image' and (self.size % 4 or self.padding > 2):
            raise projection_error()
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


def native_result(block):
    return (block.get('type') == 'tool_result' and isinstance(block.get('tool_use_id'), str)
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


def projection_error():
    return _row_limit('required JSON row exceeds the retained-byte budget')


def project_payloads(row, payloads, descriptor):
    """Confirm a candidate using its enclosing native row, independent of JSON key order."""
    message = row.get('message')
    if (row.get('type') != 'user' or not isinstance(message, dict) or message.get('role') != 'user'
            or not isinstance(message.get('content'), list)
            or not all(isinstance(block, dict) for block in message['content'])):
        raise projection_error()
    spans = {}
    for path, capture in payloads.items():
        block = at_path(row, path[:3])
        if path[3] == 'source':
            known = native_image(block)
        elif not native_result(block):
            known = False
        elif len(path) == 4:
            known = True  # string content directly on a tool result
        else:
            child = at_path(row, path[:5])
            known = native_image(child) if capture.kind == 'image' else child.get('type') == 'text'
        if not known:
            raise projection_error()
        value = capture.value()
        at_path(row, path[:-1])[path[-1]] = value
        spans[id(value)] = capture
    if not all(map(known_content, message['content'])):
        raise projection_error()
    for block in message['content']:
        if not native_result(block) or not isinstance(block.get('content'), list):
            continue
        content = block['content']
        if (any(isinstance(child.get('text'), Payload) for child in content)
                and all(child['type'] == 'text' for child in content)
                and any(any(marker in child['text'] for marker in WATCH_HEADERS) for child in content)):
            # Empty text replacing a leading block must not expose a later watch header.
            raise projection_error()
    if not any(block.get('type') == 'tool_result' for block in message['content']):
        row = ProjectedRow(row)
        hasher = sha256()
        for part in canonical_parts(row, spans, descriptor):
            hasher.update(part)
        row.identity = hasher.hexdigest()
    return row


def canonical_parts(value, spans, descriptor):
    """Hash typed rows exactly as sorted compact JSON, streaming the original ASCII image bytes."""
    if isinstance(value, Payload):
        capture = spans[id(value)]
        yield b'"'
        start, left = capture.start, capture.size
        while left:
            data = os.pread(descriptor, min(CHUNK, left), start)
            if not data:
                raise Unknown('record ended while reading typed image identity')
            start += len(data)
            left -= len(data)
            yield data
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
        self.in_snapshot = False
        self.key_bytes = None
        self.atom = None
        self.string = False
        self.escape = 0
        self.done = False

    def withheld_payload(self):
        return self.payloads or self.payload is not None and self.payload[1].inline is None

    def emit(self, data):
        if self.in_snapshot:
            if self.snapshot is not None:
                if len(self.snapshot) + len(data) <= ROW_BYTES:
                    self.snapshot.extend(data)
                else:
                    self.snapshot = None
        else:
            if len(self.output) + len(data) > ROW_BYTES:
                raise _row_limit('required JSON row exceeds the retained-byte budget')
            self.output.extend(data)
        if self.key_bytes is not None:
            self.key_bytes.extend(data)

    def value(self):
        if not self.frames:
            raise ValueError('JSON value in an unexpected position')
        frame = self.frames[-1]
        if frame.state in ('array first', 'array value'):
            frame.state = 'array next'
            part = frame.index
            frame.index += 1
        elif frame.state == 'value':
            frame.state = 'object next'
            part = frame.key
        else:
            raise ValueError('JSON value in an unexpected position')
        return None if frame.path is None else (*frame.path, part)

    def end_value(self):
        if len(self.frames) == 1:
            self.in_snapshot = False

    def punct(self, char):
        if self.done:
            raise ValueError('data after root JSON object')
        if char in b'{[':
            if not self.frames and char != ord('{'):
                raise ProjectionLimit('large row does not start with a native JSON object')
            path = self.value() if self.frames else ()
            if path is not None and path_shape(path) not in PAYLOAD_PARENTS:
                path = None
            if len(self.frames) >= DEPTH:
                raise ProjectionLimit('JSON nesting exceeds the validation budget')
            self.frames.append(Frame('object first' if char == ord('{') else 'array first', path))
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
            self.frames.pop()
            self.end_value()
            self.done = not self.frames
            return
        self.emit(bytes((char,)))

    def string_start(self, start):
        if not self.frames:
            raise ValueError('root JSON object required')
        frame = self.frames[-1]
        if frame.state in ('key', 'object first'):
            if frame.path is not None:
                self.key_bytes = bytearray()
            frame.state = 'colon'
        else:
            path = self.value()
            if self.descriptor is not None and path is not None:
                kind = PAYLOAD_PATHS.get(path_shape(path))
                if kind is not None:
                    self.payload = path, PayloadString(kind, start)
        self.string = True
        if self.payload is None:
            self.emit(b'"')

    def string_data(self, data):
        if self.payload is None:
            self.emit(data)
        elif data:
            self.payload[1].feed(data)

    def string_end(self):
        if self.payload is not None:
            path, capture = self.payload
            self.payload = None
            if capture.inline is not None:
                self.emit(b'"' + capture.inline + b'"')
            else:
                if len(self.payloads) >= MAX_PAYLOADS:
                    raise projection_error()
                self.emit(b'null')
                self.payloads[path] = capture
        else:
            self.emit(b'"')
        frame = self.frames[-1]
        if frame.state == 'colon':
            if self.key_bytes is not None:
                frame.key = json.loads(self.key_bytes.decode())
                if frame.key in frame.keys:
                    self.duplicate_key = True
                frame.keys.add(frame.key)
                self.key_bytes = None
        else:
            self.end_value()
        self.string = False

    def atom_end(self):
        data = self.atom
        value = json.loads(data.decode())
        if isinstance(value, (str, list, dict)):
            raise ValueError('invalid JSON atom')
        self.emit(data)
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
                    raise ProjectionLimit('JSON atom exceeds the validation budget')
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
                    self.punct(char)
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
            raise ProjectionLimit('large row had no discardable value')
        if OMIT_KEY in row:
            if row.get('type') == OMIT_TYPE:
                del row[OMIT_KEY]
            elif self.snapshot is None:
                raise _row_limit('required snapshot value exceeds the retained-byte budget')
            else:
                row[OMIT_KEY] = json.loads(self.snapshot.decode())
        if self.payloads:
            if self.duplicate_key:
                raise projection_error()
            row = project_payloads(row, self.payloads, self.descriptor)
        return row, None, retained_size(row, len(self.payloads) * 16)


def retained_size(row, metadata=0):
    data = json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode('utf-8', errors='surrogatepass')
    size = len(data) + metadata
    if size > ROW_BYTES:
        raise _row_limit('required projection exceeds the per-row budget')
    return size


def decoded_size(row):
    """Conservatively charge containers, keys and values, including each repeated reference."""
    pending, size = [row], 64  # kept row pair and its slot
    while pending:
        value = pending.pop()
        size += sys.getsizeof(value)
        if isinstance(value, Payload):
            size += sys.getsizeof(value.size)
        if isinstance(value, ProjectedRow):
            size += sys.getsizeof(value.identity)
        if isinstance(value, dict):
            pending.extend(value)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return size


def projection_usage(snapshot):
    if json_record._entry(snapshot).claude_full:
        return record_file.ProjectionUsage(snapshot.stamp, compatibility=True)
    if snapshot.raw_digest is None:
        return None
    return record_file.ProjectionUsage(snapshot.stamp, snapshot.line_count, snapshot.projected_bytes,
                                       snapshot.retained_cost)


class Line:
    def __init__(self, descriptor=None, start=0):
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
            raise ProjectionLimit('JSON nesting exceeds the validation budget') from None
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
    large = size > COMPATIBILITY_BOUND
    rows_bound = record_file.CLAUDE_STREAM_ROWS if large else ROWS
    retained_bound = record_file.CLAUDE_STREAM_BYTES if large else PROJECTED_BYTES
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
            if large:
                decoded = cached.decoded_bytes
                if decoded is None:
                    decoded = sum(decoded_size(row) for _, row in rows)
            proof = offset, hasher.hexdigest()
        else:
            cached = None
            os.lseek(descriptor, 0, os.SEEK_SET)
            hasher = sha256()
    else:
        cached = None

    prefix_bytes, prefix = offset, hasher.copy()
    decoder = codecs.getincrementaldecoder('utf-8')()
    line = Line(None if splitlines else descriptor, offset)
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
        if large:
            if row is not None:
                decoded += decoded_size(row)
            if decoded + number * ROW_STATE_BYTES > record_file.CLAUDE_STREAM_MEMORY:
                raise ProjectionLimit('native record exceeds its retained memory budget', 'retained memory',
                                      limit=record_file.CLAUDE_STREAM_MEMORY, unit='bytes')
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
                parts = SPLIT_LINES.split(text)
                for part in parts[:-1]:
                    line.feed(part.encode('utf-8'))
                    finish_line()
                    line = Line()
                line.feed(parts[-1].encode('utf-8'))
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
        line = Line()
    complete_lines, complete_rows, complete_bytes, complete_decoded = number, len(rows), retained, decoded
    has_tail = bool(line.small or line.giant is not None) if splitlines else size != prefix_bytes
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
        projected_bytes=retained, decoded_bytes=complete_decoded if large else None,
        retained_cost=decoded + number * ROW_STATE_BYTES if large else None,
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
                except ProjectionLimit as error:
                    bound = min(COMPATIBILITY_BOUND, raw_bound)
                    if info.st_size > bound:
                        raise
                    # A traceback would retain the abandoned projection while
                    # the legacy reader allocates its own buffer and rows.
                    limit = ProjectionLimit(str(error), error.resource)
                if limit is not None:
                    full = record_file.read_from(descriptor, info, source, bound)
            except (OSError, UnicodeError) as error:
                failure = error
            if not _same_stamp(descriptor, source, stamp) or resolve(record) != source:
                continue
            if isinstance(failure, UnicodeError):
                raise record_file.RecordError('record is not UTF-8 JSON lines') from failure
            if failure is not None:
                if isinstance(failure, ProjectionLimit) and info.st_size > COMPATIBILITY_BOUND:
                    failure.projection_usage = record_file.ProjectionUsage(stamp, exhausted=failure.resource)
                elif limit is not None:
                    failure.projection_usage = record_file.ProjectionUsage(
                        stamp, exhausted=limit.resource, compatibility=True)
                raise failure
        finally:
            os.close(descriptor)

        finished = time.time_ns()
        certified = started if max(stamp[-2:]) + json_record._TIMESTAMP_TICK_NS < started <= checked <= finished else None
        if full is not None:
            try:
                return _compatible(key, source, full, stamp, certified, cached, limit)
            except OSError as error:
                error.projection_usage = record_file.ProjectionUsage(
                    stamp, exhausted=limit.resource, compatibility=True)
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
    try:
        return _consume(scan(descriptor, size, previous))
    except PhysicalSeparators:
        # Preserve str.splitlines() for older layouts without reusing a prefix.
        os.lseek(descriptor, 0, os.SEEK_SET)
        return _consume(scan(descriptor, size, None, splitlines=True))


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
    entry.source = source
    json_record._store(record, entry, cached is not None)
    return parsed


entries = partial(json_record.entries, parse=parse)
object_lines = partial(json_record.object_lines, parse=parse)
input_rows = partial(json_record.input_rows, parse=parse)
