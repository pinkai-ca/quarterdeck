"""pi 0.74.2 against the screens and the record saved from the spike of 7 November 2026, on a local vLLM endpoint."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quarterdeck import endpoint, input_receipt
from quarterdeck.harness import pi
from quarterdeck.harness.pi import PiHarness
from quarterdeck.ship import Pool, SeatSpec, ShipError

from tests.conftest import FIXTURES

VERSION = "0.74.2"
SCREENS = FIXTURES / "pi" / VERSION
RECORD = SCREENS / "session.jsonl"
SESSION = json.loads(RECORD.read_text().splitlines()[0])["id"]  # the record's own session id, never written here
PING = "qd > question from desk · que-9999 · Ping\nSay pong."
UTC = timezone.utc


def screen(name: str):
    return PiHarness().screen((SCREENS / f"{name}.txt").read_text())


def test_every_fixture_set_has_the_screens_the_adapter_reads():
    names = {p.stem for p in SCREENS.glob("*.txt")}
    assert {"fresh", "idle", "paste-6-lines-shown", "paste-4-lines-shown", "paste-44-lines-folded", "paste-long-folded-chars",
            "paste-one-line-folded", "paste-one-line-wrapped", "working", "working-typed", "working-queued", "interrupted",
            "model-picker", "resumed"} <= names


def test_an_idle_seat_shows_an_empty_box_and_its_model_and_thinking_level():
    for name in ("fresh", "idle", "resumed", "interrupted", "idle-after-queued", "idle-long-transcript"):
        s = screen(name)
        assert s.box == "" and not s.working and s.interruption is None and s.unknown is None, name
        assert s.shows == {"model": "qwen3.8-27b", "effort": "medium"}, name


def test_a_short_paste_shows_whole_a_long_one_folds_to_its_lines_or_its_characters():
    """Probed 7 November 2026: four and six short lines show whole; 2,003 and 1,019 characters fold to their count;
    forty-four lines fold to a count of lines; a 245-character line wraps at the pane's width."""
    harness = PiHarness()
    six = "qd > order from desk · ord-5678 · Six\na\nb\nc\nd\nqd > reply: qd post report --re ord-5678"
    assert harness.box(six).matches(screen("paste-6-lines-shown").box, 120)
    assert not harness.box(six.replace("Six", "Seven")).matches(screen("paste-6-lines-shown").box, 120)
    four = "qd > order from desk · ord-5678 · Four\nline two\nline three\nqd > reply: qd post report --re ord-5678\n"
    assert harness.box(four).matches(screen("paste-4-lines-shown").box, 120)
    long = "qd probe B: first line\n" + " ".join(f"word{i}" for i in range(260)) + "\nlast line\n"
    assert len(long) == 2003 and harness.box(long).matches(screen("paste-long-folded-chars").box, 120)
    assert not harness.box(long[:-1]).matches(screen("paste-long-folded-chars").box, 120), "the count names this paste alone"
    one = "qd probe C: one long line " + " ".join(f"w{i}" for i in range(220)) + " end"
    assert len(one) == 1019 and harness.box(one).matches(screen("paste-one-line-folded").box, 120)
    many = "qd > order from desk · ord-1234 · Stars\n" + "".join(f"paragraph {i}: words\n" for i in range(41)) + "qd > reply: qd post report --re ord-1234\n"
    assert many.count("\n") == 43 and harness.box(many).matches(screen("paste-44-lines-folded").box, 120)
    wrapped = "qd probe D: " + " ".join(f"w{i}" for i in range(60)) + " end"
    columns, slack = harness.box_columns(120), harness.wrap_slack
    assert len(wrapped) == 245 and harness.box(wrapped).matches(screen("paste-one-line-wrapped").box, columns, slack)
    assert not harness.box(wrapped.replace("w29", "w29x")).matches(screen("paste-one-line-wrapped").box, columns, slack)
    words = " ".join(["a"] * 150)
    assert columns == 119 and slack == 1
    assert harness.box(words).matches(screen("paste-words-wrapped").box, columns, slack), "a word wraps a column short of a broken word"
    assert not harness.box(words).matches(screen("paste-words-wrapped").box, columns), "which a plain width cannot explain"
    assert harness.box("x" * 300).matches(screen("paste-long-word-wrapped").box, columns, slack), "a word with no space breaks at 119 columns of 120"
    assert not harness.box("x" * 299).matches(screen("paste-long-word-wrapped").box, columns, slack)


def test_a_working_seat_a_typed_box_and_a_steered_message():
    assert screen("working-start").working and screen("working-start").box == ""
    assert screen("working").working and screen("working").box == ""
    typed = screen("working-typed")
    assert typed.working and typed.box == PING and PiHarness().box(PING).matches(typed.box, 120)
    queued = screen("working-queued")
    assert queued.working and queued.queued == 1 and queued.box == ""
    assert queued.echoes == ("qd > question from desk · que-9999 · Ping",), "the steered message's first line, shown until the tool call ends"
    assert not screen("idle-after-queued").working and screen("idle-after-queued").queued == 0


def test_the_model_picker_is_dismissed_with_escape():
    picker = screen("model-picker")
    assert picker.box is None and picker.interruption.name == "model picker" and picker.interruption.key == "Escape"


def test_the_keys_the_controls_and_the_pool_it_runs_on():
    harness = PiHarness()
    assert harness.keys.submit == "Enter" and harness.keys.interrupt == "Escape" and harness.keys.send_now is None
    assert harness.queues_while_working(SeatSpec("w", "crew", "worker", "pi", ("local",)))
    assert [(c.name, c.launch, c.session, c.confirmed_by) for c in harness.controls()] == [
        ("model", "--model <value>", None, "record"), ("effort", "--thinking <value>", None, "record")]
    assert harness.accepts(Pool("local", "local", endpoint="http://127.0.0.1:8000/v1")) is None
    assert "names no endpoint" in harness.accepts(Pool("claude", "claude", config_dir=Path("/x")))


def test_the_record_reads_four_turns_a_steered_message_tools_and_the_aborted_step():
    assert pi.first_timestamp(RECORD) == datetime(2026, 11, 8, 1, 0, 17, 898000, tzinfo=UTC)
    assert pi.session_id(RECORD) == SESSION and len(SESSION) == 36
    state, turns = pi.read(RECORD)
    assert state.turn_open is False and state.turns == 4 and state.model == "qwen3.8-27b" and state.effort == "medium"
    assert state.consumed == (PING,), "a message steered into a running turn"
    assert state.last_input.startswith("qd > order from desk · ord-7777 · Long") and state.open_tool is None
    assert len(turns) == 4 and all(t.cost == 0.0 for t in turns)
    assert turns[0].input == "qd > briefed as spike. Say READY and nothing else." and turns[0].reply.strip() == "READY"
    assert turns[0].tokens == pi.Tokens(read_uncached=1565, written=19) and turns[0].seconds == pytest.approx(0.877)
    stars = turns[1]
    assert [(t.name, t.failed) for t in stars.tools] == [("bash <parameter=command", True), ("bash", True), ("bash", False)]
    assert stars.tokens.read_uncached == 13931 and stars.tokens.written == 2783 and stars.tokens.complete
    sleep = turns[2]
    assert sleep.input.startswith("qd > order from desk · ord-5678 · Sleep") and sleep.reply.strip().endswith("pong")
    assert [(t.name, round(t.seconds, 2), t.failed) for t in sleep.tools] == [("bash", 20.01, False)]
    aborted = turns[3]
    assert aborted.tokens.unmeasured == 1 and not aborted.tokens.complete and aborted.tokens.written == 58
    assert [(t.name, t.failed) for t in aborted.tools] == [("bash", True)] and aborted.reply == ""
    assert aborted.ended == datetime(2026, 11, 8, 1, 5, 9, 839000, tzinfo=UTC)


def test_a_record_cut_mid_turn_shows_the_open_turn_and_its_running_tool(tmp_path: Path):
    lines = RECORD.read_text().splitlines()
    cut = tmp_path / "sessions" / "2026-11-08T01-00-17-898Z_cut.jsonl"
    cut.parent.mkdir()
    cut.write_text("\n".join(lines[:15]) + "\n")  # through the assistant's tool call: the sleep is running
    state = pi.state(cut)
    assert state.turn_open is True and state.open_tool == "bash" and state.turns == 2
    assert state.open_tool_since == datetime(2026, 11, 8, 1, 3, 51, 413000, tzinfo=UTC)
    cut.write_text("\n".join(lines[:16]) + "\n")  # the tool result is in; the model has not answered yet
    state = pi.state(cut)
    assert state.turn_open is True and state.open_tool is None
    assert pi.records(tmp_path) == [cut]
    assert pi.state(cut).last_input.startswith("qd > order from desk · ord-5678")


def test_the_receipt_is_the_user_message_at_or_after_the_paste():
    since = datetime(2026, 11, 8, 1, 3, 55, tzinfo=UTC)  # the question was pasted during the sleep and injected at 01:04:11
    assert input_receipt.confirmed("pi", RECORD, PING, since) == "delivered"
    assert input_receipt.confirmed("pi", RECORD, PING, datetime(2026, 11, 8, 1, 4, 12, tzinfo=UTC)) is None
    assert input_receipt.confirmed("pi", RECORD, "Say ping.", since) is None


def test_another_programs_file_is_a_record_error(tmp_path: Path):
    other = tmp_path / "other.jsonl"
    other.write_text('{"type":"event_msg","payload":{"type":"task_started"}}\n')
    with pytest.raises(OSError, match="no pi entries"):
        pi.state(other)


@pytest.fixture
def served(monkeypatch):
    monkeypatch.setattr(endpoint, "models", lambda url, timeout=0, **kwargs: [endpoint.Served("qwen3.8-27b", 98304)])


def test_the_home_is_built_from_the_endpoint_and_the_launch_carries_the_brief_and_nothing_of_the_projects(tmp_path: Path, served, monkeypatch):
    harness = PiHarness()
    pool = Pool("local-a", "local", endpoint="http://127.0.0.1:8000/v1")
    seat = SeatSpec("w", "crew", "worker", "pi", ("local-a",), model="qwen3.8-27b", effort="high", cwd=tmp_path / "work", settings={"theme": "dark"})
    home = tmp_path / "ship" / "runtime" / "seats" / "w" / "home"
    assert harness.build_home(home, seat, pool, None) is None
    models = json.loads((home / "agent" / "models.json").read_text())
    provider = models["providers"]["local-a"]
    assert provider["baseUrl"] == "http://127.0.0.1:8000/v1" and provider["api"] == "openai-completions" and provider["apiKey"] == "local"
    assert provider["models"] == [{"id": "qwen3.8-27b", "name": "qwen3.8-27b", "reasoning": True, "input": ["text"], "contextWindow": 98304,
                                   "maxTokens": 8192, "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]
    settings = json.loads((home / "agent" / "settings.json").read_text())
    assert settings == {"defaultProjectTrust": "always", "quietStartup": True, "enableInstallTelemetry": False, "theme": "dark"}
    brief = tmp_path / "ship" / "runtime" / "brief" / "w.md"
    launch = harness.launch(seat, pool, home, brief, None, "qd > briefed")
    assert launch.argv == ("pi", "--provider", "local-a", "--model", "qwen3.8-27b", "--session-dir", str(home / "sessions"),
                           "--append-system-prompt", str(brief), "--no-context-files", "--no-extensions", "--no-skills", "--no-prompt-templates",
                           "--thinking", "high", "qd > briefed")
    assert launch.env == {"PI_CODING_AGENT_DIR": str(home / "agent"), "PI_SKIP_VERSION_CHECK": "1"}
    record = home / "sessions" / "2026-11-08T01-00-17-898Z_a.jsonl"
    record.write_text(RECORD.read_text())
    resumed = harness.launch(seat, pool, home, brief, SESSION, None)
    assert resumed.argv[-2:] == ("--session", str(record))
    with pytest.raises(ShipError, match="no record in .* holds the session nope"):
        harness.launch(seat, pool, home, brief, "nope", None)
    plain = SeatSpec("w", "crew", "worker", "pi", ("local-a",))
    harness.build_home(home, plain, pool, None)
    assert json.loads((home / "agent" / "models.json").read_text())["providers"]["local-a"]["models"][0]["reasoning"] is False, "no effort: no thinking"
    assert "--thinking" not in harness.launch(plain, pool, home, brief, None, None).argv
    with pytest.raises(ShipError, match="not a pi thinking level; the levels are off, minimal"):
        harness.build_home(home, SeatSpec("w", "crew", "worker", "pi", ("local-a",), effort="max"), pool, None)
    monkeypatch.setenv("LOCAL_KEY", "secret-value")
    keyed = Pool("remote", "openrouter", endpoint="https://example.invalid/v1", api_key="env:LOCAL_KEY")
    assert harness.build_home(home, plain, keyed, None).variable == "QD_ENDPOINT_KEY"
    assert json.loads((home / "agent" / "models.json").read_text())["providers"]["remote"]["apiKey"] == "QD_ENDPOINT_KEY", "a variable's name, as pi reads it"
    assert "secret-value" not in (home / "agent" / "models.json").read_text()
    assert harness.launch(plain, keyed, home, brief, None, None).env["QD_ENDPOINT_KEY"] == "secret-value"


def test_the_version_is_read_from_the_error_stream_where_pi_prints_it(monkeypatch):
    """Probed 7 November 2026: ``pi --version`` prints 0.74.2 on stderr and nothing on stdout; the first live check missed it."""
    import subprocess

    from quarterdeck.harness import base

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="0.74.2\n")

    monkeypatch.setattr(base.subprocess, "run", run)
    assert PiHarness().version() == "0.74.2"
    monkeypatch.setattr(base.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="2.1.317 (Claude Code)\n", stderr="warning 3\n"))
    assert base.program_version(("claude", "--version")) == "2.1.317", "the standard output is read first"


def test_a_model_without_a_thinking_level_shows_only_its_name_and_blank_rows_below_the_footer_are_skipped():
    """Probed 7 November 2026 on the first live check: a model declared without reasoning shows no thinking level, and
    pi draws top-down, so the rows below its footer stay blank when the pane is taller than the conversation."""
    s = screen("fresh-no-thinking")
    assert s.box == "" and not s.working and s.interruption is None and s.unknown is None
    assert s.shows == {"model": "qwen3.8"}, "no effort to show, none claimed"
    padded = (SCREENS / "fresh-no-thinking.txt").read_text().rstrip("\n") + ("\n" + " " * 120) * 5 + "\n"
    s = PiHarness().screen(padded)
    assert s.box == "" and s.shows == {"model": "qwen3.8"}
