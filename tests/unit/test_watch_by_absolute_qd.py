"""A person's inbox watch run by the ship's own command path is the watch: its output is attributed, and it reads idle.

The brief tells a seat in a person's session to run '<ship>/runtime/bin/qd inbox --wait'. The reader knows that path
only from the record's own place in the framework's seat-home layout, runtime/seats/<seat>/home/projects/..., never
from the record's text. Synthetic records only; no seat's own record is read.
"""
import json

import pytest

from quarterdeck import idle
from quarterdeck.harness.claude_watch import inputs
from quarterdeck.post import Store, typed
from quarterdeck.seat_command import qd_command
from .test_idle_watch_claude_validation import SEAT, case  # noqa: F401
from .test_order_attribution import message
from .test_watch_attribution import OUTPUT, call, origin, result, write


def placed(tmp_path, name="example-ship"):
    """A ship folder and a record where the seat-home layout puts it."""
    folder = tmp_path / name
    record = folder / "runtime/seats/worker/home/projects/synthetic/record.jsonl"
    record.parent.mkdir(parents=True)
    return folder, record


def envelope(tmp_path):
    store = Store(tmp_path / "store-runtime")
    return typed([message(store, "ord-change", to="worker")])


@pytest.mark.parametrize("form", ["bare", "absolute"])
def test_both_forms_are_the_watch_in_the_background_and_in_the_foreground(tmp_path, form):
    folder, record = placed(tmp_path)
    command = "qd inbox --wait" if form == "bare" else qd_command(folder, "inbox", "--wait")
    text = envelope(tmp_path)
    write(record, [call("watch", command), origin(), call("read", f"cat {OUTPUT}", 11), result("read", text, 12)])
    [found] = inputs(record)
    assert found.kind == "watch" and found.text == text.strip()
    write(record, [call("watch", command), result("watch", text, 12)])
    [found] = inputs(record)
    assert found.kind == "watch" and found.text == text.strip()


@pytest.mark.parametrize("which", ["another path ending in /qd", "another ship's own qd", "a word more, bare",
                                   "a word more, absolute", "the record outside the layout"])
def test_no_other_command_or_place_passes_for_the_watch(tmp_path, which):
    folder, record = placed(tmp_path)
    command = {
        "another path ending in /qd": "/usr/local/bin/qd inbox --wait",
        "another ship's own qd": qd_command(tmp_path / "other-ship", "inbox", "--wait"),
        "a word more, bare": "qd inbox --wait --other",
        "a word more, absolute": qd_command(folder, "inbox", "--wait", "--other"),
        "the record outside the layout": qd_command(folder, "inbox", "--wait"),
    }[which]
    if which == "the record outside the layout":
        record = tmp_path / "record.jsonl"
    text = envelope(tmp_path)
    write(record, [call("watch", command), origin(), call("read", f"cat {OUTPUT}", 11), result("read", text, 12)])
    assert inputs(record) == []
    write(record, [call("watch", command), result("watch", text, 12)])
    assert inputs(record) == []


def test_a_registered_watch_by_the_absolute_form_reads_idle_with_its_output_attributed(case):
    case.record = case.runtime / "seats" / SEAT / "home" / "projects" / "synthetic" / "record-hazel.jsonl"
    case.record.parent.mkdir(parents=True)
    command = qd_command(case.ship.folder, "inbox", "--wait")
    text = envelope(case.tmp)
    reading = [dict(type="assistant", message=dict(id="message-read", role="assistant", stop_reason="tool_use",
                                                   content=[dict(type="tool_use", id="call-read", name="Bash",
                                                                 input=dict(command=f"cat {OUTPUT}"))])),
               dict(type="user", message=dict(role="user", content=[dict(type="tool_result", tool_use_id="call-read",
                                                                         content=text, is_error=False)]))]
    case.write([("call-hazel", command, True, "task-hazel")], extra=reading)
    rows = [json.loads(line) for line in case.record.read_text().splitlines()]
    for row in rows:  # the watch's own result names its output file, as Claude Code writes it
        for block in row.get("message", {}).get("content", []) if isinstance(row.get("message", {}).get("content"), list) else []:
            if block.get("tool_use_id") == "call-hazel":
                block["content"] = origin()["message"]["content"][0]["content"]
    case.record.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert [tool.inbox_watch_started for tool in case.harness.state(case.record).active_tools] == [case.at(11)]
    case.watch()
    state, hold = idle.reading(case.seat)
    assert hold is None and state.pending_tools == (), "the registered watch by the absolute form reads idle"
    [found] = inputs(case.record)
    assert found.kind == "watch" and found.text == text.strip(), "and its output is attributed"
