"""Pure synthetic SQLite compatibility; no service, native client or authentication."""

import sqlite3
from datetime import datetime, timedelta, timezone

from quarterdeck.quota import Quotas

NOW = datetime(2040, 1, 1, tzinfo=timezone.utc)


def test_side_table_retains_exact_deadline_across_reopen(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    deadline = NOW + timedelta(minutes=40)
    with sqlite3.connect(path) as db:
        writer = Quotas(db)
        writer.usage_read("synthetic-pool", NOW, "synthetic refusal",
                          retry_at=deadline, retry_reason="native refusal")
    with sqlite3.connect(path) as db:
        reader = Quotas(db, create=False)
        assert reader.usage_hold("synthetic-pool") == deadline
        assert reader.usage_hold("synthetic-pool") == deadline
        assert reader.last_attempt("synthetic-pool") == NOW
        assert db.execute("SELECT set_at FROM usage_holds").fetchone() == (NOW.isoformat(),)


def test_previous_positional_writer_still_works_and_invalidates_old_hold():
    with sqlite3.connect(":memory:") as db:
        writer = Quotas(db)
        assert [row[1] for row in db.execute("PRAGMA table_info(usage_reads)")] == ["pool", "at", "error"]
        writer.usage_read("synthetic-pool", NOW, "synthetic refusal",
                          retry_at=NOW + timedelta(hours=1), retry_reason="native refusal")
        db.execute("INSERT OR REPLACE INTO usage_reads VALUES (?,?,?)",
                   ("synthetic-pool", (NOW + timedelta(hours=2)).isoformat(), None))
        assert writer.usage_hold("synthetic-pool") is None
        assert writer.last_attempt("synthetic-pool") == NOW + timedelta(hours=2)


def test_old_read_only_shape_has_no_fabricated_hold_or_migration(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE usage_reads (pool TEXT PRIMARY KEY, at TEXT NOT NULL, error TEXT)")
        db.execute("INSERT INTO usage_reads VALUES (?,?,?)", ("synthetic-pool", NOW.isoformat(), "old refusal"))
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        reader = Quotas(db, create=False)
        assert reader.usage_hold("synthetic-pool") is None
        assert reader.last_attempt("synthetic-pool") == NOW
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("usage_reads",)]
