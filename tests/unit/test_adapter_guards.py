"""Claude-owned synthetic enforcement of the optional adapter delivery/interrupt guards.

No native program, provider, credential or tmux server is involved. A synthetic
adapter (a Codex subclass, so it keeps a registered reader name) overrides the
two hooks; every Door action path must consult them, a human session must never
receive a key, and the record evidence handed to a hook must be the exact current
fresh join of the current launch, read without writing anything.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import door as doormod, pending
from quarterdeck.door import Door, Reading
from quarterdeck.harness.base import Harness, Keys
from quarterdeck.harness import muse_home, muse_program
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.types import Interruption, Screen, State
from quarterdeck.seat import Launched
from quarterdeck.ship import ShipError

from .test_owned_paste import editor, frame  # noqa: F401  (shared synthetic editor fixture)

FAULT = "synthetic current native fault; needs a human"
UNKNOWN = "synthetic activity unknown; no interrupt sent"
WORKING = State(turn_open=True, conversation="synthetic-conversation")
IDLE = State(turn_open=False, conversation="synthetic-conversation")
NO_KEY = "no interrupt key sent"
UNREADABLE = Screen("synthetic", None, unknown="synthetic unreadable frame")


def unknown_when_interrupting(door, monkeypatch):
    """The Door's own immediate re-read in the interrupt check sees an unrecognized frame; others see the real one."""
    real = door.screen

    def screen():
        return UNREADABLE if sys._getframe(1).f_code.co_name == "_interrupt" else real()

    monkeypatch.setattr(door, "screen", screen)


class Guarded(CodexHarness):
    """Hooks driven by test switches; it records every state it was handed."""

    def __init__(self, fault=lambda state, screen: None):
        super().__init__()
        self.fault = fault
        self.seen = []

    def delivery_obstruction(self, state, screen):
        self.seen.append(("delivery", state))
        return self.fault(state, screen)

    def interrupt_obstruction(self, state, screen):
        self.seen.append(("interrupt", state))
        # The only safe Muse-shaped rule: interrupt on positive current activity alone.
        return None if state is not None and state.turn_open is True else UNKNOWN


def guarded(editor, monkeypatch, state, fault=lambda state, screen: None):
    editor.seat.harness = Guarded(fault)
    monkeypatch.setattr(Door, "adapter_state", lambda self: state)
    return Door(editor.seat)


def interrupt_key(harness):
    return tuple(harness.keys.interrupt.split())


# -- defaults: no hook, no extra I/O ---------------------------------------------------------


def test_default_adapters_resolve_no_hook_and_do_no_extra_reads(editor, monkeypatch):
    door = Door(editor.seat)
    assert door.adapter_hook("delivery_obstruction") is None
    assert door.adapter_hook("interrupt_obstruction") is None

    def forbidden(*a, **kw):
        raise AssertionError("a default adapter performed an adapter-guard read")

    monkeypatch.setattr(Door, "adapter_state", forbidden)
    monkeypatch.setattr(door, "screen", forbidden)
    assert door.adapter_obstruction() is None


def test_every_registered_adapter_keeps_the_no_op_defaults():
    from quarterdeck import harness as harnesses
    for name in (name for name in harnesses.NAMES if name != "muse"):  # Muse: test_muse_m1_gates.py
        cls = type(harnesses.get(name))
        assert cls.delivery_obstruction is Harness.delivery_obstruction, name
        assert cls.interrupt_obstruction is Harness.interrupt_obstruction, name
        assert cls.strict_usage_interval is False, name


def test_a_human_session_is_reported_without_consulting_the_hook(editor, monkeypatch):
    door = guarded(editor, monkeypatch, WORKING, lambda s, sc: FAULT)
    editor.seat.session.human = True
    assert door.adapter_obstruction() == "a human owns this session; its watch delivers"
    assert door.harness.seen == []


# -- delivery fault: every input action path ------------------------------------------------


def test_fault_before_delivery_withholds_with_no_key_or_paste(editor, monkeypatch):
    door = guarded(editor, monkeypatch, WORKING, lambda s, sc: FAULT)
    text = "synthetic order"
    result = door.deliver(text, proof=editor.proof(text))
    assert result.result == "withheld" and FAULT in result.detail
    assert editor.terminal.pastes == [] and editor.terminal.submitted == [] and editor.terminal.clears == []


def test_fault_appearing_after_paste_never_submits_or_clears(editor, monkeypatch):
    door = guarded(editor, monkeypatch, IDLE, lambda s, sc: FAULT if editor.terminal.pastes else None)
    text = "synthetic order"
    result = door.deliver(text, proof=editor.proof(text))
    assert result.result in ("withheld", "blind") and FAULT in result.detail
    assert editor.terminal.submitted == [] and editor.terminal.clears == []


def test_fault_appearing_after_a_failed_paste_check_never_clears(editor, monkeypatch):
    door = guarded(editor, monkeypatch, IDLE, lambda s, sc: FAULT if editor.terminal.pastes else None)
    monkeypatch.setattr(door, "box_shows", lambda *a, **kw: None)  # the paste is never seen
    text = "synthetic order"
    result = door.deliver(text, proof=editor.proof(text))
    assert result.result == "blind" and FAULT in result.detail and "needs a human" in result.detail
    assert editor.terminal.clears == [] and editor.terminal.submitted == []


def test_submit_itself_refuses_under_a_fault(editor, monkeypatch):
    door = guarded(editor, monkeypatch, IDLE, lambda s, sc: FAULT)
    assert door.submit(editor.pane.id) == FAULT
    assert editor.terminal.submitted == [] and editor.terminal.pastes == []


def test_fault_after_submit_sends_no_send_now_key(editor, monkeypatch):
    class SendNow(Guarded):
        keys = Keys(submit="Enter", send_now=("Tab",), interrupt="Escape")

    editor.seat.harness = SendNow(lambda s, sc: FAULT if ("Enter",) in editor.terminal.submitted else None)
    monkeypatch.setattr(Door, "adapter_state", lambda self: WORKING)
    door = Door(editor.seat)
    text = "synthetic order"
    monkeypatch.setattr(door, "box_shows", lambda *a, **kw: Screen(frame(text), text, working=True))
    monkeypatch.setattr(door, "matches", lambda *a, **kw: True)
    result = door.deliver(text, speed="now", proof=editor.proof(text))
    assert editor.terminal.submitted == [("Enter",)], "no key follows the submit once a fault is current"
    assert result.result == "blind" and "input already submitted" in result.detail


def test_clear_known_presses_no_dismissal_under_a_fault(editor, monkeypatch):
    door = guarded(editor, monkeypatch, IDLE, lambda s, sc: FAULT)
    notice = Screen("synthetic notice", "", interruption=Interruption("synthetic notice", "synthetic", key="Enter"))
    monkeypatch.setattr(pending, "held", lambda *a, **kw: None)
    monkeypatch.setattr(door, "screen", lambda: notice)
    assert door.clear_known(notice) is notice
    assert editor.terminal.submitted == []


def test_control_keys_send_nothing_under_a_fault_found_at_the_last_check(editor, monkeypatch):
    door = guarded(editor, monkeypatch, IDLE, lambda s, sc: FAULT)
    monkeypatch.setattr(door, "reading", lambda screen=None: Reading(True))
    monkeypatch.setattr(door, "screen", lambda: Screen("synthetic", ""))
    proof = SimpleNamespace(same_launch=lambda seat: True)
    result = door.control_keys(("synthetic-key",), proof, lambda: True)
    assert result.result == "withheld" and result.detail == FAULT
    assert editor.terminal.submitted == []


def test_the_reading_reports_a_fault_as_needing_a_human(editor, monkeypatch):
    door = guarded(editor, monkeypatch, IDLE, lambda s, sc: FAULT)
    reading = door._reading(Screen(frame(""), ""))
    assert not reading.open and reading.reason == FAULT and reading.clears == "needs a human"
    assert door.initial_obstruction(Screen(frame(""), "")) == reading


def test_the_hook_receives_the_unknown_state_and_the_screen_it_must_judge(editor, monkeypatch):
    seen = []
    door = guarded(editor, monkeypatch, None, lambda s, sc: seen.append((s, sc)) or None)
    shown = Screen("synthetic", "")
    door.adapter_obstruction(shown)
    assert seen == [(None, shown)], "unknown record evidence reaches the adapter as None, never a default State"


# -- interrupts: direct (qd stop) and before delivery --------------------------------------


@pytest.mark.parametrize("state", [None, State(turn_open=None)])
def test_direct_interrupt_with_unknown_activity_sends_no_key(editor, monkeypatch, state):
    door = guarded(editor, monkeypatch, state)
    unknown_when_interrupting(door, monkeypatch)
    detail = door.interrupt()
    assert NO_KEY in detail and "unknown" in detail and editor.terminal.submitted == []


@pytest.mark.parametrize("state", [None, IDLE, State(turn_open=None)])
def test_direct_interrupt_without_positive_current_activity_sends_no_key(editor, monkeypatch, state):
    """Whatever the record or the frame says short of positive work, qd stop sends no key."""
    door = guarded(editor, monkeypatch, state)
    detail = door.interrupt()
    assert "interrupted" not in detail and editor.terminal.submitted == []


def test_direct_interrupt_to_a_positively_idle_seat_reports_idle_and_sends_no_key(editor, monkeypatch):
    door = guarded(editor, monkeypatch, IDLE)
    detail = door.interrupt()
    assert "idle" in detail and NO_KEY in detail and editor.terminal.submitted == []


def test_direct_interrupt_with_positive_current_activity_sends_one_key(editor, monkeypatch):
    door = guarded(editor, monkeypatch, WORKING)
    assert "interrupted with" in door.interrupt()
    assert editor.terminal.submitted == [interrupt_key(door.harness)]


def test_direct_interrupt_refuses_under_a_delivery_fault(editor, monkeypatch):
    door = guarded(editor, monkeypatch, WORKING, lambda s, sc: FAULT)
    assert door.interrupt() == FAULT and editor.terminal.submitted == []


def test_direct_interrupt_never_keys_a_human_session(editor, monkeypatch):
    door = guarded(editor, monkeypatch, WORKING)
    editor.seat.session.human = True
    assert door.interrupt() == f"{editor.seat.name}: a human owns this session; no interrupt key sent"
    assert editor.terminal.submitted == [] and door.harness.seen == []


def test_direct_interrupt_refuses_when_the_launch_changes_after_the_check(editor, monkeypatch):
    door = guarded(editor, monkeypatch, WORKING)
    launches = iter([editor.launch, SimpleNamespace(**{**vars(editor.launch), "pid": editor.launch.pid + 1})])
    editor.seat.launched = lambda: next(launches)
    assert door.interrupt() == "launch changed before interrupt; no key sent"
    assert editor.terminal.submitted == []


def test_direct_interrupt_rereads_and_ignores_an_older_working_frame(editor, monkeypatch):
    door = guarded(editor, monkeypatch, None)
    monkeypatch.setattr(door, "screen", lambda: Screen("synthetic", "", working=True))
    assert door.interrupt() == UNKNOWN and editor.terminal.submitted == []


@pytest.mark.parametrize("state", [None, State(turn_open=None)])
def test_interrupt_speed_with_unknown_activity_withholds_without_a_key(editor, monkeypatch, state):
    door = guarded(editor, monkeypatch, state)
    unknown_when_interrupting(door, monkeypatch)
    text = "synthetic order"
    result = door.deliver(text, speed="interrupt", proof=editor.proof(text))
    assert result.result == "withheld" and NO_KEY in result.detail and "unknown" in result.detail
    assert editor.terminal.submitted == [] and editor.terminal.pastes == []


def test_interrupt_speed_with_an_unknown_record_and_a_working_frame_sends_no_key(editor, monkeypatch):
    """A working frame alone is not the record's positive activity: the guard still decides, and refuses."""
    door = guarded(editor, monkeypatch, None)
    monkeypatch.setattr(door, "screen", lambda: Screen(frame(""), "", working=True))
    text = "synthetic order"
    result = door.deliver(text, speed="interrupt", proof=editor.proof(text))
    assert result.result == "withheld"
    assert interrupt_key(door.harness) not in editor.terminal.submitted and editor.terminal.pastes == []


def test_interrupt_speed_with_an_unknown_record_never_sends_a_key(editor, monkeypatch):
    door = guarded(editor, monkeypatch, None)
    text = "synthetic order"
    door.deliver(text, speed="interrupt", proof=editor.proof(text))
    assert interrupt_key(door.harness) not in editor.terminal.submitted


def test_interrupt_speed_with_positive_activity_interrupts_then_delivers(editor, monkeypatch):
    door = guarded(editor, monkeypatch, WORKING)
    monkeypatch.setattr(doormod, "SETTLE_SECONDS", 0)
    text = "synthetic order"
    door.deliver(text, speed="interrupt", proof=editor.proof(text))
    assert editor.terminal.submitted[0] == interrupt_key(door.harness)
    assert editor.terminal.submitted.count(interrupt_key(door.harness)) == 1


def test_interrupt_speed_to_a_positively_idle_guarded_seat_delivers_without_a_key(editor, monkeypatch):
    """No turn to interrupt: the message is typed as any other, and no interrupt key reaches the idle pane."""
    door = guarded(editor, monkeypatch, IDLE)
    text = "synthetic order"
    result = door.deliver(text, speed="interrupt", proof=editor.proof(text))
    assert interrupt_key(door.harness) not in editor.terminal.submitted
    assert result.result == "delivered", "positively idle is not unknown activity"
    assert door.harness.seen.count(("interrupt", IDLE)) == 0, "no interrupt is judged for an idle seat"


def test_interrupt_speed_to_an_idle_seat_whose_launch_changes_is_withheld_without_a_key(editor, monkeypatch):
    door = guarded(editor, monkeypatch, IDLE)
    launches = iter([editor.launch] + [SimpleNamespace(**{**vars(editor.launch), "pid": editor.launch.pid + 1})] * 50)
    real = editor.seat.launched
    calls = []

    def launched():
        if sys._getframe(1).f_code.co_name == "_interrupt":
            calls.append(1)
            return next(launches)
        return real()

    editor.seat.launched = launched
    text = "synthetic order"
    result = door.deliver(text, speed="interrupt", proof=editor.proof(text))
    assert len(calls) >= 2, "the interrupt check reads the launch before and after its activity reading"
    assert result.result == "withheld" and "launch changed" in result.detail
    assert editor.terminal.submitted == [] and editor.terminal.pastes == []


# -- current fresh join: the only record state a hook may receive --------------------------


START = datetime(2040, 1, 1, 12, 0, tzinfo=timezone.utc)


class Recorded(Guarded):
    """Reads a synthetic record written as JSON: its creation, conversation and turn flag."""

    def __init__(self, on_state=None):
        super().__init__()
        self.on_state = on_state

    def first_timestamp(self, record):
        return datetime.fromisoformat(json.loads(record.read_text())["created"])

    def session_id(self, record):
        return json.loads(record.read_text())["session"]

    def state(self, record):
        data = json.loads(record.read_text())
        if self.on_state:
            self.on_state()
        return State(turn_open=data["working"], conversation=data["conversation"])


def snapshot(folder: Path) -> dict:
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in sorted(folder.iterdir())}


@pytest.fixture
def joined(tmp_path, monkeypatch):
    """A seat whose current fresh launch has exactly one proved join, built by hand, never by Seat."""
    folder = tmp_path / "seat"
    folder.mkdir()
    records = tmp_path / "records"
    records.mkdir()
    record = records / "synthetic-session.json"
    record.write_text(json.dumps(dict(created=(START + timedelta(seconds=2)).isoformat(),
                                      session="synthetic-conversation", conversation="synthetic-conversation",
                                      working=True)))
    old = records / "older-session.json"
    old.write_text(json.dumps(dict(created=(START - timedelta(days=1)).isoformat(), session="older",
                                   conversation="older", working=True)))
    launch = Launched(pane="%7", pid=4242, at=START.isoformat(), pool="synthetic-pool", resume=None,
                      argv=["synthetic"], version="0", harness="codex", records_before=[str(old)])
    pane = SimpleNamespace(id="%7", pid=4242, dead=False)
    state = SimpleNamespace(launch=launch, pane=pane)
    (folder / "record").write_text(str(record) + "\n")
    (folder / "record-readers.jsonl").write_text(
        json.dumps(dict(record=str(old), harness="codex")) + "\n" + json.dumps(dict(record=str(record), harness="codex")) + "\n")
    (folder / "record-launches.jsonl").write_text(
        json.dumps(dict(record=str(old), at=(START - timedelta(days=1)).isoformat(), pool="synthetic-pool",
                        harness="codex", price=None)) + "\n"
        + json.dumps(dict(record=str(record), at=launch.at, pool="synthetic-pool", harness="codex", price=None)) + "\n")
    seat = SimpleNamespace(name="synthetic", folder=folder, harness=Recorded(), tmux=SimpleNamespace(),
                           session=SimpleNamespace(human=False), launched=lambda: state.launch,
                           pane=lambda: state.pane)

    def forbidden(*a, **kw):
        raise AssertionError("reading current evidence must never join, register or journal")

    from quarterdeck import provenance, record_owner
    monkeypatch.setattr(provenance, "remember_launch", forbidden)
    for name in ("register", "remember", "bindings", "pinned_reader"):
        if hasattr(record_owner, name):
            monkeypatch.setattr(record_owner, name, forbidden)
    return SimpleNamespace(seat=seat, folder=folder, record=record, old=old, state=state)


def read(joined):
    before = snapshot(joined.folder), joined.record.read_bytes()
    result = Door(joined.seat).adapter_state()
    assert (snapshot(joined.folder), joined.record.read_bytes()) == before, "reading wrote evidence"
    return result


def test_the_exact_current_fresh_join_supplies_native_state(joined):
    state = read(joined)
    assert state is not None and state.turn_open is True and state.conversation == "synthetic-conversation"


def test_unknown_before_the_current_launch_has_joined(joined):
    (joined.folder / "record-launches.jsonl").write_text(
        (joined.folder / "record-launches.jsonl").read_text().splitlines()[0] + "\n")
    assert read(joined) is None, "a historical join supplies no current facts"


def test_no_join_journal_at_all_is_unknown(joined):
    (joined.folder / "record-launches.jsonl").unlink()
    assert read(joined) is None


def test_historical_reader_registry_alone_is_not_a_current_join(joined):
    (joined.folder / "record").write_text(str(joined.old) + "\n")
    assert read(joined) is None


def test_a_pinned_record_retained_from_before_the_launch_is_unknown(joined):
    joined.state.launch = Launched(**{**vars(joined.state.launch), "records_before": [str(joined.record)]})
    assert read(joined) is None


@pytest.mark.parametrize("before", [None, [""], [7]])
def test_untyped_records_before_is_unknown(joined, before):
    joined.state.launch = Launched(**{**vars(joined.state.launch), "records_before": before})
    assert read(joined) is None


def test_resumed_launch_fields_stay_unknown_pending_measured_recency(joined):
    joined.state.launch = Launched(**{**vars(joined.state.launch), "resume": "synthetic-conversation"})
    assert read(joined) is None


def test_another_programs_launch_is_unknown(joined):
    joined.state.launch = Launched(**{**vars(joined.state.launch), "harness": "claude"})
    assert read(joined) is None


def test_two_readers_named_for_the_record_are_unknown(joined):
    with (joined.folder / "record-readers.jsonl").open("a") as stream:
        stream.write(json.dumps(dict(record=str(joined.record), harness="claude")) + "\n")
    assert read(joined) is None


def test_a_conflicting_second_join_for_the_same_launch_is_unknown(joined):
    with (joined.folder / "record-launches.jsonl").open("a") as stream:
        stream.write(json.dumps(dict(record=str(joined.old), at=joined.state.launch.at, pool="synthetic-pool",
                                     harness="codex", price=None)) + "\n")
    assert read(joined) is None


def test_a_join_charged_to_another_pool_is_unknown(joined):
    joined.state.launch = Launched(**{**vars(joined.state.launch), "pool": "other-pool"})
    assert read(joined) is None


@pytest.mark.parametrize("pin", ["", "relative/record.json\n"])
def test_an_empty_or_relative_pin_is_unknown(joined, pin):
    (joined.folder / "record").write_text(pin)
    assert read(joined) is None


@pytest.mark.parametrize("name", ["record", "record-readers.jsonl", "record-launches.jsonl"])
def test_a_linked_operational_file_is_unknown(joined, name):
    target = joined.folder / name
    real = joined.folder.parent / f"elsewhere-{name}"
    real.write_bytes(target.read_bytes())
    target.unlink()
    os.symlink(real, target)
    assert read(joined) is None


@pytest.mark.parametrize("name", ["record-readers.jsonl", "record-launches.jsonl"])
def test_malformed_journal_rows_are_unknown(joined, name):
    with (joined.folder / name).open("a") as stream:
        stream.write("{not json\n")
    assert read(joined) is None


@pytest.mark.parametrize("pane", [None, SimpleNamespace(id="%7", pid=4242, dead=True),
                                  SimpleNamespace(id="%8", pid=4242, dead=False),
                                  SimpleNamespace(id="%7", pid=4243, dead=False)])
def test_a_pane_that_is_not_the_launchs_own_is_unknown(joined, pane):
    joined.state.pane = pane
    assert read(joined) is None


@pytest.mark.parametrize("created", [START - timedelta(seconds=1), START.replace(tzinfo=None)])
def test_native_creation_before_the_launch_or_without_a_zone_is_unknown(joined, created):
    data = json.loads(joined.record.read_text())
    joined.record.write_text(json.dumps({**data, "created": created.isoformat()}))
    assert read(joined) is None


@pytest.mark.parametrize("field,value", [("session", ""), ("conversation", "another-conversation"),
                                         ("conversation", None)])
def test_native_identity_must_agree_with_the_state(joined, field, value):
    data = json.loads(joined.record.read_text())
    joined.record.write_text(json.dumps({**data, field: value}))
    assert read(joined) is None


def test_a_record_that_changes_while_read_is_unknown(joined):
    def grow():
        with joined.record.open("a") as stream:
            stream.write(" ")
    joined.seat.harness = Recorded(grow)
    assert Door(joined.seat).adapter_state() is None


@pytest.mark.parametrize("name", ["record", "record-readers.jsonl", "record-launches.jsonl"])
def test_operational_evidence_that_changes_while_read_is_unknown(joined, name):
    def touch():
        with (joined.folder / name).open("a") as stream:
            stream.write("\n")
    joined.seat.harness = Recorded(touch)
    assert Door(joined.seat).adapter_state() is None


def test_a_relaunch_while_read_is_unknown(joined):
    def relaunch():
        joined.state.launch = Launched(**{**vars(joined.state.launch), "pid": 4243})
        joined.state.pane = SimpleNamespace(id="%7", pid=4243, dead=False)
    joined.seat.harness = Recorded(relaunch)
    assert Door(joined.seat).adapter_state() is None


def test_a_human_session_receives_no_record_state(joined):
    joined.seat.session.human = True
    assert read(joined) is None


def test_no_launch_is_unknown(joined):
    joined.state.launch = None
    assert read(joined) is None


# -- Pike rule 6: the seat runs the versioned binary, found by reading the marker as a file -----


RELEASE = "1.4.15-R8339.1"


@pytest.fixture
def install(tmp_path):
    """A synthetic install folder: an updating launcher that must never run, its marker and its binary."""
    folder = tmp_path / "install"
    folder.mkdir()
    canary = tmp_path / "launcher-ran"
    launcher = folder / "muse"
    launcher.write_text(f"#!/bin/sh\ntouch {canary}\n")
    marker = folder / ".muse-version"
    marker.write_text(RELEASE + "\n")
    binary = folder / f"muse-bin-{RELEASE}"
    binary.write_text(f"#!/bin/sh\ntouch {canary}\n")
    for program in (launcher, binary):
        program.chmod(program.stat().st_mode | stat.S_IXUSR)
    return SimpleNamespace(folder=folder, launcher=launcher, marker=marker, binary=binary, canary=canary)


@pytest.fixture
def no_execution(monkeypatch):
    import subprocess

    def forbidden(*a, **kw):
        raise AssertionError("locating the Muse binary executed a program")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    for name in ("system", "execv", "execve", "execvp", "execvpe", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, forbidden)


def test_the_versioned_binary_is_selected_and_nothing_runs(install, no_execution):
    found = muse_program.binary(path=str(install.folder))
    assert found == install.binary.resolve()
    assert found != install.launcher.resolve()
    assert not install.canary.exists()


def test_a_launcher_reached_through_a_link_still_selects_the_binary_beside_its_target(install, tmp_path, no_execution):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "muse").symlink_to(install.launcher)
    assert muse_program.binary(path=str(bin_dir)) == install.binary.resolve()
    assert not install.canary.exists()


@pytest.mark.parametrize("damage", ["no marker", "empty marker", "malformed marker", "path in marker", "oversized marker",
                                    "no binary", "binary not executable", "binary links elsewhere", "no launcher"])
def test_a_damaged_install_is_refused_and_nothing_runs(install, tmp_path, no_execution, damage):
    if damage == "no marker":
        install.marker.unlink()
    elif damage == "empty marker":
        install.marker.write_text("")
    elif damage == "malformed marker":
        install.marker.write_text("1.4.15\n")
    elif damage == "path in marker":
        install.marker.write_text("../../" + RELEASE + "\n")
    elif damage == "oversized marker":
        install.marker.write_text(RELEASE + " " * 200 + "\n")
    elif damage == "no binary":
        install.binary.unlink()
    elif damage == "binary not executable":
        install.binary.chmod(0o644)
    elif damage == "binary links elsewhere":
        other = tmp_path / "other-program"
        other.write_text("#!/bin/sh\n")
        other.chmod(0o755)
        install.binary.unlink()
        install.binary.symlink_to(other)
    elif damage == "no launcher":
        install.launcher.unlink()
    with pytest.raises(ShipError):
        muse_program.binary(path=str(install.folder))
    assert not install.canary.exists()


# -- Pike rule 7: no unproved credential place or launcher switch reaches the seat ---------------


def test_the_seat_environment_carries_no_unproved_credential_or_login_switch(tmp_path):
    env = muse_home.environment(tmp_path / "seat-home")
    for name in ("MUSE_AUTH_PATH", "MUSE_LOGIN", "META_API_KEY", "MUSE_MODEL", "MUSE_FLAGS", "MUSE_SESSIONS",
                 "MUSE_PROJECTS_HOME"):
        assert name not in env, name
    assert not any("auth" in value.casefold() for value in env.values())


def test_the_seat_environment_is_private_to_the_seat_home(tmp_path):
    home = tmp_path / "seat-home"
    env = muse_home.environment(home)
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        assert Path(env[name]).is_relative_to(home), name
    assert Path(env["HOME"]) != Path(os.path.expanduser("~"))
    assert not home.exists(), "the environment is a proposal; it creates nothing"
