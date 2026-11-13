"""Per-turn tool lookups stay indexed on new and upgraded ledgers."""

import sqlite3

import pytest

from quarterdeck.ledger import Ledger, TOOL_COLUMNS


@pytest.mark.parametrize("existing", [False, True], ids=["new", "existing"])
@pytest.mark.parametrize("statement", [
    f"SELECT {', '.join(TOOL_COLUMNS)} FROM tool_calls WHERE record = ? AND turn = ? ORDER BY rowid",
    "DELETE FROM tool_calls WHERE record = ? AND turn = ?",
], ids=["select", "delete"])
def test_per_turn_tool_query_uses_index(tmp_path, existing, statement):
    path = tmp_path / "ledger.sqlite"
    if existing:
        with sqlite3.connect(path) as old:
            old.execute("CREATE TABLE tool_calls (record TEXT NOT NULL, turn INTEGER NOT NULL, "
                        "name TEXT NOT NULL, seconds REAL, failed INTEGER)")
            old.execute("INSERT INTO tool_calls VALUES ('synthetic-record', 1, 'read', 0.1, 0)")
            assert not old.execute("PRAGMA index_list(tool_calls)").fetchall()

    with Ledger(path) as ledger:
        plan = ledger.db.execute("EXPLAIN QUERY PLAN " + statement, ("synthetic-record", 1)).fetchall()
        detail = " ".join(row[3] for row in plan)
        assert "SEARCH tool_calls USING INDEX tool_calls_turn" in detail
        assert "SCAN" not in detail
        assert "TEMP B-TREE" not in detail


def test_existing_ledger_upgrade_preserves_tools_and_releases_transaction(tmp_path):
    path = tmp_path / "ledger.sqlite"
    tools = [("synthetic-record", 1, "read", 0.1, 0),
             ("synthetic-record", 1, "read", 0.2, 1),
             ("another-record", 1, "write", None, None)]
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE tool_calls (record TEXT NOT NULL, turn INTEGER NOT NULL, "
                    "name TEXT NOT NULL, seconds REAL, failed INTEGER)")
        old.executemany("INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?)", tools)

    for _ in range(2):
        with Ledger(path) as ledger:
            assert not ledger.db.in_transaction
            assert ledger.db.execute("SELECT * FROM tool_calls ORDER BY rowid").fetchall() == tools
            assert ledger.db.execute("PRAGMA index_info(tool_calls_turn)").fetchall() == [
                (0, 0, "record"), (1, 1, "turn")]
            assert len(ledger.db.execute("PRAGMA index_list(tool_calls)").fetchall()) == 1
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0) as reader:
                assert reader.execute("SELECT COUNT(*) FROM tool_calls").fetchone() == (len(tools),)
