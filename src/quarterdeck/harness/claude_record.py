"""Bounded native Claude reads, with guarded raw record identity."""

from hashlib import sha256
import codecs
import json
import os
from functools import partial
import time
from pathlib import Path
import re

from . import json_record, record_file
from ..record_location import resolve


CHUNK = 64 * 1024
ROW_BYTES = 256 * 1024
PROJECTED_BYTES = 16 * 1024 * 1024
ROWS = 65_536
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


class Unknown(record_file.RecordError):
    """Required native evidence exceeds the reader's retained-data budget."""


class PhysicalSeparators(Exception):
    """An older text layout needs Unicode splitlines boundaries."""


class GiantRow:
    """Validate JSON tokens and nesting while withholding the root snapshot value."""

    def __init__(self):
        self.frames = []
        self.output = bytearray()
        self.snapshot = None
        self.in_snapshot = False
        self.root_key = None
        self.key_bytes = None
        self.atom = None
        self.string = False
        self.escape = 0
        self.done = False

    def emit(self, data):
        if self.in_snapshot:
            if self.snapshot is not None:
                if len(self.snapshot) + len(data) <= ROW_BYTES:
                    self.snapshot.extend(data)
                else:
                    self.snapshot = None
        else:
            if len(self.output) + len(data) > ROW_BYTES:
                raise Unknown('required JSON row exceeds the retained-byte budget')
            self.output.extend(data)
        if self.key_bytes is not None:
            self.key_bytes.extend(data)

    def value(self):
        if not self.frames:
            raise ValueError('JSON value in an unexpected position')
        if self.frames[-1] in ('array first', 'array value'):
            self.frames[-1] = 'array next'
        elif self.frames[-1] == 'value':
            self.frames[-1] = 'object next'
        else:
            raise ValueError('JSON value in an unexpected position')

    def end_value(self):
        if len(self.frames) == 1:
            self.in_snapshot = False

    def punct(self, char):
        if self.done:
            raise ValueError('data after root JSON object')
        if char in b'{[':
            if self.frames:
                self.value()
            elif char != ord('{'):
                raise Unknown('large row does not start with a native JSON object')
            if len(self.frames) >= DEPTH:
                raise Unknown('JSON nesting exceeds the validation budget')
            self.frames.append('object first' if char == ord('{') else 'array first')
        elif not self.frames:
            raise ValueError('root JSON object required')
        elif char == ord(':'):
            if self.frames[-1] != 'colon':
                raise ValueError('unexpected JSON colon')
            self.frames[-1] = 'value'
            self.emit(bytes((char,)))
            if len(self.frames) == 1 and self.root_key == OMIT_KEY:
                self.emit(b'null')
                self.in_snapshot = True
                self.snapshot = bytearray()
            return
        elif char == ord(','):
            if self.frames[-1] == 'object next':
                self.frames[-1] = 'key'
            elif self.frames[-1] == 'array next':
                self.frames[-1] = 'array value'
            else:
                raise ValueError('unexpected JSON comma')
        else:
            wanted = ('object first', 'object next') if char == ord('}') else ('array first', 'array next')
            if self.frames[-1] not in wanted:
                raise ValueError('incomplete JSON value')
            self.emit(bytes((char,)))
            self.frames.pop()
            self.end_value()
            self.done = not self.frames
            return
        self.emit(bytes((char,)))

    def string_start(self):
        if not self.frames:
            raise ValueError('root JSON object required')
        if self.frames[-1] in ('key', 'object first'):
            if len(self.frames) == 1:
                self.key_bytes = bytearray()
            self.frames[-1] = 'colon'
        else:
            self.value()
        self.string = True
        self.emit(b'"')

    def string_end(self):
        if self.frames[-1] == 'colon':
            if self.key_bytes is not None:
                self.root_key = json.loads(self.key_bytes.decode())
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
        at = 0
        while at < len(data):
            if self.string:
                if self.escape:
                    char = data[at]
                    if self.escape == -1:
                        if char not in b'"\\/bfnrtu':
                            raise ValueError('invalid JSON escape')
                        self.escape = 4 if char == ord('u') else 0
                    else:
                        if char not in HEX_DIGITS:
                            raise ValueError('invalid JSON Unicode escape')
                        self.escape -= 1
                    self.emit(data[at:at + 1])
                    at += 1
                    continue
                special = SPECIAL.search(data, at)
                if special is None:
                    self.emit(data[at:])
                    return
                self.emit(data[at:special.end()])
                at = special.end()
                char = data[special.start()]
                if char == ord('"'):
                    self.string_end()
                elif char == ord('\\'):
                    self.escape = -1
                else:
                    raise ValueError('unescaped control byte in JSON string')
            elif self.atom is not None:
                stop = ATOM_END.search(data, at)
                end = stop.start() if stop else len(data)
                if len(self.atom) + end - at > ATOM_BYTES:
                    raise Unknown('JSON atom exceeds the validation budget')
                self.atom.extend(data[at:end])
                at = end
                if stop:
                    self.atom_end()
            else:
                char = data[at]
                if char in b' \t':
                    at += 1
                elif char == ord('"'):
                    self.string_start()
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
        if OMIT_KEY not in row:
            raise Unknown('large row had no discardable value')
        if row.get('type') == OMIT_TYPE:
            del row[OMIT_KEY]
        elif self.snapshot is None:
            raise Unknown('required snapshot value exceeds the retained-byte budget')
        else:
            row[OMIT_KEY] = json.loads(self.snapshot.decode())
        return row, None, retained_size(row)


def retained_size(row):
    data = json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode('utf-8', errors='surrogatepass')
    if len(data) > ROW_BYTES:
        raise Unknown('required projection exceeds the per-row budget')
    return len(data)


class Line:
    def __init__(self):
        self.small = bytearray()
        self.giant = None
        self.malformed = False

    def feed(self, data):
        if self.malformed:
            return
        if self.giant is None and len(self.small) + len(data) <= ROW_BYTES:
            self.small.extend(data)
            return
        try:
            if self.giant is None:
                self.giant = GiantRow()
                self.giant.feed(self.small)
                self.small.clear()
            self.giant.feed(data)
        except ValueError:
            self.malformed = True

    def finish(self):
        if self.malformed:
            return MALFORMED
        if self.giant is not None:
            try:
                return self.giant.finish()
            except (ValueError, RecursionError):
                return MALFORMED
        if not self.small.strip():
            return None, None, 0
        text = self.small.decode('utf-8')
        if not text.isascii() and not text.strip():
            return None, None, 0  # matches str.splitlines rows containing only Unicode whitespace
        try:
            row = json.loads(text)
        except RecursionError:
            raise Unknown('JSON nesting exceeds the validation budget') from None
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
    offset = number = retained = 0
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
            proof = offset, hasher.hexdigest()
        else:
            cached = None
            os.lseek(descriptor, 0, os.SEEK_SET)
            hasher = sha256()
    else:
        cached = None

    prefix_bytes, prefix = offset, hasher.copy()
    decoder = codecs.getincrementaldecoder('utf-8')()
    line = Line()
    trailing_cr = ends_lf = False

    def finish_line():
        nonlocal retained, number
        number += 1
        row, diagnostic, weight = line.finish()
        retained += weight
        if retained > PROJECTED_BYTES or number > ROWS:
            raise Unknown('native record exceeds its retained-byte or line budget')
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
            for part in parts[:-1]:
                line.feed(part)
                finish_line()
                line = Line()
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
    complete_lines, complete_rows, complete_bytes = number, len(rows), retained
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


def _parse(record):
    key = Path(record)
    cached = json_record._snapshots.pop(key, None)
    if cached is not None and cached.raw_digest is None:
        cached = None
    for attempt in range(3):
        source = resolve(record)
        started = time.time_ns()
        descriptor, info = record_file.open_record(source)
        try:
            checked = time.time_ns()
            stamp = json_record._stamp(info)
            if info.st_size > record_file.LINES_BOUND:
                raise record_file.past_bound(source, info.st_size, record_file.LINES_BOUND)
            if (attempt == 0 and cached is not None and cached.stamp == stamp and cached.certified_at is not None
                    and cached.source == source and cached.certified_at <= started <= checked):
                json_record._snapshots[key] = cached
                return cached
            previous = cached if cached is not None and cached.source == source and cached.stamp[:2] == stamp[:2] else None
            failure = None
            try:
                try:
                    entry, reused = _consume(scan(descriptor, info.st_size, previous))
                except PhysicalSeparators:
                    # Preserve str.splitlines() for older layouts. Their prefix
                    # stays uncached, so split CRLF and Unicode separators are
                    # always re-established from the original beginning.
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    entry, reused = _consume(scan(descriptor, info.st_size, None, splitlines=True))
            except (OSError, UnicodeError) as error:
                failure = error
            if not _same_stamp(descriptor, source, stamp) or resolve(record) != source:
                continue
            if isinstance(failure, UnicodeError):
                raise record_file.RecordError('record is not UTF-8 JSON lines') from failure
            if failure is not None:
                raise failure
        finally:
            os.close(descriptor)

        finished = time.time_ns()
        entry.stamp, entry.source = stamp, source
        if max(stamp[-2:]) + json_record._TIMESTAMP_TICK_NS < started <= checked <= finished:
            entry.certified_at = started
        if reused is not None:
            if entry.raw_digest == reused.raw_digest:
                reused.stamp, reused.certified_at = entry.stamp, entry.certified_at
                entry = reused
            elif reused.ends_newline and entry.ends_newline:
                json_record._carry(reused, entry, entry.rows[reused.row_count:], entry.skipped[len(reused.skipped):])
        json_record._store(key, entry, cached is not None)
        return entry
    raise record_file.RecordError('record changed while being read')


entries = partial(json_record.entries, parse=parse)
object_lines = partial(json_record.object_lines, parse=parse)
input_rows = partial(json_record.input_rows, parse=parse)
