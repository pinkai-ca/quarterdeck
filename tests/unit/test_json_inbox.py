"""A watch accepts its batch only after complete serialization, output and flush."""

import io
import json
import hashlib

import pytest

from quarterdeck import cli, output, pending, post


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


@pytest.mark.parametrize("name", ["captian", "check-fake-01ag"])
def test_unknown_inbox_name_lists_declared_seats(inbox, capsys, name):
    folder, _, _ = inbox
    assert cli.main(["--ship", str(folder), "inbox", name]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"qd: no seat {name!r} in {folder / 'ship.toml'}; declared: captain, desk, worker-1\n"


@pytest.mark.parametrize("name", ["check-fake-01ab", "check-wake-fake-01ab"])
def test_unknown_check_inbox_name_keeps_the_temporary_diagnostic(inbox, capsys, name):
    folder, _, _ = inbox
    assert cli.main(["--ship", str(folder), "inbox", name]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"qd: {name}: no live check-owned temporary inbox; missing, stale or unknown ownership\n"


@pytest.mark.parametrize("name", ["check-fake-01ab", "synthetic-check"])
def test_live_temporary_inbox_still_reads_its_messages(inbox, capsys, name):
    folder, store, _ = inbox
    runtime = folder / "runtime"
    message = store.new("order", "captain", name, "synthetic temporary inbox message")
    with pending.transient_lock(runtime, name) as held:
        assert held
        with pending.check_inbox_registration(runtime, name, False, enabled=True):
            assert cli.main(["--ship", str(folder), "--json", "inbox", name]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert captured.err == ""
    assert data["seat"] == name and [item["id"] for item in data["open"]] == [message.id]


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("failure", [None, "serialize", "write", "short write", "flush"])
def test_wait_batch_receipts_follow_successful_output_only(inbox, monkeypatch, json_output, failure):  # law: one-door
    folder, store, messages = inbox
    monkeypatch.setenv("QD_SEAT", "desk")
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

    def printed(runtime, opened, *, proof):
        assert events[-1] == "flush"
        # The complete document is available before any receipt is accepted.
        if json_output:
            assert proof is None
            data = json.loads(target.getvalue())
            assert [m["id"] for m in data["messages"]] == [m.id for m in messages]
            assert [m["body"] for m in data["messages"]] == [m.body for m in messages]
            assert set(data) == {"seat", "messages"} and data["seat"] == "desk"
        else:
            from quarterdeck.seat_command import qd_command
            text = target.getvalue()
            assert text.startswith(post.typed(messages))
            assert text.endswith('To re-arm your inbox watch, run this command in the background.\n'
                                 + qd_command(folder, 'inbox', '--wait') + '\n')
            assert 'Finish your current task, then read and act on the delivered messages.' in text
            raw = text.encode()
            assert proof['size'] == len(raw) and proof['sha256'] == hashlib.sha256(raw).hexdigest()
            assert proof['generation'] == post.watch_reading(runtime, 'desk').generation
        events.append("receipt")
        original_printed(runtime, opened, proof=proof)

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


def test_wait_batch_rearm_names_a_seat_selected_by_the_operator(inbox, capsys):
    from quarterdeck.seat_command import qd_command

    folder, _, _ = inbox
    assert cli.main(["--ship", str(folder), "inbox", "desk", "--wait"]) == 0
    assert capsys.readouterr().out.endswith(qd_command(folder, 'inbox', '--wait', 'desk') + '\n')


def test_operator_wait_batch_ends_with_the_reply(inbox, capsys, monkeypatch):
    from quarterdeck import ship

    folder, store, _ = inbox
    declaration = folder / "ship.toml"
    declaration.write_text(ship.edit(declaration.read_text(), "ship", "operator", "TestOperator"))
    order = store.new("order", "testoperator", "captain", "synthetic work")
    runtime = folder / "runtime"
    earlier = store.new("report", "captain", "testoperator", "already printed")
    attempts = post.Attempts(runtime)
    attempts.close(attempts.open(earlier, "testoperator", None, None, None), "watch")
    reply = store.new("report", "captain", "testoperator", "finished\nwith details", re=order.id)

    def must_not_wait(*args, **kwargs):
        raise AssertionError("operator already had an unprinted reply")

    monkeypatch.setattr(post, "wait_batch", must_not_wait)

    assert cli.main(["--ship", str(folder), "inbox", "--wait"]) == 0
    assert capsys.readouterr().out == post.typed([reply])
    assert not post.watch_file(runtime, "testoperator").exists()
    assert attempts.latest("testoperator") == {earlier.id: "watch", reply.id: "watch"}


def test_human_session_wait_batch_keeps_the_full_rearm_instruction(inbox, capsys, monkeypatch):
    from quarterdeck.seat_command import qd_command

    folder, _, messages = inbox
    monkeypatch.setenv("QD_SEAT", "desk")

    assert cli.main(["--ship", str(folder), "inbox", "--wait"]) == 0
    assert capsys.readouterr().out == (
        post.typed(messages)
        + "qd > Finish your current task, then read and act on the delivered messages. "
        + "To re-arm your inbox watch, run this command in the background.\n"
        + qd_command(folder, "inbox", "--wait") + "\n"
    )


def test_waiting_a_door_seat_is_still_refused(inbox, capsys):
    folder, _, _ = inbox
    assert cli.main(["--ship", str(folder), "--json", "inbox", "captain", "--wait"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "--wait is for seats in a human's session" in captured.err
    assert post.Attempts(folder / "runtime").all() == []
