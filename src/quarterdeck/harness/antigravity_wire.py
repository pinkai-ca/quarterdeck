"""Strict protobuf fields over the source-read-only SQLite snapshot of ``sqlite_snapshot``."""

from __future__ import annotations

import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

from .base import RecordError
from .sqlite_snapshot import _apply_wal, _wal_cksum, snapshot  # the two helpers stay importable here for their tests

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

def _snapshot_query(record: Path) -> tuple[tuple, str | None, bytes | None, list, list, list]:
    """Read ``.db`` and any ``.db-wal`` into an in-memory SQLite snapshot with zero disk writes (law: one-folder)."""
    after, merged = snapshot(record)
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
