"""The inbox is a bounded reading; its immutable store retains every message."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from quarterdeck import cli, post, service


AT = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)


def message(store, index, *, kind="report", at=AT, body=None):
    item = post.Message(f"{kind[:3]}-20260102-1200-desk-tail{index}", kind,
                        "desk", "worker", at, body or f"whole message {index}")
    store._write(item)
    return item


def test_all_open_messages_survive_count_and_age_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(post, "now", lambda: AT)
    store = post.Store(tmp_path)
    opened = message(store, "open", kind="order", at=AT - timedelta(days=7), body="open body " * 40000 + "end")
    for i in range(205):
        message(store, f"new{i:03}", at=AT - timedelta(minutes=i))
    message(store, "old", at=AT - timedelta(days=2))
    reading = post.inbox_reading(store, "worker", recent=True)
    assert reading["open"] == [opened]
    assert len(reading["received"]) == 200
    assert [m.at for m in reading["received"]] == sorted((m.at for m in reading["received"]), reverse=True)
    assert reading["older_received"] == 6
    text = post.inbox_text(store, "worker")
    assert opened.body in text
    assert "whole message new199" in text
    assert "whole message new200" not in text
    assert text.endswith("Older received messages: 6. Read the whole history with `qd inbox --all`.\n")
    assert len(store.for_seat("worker")) == 207


def test_received_tail_is_bounded_in_utf8_bytes_without_partial_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(post, "now", lambda: AT)
    store = post.Store(tmp_path)
    for i in range(5):
        message(store, str(i), at=AT - timedelta(minutes=i), body=(f"entry {i}: " + "é" * 50000))
    reading = post.inbox_reading(store, "worker", recent=True)
    assert len(reading["received"]) == 2
    text = post.render_inbox_reading(reading)
    assert len(text.split("## Received", 1)[1].encode("utf-8")) < post.INBOX_TAIL_BYTES
    assert all(m.body in text for m in reading["received"])
    assert "entry 2:" not in text
    assert reading["older_received"] == 3


def test_tail_stops_at_age_boundary_and_an_oversized_newest_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(post, "now", lambda: AT)
    store = post.Store(tmp_path)
    included = message(store, "boundary", at=AT - timedelta(hours=24))
    message(store, "earlier", at=included.at - timedelta(microseconds=1))
    assert post.inbox_reading(store, "worker", recent=True)["received"] == [included]
    message(store, "oversized", body="z" * post.INBOX_TAIL_BYTES)
    reading = post.inbox_reading(store, "worker", recent=True)
    assert reading["received"] == []
    assert reading["older_received"] == 3


def test_unchanged_inbox_is_not_rewritten_by_renderer_or_service(tmp_path, monkeypatch):
    monkeypatch.setattr(post, "now", lambda: AT)
    store = post.Store(tmp_path)
    message(store, "current")
    path = post.render_inbox(tmp_path, store, "worker")
    original = path.stat()
    assert post.render_inbox(tmp_path, store, "worker") == path
    unchanged = path.stat()
    assert (original.st_ino, original.st_mtime_ns) == (unchanged.st_ino, unchanged.st_mtime_ns)
    monkeypatch.setattr(post, "render_inbox", lambda *args: (_ for _ in ()).throw(AssertionError("unchanged service inbox rewritten")))
    service.Service.inboxes(SimpleNamespace(runtime=tmp_path, store=store,
                                           ship=SimpleNamespace(seats={"worker": None})))


def test_inbox_all_prints_store_history_to_stdout_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(post, "now", lambda: AT)
    store = post.Store(tmp_path)
    old = message(store, "old", at=AT - timedelta(days=3))
    new = message(store, "new")
    ship = SimpleNamespace(runtime=tmp_path)
    monkeypatch.setattr(cli, "load_ship", lambda args: ship)
    monkeypatch.setattr(cli, "_inbox_target", lambda ship, seat: ("worker", False, None))
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    args = SimpleNamespace(seat="worker", wait=False, all=True)
    reading = cli.cmd_inbox(args)
    assert reading.data["received"] == [new, old]
    cli.emit(SimpleNamespace(json=False), reading)
    output = capsys.readouterr().out
    assert old.body in output and new.body in output
    assert "Older received" not in output
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    args.all = False
    assert cli.cmd_inbox(args).data["received"] == [new]
    parser = cli.build_parser()
    assert parser.parse_args(["inbox", "--all"]).all
