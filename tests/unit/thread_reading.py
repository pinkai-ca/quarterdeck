"""Read a launch's index rows and what qd thread prints for one reader, for the carriage's protection tests."""

import json

from quarterdeck import post_threads


def index_rows(first: str) -> list[dict]:
    """The index rows of a first message: one JSON row per thread."""
    return [json.loads(line) for line in first.splitlines() if line.startswith('{"id"')]


def row_for(first: str, root_id: str) -> dict:
    [row] = [row for row in index_rows(first) if row['id'] == root_id]
    return row


def thread_text(store, message_id: str, reader: str) -> str:
    """What `qd thread <message_id>` prints for this reader."""
    return post_threads.render(post_threads.whole(store, message_id, reader))
