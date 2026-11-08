"""The Antigravity record's shapes, written for the tests; the package itself never writes this record."""
from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from quarterdeck.harness import antigravity
from quarterdeck.harness.synthetic_record import now, tokens_for


def _pb_varint(val: int) -> bytes:
    out = bytearray()
    while val > 0x7F:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val & 0x7F)
    return bytes(out)


def _pb_uint(field: int, val: int) -> bytes:
    return _pb_varint((field << 3) | 0) + _pb_varint(val)


def _pb_bytes(field: int, data: bytes) -> bytes:
    return _pb_varint((field << 3) | 2) + _pb_varint(len(data)) + data


def _pb_str(field: int, text: str) -> bytes:
    return _pb_bytes(field, text.encode("utf-8"))


def _pb_ts(field: int, moment: datetime) -> bytes:
    ts = moment.timestamp()
    sec = int(ts)
    nanos = int(round((ts - sec) * 1e9))
    return _pb_bytes(field, _pb_uint(1, sec) + (_pb_uint(2, nanos) if nanos else b""))


AGY_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS trajectory_meta (key TEXT PRIMARY KEY, cascade_id TEXT, last_modified_time TEXT, trajectory_type INTEGER);
CREATE TABLE IF NOT EXISTS trajectory_metadata_blob (id TEXT PRIMARY KEY, data BLOB);
CREATE TABLE IF NOT EXISTS steps (idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER, has_subtrajectory INTEGER,
                                  metadata BLOB, error_details BLOB, step_payload BLOB);
CREATE TABLE IF NOT EXISTS gen_metadata (idx INTEGER PRIMARY KEY, data BLOB);
CREATE TABLE IF NOT EXISTS executor_metadata (idx INTEGER PRIMARY KEY, data BLOB);
CREATE TABLE IF NOT EXISTS subtrajectories (step_idx INTEGER PRIMARY KEY, data BLOB);
"""


class AntigravityShape:
    """``conversations/<session>.db``: SQLite in WAL mode with CortexStep / generator / executor protobuf blobs."""

    def __init__(self, home: Path, cwd: Path, session: str, model: str, path: Path | None = None):
        self.home, self.cwd, self.session, self.model = home, str(cwd), session, model
        self.path = path or (home / "conversations" / f"{session}.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        home.mkdir(parents=True, exist_ok=True)
        owned = home / antigravity.OWNED
        existing = owned.read_text().splitlines() if owned.is_file() else []
        if str(self.path) not in existing:
            owned.write_text("".join(f"{line}\n" for line in [*existing, str(self.path)]))
        self.exec_id = ""
        self.text = ""
        self.call_id = ""
        self.call_name = ""
        self.tool_idx: int | None = None
        self.tool_started: datetime | None = None
        self.turn = len(antigravity.turns(self.path)) if self.path.exists() else 0
        with self._conn() as con:
            con.executescript(AGY_SCHEMA)
            if not con.execute("SELECT 1 FROM trajectory_meta").fetchone():
                stamp = now()
                con.execute("INSERT INTO trajectory_meta VALUES ('main', ?, ?, 17)", (session, stamp.isoformat()))
                con.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)",
                            (_pb_ts(2, stamp) + _pb_str(6, session),))

    @contextlib.contextmanager
    def _conn(self):
        con = sqlite3.connect(self.path)
        try:
            with con:
                yield con
        finally:
            con.close()

    def _next_idx(self, con: sqlite3.Connection, table: str = "steps") -> int:
        row = con.execute(f"SELECT COALESCE(MAX(idx), -1) + 1 FROM {table}").fetchone()
        return int(row[0])

    def begin(self, text: str, model: str, effort: str) -> None:
        self.text, self.model, self.exec_id = text, model, str(uuid.uuid4())
        stamp = now()
        meta = _pb_ts(1, stamp) + _pb_ts(6, stamp) + _pb_str(12, self.exec_id)
        payload = _pb_bytes(5, meta) + _pb_bytes(19, _pb_str(2, text))
        with self._conn() as con:
            idx = self._next_idx(con)
            con.execute("INSERT INTO steps VALUES (?, 14, 3, 0, ?, ?, ?)", (idx, meta, b"", payload))

    def consumed(self, text: str) -> None:
        """A mid-turn user input absorbed into the running execution."""
        stamp = now()
        meta = _pb_ts(1, stamp) + _pb_ts(6, stamp) + _pb_str(12, self.exec_id)
        payload = _pb_bytes(5, meta) + _pb_bytes(19, _pb_str(2, text))
        with self._conn() as con:
            idx = self._next_idx(con)
            con.execute("INSERT INTO steps VALUES (?, 14, 3, 0, ?, ?, ?)", (idx, meta, b"", payload))

    def _planner_step(self, con: sqlite3.Connection, reply: str = "", tool_call: bytes = b"") -> int:
        idx = self._next_idx(con)
        stamp = now()
        t = tokens_for(self.text, reply, self.turn)
        usage = (_pb_uint(1, 0) + _pb_uint(2, t["read_uncached"])
                 + _pb_uint(3, t["written"]) + _pb_uint(5, t["read_cached"]))
        meta = _pb_ts(1, stamp) + _pb_ts(6, stamp) + _pb_ts(8, stamp) + _pb_bytes(9, usage) + _pb_str(12, self.exec_id) + _pb_uint(15, 1)
        pr = (_pb_str(1, reply) + _pb_str(8, reply) if reply else b"") + (_pb_bytes(7, tool_call) if tool_call else b"")
        payload = _pb_bytes(5, meta) + _pb_bytes(20, pr)
        con.execute("INSERT INTO steps VALUES (?, 15, 3, 0, ?, ?, ?)", (idx, meta, b"", payload))
        chat = _pb_bytes(4, usage) + _pb_str(19, self.model) + _pb_str(21, self.model)
        gm = _pb_bytes(1, chat) + _pb_uint(2, idx) + _pb_str(4, self.exec_id)
        con.execute("INSERT INTO gen_metadata VALUES (?, ?)", (self._next_idx(con, "gen_metadata"), gm))
        return idx

    def tool_start(self, name: str, input_: str) -> None:
        self.call_id, self.call_name, self.tool_started = f"call_{uuid.uuid4().hex[:12]}", name, now()
        tc = _pb_str(1, self.call_id) + _pb_str(2, name) + _pb_str(3, json.dumps({"command": input_}))
        with self._conn() as con:
            self._planner_step(con, tool_call=tc)
            self.tool_idx = self._next_idx(con)
            meta = _pb_ts(1, self.tool_started) + _pb_bytes(4, tc) + _pb_str(12, self.exec_id) + _pb_ts(32, self.tool_started)
            con.execute("INSERT INTO steps VALUES (?, 132, 9, 0, ?, ?, ?)", (self.tool_idx, meta, b"", _pb_bytes(5, meta)))

    def tool_end(self, output: str, failed: bool) -> None:
        ended = now()
        started = self.tool_started or ended
        tc = _pb_str(1, self.call_id) + _pb_str(2, self.call_name)
        meta = (_pb_ts(1, started) + _pb_bytes(4, tc) + _pb_ts(6, ended) + _pb_ts(7, ended)
                + _pb_ts(8, ended) + _pb_str(12, self.exec_id) + _pb_ts(32, started))
        rc = _pb_uint(1, 1 if failed else 0) + _pb_uint(2, 1 if failed else 0)
        any_rc = _pb_str(1, "type.googleapis.com/exa.cortex.core.tools.RunCommand") + _pb_bytes(2, rc)
        res = _pb_str(1, f"Command exited with code {int(failed)}\n{output}") + _pb_bytes(6, any_rc)
        payload = _pb_bytes(5, meta) + _pb_bytes(140, _pb_bytes(2, res))
        with self._conn() as con:
            con.execute("UPDATE steps SET status = ?, metadata = ?, step_payload = ? WHERE idx = ?",
                        (7 if failed else 3, meta, payload, self.tool_idx))

    def tool(self, name: str, input_: str, output: str, failed: bool) -> None:
        self.tool_start(name, input_)
        self.tool_end(output, failed)

    def end(self, reply: str) -> None:
        stamp = now()
        with self._conn() as con:
            last_idx = self._planner_step(con, reply=reply)
            cfg = _pb_bytes(1, _pb_str(28, self.model))
            em = _pb_uint(1, 4) + _pb_uint(2, 1) + _pb_uint(3, last_idx) + _pb_str(9, self.exec_id) + _pb_bytes(10, cfg)
            con.execute("INSERT INTO executor_metadata VALUES (?, ?)", (self._next_idx(con, "executor_metadata"), em))
        self.turn += 1

    def abort(self) -> None:
        stamp = now()
        with self._conn() as con:
            idx = self._next_idx(con)
            meta = _pb_ts(1, stamp) + _pb_ts(7, stamp) + _pb_str(12, self.exec_id)
            con.execute("INSERT INTO steps VALUES (?, 15, 6, 0, ?, ?, ?)", (idx, meta, b"", _pb_bytes(5, meta)))
            cfg = _pb_bytes(1, _pb_str(28, self.model))
            em = _pb_uint(1, 2) + _pb_uint(2, 1) + _pb_uint(3, idx) + _pb_str(9, self.exec_id) + _pb_bytes(10, cfg)
            con.execute("INSERT INTO executor_metadata VALUES (?, ?)", (self._next_idx(con, "executor_metadata"), em))
        self.turn += 1

    def turns_before(self) -> int:
        return self.turn
