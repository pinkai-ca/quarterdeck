"""A SQLite record read from a snapshot of its bytes and its write-ahead log, never opened in place.

Two programs keep their record in a SQLite database that they hold open while they run: Antigravity
and OpenCode. Opening such a database, even read-only, would write beside it (a shared-memory index,
a journal), so the reader copies the database file and its ``-wal`` into memory, applies the committed
frames of the log itself, and queries the result with the standard library's SQLite deserialized from
those bytes (law: one-folder). A record that changes while it is being copied is read again, three
times at most, so that a half-written frame is never taken for the truth.
"""

from __future__ import annotations

from collections import OrderedDict
import os
import struct
import threading
import time
from pathlib import Path

from . import record_file
from .base import RecordError, record_fingerprint
from ..record_location import resolve

WAL_MAGIC = (0x377F0682, 0x377F0683)
WAL_VERSION = 3007000


def _wal_cksum(data: bytes, s0: int, s1: int, endian: str) -> tuple[int, int]:
    ints = struct.unpack(f"{endian}{len(data) // 4}I", data)
    for i in range(0, len(ints), 2):
        s0 = (s0 + ints[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + ints[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def _apply_wal(name: str, db_bytes: bytes, wal_bytes: bytes | None) -> bytes:
    """The database with every committed frame of its write-ahead log applied, as bytes."""
    buf = bytearray(db_bytes)
    if len(buf) < 100 or not buf.startswith(b"SQLite format 3\x00"):
        raise RecordError(f"{name}: not a valid SQLite database")
    db_psize = int.from_bytes(buf[16:18], "big")
    db_psize = 65536 if db_psize == 1 else db_psize
    if wal_bytes:
        if len(wal_bytes) < 32:
            raise RecordError(f"{name}: truncated WAL header")
        magic, version, psize, _, salt1, salt2, h_s0, h_s1 = struct.unpack(">8I", wal_bytes[:32])
        endian = ">" if (magic & 1) else "<"
        valid_header = (magic in WAL_MAGIC and version == WAL_VERSION and psize == db_psize
                        and 512 <= psize <= 65536 and not (psize & (psize - 1)))
        if not valid_header or (ck := _wal_cksum(wal_bytes[:24], 0, 0, endian)) != (h_s0, h_s1):
            raise RecordError(f"{name}: invalid WAL header")
        s0, s1 = ck
        pos, fsize = 32, 24 + psize
        staged: dict[int, bytes] = {}
        committed: dict[int, bytes] = {}
        n_pages = None
        while pos + fsize <= len(wal_bytes):
            pno, db_sz, fs1, fs2, f_s0, f_s1 = struct.unpack(">6I", wal_bytes[pos:pos + 24])
            page = wal_bytes[pos + 24:pos + fsize]
            s0, s1 = _wal_cksum(page, *_wal_cksum(wal_bytes[pos:pos + 8], s0, s1, endian), endian)
            if (fs1, fs2) != (salt1, salt2):
                break  # stale frames following a WAL restart belong to the previous generation
            if (s0, s1) != (f_s0, f_s1) or pno == 0:
                raise RecordError(f"{name}: invalid WAL frame")
            staged[pno] = page
            if db_sz > 0:
                committed.update(staged)
                staged.clear()
                n_pages = db_sz
            pos += fsize
        if n_pages is not None:
            total = n_pages * psize
            if total > len(db_bytes) + len(wal_bytes):
                raise RecordError(f"{name}: invalid WAL database size")
            buf[total:] = b""
            buf.extend(b"\x00" * max(0, total - len(buf)))
            for pno, page in committed.items():
                if pno <= n_pages:
                    buf[(pno - 1) * psize:pno * psize] = page
    buf[18:20] = b"\x01\x01"  # a plain, non-WAL header: the bytes stand alone in memory
    return bytes(buf)


def snapshot(record: Path, bound: int | None = record_file.DATABASE_BOUND) -> tuple[tuple, bytes]:
    """The record's fingerprint and its merged bytes, read without a write beside the source (law: one-folder). The
    database and its log are read together within ``bound``; ``None`` only for the ship's own ledger, never a seat's."""
    record = resolve(record)
    if not record.is_file():
        raise RecordError(f"{record.name}: record does not exist")
    wal = Path(f"{record}-wal")
    if Path(f"{record}-journal").exists():
        raise RecordError(f"{record.name}: rollback journal present; snapshot unsupported")
    for _ in range(3):
        before = record_fingerprint(record)
        try:  # never through a link, never another file, and the database and its log together within one bound
            db_bytes, _ = record_file.read_bytes(record, bound)  # law: one-folder
            try:
                wal_bytes, _ = record_file.read_bytes(wal, None if bound is None else bound - len(db_bytes))
            except FileNotFoundError:
                wal_bytes = None
            except record_file.PastBound:
                raise record_file.past_bound(record, len(db_bytes) + os.lstat(wal).st_size, bound) from None
        except RecordError:
            raise
        except OSError as error:
            raise RecordError(f"{record.name}: {error}") from error
        if before == (after := record_fingerprint(record)):
            break
        time.sleep(0.01)
    else:
        raise RecordError(f"{record.name}: record changed concurrently during snapshot")
    return after, _apply_wal(record.name, db_bytes, wal_bytes)


# -- one snapshot per change ------------------------------------------------------------------------------------------

# The post store's filesystem tick (post.POST_TIMESTAMP_TICK_NS, docs/design/post.md), repeated here: a record reader
# does not import the post. A file whose stamp is not older than this at the moment a read began may still change
# without a new stamp, so such a read is never kept.
TICK_NS = 2_000_000_000
CACHE_BYTES = 256 * 1024 * 1024  # all kept snapshots together; the least recently used goes first
CACHE_ENTRIES = 64
_now = time.time_ns  # the clock a read is certified by; tests inject their own
_entries: OrderedDict[str, "_Entry"] = OrderedDict()
_lock = threading.Lock()


class _Entry:
    """One database path's kept snapshot: its stamp, its bytes, and what readers derived from those bytes."""

    __slots__ = ('stamp', 'bound', 'merged', 'derived')

    def __init__(self, stamp: tuple, bound: int | None, merged: bytes):
        self.stamp, self.bound, self.merged = stamp, bound, merged
        self.derived: dict = {}  # immutable values only, keyed by the reader that made them


def _certified(stamp: tuple, started: int) -> bool:
    """Every file the stamp names, the database and its -wal where present, is older than the tick when the read
    began: the post store's rule for certifying a listing."""
    return all(not present or max(mtime, ctime) + TICK_NS < started
               for present, _inode, _size, mtime, _device, ctime in stamp)


def cached(record: Path, bound: int | None = record_file.DATABASE_BOUND) -> tuple[tuple, bytes, dict | None]:
    """``snapshot``, taken once per change of the database and its -wal. The stamp (record_fingerprint: presence,
    device, inode, size, mtime_ns and ctime_ns of each) is read first; a kept snapshot with the same stamp and bound
    is returned as it was. Otherwise a fresh snapshot is taken, and kept only where its stamp did not move during the
    read and was certified when the read began. A path that is gone drops its entry. The third value is the entry's
    store for derived readings, None where nothing was kept: what a reader puts there must be immutable."""
    record = resolve(record)
    key = str(record)
    started = _now()
    stamp = record_fingerprint(record)
    with _lock:
        entry = _entries.pop(key, None)
        if entry is not None and stamp[0][0] and entry.stamp == stamp and entry.bound == bound:
            _entries[key] = entry  # most recently used, last
            return entry.stamp, entry.merged, entry.derived
    after, merged = snapshot(record, bound)  # a gone path raises here, its entry already dropped
    if after != stamp or not _certified(after, started) or len(merged) > CACHE_BYTES:
        return after, merged, None
    entry = _Entry(after, bound, merged)
    with _lock:
        _entries[key] = entry
        held = sum(len(kept.merged) for kept in _entries.values())
        while len(_entries) > CACHE_ENTRIES or held > CACHE_BYTES:
            _, evicted = _entries.popitem(last=False)
            held -= len(evicted.merged)
    return after, merged, entry.derived


def forget() -> None:
    """Drop every kept snapshot (a test's fresh start)."""
    with _lock:
        _entries.clear()
