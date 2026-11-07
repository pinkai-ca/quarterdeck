"""Strict protobuf fields and a source-read-only, in-memory SQLite WAL snapshot."""

from __future__ import annotations

import sqlite3
import struct
import time
from datetime import datetime, timezone
from pathlib import Path

from .base import RecordError, record_fingerprint

def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while pos < len(buf) and shift <= 63:
        b, pos = buf[pos], pos + 1
        if shift == 63 and b > 1:
            raise RecordError("protobuf varint exceeds 64 bits")
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    raise RecordError("corrupt protobuf varint")

def _proto(buf: bytes | None) -> dict[int, list]:
    if not buf:
        return {}
    if not isinstance(buf, (bytes, bytearray, memoryview)):
        raise RecordError("protobuf payload is not bytes")
    data, out, pos, n = bytes(buf), {}, 0, len(buf)
    while pos < n:
        key, pos = _varint(data, pos)
        field, wire = key >> 3, key & 0x7
        if field <= 0 or wire not in (0, 1, 2, 5):
            raise RecordError(f"invalid protobuf tag ({field}, {wire})")
        if wire == 0:
            val, pos = _varint(data, pos)
        else:
            size, pos = ((8 if wire == 1 else 4), pos) if wire != 2 else _varint(data, pos)
            if pos + size > n:
                raise RecordError("truncated protobuf field")
            val, pos = data[pos : pos + size], pos + size
        out.setdefault(field, []).append(val)
    return out

def _bytes(fields: dict[int, list], num: int) -> bytes | None:
    if num in fields and not isinstance(fields[num][-1], bytes):
        raise RecordError(f"field {num} is not bytes")
    return fields[num][-1] if num in fields else None

def _str(fields: dict[int, list], num: int) -> str | None:
    try:
        return raw.decode("utf-8") if (raw := _bytes(fields, num)) is not None else None
    except UnicodeDecodeError as error:
        raise RecordError(f"field {num} is not valid UTF-8") from error

def _int(fields: dict[int, list], num: int) -> int | None:
    if num in fields and not isinstance(fields[num][-1], int):
        raise RecordError(f"field {num} is not a varint")
    return fields[num][-1] if num in fields else None

def _ts(raw: bytes | None) -> datetime | None:
    if not raw or not ((sec := _int(f := _proto(raw), 1) or 0) or (_int(f, 2) or 0)):
        return None
    try:
        return datetime.fromtimestamp(sec + (_int(f, 2) or 0) / 1e9, timezone.utc)
    except (OSError, OverflowError, ValueError) as error:
        raise RecordError("invalid protobuf timestamp") from error

def _uints(fields: dict[int, list], num: int) -> list[int]:
    out: list[int] = []
    for item in fields.get(num, ()):
        if isinstance(item, int):
            out.append(item)
        elif isinstance(item, bytes):
            pos = 0
            while pos < len(item):
                val, pos = _varint(item, pos); out.append(val)
        else:
            raise RecordError(f"field {num} is not uints")
    return out

def _wal_cksum(data: bytes, s0: int, s1: int, endian: str) -> tuple[int, int]:
    ints = struct.unpack(f"{endian}{len(data) // 4}I", data)
    for i in range(0, len(ints), 2):
        s0 = (s0 + ints[i] + s1) & 0xFFFFFFFF; s1 = (s1 + ints[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1

def _apply_wal(name: str, db_bytes: bytes, wal_bytes: bytes | None) -> bytes:
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
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or psize != db_psize or not (512 <= psize <= 65536) or (psize & (psize - 1)) or (ck := _wal_cksum(wal_bytes[:24], 0, 0, endian)) != (h_s0, h_s1):
            raise RecordError(f"{name}: invalid WAL header")
        s0, s1, pos, fsize, staged, committed, n_pages = *ck, 32, 24 + psize, {}, {}, None
        while pos + fsize <= len(wal_bytes):
            pno, db_sz, fs1, fs2, f_s0, f_s1 = struct.unpack(">6I", wal_bytes[pos : pos + 24])
            page = wal_bytes[pos + 24 : pos + fsize]
            s0, s1 = _wal_cksum(page, *_wal_cksum(wal_bytes[pos : pos + 8], s0, s1, endian), endian)
            if (fs1, fs2) != (salt1, salt2):
                break  # stale frames following WAL restart belong to the previous generation
            if (s0, s1) != (f_s0, f_s1) or pno == 0:
                raise RecordError(f"{name}: invalid WAL frame")
            staged[pno] = page
            if db_sz > 0:
                committed.update(staged); staged.clear(); n_pages = db_sz
            pos += fsize
        if n_pages is not None:
            total = n_pages * psize
            if total > len(db_bytes) + len(wal_bytes):
                raise RecordError(f"{name}: invalid WAL database size")
            buf[total:] = b""
            buf.extend(b"\x00" * max(0, total - len(buf)))
            for pno, page in committed.items():
                if pno <= n_pages:
                    buf[(pno - 1) * psize : pno * psize] = page
    buf[18:20] = b"\x01\x01"
    return bytes(buf)

def _snapshot_query(record: Path) -> tuple[tuple, str | None, bytes | None, list, list, list]:
    """Read ``.db`` and any ``.db-wal`` into an in-memory SQLite snapshot with zero disk writes (law: one-folder)."""
    if not record.is_file():
        raise RecordError(f"{record.name}: record does not exist")
    wal = Path(f"{record}-wal")
    if Path(f"{record}-journal").exists():
        raise RecordError(f"{record.name}: rollback journal present; snapshot unsupported")
    for _ in range(3):
        before = record_fingerprint(record)
        try:
            db_bytes = record.read_bytes()  # law: one-folder
            wal_bytes = wal.read_bytes() if wal.is_file() else None  # law: one-folder
        except OSError as error:
            raise RecordError(f"{record.name}: {error}") from error
        if before == (after := record_fingerprint(record)):
            break
        time.sleep(0.01)
    else:
        raise RecordError(f"{record.name}: record changed concurrently during snapshot")
    merged = _apply_wal(record.name, db_bytes, wal_bytes)
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.deserialize(merged); q = con.execute
            if "steps" not in (tables := {r[0] for r in q("SELECT name FROM sqlite_master WHERE type='table'")}):
                raise RecordError(f"{record.name}: missing steps table")
            if not {"gen_metadata", "executor_metadata"} <= tables:
                raise RecordError(f"{record.name}: incomplete Antigravity schema")
            cascade = q("SELECT cascade_id FROM trajectory_meta LIMIT 1").fetchone() if "trajectory_meta" in tables else None
            meta = q("SELECT data FROM trajectory_metadata_blob WHERE id = 'main' LIMIT 1").fetchone() if "trajectory_metadata_blob" in tables else None
            steps = q("SELECT idx, step_type, status, has_subtrajectory, metadata, error_details, step_payload FROM steps ORDER BY idx").fetchall()
            if any(not isinstance(v, int) for row in steps for v in row[:4]):
                raise RecordError(f"{record.name}: unsupported step column types")
            gen_rows = q("SELECT idx, data FROM gen_metadata ORDER BY idx").fetchall() if "gen_metadata" in tables else []
            exec_rows = q("SELECT idx, data FROM executor_metadata ORDER BY idx").fetchall() if "executor_metadata" in tables else []
        finally:
            con.close()
    except sqlite3.Error as error:
        raise RecordError(f"{record.name}: {error}") from error
    return after, (cascade[0] if cascade and cascade[0] else None), (meta[0] if meta else None), steps, gen_rows, exec_rows

