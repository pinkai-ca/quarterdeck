"""A watch accepts its batch only after complete serialization, output and flush."""

import io
import json
import hashlib
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from quarterdeck import cli, desk_request, fleet_post, output, pending, post, post_index


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


@pytest.mark.parametrize('as_json', [False, True])
def test_operator_inbox_restarts_on_index_failure(inbox, capsys, monkeypatch, as_json):
    folder, store, _ = inbox
    monkeypatch.setattr(post, 'now', lambda: datetime(2026, 1, 2, 12, tzinfo=timezone.utc))
    pending_order = store.new('order', 'captain', 'operator', 'Open work', subject='Open')
    closed_order = store.new('order', 'captain', 'operator', 'Closed work', subject='Closed')
    closed = store.new('report', 'operator', 'captain', 'Actual closer', re=closed_order.id)
    sent = store.new('order', 'operator', 'captain', 'A terminal request')
    direct = store.new('report', 'captain', 'operator', 'A directly printed reply', re=sent.id)
    desk_request.mark_printed(folder / 'runtime', 'operator', direct)
    args = ['--ship', str(folder), *(['--json'] if as_json else []), 'inbox']

    with monkeypatch.context() as legacy:
        legacy.setattr(post, 'read_operator_inbox', lambda store, read: read(store))
        assert cli.main(args) == 0
    baseline = capsys.readouterr()
    assert pending_order.body in baseline.out and closed_order.body in baseline.out
    assert (closed.id if as_json else closed.short) in baseline.out and direct.body not in baseline.out

    def no_scan(self):
        pytest.fail('operator indexed-reading view reopened the whole store')

    monkeypatch.setattr(post._OperatorInboxStore, '_loaded', no_scan)
    assert cli.main(args) == 0
    assert capsys.readouterr() == baseline

    queried = []
    replies = post_index.MessageIndex.replies

    def fail_on_closure(self, identifier):
        queried.append(identifier)
        if identifier == closed_order.id:
            raise post_index.IndexUnavailable('synthetic failure after selecting the recipient')
        return replies(self, identifier)

    monkeypatch.setattr(post_index.MessageIndex, 'replies', fail_on_closure)
    assert cli.main(args) == 0
    assert capsys.readouterr() == baseline and closed_order.id in queried


def test_operator_terminal_notice_can_follow_a_context_only_root(inbox, capsys, monkeypatch):
    folder, store, _ = inbox
    runtime = folder / 'runtime'
    draft = store.prepare('order', 'captain', 'operator', 'Context-only ancestor')
    root = replace(draft, sender='captain@other')
    raw = replace(root, to='operator@drydock').to_json().encode()
    (store.dir / (root.id + '.json')).write_bytes(raw)
    imported = fleet_post.import_path(runtime, root.id)
    imported.parent.mkdir()
    imported.write_text(json.dumps(dict(origin='other', context=True, sha256=hashlib.sha256(raw).hexdigest())))
    copy = store.new('order', 'operator', 'desk', 'Group copy', re=root.id)
    store.new('report', 'operator', 'ship', 'Group closed', re=root.id)
    notice = store.new('report', 'ship', 'operator', 'Group terminal notice', re=root.id)
    groups = runtime / 'post' / 'groups'
    groups.mkdir()
    (groups / (root.id + '.json')).write_text(json.dumps(dict(
        format=1, lead='operator', group='synthetic', root=json.loads(root.to_json()),
        notice=json.loads(notice.to_json()), copies=[json.loads(copy.to_json())])))

    args = ['--ship', str(folder), '--json', 'inbox']
    with monkeypatch.context() as legacy:
        legacy.setattr(post, 'read_operator_inbox', lambda store, read: read(store))
        assert cli.main(args) == 0
    baseline = capsys.readouterr()
    data = json.loads(baseline.out)
    assert data['open'] == [] and [m['id'] for m in data['received']] == [notice.id]
    assert data['settled'] == {notice.id: 'terminal notice: settled by closure of its work'}

    def no_scan(self):
        pytest.fail('notice settlement reopened the whole store')

    monkeypatch.setattr(post._OperatorInboxStore, '_loaded', no_scan)
    assert cli.main(args) == 0
    assert capsys.readouterr() == baseline


def test_operator_wait_uses_one_attempt_inventory_for_initial_batch(inbox, capsys, monkeypatch):
    folder, store, _ = inbox
    runtime = folder / 'runtime'
    old = store.new('report', 'captain', 'operator', 'Already printed')
    desk_request.mark_printed(runtime, 'operator', old)
    attempts = post.Attempts(runtime)
    inventories = []
    all_current = post.Attempts._all_current
    again = store.new('report', 'captain', 'operator', 'Print this second delivery')
    attempts.close(attempts.open(again, 'operator', None, None, None), 'cancelled')
    new = store.new('question', 'captain', 'operator', 'Print despite a receipt for a different seat')
    attempts.close(attempts.open(new, 'desk', None, None, None), 'watch')

    def inventory(self, **fields):
        inventories.append(self)
        return all_current(self, **fields)

    def no_scan(self):
        pytest.fail('indexed delivery selection reopened the whole store')

    monkeypatch.setattr(post.Attempts, '_all_current', inventory)
    monkeypatch.setattr(post._OperatorInboxStore, '_loaded', no_scan)
    assert cli.main(['--ship', str(folder), 'inbox', '--wait']) == 0
    assert len(inventories) == 1
    messages = sorted([again, new], key=lambda message: (message.at, message.id))
    assert capsys.readouterr().out == post.typed(messages, second={again.id})
    assert attempts.latest('operator') == {m.id: 'watch' for m in [old, again, new]}


def test_operator_wait_enters_watch_when_initial_indexed_read_is_empty(inbox, capsys, monkeypatch):
    folder, store, _ = inbox
    wait = post.wait_batch

    def arrive(runtime, seat):
        new = store.new('question', 'captain', 'operator', 'Arrived at watch start')
        arrivals.append(new)
        return wait(runtime, seat)

    arrivals = []
    monkeypatch.setattr(post, 'wait_batch', arrive)
    assert cli.main(['--ship', str(folder), 'inbox', '--wait']) == 0
    assert len(arrivals) == 1 and capsys.readouterr().out == post.typed(arrivals)


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
