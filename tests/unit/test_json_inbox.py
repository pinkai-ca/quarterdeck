"""A watch accepts its batch only after complete serialization, output and flush."""

import io
import json

import pytest

from quarterdeck import cli, output, post


@pytest.fixture
def inbox(drydock, monkeypatch):
    monkeypatch.delenv("QD_SHIP", raising=False)
    monkeypatch.delenv("QD_SEAT", raising=False)
    monkeypatch.delenv("QD_TMUX_SOCKET", raising=False)  # no server: the test ship's own, never started
    store = post.Store(drydock / "runtime")
    messages = [store.new("order", "captain", "desk", 'first\nquoted "body"', subject="synthetic"),
                store.new("question", "captain", "desk", "second ünicode")]
    return drydock, store, messages


def test_inbox_contains_messages_open_state_and_closing_references(inbox, capsys):
    folder, store, messages = inbox
    closed = store.new("report", "desk", "captain", "done", re=messages[0].id)
    assert cli.main(["--ship", str(folder), "--json", "inbox", "desk"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["seat"] == "desk" and data["open"][0]["id"] == messages[1].id
    assert data["received"][0]["body"] == messages[0].body
    assert data["closed_by"] == {messages[0].id: closed.id}
    assert post.Attempts(folder / "runtime").all() == [], "an inbox reading is not a watch receipt"
    assert cli.main(["--ship", str(folder), "inbox", "desk"]) == 0
    text = capsys.readouterr().out
    assert f"closed by {closed.short}" in text and messages[0].body in text and "## Open (1)" in text
    assert cli.main(["--ship", str(folder), "--json", "inbox"]) == 0
    assert json.loads(capsys.readouterr().out) == {"seat": "operator", "open": [], "received": [], "closed_by": {}, "older_received": 0}


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("failure", [None, "serialize", "write", "short write", "flush"])
def test_wait_batch_receipts_follow_successful_output_only(inbox, monkeypatch, json_output, failure):  # law: one-door
    folder, store, messages = inbox
    attempts = post.Attempts(folder / "runtime")
    events = []

    def still_open():
        opened = attempts.all()
        assert len(opened) == 2 and all(a.result is None for a in opened)

    class Destination(io.StringIO):
        def write(self, text):
            still_open()
            events.append("write")
            if failure == "write":
                raise OSError("synthetic output failure")
            if failure == "short write":
                return super().write(text[:1])
            return super().write(text)

        def flush(self):
            still_open()
            events.append("flush")
            if failure == "flush":
                raise OSError("synthetic flush failure")

    target = Destination()
    original_printed = post.printed

    def printed(runtime, opened):
        assert events[-1] == "flush"
        # The complete document is available before any receipt is accepted.
        if json_output:
            data = json.loads(target.getvalue())
            assert [m["id"] for m in data["messages"]] == [m.id for m in messages]
            assert [m["body"] for m in data["messages"]] == [m.body for m in messages]
            assert "result" not in data, "emitted data cannot claim its own later receipt"
        else:
            assert target.getvalue() == post.typed(messages)
        events.append("receipt")
        original_printed(runtime, opened)

    monkeypatch.setattr(post, "printed", printed)
    # Failure before writing can arise from JSON serialization or a text renderer.
    if failure == "serialize":
        def broken(*args, **kwargs):
            still_open()
            raise ValueError("synthetic serialization failure")
        if json_output:
            monkeypatch.setattr(output, "value", broken)
        else:
            monkeypatch.setattr(post, "typed", broken)
    args = ["--ship", str(folder), *(["--json"] if json_output else []), "inbox", "desk", "--wait"]
    with monkeypatch.context() as capture:
        capture.setattr(cli.sys, "stdout", target)
        if failure:
            with pytest.raises((ValueError, OSError), match="synthetic|incomplete reading output"):
                cli.main(args)
        else:
            assert cli.main(args) == 0
    if failure:
        assert all(a.result is None for a in attempts.all())
        assert "receipt" not in events
        if failure == "serialize":
            assert target.getvalue() == "" and events == []
    else:
        assert events[-2:] == ["flush", "receipt"]
        assert [a.result for a in attempts.all()] == ["watch", "watch"]
    assert not post.watch_file(folder / "runtime", "desk").exists()


def test_waiting_a_door_seat_is_still_refused(inbox, capsys):
    folder, _, _ = inbox
    assert cli.main(["--ship", str(folder), "--json", "inbox", "captain", "--wait"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "--wait is for seats in a human's session" in captured.err
    assert post.Attempts(folder / "runtime").all() == []
