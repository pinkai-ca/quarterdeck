"""A waiting inbox watch reads the delivery attempts in full only when they may have changed.

The look trusts a matching stamp of the attempts folder and the retired index under the store's
racy-timestamp rule, and reads in full at least every rescan period, so an edit made in place is still
seen. What it delivers is decided as before, under the seat's admission (law: one-door).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from quarterdeck import fleet_post, post, post_dir
from quarterdeck.post import Store

TICK = 50_000_000  # a short tick for tests; the rule is the same as for the real two seconds


def settle():
    time.sleep(TICK * 2 / 1e9)


@pytest.fixture
def short_tick(monkeypatch):
    monkeypatch.setattr(post, "POST_TIMESTAMP_TICK_NS", TICK)


def test_a_settled_look_reads_the_attempts_once_while_nothing_changes(tmp_path: Path, short_tick):
    store, attempts = Store(tmp_path), post.Attempts(tmp_path)
    question = store.new("question", "operator", "desk", "Which branch?")
    attempts.open(question, "desk", None, None, None)
    settle()
    look = post.AttemptsLook(attempts)
    first = look.latest()
    assert first == {question.id: None} and look.full_reads == 1
    for _ in range(5):
        assert look.latest() == first
    assert look.full_reads == 1, "nothing changed, so nothing was read again"


def test_a_new_attempt_and_a_rewritten_one_are_both_seen(tmp_path: Path, short_tick):
    store, attempts = Store(tmp_path), post.Attempts(tmp_path)
    one = store.new("question", "operator", "desk", "one")
    opened = attempts.open(one, "desk", None, None, None)
    settle()
    look = post.AttemptsLook(attempts)
    assert look.latest() == {one.id: None}
    settle()
    # Another process closes the attempt at the same path: the rename moves the folder's times.
    post.Attempts(tmp_path).close(opened, "watch", "printed by qd inbox --wait")
    assert look.latest() == {one.id: "watch"} and look.full_reads == 2
    settle()
    two = store.new("question", "operator", "desk", "two")
    post.Attempts(tmp_path).open(two, "desk", None, None, None)
    assert look.latest() == {one.id: "watch", two.id: None} and look.full_reads == 3


def test_a_recent_change_is_never_trusted(tmp_path: Path):
    # The real two-second tick: a folder changed within it is read in full on every look.
    store, attempts = Store(tmp_path), post.Attempts(tmp_path)
    attempts.open(store.new("question", "operator", "desk", "one"), "desk", None, None, None)
    look = post.AttemptsLook(attempts)
    for _ in range(3):
        look.latest()
    assert look.full_reads == 3


def test_a_retirement_batch_is_seen(tmp_path: Path, short_tick):
    store, attempts = Store(tmp_path), post.Attempts(tmp_path)
    attempts.open(store.new("question", "operator", "desk", "one"), "desk", None, None, None)
    index = tmp_path / "post" / "retired" / "index"
    index.mkdir(parents=True)
    settle()
    look = post.AttemptsLook(attempts)
    look.latest()
    look.latest()
    assert look.full_reads == 1
    settle()
    (index / "batch-1.json").write_text(json.dumps(dict(format=1, attempts={})) + "\n")
    look.latest()
    assert look.full_reads == 2, "a new batch in the retired index moves its folder's times"


def test_an_edit_in_place_is_seen_by_the_full_read_within_the_rescan_period(tmp_path: Path, short_tick):
    store, attempts = Store(tmp_path), post.Attempts(tmp_path)
    question = store.new("question", "operator", "desk", "one")
    opened = attempts.open(question, "desk", None, None, None)
    settle()
    look = post.AttemptsLook(attempts, rescan=0.3)
    assert look.latest() == {question.id: None}
    folder_times = os.stat(attempts.dir).st_mtime_ns
    data = json.loads(opened.path.read_text())
    data["result"] = "blind"
    with open(opened.path, "r+") as handle:  # no framework writer does this; the full read still sees it
        handle.write(json.dumps(data, indent=2) + "\n")
    assert os.stat(attempts.dir).st_mtime_ns == folder_times, "an edit in place leaves the folder's times"
    assert look.latest() == {question.id: None}, "inside the rescan period the settled reading stands"
    time.sleep(0.35)
    assert look.latest() == {question.id: "blind"} and look.full_reads == 2


def test_the_waiting_watch_reads_the_attempts_once_and_delivers_an_arrival_once(tmp_path: Path, short_tick, monkeypatch):
    store, attempts = Store(tmp_path), post.Attempts(tmp_path)
    old = store.new("question", "operator", "desk", "already printed")
    attempts.close(attempts.open(old, "desk", None, None, None), "watch", "printed by qd inbox --wait")
    settle()
    reads = []
    original = post.Attempts._all_current

    def counted(self, **kwargs):
        reads.append(time.monotonic())
        return original(self, **kwargs)

    monkeypatch.setattr(post.Attempts, "_all_current", counted)
    assert post.wait_batch(tmp_path, "desk", interval=0.02, stop_after=0.6) is None
    assert len(reads) <= 2, f"an idle wait of about 30 checks read the attempts in full {len(reads)} times"

    reads.clear()
    found = {}
    waiting = threading.Thread(target=lambda: found.setdefault(
        "batch", post.wait_batch(tmp_path, "desk", interval=0.02, stop_after=5)))
    waiting.start()
    time.sleep(0.4)
    arrival = Store(tmp_path).new("question", "operator", "desk", "new")
    waiting.join(10)
    messages, opened = found["batch"]
    assert [m.id for m in messages] == [arrival.id], "the arrival alone, and the printed message never again"
    assert [(a.message, a.result) for a in opened] == [(arrival.id, None)]
    post.printed(tmp_path, opened)
    assert post.wait_batch(tmp_path, "desk", interval=0.02, stop_after=0.3) is None, "delivered once"  # law: one-door


def test_a_waiting_watch_lists_imports_and_forwarded_once_a_check_never_once_a_message(tmp_path: Path, short_tick,
                                                                                         monkeypatch):
    # Every message ever addressed to the seat is judged on each check; none of them is walked to its absent
    # imports or forwarded file. Those folders are listed once in the check's reading pass instead.
    store, attempts = Store(tmp_path), post.Attempts(tmp_path)
    for number in range(40):
        message = store.new("question", "operator", "desk", f"printed {number}")
        attempts.close(attempts.open(message, "desk", None, None, None), "watch", "printed by qd inbox --wait")
    settle()
    walked, listed = [], []
    stable_bytes, list_children = fleet_post.stable_bytes, post_dir.list_children

    def walk(path, *args, **kwargs):
        walked.append(path.parent.name)
        return stable_bytes(path, *args, **kwargs)

    def listing(folder, *args, **kwargs):
        listed.append(folder.name)
        return list_children(folder, *args, **kwargs)

    monkeypatch.setattr(fleet_post, "stable_bytes", walk)
    monkeypatch.setattr(post_dir, "list_children", listing)
    assert post.wait_batch(tmp_path, "desk", interval=0.02, stop_after=0.4) is None
    checks = listed.count("imports")
    assert checks >= 5, "the wait made its checks"
    assert walked == [], "no message was walked to an imports or forwarded file"
    assert listed.count("forwarded") == checks, "each folder is listed once a check"


@pytest.mark.parametrize("result, for_launch, delivered", [
    *[(held, False, False) for held in
      (*post.ACCEPTED, "blind", None, "pending", "abandoned", "uncertain", "not-in-closed-record", "unacted")],
    ("unacted", True, True),
    ("withheld", False, True),
    ("cancelled", False, True),
])
def test_undelivered_judges_only_the_seats_own_messages_by_their_latest_result(tmp_path: Path, result, for_launch,
                                                                             delivered):
    store, attempts = Store(tmp_path), post.Attempts(tmp_path)
    judged = store.new("question", "operator", "desk", "judged")
    fresh = store.new("question", "operator", "desk", "never attempted")
    store.new("ack", "operator", "desk", "an ack is never delivered", re=judged.id)
    other = store.new("question", "operator", "scott", "another seat's")
    results = {judged.id: result, other.id: None}
    expected = [judged, fresh] if delivered else [fresh]
    assert attempts.undelivered(store, "desk", results=results, for_launch=for_launch) == expected
