"""Grok's seeded acknowledgement opens the door on the measured composer, and nothing else does.

A home the framework seeded because the account's cached opt-out read true starts with no training question,
straight to the fresh composer (worker5, 13 November, 1.0.77, 112 x 35): frame for frame the composer drawn after
the framework's own [Opt out] press. The door reads it idle when, and only when, the launch record names the seed
and the home still holds the framework's seed record with the config's acknowledgement equal to it. The home and
the notice here come from the real build_home and launch with a synthetic linked sign-in; tmux is a stand-in that
draws the measured frame, records every paste and key, and writes Grok's record row when Enter takes the box. No
program, server, credential or provider is touched.
"""

from __future__ import annotations

import json
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import human_wake, post, ship as shipmod
from quarterdeck.door import Door
from quarterdeck.harness import grok, grok_screen
from quarterdeck.harness.types import Join
from quarterdeck.seat import Launched, Seat
from quarterdeck.ship import Pool
from tests.conftest import FIXTURES


def lines(name: str) -> list[str]:
    return json.loads((FIXTURES / "grok/1.0.77" / f"{name}.json").read_text())["lines"]


SEEDED = lines("training-seeded-256")
EXACT = human_wake.LINE
PAYLOAD = "Synthetic order: reply with the word ready."
HINT = "  Enter:send  │  Alt+Enter:newline  │  Shift+Tab:mode  │  Ctrl+x:shortcuts"
WORKSPACE_NOTICE = 'Grok trusted workspace "/sample/project": inventory only, native activation not inspected'
SESSION = "synthetic-session-folder"  # the join is stood in for, so no native folder name is needed


def composer(box: str) -> str:
    """The seeded frame; with text, the measured draft composer (colour form) and its draft hint row."""
    if not box:
        return "\n".join(SEEDED) + "\n"
    plain = grok_screen.characters(SEEDED[30])[0]
    inner = len(plain) - len("  │ ❯ ") - 1
    row = ("  \x1b[49m\x1b[38;2;80;80;88m│\x1b[38;2;225;225;225m ❯ " + box.ljust(inner)
           + "\x1b[38;2;80;80;88m│\x1b[39m")
    return "\n".join(SEEDED[:30] + [row, SEEDED[31], "", HINT, ""]) + "\n"


class GrokTmux:
    def __init__(self, pane):
        self.panes = {pane.id: pane}
        self.box = ""
        self.sent: list[tuple] = []
        self.record: Path | None = None
        self.override: str | None = None

    def frame(self) -> str:
        return self.override if self.override is not None else composer(self.box)

    def pane(self, ident):
        return self.panes.get(ident)

    def read_pane(self, ident, history=0, **options):
        pane = self.panes.get(ident)
        return (pane, self.frame()) if pane else None

    def capture(self, ident, history=0, **options):
        return self.frame()

    def paste(self, ident, text, bracketed=True):
        self.sent.append(("paste", text))
        self.box += text

    def keys(self, ident, *keys):
        self.sent.append(("keys", keys))
        if keys == ("Enter",):
            taken, self.box = self.box, ""
            row = {"timestamp": time.time(), "method": "session/update", "params": {
                "sessionId": "synthetic-session",
                "update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": taken}}}}
            with self.record.open("a") as handle:
                handle.write(json.dumps(row) + "\n")

    def click(self, *args):
        self.sent.append(("click", args))

    def mouse_modes(self, *args):
        return None


def ended_turn(record: Path) -> None:
    """One earlier synthetic turn, ended a minute ago."""
    at = time.time() - 60
    update = lambda kind, **more: {"sessionId": "synthetic-session", "update": {"sessionUpdate": kind, **more}}
    rows = [{"timestamp": at, "method": "session/update",
             "params": update("user_message_chunk", content={"type": "text", "text": "Synthetic earlier request"})},
            {"timestamp": at + 1, "method": "session/update",
             "params": update("agent_message_chunk", content={"type": "text", "text": "Synthetic answer."})},
            {"timestamp": at + 2, "method": "_x.ai/session/update",
             "params": update("turn_completed", stop_reason="end_turn",
                              usage={"inputTokens": 8, "cachedReadTokens": 0, "outputTokens": 2,
                                     "cacheCreationTokens": 0})}]
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("".join(json.dumps(row) + "\n" for row in rows))


@pytest.fixture
def seeded(drydock, tmp_path, monkeypatch):
    """A Grok seat whose home and launch record come from the real seed, on its seeded frame."""
    ship = shipmod.load(drydock)

    def make(seat_name: str = "worker-1", *, opt_out: object = True, version: str = "1.0.77"):
        spec = replace(ship.seat(seat_name), harness="grok")
        pane = SimpleNamespace(id="%1", pid=123, dead=False, width=112, height=35, in_mode=False)
        tmux = GrokTmux(pane)
        seat = Seat(ship, spec, tmux)
        seat.folder.mkdir(parents=True, exist_ok=True)
        seat.trusted.mkdir(parents=True, exist_ok=True)
        sign_in = tmp_path / f"sign-in-{seat_name}"
        sign_in.mkdir()
        (sign_in / "auth.json").write_text(json.dumps({"synthetic-account": {"coding_data_retention_opt_out": opt_out}}))
        pool = Pool("example-subscription", "xai", config_dir=sign_in)
        adapter = grok.GrokHarness()
        program = tmp_path / f"synthetic-grok-{seat_name}"
        program.write_text("Synthetic selected program; never executed.\n")
        brief = tmp_path / f"brief-{seat_name}.md"
        brief.write_text("Synthetic startup brief.\n")
        monkeypatch.setattr(adapter, "preflight", lambda *args: None)
        monkeypatch.setattr(adapter, "selected_program", lambda: program)
        monkeypatch.setattr(adapter, "workspace_notice", lambda *args: WORKSPACE_NOTICE)
        monkeypatch.setattr(grok.grok_workspace, "launch_args", lambda cwd: ("--trust",))
        built = replace(spec, cwd=ship.workspace, sandbox=False, human_session=seat.session.human)
        adapter.build_home(seat.home, built, pool, None)
        launch = adapter.launch(built, pool, seat.home, brief, None, None)
        Launched(pane.id, pane.pid, (post.now()).isoformat(), "example-subscription", None, list(launch.argv),
                 version, "grok", notice=launch.notice).write(seat.trusted / "launch.json")
        record = seat.home / "sessions" / "synthetic" / SESSION / "updates.jsonl"
        ended_turn(record)
        tmux.record = record
        monkeypatch.setattr(seat, "pane", lambda: pane)
        monkeypatch.setattr(seat, "join", lambda: Join(record))
        return SimpleNamespace(seat=seat, tmux=tmux, record=record, notice=launch.notice, ship=ship)

    return make


def relaunch(seat: Seat, **fields) -> None:
    replace(seat.launched(), **fields).write(seat.trusted / "launch.json")


def pastes(d) -> list[str]:
    return [s[1] for s in d.tmux.sent if s[0] == "paste"]


# -- the measured shape ---------------------------------------------------------------------------


def test_the_seeded_start_is_the_composer_after_the_press_frame_for_frame():
    """worker5's seeded human-session frame, its workspace path made synthetic, is M3's after-press frame."""
    assert SEEDED == lines("training-answered-256")
    screen = grok.GrokHarness().screen(composer(""))
    assert screen.fresh == grok_screen.TRAINING_QUESTION.name and screen.unknown == grok_screen.FRESH
    assert screen.box is None and screen.working is None


def test_the_real_seed_names_its_evidence_last_in_the_launch_notice(seeded):
    d = seeded()
    assert d.notice == f"{WORKSPACE_NOTICE}; {grok.PRIVACY_ACK_NOTICE}"
    assert grok.privacy_seed_record(d.seat.home) is not None


# -- the door opens on the seed's evidence, and only on it ---------------------------------------------


def test_with_the_seed_named_and_marked_the_fresh_composer_reads_idle(seeded):
    d = seeded()
    reading = Door(d.seat).reading()
    assert reading.open and not reading.working
    screen = Door(d.seat).screen()
    assert screen.box == "" and screen.unknown is None and screen.fresh is None and screen.working is False


def test_without_a_seed_the_existing_rule_stands(seeded):
    d = seeded(opt_out=False)
    assert d.notice == WORKSPACE_NOTICE and grok.privacy_seed_record(d.seat.home) is None
    reading = Door(d.seat).reading()
    assert not reading.open and reading.clears == "needs a human" and grok_screen.FRESH in reading.reason


def test_the_notice_alone_does_not_open_the_door(seeded):
    d = seeded()
    (d.seat.home / grok.PRIVACY_SEED_RECORD).unlink()
    reading = Door(d.seat).reading()
    assert not reading.open and grok_screen.FRESH in reading.reason


def test_the_home_seed_alone_does_not_open_the_door(seeded):
    d = seeded()
    relaunch(d.seat, notice=WORKSPACE_NOTICE)
    assert not Door(d.seat).reading().open


@pytest.mark.parametrize("notice", [
    grok.PRIVACY_ACK_NOTICE + "; a later inventory",  # not the last part: the seed is appended last
    "Grok privacy banner acknowledged",  # a shortened claim
    WORKSPACE_NOTICE + "; " + grok.PRIVACY_ACK_NOTICE.replace("true", "false"),
    WORKSPACE_NOTICE + " " + grok.PRIVACY_ACK_NOTICE,  # words inside the inventory, not its own part
])
def test_only_the_seeds_own_notice_as_the_last_part_counts(seeded, notice):
    d = seeded()
    relaunch(d.seat, notice=notice)
    assert not Door(d.seat).reading().open


def test_a_native_acknowledgement_with_no_seed_record_does_not_open_the_door(seeded):
    """The program's own acknowledgement is kept across rebuilds, but it is no evidence of the framework's seed."""
    d = seeded()
    (d.seat.home / grok.PRIVACY_SEED_RECORD).unlink()
    assert grok.program_privacy(d.seat.home / "config.toml") is not None
    assert not Door(d.seat).reading().open


def test_a_config_acknowledgement_that_differs_from_its_seed_does_not_open_the_door(seeded):
    d = seeded()
    config = d.seat.home / "config.toml"
    acked = grok.program_privacy(config)["privacy_banner_acked"]
    config.write_text(config.read_text().replace(acked, "2030-01-01T00:00:00+00:00"))
    assert not Door(d.seat).reading().open


def test_a_withdrawn_acknowledgement_does_not_open_the_door(seeded):
    d = seeded()
    config = d.seat.home / "config.toml"
    settings = tomllib.loads(config.read_text())
    del settings["privacy"]
    config.write_text(grok.config_text(settings))
    assert grok.program_privacy(config) is None
    assert not Door(d.seat).reading().open


@pytest.mark.parametrize("content", ["{}", "not json", json.dumps({"format": 1, "writer": "someone else",
                                                                    "privacy_banner_acked": "2030-01-01T00:00:00+00:00"})])
def test_an_unrecognized_seed_record_is_held_never_read_as_evidence(seeded, content):
    d = seeded()
    (d.seat.home / grok.PRIVACY_SEED_RECORD).write_text(content)
    reading = Door(d.seat).reading()
    assert not reading.open and grok_screen.FRESH in reading.reason


def test_a_linked_seed_record_is_not_evidence(seeded, tmp_path):
    d = seeded()
    path = d.seat.home / grok.PRIVACY_SEED_RECORD
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(path.read_text())
    path.unlink()
    path.symlink_to(elsewhere)
    assert not Door(d.seat).reading().open


@pytest.mark.parametrize("version", ["1.0.70", "1.0.78", None])
def test_a_version_not_measured_seeded_keeps_the_composer_unknown(seeded, version):
    d = seeded()
    relaunch(d.seat, version=version)
    reading = Door(d.seat).reading()
    assert not reading.open and grok_screen.FRESH in reading.reason


def test_something_under_the_footer_is_read_as_before(seeded):
    d = seeded()
    rows = composer("").split("\n")
    rows[32] = "  unmeasured"
    d.tmux.override = "\n".join(rows)
    reading = Door(d.seat).reading()
    assert not reading.open and "unmeasured Grok composer hint" in reading.reason


def test_the_question_itself_is_never_read_idle_by_the_seed(seeded):
    d = seeded()
    d.tmux.override = "\n".join(lines("training-question-256")) + "\n"
    reading = Door(d.seat).reading()
    assert not reading.open and grok_screen.TRAINING_QUESTION.name in reading.reason


def test_the_seed_settles_no_other_question(seeded):
    d = seeded()
    assert not d.seat.harness.seeded_acknowledgement("some other question", d.seat.launched(), d.seat.home)
    assert not d.seat.harness.seeded_acknowledgement(grok_screen.TRAINING_QUESTION.name, d.seat.launched(), None)


# -- a delivery through the door ------------------------------------------------------------------


def test_a_crew_seat_takes_a_delivery_through_the_door_confirmed_by_the_record(seeded):
    d = seeded()
    delivery = Door(d.seat).deliver(PAYLOAD)
    assert delivery.result == "delivered", delivery
    assert pastes(d) == [PAYLOAD] and [s for s in d.tmux.sent if s[0] == "keys"] == [("keys", ("Enter",))]
    assert not [s for s in d.tmux.sent if s[0] == "click"], "nothing is pressed: the seed answered nothing on screen"


def test_without_the_seed_a_crew_seat_takes_nothing(seeded):
    d = seeded(opt_out=False)
    delivery = Door(d.seat).deliver(PAYLOAD)
    assert delivery.result == "withheld" and grok_screen.FRESH in delivery.detail
    assert d.tmux.sent == []


def test_a_human_session_launch_reaches_idle_and_takes_the_waking_line_through_the_door(seeded):
    from quarterdeck.tmux import ControlFrame
    d = seeded("desk")
    assert d.seat.session.human and d.notice.endswith(grok.PRIVACY_ACK_NOTICE)
    # A person's fresh composer reads idle only with the cursor at its first cell, as measured (column 6).
    d.tmux.read_control_frame = lambda ident: ControlFrame(d.tmux.panes[ident], d.tmux.frame(), True, (6, 30))
    post.Store(d.ship.runtime).new("order", "captain", "desk", "Synthetic waiting order", subject="synthetic")
    human_wake.run(d.seat)
    assert pastes(d) == [EXACT] and [s for s in d.tmux.sent if s[0] == "keys"] == [("keys", ("Enter",))]
    proof = human_wake.load(d.seat)
    assert proof.data["phase"] == "delivered" and proof.data["record"] == str(d.record)
    assert not [s for s in d.tmux.sent if s[0] == "click"]


def test_without_the_seed_a_human_session_launch_is_left_untouched(seeded):
    d = seeded("desk", opt_out=False)
    post.Store(d.ship.runtime).new("order", "captain", "desk", "Synthetic waiting order", subject="synthetic")
    reason = human_wake.run(d.seat)
    assert reason == "human wake box unknown or obstructed; untouched"
    assert d.tmux.sent == []
