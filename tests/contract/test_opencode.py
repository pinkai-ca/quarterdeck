"""OpenCode 1.19.5 against the screens and the record saved from the spike of 7 November 2026, on a local vLLM endpoint."""

from __future__ import annotations

import json
from dataclasses import replace
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import endpoint, input_receipt
from quarterdeck.harness import opencode
from quarterdeck.harness.opencode import OpenCodeHarness
from quarterdeck.seat import Launched, Seat
from quarterdeck.ship import Pool, SeatSpec, Session, Ship, ShipError
from quarterdeck.tmux import Pane

from tests.conftest import FIXTURES

VERSION = "1.19.5"
SCREENS = FIXTURES / "opencode" / VERSION
ROWS = json.loads((SCREENS / "record.json").read_text())
PING = "qd > question from desk · que-9999 · Ping\nSay pong.\n"
UTC = timezone.utc
SCHEMA = (
    "CREATE TABLE session (id text PRIMARY KEY, project_id text NOT NULL, slug text NOT NULL, directory text NOT NULL, title text NOT NULL,"
    " version text NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL, model text, agent text, parent_id text)",
    "CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL)",
    "CREATE TABLE part (id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL, time_created integer NOT NULL,"
    " time_updated integer NOT NULL, data text NOT NULL)",
)


def screen(name: str):
    return OpenCodeHarness().screen((SCREENS / f"{name}.txt").read_text())


def test_captured_blank_startup_is_drawing_not_an_unknown_dialog():
    """8 Nov 2026, isolated 1.19.5 launch at 80x24: blank at 0.38s, ready at 2.16s."""
    s = screen("startup-blank")
    assert s.starting and s.box is None and s.unknown is None and s.interruption is None
    unknown = OpenCodeHarness().screen("Unfamiliar loading dialog\n")
    assert not unknown.starting and unknown.unknown is not None


HELD: list[sqlite3.Connection] = []  # a connection kept open per record, as the running program keeps its own: closing the last one would fold the log into the file


def build_record(home: Path, number: int = 1, until: int | None = None, *,
                 directory: Path | None = None, session: str | None = None) -> Path:
    """The spike's session rebuilt as OpenCode writes it: a database in WAL mode, the rows still in the log."""
    record = home / f"data-{number:03d}" / "opencode" / "opencode.db"
    record.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(record)
    HELD.append(con)
    con.execute("PRAGMA journal_mode=WAL")
    for statement in SCHEMA:
        con.execute(statement)
    s = ROWS["session"]
    session = session or s["id"]
    location = str(directory) if directory is not None else s["directory"]
    con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?)", (session, s["project_id"], s["slug"], location, s["title"],
                                                                      s["version"], s["time_created"], s["time_updated"], s["model"], s["agent"], s["parent_id"]))
    for m in ROWS["messages"]:
        if until is None or m["time_created"] < until:
            con.execute("INSERT INTO message VALUES (?,?,?,?,?)", (m["id"], session, m["time_created"], m["time_updated"], m["data"]))
    for p in ROWS["parts"]:
        if until is None or p["time_created"] < until:
            con.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (p["id"], p["message_id"], session, p["time_created"], p["time_updated"], p["data"]))
    con.commit()
    assert Path(f"{record}-wal").stat().st_size > 0, "the rows live in the write-ahead log, as under a running program"
    return record


def test_every_fixture_set_has_the_screens_the_adapter_reads():
    names = {p.stem for p in SCREENS.glob("*.txt")}
    assert {"fresh", "idle", "paste-6-lines-folded", "paste-2-lines-shown", "paste-long-one-line-folded", "working", "working-typed",
            "working-queued", "interrupted", "model-picker", "compact-fresh", "resumed"} <= names


def test_a_fresh_seat_shows_its_placeholder_and_an_idle_one_nothing_both_as_an_empty_box():
    for name in ("fresh", "idle", "resumed", "interrupted", "idle-after-queued", "idle-long-transcript"):
        s = screen(name)
        assert s.box == "" and not s.working and s.interruption is None and s.unknown is None, name


def test_a_paste_folds_to_its_count_of_lines_and_a_two_line_one_shows_whole():
    """Probed 7 November 2026: three lines or more fold, so does a long line; two short lines show whole."""
    harness = OpenCodeHarness()
    six = "qd > order from desk · ord-5678 · Six\na\nb\nc\nd\nqd > reply: qd post report --re ord-5678"
    assert harness.box(six).matches(screen("paste-6-lines-folded").box, 113)
    assert not harness.box("two\nlines").matches(screen("paste-6-lines-folded").box, 113)
    long = "qd > order from desk · ord-1234 · Stars\n" + "".join(f"paragraph {i}: words\n" for i in range(41)) + "qd > reply: qd post report --re ord-1234\n"
    assert long.count("\n") == 43 and harness.box(long).matches(screen("paste-43-lines-folded").box, 113)
    one = "qd probe C: one long line " + " ".join(f"w{i}" for i in range(220)) + " end"
    assert len(one) == 1019 and harness.box(one).matches(screen("paste-long-one-line-folded").box, 113)
    shown = screen("paste-2-lines-shown")
    assert shown.box.startswith("qd > question from desk · que-9999 · Ping") and shown.box.endswith(" "), "padding kept for ownership checks"
    assert harness.box(PING).matches(shown.box, harness.box_columns(120))
    assert not harness.box("qd > question from desk · que-9999 · Ping\nSay ping.\n").matches(shown.box, 113)


def test_a_working_seat_a_typed_box_and_a_queued_message():
    assert screen("working-start").working and screen("working-start").box == ""
    assert screen("working").working and screen("working").box == ""
    assert screen("working-tool").working
    typed = screen("working-typed")
    assert typed.working and typed.box == "qd > question from desk · que-9999 · Ping\nSay pong."
    assert OpenCodeHarness().box(PING).matches(typed.box, 113)
    queued = screen("working-queued")
    assert queued.working and queued.queued == 1 and queued.box == "", "the queued block above the box is not the box"
    assert not screen("idle-after-queued").working and screen("idle-after-queued").queued == 0


def test_the_425_column_working_box_ignores_the_wrapped_sidebar_folder_on_its_pad():
    s = screen("working-wrapped-folder")
    assert s.working and s.box == "" and s.unknown is None and s.queued == 0


def test_a_wrapped_sidebar_does_not_change_the_draft_or_queued_count():
    raw = (SCREENS / "working-wrapped-folder.txt").read_text()
    lines = raw.splitlines()
    pad = next(i for i, row in enumerate(lines) if row.endswith("/fixture/project/synthetic/folder/"))
    draft = ["/fixture/project/synthetic/folder/", "    read /fixture/input.txt",
             " " * 195 + "/fixture/other.txt", "Commands" + " " * 41 + "esc"]
    lines[pad - 1:pad] = ["  ┃  " + row for row in draft]
    lines[2:2] = ["  ┃", "  ┃  Queued input", "  ┃   QUEUED", "  ┃"]
    s = OpenCodeHarness().screen("\n".join(lines))
    assert s.working and s.queued == 1 and s.unknown is None
    assert s.box == "\n".join(draft)


@pytest.mark.parametrize("change", [
    lambda text: text.replace("• OpenCode 1.19.5", "Unproved footer"),
    lambda text: text.replace("/fixture/project/synthetic/folder/", " /fixture/project/synthetic/folder/"),
    lambda text: text.replace("  ┃" + " " * 382 + "/fixture/", "  ┃  Unexpected prompt".ljust(385) + "/fixture/"),
], ids=["missing-version", "misaligned-folder", "occupied-pad"])
def test_an_unproved_sidebar_leaves_the_box_unknown(change):
    raw = (SCREENS / "working-wrapped-folder.txt").read_text()
    s = OpenCodeHarness().screen(change(raw))
    assert s.working and s.box is None and s.unknown and s.interruption is None


@pytest.mark.parametrize("search_row", ["Search", "model"], ids=["empty-filter", "filled-filter"])
def test_the_native_command_palette_leaves_the_wrapped_box_unknown(search_row):
    raw = (SCREENS / "working-wrapped-folder.txt").read_text()
    # Ctrl+p draws the heading and key hint at fixed offsets. Typing changes the search row.
    indent = " " * 187
    overlay = indent + "Commands" + " " * 41 + "esc\n\n" + indent + search_row + "\n\n"
    s = OpenCodeHarness().screen(overlay + raw)
    assert s.working and s.box is None and s.unknown and s.interruption is None


@pytest.mark.parametrize("title,left,hint_column,hint", [
    ("Themes", 187, 236, "esc"),
    ("Select model", 187, 236, "esc"),
    ("Sessions", 173, 250, "esc"),
    ("Rename Session", 185, 238, "esc"),
    ("Status", 185, 238, "esc"),
    ("Debug", 171, 252, "esc"),
    ("Help", 185, 232, "esc/enter"),
    ("Synthetic selector", 187, 236, "esc"),
])
@pytest.mark.parametrize("width", [160, 200, 425])
def test_a_centered_native_modal_keeps_the_wrapped_composer_unknown(title, left, hint_column, hint, width):
    raw = (SCREENS / "working-wrapped-folder.txt").read_text()
    rows = []
    for row in raw.splitlines():
        if row.startswith("  ╹"):
            row = row[:width - 44]
        elif len(row) > 385:
            row = row[:width - 44].rstrip(" ").ljust(width - 40) + row[385:]
        rows.append(row)
    raw = "\n".join(rows)
    assert OpenCodeHarness().screen(raw).box == ""
    shift = (width + width % 2 - 426) // 2
    heading = (" " * (left + shift) + title).ljust(hint_column + shift) + hint
    s = OpenCodeHarness().screen("\n" + heading + "\n\nSynthetic filter or content\n\n" + raw)
    assert s.box is None and s.unknown and s.interruption is None and s.working


def test_transcript_rows_with_dismiss_key_text_leave_the_composer_readable():
    raw = (SCREENS / "working-wrapped-folder.txt").read_text()
    transcript = ("     Themes     esc\n     Help     esc/enter\n"
                  + "     An ordinary transcript row".ljust(236) + "esc\n")
    s = OpenCodeHarness().screen(transcript + raw)
    assert s.box == "" and s.unknown is None and s.working


def test_the_model_picker_is_dismissed_with_escape_and_the_compact_layout_is_said_to_be_unread():
    picker = screen("model-picker")
    assert picker.box is None and picker.interruption is not None and picker.interruption.name == "model picker"
    assert picker.interruption.key == "Escape" and picker.interruption.dismissable
    compact = screen("compact-fresh")
    assert compact.box is None and compact.interruption is None and "compact" in compact.unknown


def test_the_keys_the_controls_and_the_pool_it_runs_on():
    harness = OpenCodeHarness()
    assert harness.keys.submit == "Enter" and harness.keys.interrupt == "Escape Escape"
    assert not harness.reads_usage and not harness.remote_control
    assert [c.name for c in harness.controls()] == ["model"] and harness.control("model").session is None
    assert harness.accepts(Pool("local", "local", endpoint="http://127.0.0.1:8000/v1")) is None
    assert "native harness codex" in harness.accepts(Pool("codex", "codex", config_dir=Path("/x")))
    assert "names no endpoint" in harness.accepts(Pool("api", "openai", api_key="env:KEY"))


def test_the_record_reads_five_turns_their_tools_and_tokens_and_marks_the_aborted_step_unmeasured(tmp_path: Path):
    record = build_record(tmp_path / "home")
    assert opencode.records(tmp_path / "home") == [record]
    assert opencode.first_timestamp(record) == datetime(2026, 11, 8, 1, 0, 15, 942000, tzinfo=UTC)
    assert opencode.session_id(record) == "ses_0001"
    state, turns = opencode.read(record)
    assert state.turn_open is False and state.turns == 5 and state.model == "qwen3.8-27b" and state.queued == () and state.effort is None
    assert state.last_input.startswith("qd > order from desk · ord-7777 · Long") and state.last_input_at == datetime(2026, 11, 8, 1, 4, 54, 773000, tzinfo=UTC)
    assert state.last_write == datetime(2026, 11, 8, 1, 5, 44, 903000, tzinfo=UTC)
    assert len(turns) == 5 and all(t.cost is None and t.recorded_cost == 0.0 for t in turns), "default endpoint zeros do not establish a stated price"
    first = turns[0]
    assert first.input == "qd > briefed as spike. Say READY and nothing else." and first.reply == "READY" and first.model == "qwen3.8-27b"
    assert first.tokens == opencode.Tokens(read_uncached=14181, written=2) and first.seconds == pytest.approx(2.588)
    stars = turns[1]
    assert [t.name for t in stars.tools] == ["invalid", "bash", "bash", "bash", "bash", "bash", "bash", "write", "bash"]
    assert [t.failed for t in stars.tools] == [True, True, True, False, False, False, False, False, True], "a malformed call and a non-zero exit fail"
    assert all(t.seconds is not None and t.seconds >= 0 for t in stars.tools)
    assert stars.tokens.read_uncached == 183431 and stars.tokens.written == 2303 and stars.tokens.complete, "every step of a turn is summed"
    sleep = turns[2]
    assert sleep.input.startswith("qd > order from desk · ord-5678 · Sleep") and sleep.reply == ""
    assert [(t.name, round(t.seconds, 1), t.failed) for t in sleep.tools] == [("bash", 0.0, True), ("bash", 20.0, False)]
    assert sleep.ended == datetime(2026, 11, 8, 1, 3, 45, 510000, tzinfo=UTC) and sleep.timed, "closed by the next message's answer, at its last step"
    assert turns[3].input == PING and turns[3].reply == "pong"
    aborted = turns[4]
    assert aborted.tokens.unmeasured == 1 and not aborted.tokens.complete and aborted.tokens.written == 102
    assert [(t.name, t.failed) for t in aborted.tools] == [("bash", True), ("bash", True), ("bash", False)], "an aborted command did not fail; the program stopped it"
    assert aborted.tools[-1].seconds == pytest.approx(48.681) and aborted.ended == datetime(2026, 11, 8, 1, 5, 44, 903000, tzinfo=UTC)
    assert aborted.failure is None and state.failure is None, "an interrupt is recorded as an error but fails nothing"


def test_an_open_turn_shows_its_running_tool_and_a_message_typed_meanwhile_is_queued_and_receipted(tmp_path: Path):
    record = build_record(tmp_path / "home", until=1794099811605)  # cut before the queued question: the sleep is running
    con = sqlite3.connect(record)
    con.execute("UPDATE message SET data = json_remove(data, '$.time.completed') WHERE time_created = 1794099805014")
    con.execute("UPDATE part SET data = json_set(json_remove(data, '$.state.time.end'), '$.state.status', 'running')"
                " WHERE json_extract(data, '$.state.input.command') = 'sleep 20; echo slept'")
    con.commit()
    state = opencode.state(record)
    assert state.turn_open is True and state.turns == 2 and state.queued == ()
    assert state.open_tool == "bash" and state.open_tool_since == datetime(2026, 11, 8, 1, 3, 25, 501000, tzinfo=UTC)
    con.execute("INSERT INTO message VALUES ('msg_q','ses_0001',1794099811605,1794099811605,'{\"role\":\"user\",\"time\":{\"created\":1794099811605}}')")
    con.execute("INSERT INTO part VALUES ('prt_q','msg_q','ses_0001',1794099811605,1794099811605,?)", (json.dumps({"type": "text", "text": PING}),))
    con.commit()
    con.close()
    state = opencode.state(record)
    assert state.turn_open is True and state.queued == (PING,), "a message typed during a turn is in the record at once, as queued"
    since = datetime(2026, 11, 8, 1, 3, 30, tzinfo=UTC)
    assert input_receipt.confirmed("opencode", record, PING, since) == "queued"
    assert input_receipt.confirmed("opencode", record, PING, datetime(2026, 11, 8, 1, 3, 32, tzinfo=UTC)) is None, "a receipt before the paste is no receipt"
    assert input_receipt.confirmed("opencode", record, "Say ping.", since) is None


def test_a_database_of_another_shape_or_no_database_is_a_record_error(tmp_path: Path):
    other = tmp_path / "other.db"
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE steps (idx integer)")
    con.commit()
    con.close()
    with pytest.raises(OSError, match="not an OpenCode database"):
        opencode.state(other)
    with pytest.raises(OSError, match="does not exist"):
        opencode.state(tmp_path / "missing.db")


@pytest.fixture
def served(monkeypatch):
    monkeypatch.setattr(endpoint, "models", lambda url, timeout=0, **kwargs: [endpoint.Served("qwen3.8-27b", 98304), endpoint.Served("small", None)])


def test_the_home_is_built_from_the_endpoint_and_each_launch_gets_a_data_directory_of_its_own(tmp_path: Path, served, monkeypatch):
    harness = OpenCodeHarness()
    pool = Pool("local-a", "local", endpoint="http://127.0.0.1:8000/v1")
    seat = SeatSpec("w", "crew", "worker", "opencode", ("local-a",), model="qwen3.8-27b", cwd=tmp_path / "work", settings={"theme": "opencode"})
    runtime = tmp_path / "ship" / "runtime"
    home = runtime / "seats" / "w" / "home"
    assert harness.build_home(home, seat, pool, None) is None, "an endpoint without a key shares no credential"
    config = json.loads((home / "config" / "opencode" / "opencode.json").read_text())
    assert config["model"] == "local-a/qwen3.8-27b" and config["autoupdate"] is False and config["share"] == "disabled"
    provider = config["provider"]["local-a"]
    assert provider["npm"] == "@ai-sdk/openai-compatible" and provider["options"] == {"baseURL": "http://127.0.0.1:8000/v1", "apiKey": "local"}
    assert provider["models"] == {"qwen3.8-27b": {"name": "qwen3.8-27b", "tool_call": True, "limit": {"context": 98304, "output": 8192}}}
    assert config["instructions"] == [str(runtime / "brief" / "w.md")] and config["theme"] == "opencode", "the brief, and the seat's own keys"
    brief = runtime / "brief" / "w.md"
    launch = harness.launch(seat, pool, home, brief, None, "qd > briefed")
    assert launch.argv == ("opencode", "--auto", "--model", "local-a/qwen3.8-27b", "--prompt", "qd > briefed")  # law: can-post
    assert launch.env["XDG_DATA_HOME"] == str(home / "data-001") and launch.env["XDG_CONFIG_HOME"] == str(home / "config")
    assert launch.env["OPENCODE_DISABLE_AUTOUPDATE"] == "1" and "QD_ENDPOINT_KEY" not in launch.env
    record = build_record(home)
    resumed = harness.launch(seat, pool, home, brief, "ses_0001", None)
    assert resumed.argv[-2:] == ("--session", "ses_0001") and resumed.env["XDG_DATA_HOME"] == str(record.parents[1])
    assert harness.launch(seat, pool, home, brief, None, None).env["XDG_DATA_HOME"] == str(home / "data-002"), "a fresh launch takes the next directory"
    with pytest.raises(ShipError, match="no record in .* holds the session ses_9999"):
        harness.launch(seat, pool, home, brief, "ses_9999", None)
    monkeypatch.setenv("LOCAL_KEY", "secret-value")
    keyed = Pool("remote", "openrouter", endpoint="https://example.invalid/v1", api_key="env:LOCAL_KEY")
    assert harness.build_home(home, seat, keyed, None).variable == "QD_ENDPOINT_KEY"
    config = json.loads((home / "config" / "opencode" / "opencode.json").read_text())
    assert config["provider"]["remote"]["options"]["apiKey"] == "{env:QD_ENDPOINT_KEY}", "the variable's name; the key never lands in a file"
    assert "secret-value" not in (home / "config" / "opencode" / "opencode.json").read_text()
    assert harness.launch(seat, keyed, home, brief, None, None).env["QD_ENDPOINT_KEY"] == "secret-value"
    with pytest.raises(ShipError, match="does not serve 'huge'; it serves qwen3.8-27b, small"):
        harness.build_home(home, SeatSpec("w", "crew", "worker", "opencode", ("local-a",), model="huge"), pool, None)
    unnamed = harness.build_home(home, SeatSpec("w", "crew", "worker", "opencode", ("local-a",)), pool, None)
    assert unnamed is None and endpoint.Choice.read(home).model == "qwen3.8-27b", "no model named: the first the endpoint lists"


@pytest.mark.parametrize("role,human,sandbox,scoped", [
    ("hand", False, None, True),
    ("hand", False, True, True),
    ("hand", False, False, False),
    ("hand", True, None, False),
    ("worker", False, None, False),
])
def test_jailed_opencode_hands_start_the_native_finder_in_their_declared_folder(
    tmp_path, served, role, human, sandbox, scoped,
):
    harness = OpenCodeHarness()
    pool = Pool("local", "local", endpoint="http://127.0.0.1:9/v1")
    workspace = tmp_path / "work/hands/w"  # the hand's own declared folder
    workspace.mkdir(parents=True)
    home = tmp_path / "ship/runtime/seats/w/home"
    retired = home.parent / "work"  # the seat-runtime folder a hand worked in before
    seat = SeatSpec("w", "crew", role, "opencode", (pool.name,), cwd=workspace,
                    human_session=human, sandbox=sandbox)
    harness.build_home(home, seat, pool, None)
    brief = home.parents[2] / "brief/w.md"
    launch = harness.launch(seat, pool, home, brief, None, "synthetic brief")
    expected = ("opencode", str(workspace), "--auto") if scoped else ("opencode", "--auto")
    assert launch.argv[:len(expected)] == expected
    assert launch.argv[-2:] == ("--prompt", "synthetic brief")
    assert launch.env.get("QD_WORKSPACE") == (str(workspace) if scoped else None)
    assert "OPENCODE_DISABLE_FFF" not in launch.env
    assert not retired.exists(), "no folder is made in the seat's runtime; the declared one is the hand's"
    assert not list(workspace.iterdir()), "the launch writes nothing in the hand's folder"

    if scoped:
        legacy = build_record(home, directory=retired)  # a session saved in the old seat-runtime folder
        wal = legacy.with_name(legacy.name + "-wal")
        before = legacy.read_bytes(), wal.read_bytes()
        with pytest.raises(ShipError, match="qd fresh w starts a scoped session"):
            harness.launch(seat, pool, home, brief, "ses_0001", None)
        assert (legacy.read_bytes(), wal.read_bytes()) == before
        fresh = harness.launch(seat, pool, home, brief, None, "synthetic brief")
        assert fresh.env["XDG_DATA_HOME"] == str(home / "data-002")
        assert fresh.argv[-2:] == ("--prompt", "synthetic brief")
        record = build_record(home, number=2, directory=workspace, session="ses_scoped")
        resumed = harness.launch(seat, pool, home, brief, "ses_scoped", None)
        assert resumed.argv[:len(expected)] == expected
        assert resumed.argv[-2:] == ("--session", "ses_scoped")
        assert resumed.env["XDG_DATA_HOME"] == str(record.parents[1])
        build_record(home, number=3, directory=retired, session="ses_scoped")
        with pytest.raises(ShipError, match="ambiguous directory evidence.*qd fresh w"):
            harness.launch(seat, pool, home, brief, "ses_scoped", None)
        elsewhere = tmp_path / "work/hands/other"  # another hand's folder, reached by a link at this hand's place
        elsewhere.mkdir()
        workspace.rmdir()
        workspace.symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(ShipError, match="must be a seat-owned working directory"):
            harness.launch(seat, pool, home, brief, None, None)
        assert not list(elsewhere.iterdir())


@pytest.mark.parametrize("joined", [False, True])
def test_scoped_hand_relaunch_refuses_unproved_directory_before_retirement(tmp_path, monkeypatch, joined):
    pool = Pool("local", "local", endpoint="http://127.0.0.1:9/v1")
    workspace = tmp_path / "declared-workspace"
    workspace.mkdir()
    spec = SeatSpec("w", "crew", "hand", "opencode", (pool.name,), cwd=workspace)
    ship = Ship(tmp_path / "ship", "synthetic", "operator", workspace, {pool.name: pool},
                {"crew": Session("crew", "crew")}, {spec.name: spec})
    pane = Pane("%1", "crew", "w", 101, 160, 48, False, False, None)
    seat = Seat(ship, spec, SimpleNamespace(pane=lambda ident: pane if ident == pane.id else None))
    legacy = build_record(seat.home, directory=seat.home.parent / "work")  # saved in the old seat-runtime folder
    Launched(pane.id, pane.pid, datetime.now(UTC).isoformat(), pool.name, None,
             ["opencode"], VERSION, "opencode").write(seat.trusted / "launch.json")
    monkeypatch.setattr(seat, "record", lambda: legacy if joined else None)
    assert seat.is_up
    retired = []
    monkeypatch.setattr(seat, "_down", lambda: retired.append(True) or "down")
    # The resume of an unproved directory is refused before anything retires ...
    with pytest.raises(ShipError, match="qd fresh w starts a scoped session"):
        seat.harness.preflight_resume(spec, seat.home, seat._resume_id)
    assert not retired
    # ... and a hand is never resumed: its restart is the fresh start that refusal names, never that resume.
    monkeypatch.setattr(seat, "_up", lambda resume, *, observation=None:
                        pytest.fail("a hand's restart asked to resume") if resume else "fresh")
    assert seat.restart().startswith("down\nfresh") and retired == [True]
    assert seat.fresh() == "down\nfresh" and retired == [True, True]


@pytest.mark.parametrize("declared", [False, True])
def test_a_jailed_hand_launched_then_resumed_through_the_seat_is_checked_against_the_folder_it_launched_in(
        tmp_path, monkeypatch, declared):
    """One rule at launch and at a resume's preflight: the seat gives its adapter the folder it works in, its declared
    cwd or, with none, the ship's workspace. A hand is never resumed today, so the restart below lets this one resume,
    as any seat whose role may: the preflight passes, through the seat, whether the folder was declared or not."""
    pool = Pool("local", "local", endpoint="http://127.0.0.1:9/v1")
    workspace = tmp_path / "project"
    workspace.mkdir()
    own = tmp_path / "work/hands/w"
    own.mkdir(parents=True)
    spec = SeatSpec("w", "crew", "hand", "opencode", (pool.name,), model="synthetic-model",
                    cwd=own if declared else None)
    ship = Ship(tmp_path / "ship", "synthetic", "operator", workspace, {pool.name: pool},
                {"crew": Session("crew", "crew")}, {spec.name: spec})
    pane = Pane("%1", "crew", "w", 101, 160, 48, False, False, None)
    seat = Seat(ship, spec, SimpleNamespace(pane=lambda ident: pane if ident == pane.id else None))
    assert ship.jailed(spec)
    folder = own if declared else workspace
    # Launched as the seat's launch gives its adapter the seat, with ship.cwd_of: the program's project is the folder
    # it works in, the workspace where the hand declares none.
    seat.home.mkdir(parents=True, exist_ok=True)
    endpoint.Choice(pool.name, "synthetic-model", 32768, 8192).write(seat.home)
    launched = seat.harness.launch(replace(spec, cwd=ship.cwd_of(spec)), pool, seat.home, seat.home / "brief",
                                   None, None)
    assert launched.argv[:2] == ("opencode", str(folder))
    record = build_record(seat.home, number=2, directory=folder)  # the session saved where that launch worked
    Launched(pane.id, pane.pid, datetime.now(timezone.utc).isoformat(), pool.name, None,
             ["opencode"], VERSION, "opencode").write(seat.trusted / "launch.json")
    monkeypatch.setattr(seat, "record", lambda: record)
    monkeypatch.setattr(seat, "_never_resumed", lambda: False)  # stands for a seat whose role may resume
    monkeypatch.setattr(seat, "_down", lambda: "down")
    resumed = []
    monkeypatch.setattr(seat, "_up", lambda resume, *, observation=None: resumed.append(resume) or "up")
    assert seat.restart() == "down\nup" and resumed == [True], "the preflight passed and the seat resumed"
    # A session saved anywhere else is still refused by the same preflight, naming both folders.
    elsewhere = tmp_path / "elsewhere"
    record = build_record(seat.home, number=3, directory=elsewhere, session="ses_elsewhere")
    monkeypatch.setattr(seat, "record", lambda: record)
    with pytest.raises(ShipError, match=f"recorded {str(elsewhere)!r}, expected {str(folder)!r}"):
        seat.restart()
    assert resumed == [True]


class Given(Exception):
    """Raised by the stand-in adapter once it has been given the seat: the path stops there."""


PATHS = ["launch", "restart", "failover", "up on a chosen pool", "off a wall"]


@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("path", PATHS)
def test_the_seat_gives_its_adapter_the_same_folder_at_launch_and_at_every_resume_preflight(
        tmp_path, monkeypatch, path, declared):
    """Seat.adapter_spec, one rule: the launch (build_home and launch) and the four preflights, a restart, a failover,
    an up on a chosen pool and a relaunch off a walled pool, are each given the seat with the folder it works in."""
    from quarterdeck import recovery
    pools = {name: Pool(name, "local", endpoint="http://127.0.0.1:9/v1") for name in ("a", "b")}
    workspace = tmp_path / "project"
    workspace.mkdir()
    own = tmp_path / "work/hands/w"
    own.mkdir(parents=True)
    spec = SeatSpec("w", "crew", "hand", "opencode", ("a", "b"), model="synthetic-model",
                    cwd=own if declared else None, pool_choices={"a": {}, "b": {}})
    ship = Ship(tmp_path / "ship", "synthetic", "operator", workspace, pools,
                {"crew": Session("crew", "crew")}, {spec.name: spec})
    seat = Seat(ship, spec, SimpleNamespace(pane=lambda ident: None))
    given = []

    def adapter(spec, *args, **kwargs):
        given.append(spec.cwd)
        raise Given

    monkeypatch.setattr(seat.harness, "preflight_resume", adapter)
    monkeypatch.setattr(seat.harness, "build_home", lambda home, spec, *args: adapter(spec))
    monkeypatch.setattr(seat, "_never_resumed", lambda: False)  # stands for a seat whose role may resume
    monkeypatch.setattr(seat, "_screened", lambda: False)
    monkeypatch.setattr(seat, "_observe_selection", lambda *args: (VERSION, None))
    monkeypatch.setattr(seat, "_walled_now", lambda: path == "off a wall")
    monkeypatch.setattr(seat, "launch_choice", lambda: ("b", "synthetic room"))
    monkeypatch.setattr(recovery, "require_clear", lambda seat: None)
    run = {"launch": lambda: seat._up(resume=False), "restart": seat.restart,
           "failover": lambda: seat._failover_to("b"), "up on a chosen pool": lambda: seat.up(pool="b"),
           "off a wall": lambda: seat._relaunch_off_wall(resume=True)}[path]
    with pytest.raises(Given):
        run()
    assert given == [own if declared else workspace] == [ship.cwd_of(spec)]
