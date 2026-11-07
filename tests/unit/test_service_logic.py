"""The service's pure judgments: whether a record shows that this very message was taken."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from quarterdeck import post
from quarterdeck.fakerecord import ClaudeShape
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.post import Message, Store
from quarterdeck.service import Service


def seat_with(record: Path):
    return SimpleNamespace(record=lambda: record, harness=FakeHarness())


def test_taken_matches_the_message_id_not_a_look_alike_body(tmp_path: Path):
    store = Store(tmp_path)
    earlier = store.new("order", "desk", "scott", "done")
    record = ClaudeShape(tmp_path / "home", Path("/work"), "s1", "fake-1")
    record.begin(post.typed([earlier]), "fake-1", "low")
    record.end("ok")
    since = datetime.now(timezone.utc) - timedelta(seconds=1)
    later = store.new("order", "desk", "scott", "done")  # the same body, another id
    assert Service.taken(seat_with(record.path), earlier, since) is True
    assert Service.taken(seat_with(record.path), later, since) is False  # law: one-door
    # an input older than the attempt proves nothing about it
    assert Service.taken(seat_with(record.path), earlier, datetime.now(timezone.utc) + timedelta(seconds=5)) is False
    # queued, not yet an input: still taken
    record.begin("something else", "fake-1", "low")
    record.queued(post.typed([later]))
    assert Service.taken(seat_with(record.path), later, since) is True


def test_taken_without_a_record_is_false(tmp_path: Path):
    message = Message("ord-1-d-0001", "order", "desk", "scott", post.now(), "x")
    assert Service.taken(SimpleNamespace(record=lambda: None, harness=FakeHarness()), message, post.now()) is False
