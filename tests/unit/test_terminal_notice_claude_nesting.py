"""Claude validation of the terminal-notice carriage: deeply nested or oversized structure journals are unknown evidence.

A deeply nested parse ends in RecursionError rather than ValueError; an oversized
journal is read as unknown without being parsed (the 1 MiB read bound). Neither grants
a terminal-notice exception, and neither stops a fresh start's carriage.
Wholly synthetic; no native program, provider or delivery action runs here.
"""

import json

from quarterdeck import post, thread_context
from quarterdeck.harness.pi_thinking import Client as PiThinking
from .test_pending_delivery import setup  # noqa: F401
from .test_terminal_notice_claude_validation import (STRUCTURES, deliver_all, delivered, journal, snapshot,
                                                     structure_with_recovery)

NESTED = b'[' * 200_000 + b']' * 200_000


@STRUCTURES
def test_deeply_nested_journal_grants_no_exception_in_selection(tmp_path, structure):
    store = post.Store(tmp_path)
    root, continued, terminal = structure_with_recovery(tmp_path, store, 'lead', structure,
                                                        PiThinking.continuation)
    delivered(tmp_path, 'lead', root, continued, terminal)
    path = journal(tmp_path, structure, root)
    path.unlink()
    path.write_bytes(NESTED)
    before = snapshot(tmp_path)
    assert thread_context.select(store, 'lead').incoming == [root]
    assert snapshot(tmp_path) == before


@STRUCTURES
def test_deeply_nested_journal_does_not_stop_fresh_start_carriage(setup, structure):
    svc, seat, live, record = setup
    runtime = svc.ship.runtime
    root, continued, terminal = structure_with_recovery(runtime, svc.store, seat.name, structure,
                                                        PiThinking.continuation)
    deliver_all(runtime, svc.store, seat.name)
    path = journal(runtime, structure, root)
    path.unlink()
    path.write_bytes(NESTED)
    text, incoming = seat.first_message()
    assert incoming == [root] and PiThinking.continuation not in text


OVERSIZED = 16 * 1024 * 1024  # far above any bound near the 1 MiB the captain named


@STRUCTURES
def test_oversized_journal_is_unknown_without_being_parsed(tmp_path, structure, monkeypatch):
    store = post.Store(tmp_path)
    root, continued, terminal = structure_with_recovery(tmp_path, store, 'lead', structure,
                                                        PiThinking.continuation)
    delivered(tmp_path, 'lead', root, continued, terminal)
    path = journal(tmp_path, structure, root)
    raw = path.read_bytes()  # consistent metadata: only its size is wrong
    path.unlink()
    path.write_bytes(raw + b' ' * (OVERSIZED - len(raw)))
    parsed = []
    original = json.loads
    monkeypatch.setattr(json, 'loads', lambda text, *a, **k: parsed.append(len(text)) or original(text, *a, **k))
    before = snapshot(tmp_path)
    assert thread_context.select(store, 'lead').incoming == [root]
    assert not any(size >= OVERSIZED for size in parsed), 'an oversized journal was parsed'
    assert snapshot(tmp_path) == before
