"""qd press and qd type: Systems' acts on a screen the door does not know (law: one-door).

A synthetic Codex seat and a stand-in tmux that records every key, paste and click in order, so a test can
see that the act was written to the seat's record before anything was sent.
"""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import acts, door as doormod, post, watch
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane

DIALOG = "  Synthetic question from the program?\n\n  1. Yes, go on\n  2. No, stop here\n\n  test low · /work\n"
MOUSE = {"sgr": True, "button": True, "all": False, "standard": False}


@pytest.fixture
def ship(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pool = Pool("test", "codex")
    seats = {"worker": SeatSpec("worker", "bridge", "worker", "codex", ("test",), cwd=tmp_path),
             "spock": SeatSpec("spock", "bridge", "systems", "codex", ("test",), cwd=tmp_path),
             "scott": SeatSpec("scott", "bridge", "engineer", "codex", ("test",), cwd=tmp_path),
             "desk": SeatSpec("desk", "office", "desk", "codex", ("test",), cwd=tmp_path)}
    ship = Ship(tmp_path, "example", "operator", tmp_path, {"test": pool},
                {"bridge": Session("bridge", "Test"), "office": Session("office", "Office", human=True)}, seats)
    live = {"pane": Pane("%1", "Test", "worker", 123, 80, 24, False, False, None), "screen": DIALOG, "sent": [],
            "mouse": None, "record_at_send": []}

    def capture(*a, **k):
        return live["screen"]

    def sent(kind, value):
        path = seat.folder / "acts.jsonl"
        live["record_at_send"].append(path.read_text().count("\n") if path.exists() else 0)
        live["sent"].append((kind, value))
        live.get("after", lambda kind, value: None)(kind, value)

    def paste(pane, text, bracketed=True):
        sent("paste", text)
        live["screen"] = live["screen"].replace("› \n", f"› {text}\n")

    tmux = SimpleNamespace(pane=lambda p: live["pane"], capture=capture, paste=paste,
                           keys=lambda pane, *keys: sent("keys", keys),
                           click=lambda pane, x, y: sent("click", (x, y)),
                           mouse_modes=lambda pane, pid: live["mouse"])
    svc = Service(ship, tmux)
    seat = svc.seat("worker")
    seat.folder.mkdir(parents=True)
    launch = {"pane": "%1", "pid": 123, "at": (post.now() - timedelta(minutes=1)).isoformat(),
              "pool": "test", "argv": ["codex"], "harness": "codex", "resume": None, "version": "synthetic"}
    (seat.folder / "launch.json").write_text(json.dumps(launch))
    monkeypatch.setattr(doormod, "ANSWER_SECONDS", 0.3)
    monkeypatch.setattr(doormod, "BOX_SECONDS", 0.3)
    monkeypatch.setattr(doormod.Door, "confirm", lambda self, text, seconds=0: doormod.Delivery("blind", "no record"))
    yield svc, seat, live
    svc.ledger.close()


def acts_of(seat):
    path = acts.path(seat)
    return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []


def test_a_press_by_a_systems_seat_is_recorded_before_it_is_pressed(ship):
    svc, seat, live = ship
    live["after"] = lambda kind, value: live.update(screen="• Working (1s • esc to interrupt)\n› \n\n  test low · /work\n")
    line = svc.answer(dict(kind="press", seat="worker", choice="Yes, go on", caller="spock"))
    assert live["sent"] == [("keys", ("1",))], "the key its own label names, once"
    assert live["record_at_send"] == [1], "the act was on the record before the key was sent"  # law: one-door
    first, outcome = acts_of(seat)
    assert first["verb"] == "press" and first["choice"] == "Yes, go on" and first["caller"] == "spock"
    assert first["outcome"] == "pressing" and "Synthetic question from the program?" in first["screen"]
    assert first["launch"] == {"pane": "%1", "pid": 123, "at": seat.launched().at}
    assert outcome["act"] == first["act"] and outcome["outcome"] == "pressed"
    assert line.startswith("worker: pressed 'Yes, go on' by key 1 · the choice is gone from the screen")
    shown = "\n".join(watch.render_full(dict(watch.seat_reading(seat))))
    assert f"last act: pressed 'Yes, go on' by spock at {first['at']} · pressed (the choice is gone" in shown


def test_a_choice_named_with_its_label_and_a_click_where_the_program_takes_the_mouse(ship):
    svc, seat, live = ship
    svc.answer(dict(kind="press", seat="worker", choice="2. No, stop here", caller="spock"))
    assert live["sent"] == [("keys", ("2",))]
    live.update(screen="  Pick a colour\n  Red  Green  Blue\n› \n\n  test low · /work\n", sent=[], mouse=MOUSE)
    line = svc.answer(dict(kind="press", seat="worker", choice="Blue", caller="spock"))
    assert live["sent"] == [("click", (17, 2))] and "by click at 17,2" in line and "still shows" in line
    live.update(sent=[], mouse=None)
    line = svc.answer(dict(kind="press", seat="worker", choice="Blue", caller="spock"))
    assert line == "refused: 'Blue' names no key of its own and worker's program takes no mouse; nothing is pressed"
    assert live["sent"] == []


@pytest.mark.parametrize("caller", ["scott", None, "stranger"])
def test_a_press_by_any_other_seat_is_refused(ship, caller):
    svc, seat, live = ship
    line = svc.answer(dict(kind="press", seat="worker", choice="Yes, go on", caller=caller))
    assert line.startswith("refused:") and "only a systems seat acts on a screen" in line
    assert {"scott": "scott is a engineer seat", None: "QD_SEAT names no seat",
            "stranger": "stranger is not a seat of example"}[caller] in line
    line = svc.answer(dict(kind="type", seat="worker", line="go on", caller=caller))
    assert line.startswith("refused:") and live["sent"] == [] and not acts.path(seat).exists()


def test_a_press_or_type_into_a_human_session_is_refused_whoever_asks(ship):
    svc, seat, live = ship
    for kind, field in (("press", "choice"), ("type", "line")):
        line = svc.answer({"kind": kind, "seat": "desk", field: "Yes, go on", "caller": "spock"})
        assert line == "refused: desk sits in a human's session; nothing is pressed or typed there, whoever asks"
    assert live["sent"] == []


def test_an_unknown_choice_name_is_refused(ship):
    svc, seat, live = ship
    line = svc.answer(dict(kind="press", seat="worker", choice="Maybe", caller="spock"))
    assert line == "refused: no visible choice on worker's screen is named 'Maybe'; nothing is pressed"
    live["screen"] = DIALOG.replace("2. No, stop here", "2. Yes, go on")
    line = svc.answer(dict(kind="press", seat="worker", choice="Yes, go on", caller="spock"))
    assert line == "refused: 'Yes, go on' shows 2 times on worker's screen, not once; nothing is pressed"
    assert live["sent"] == [] and not acts.path(seat).exists()


def test_nothing_is_pressed_if_the_act_cannot_be_recorded(ship, monkeypatch):
    svc, seat, live = ship

    def unwritable(seat, entry):
        raise OSError("synthetic: read-only")
    monkeypatch.setattr(acts, "write", unwritable)
    line = svc.answer(dict(kind="press", seat="worker", choice="Yes, go on", caller="spock"))
    assert line == "refused: the act could not be recorded (synthetic: read-only); nothing is pressed"
    line = svc.answer(dict(kind="type", seat="worker", line="go on", caller="spock"))
    assert live["sent"] == []


def test_a_screen_that_changes_while_it_is_read_is_not_pressed(ship, monkeypatch):
    svc, seat, live = ship
    frames = iter([DIALOG, DIALOG + "  a later line\n"])
    monkeypatch.setattr(svc.tmux, "capture", lambda *a, **k: next(frames))
    line = svc.answer(dict(kind="press", seat="worker", choice="Yes, go on", caller="spock"))
    assert line == "refused: worker's screen changed while it was read; nothing is pressed" and live["sent"] == []


def test_a_type_into_a_non_empty_box_is_refused(ship):
    svc, seat, live = ship
    live["screen"] = "› a person's draft\n\n  test low · /work\n"
    line = svc.answer(dict(kind="type", seat="worker", line="go on", caller="spock"))
    assert line == "refused: worker's box is not empty, it shows \"a person's draft\"; nothing is typed"
    live["screen"] = DIALOG
    line = svc.answer(dict(kind="type", seat="worker", line="go on", caller="spock"))
    assert line == "refused: worker's box is not on the screen; nothing is typed"
    assert live["sent"] == [] and not acts.path(seat).exists()


def test_a_type_into_an_empty_box_is_recorded_typed_checked_and_submitted_once(ship):
    svc, seat, live = ship
    live["screen"] = "› \n\n  test low · /work\n"
    line = svc.answer(dict(kind="type", seat="worker", line="please go on", caller="spock"))
    assert live["sent"] == [("paste", "please go on"), ("keys", ("Enter",))]
    assert live["record_at_send"] == [1, 1], "recorded before the paste"
    first, outcome = acts_of(seat)
    assert first["verb"] == "type" and first["line"] == "please go on" and first["outcome"] == "typing"
    assert outcome["outcome"] == "typed"
    assert line.startswith("worker: typed 'please go on' · submitted once; the record does not show it")
    assert "last act: typed 'please go on' by spock" in "\n".join(watch.render_full(dict(watch.seat_reading(seat))))


def test_a_typed_line_the_box_does_not_show_is_not_submitted(ship):
    svc, seat, live = ship
    live["screen"] = "› \n\n  test low · /work\n"
    live["after"] = lambda kind, value: live.update(screen="› something else\n\n  test low · /work\n")
    line = svc.answer(dict(kind="type", seat="worker", line="please go on", caller="spock"))
    assert live["sent"] == [("paste", "please go on")], "never submitted"
    assert "type 'please go on': not submitted" in line and acts_of(seat)[-1]["outcome"] == "not submitted"


@pytest.mark.parametrize("text", ["two\nlines", "a \x1b[31m colour", "", "   "])
def test_a_type_takes_one_plain_line(ship, text):
    svc, seat, live = ship
    live["screen"] = "› \n\n  test low · /work\n"
    assert svc.answer(dict(kind="type", seat="worker", line=text, caller="spock")).startswith("refused:")
    assert live["sent"] == []


def test_the_command_line_refuses_a_caller_who_is_no_systems_seat_before_any_request(ship, monkeypatch, capsys):
    from quarterdeck import cli
    svc, seat, live = ship
    filed = []
    monkeypatch.setattr(cli, "load_ship", lambda args: svc.ship)
    monkeypatch.setattr(cli, "seat_of", lambda ship, name: svc.seat(name))
    monkeypatch.setattr(cli.service, "request", lambda runtime, kind, **data: filed.append((kind, data)) or "ok")
    monkeypatch.setenv("QD_SEAT", "scott")
    args = cli.build_parser().parse_args(["press", "worker", "1. Yes, go on"])
    with pytest.raises(cli.ShipError, match="scott is a engineer seat; only a systems seat acts on a screen"):
        args.run(args)
    monkeypatch.setenv("QD_SEAT", "spock")
    assert args.run(args) == "ok"
    args = cli.build_parser().parse_args(["type", "worker", "go on"])
    assert args.run(args) == "ok"
    assert filed == [("press", dict(seat="worker", caller="spock", choice="1. Yes, go on")),
                     ("type", dict(seat="worker", caller="spock", line="go on"))]


def test_label_keys_are_read_only_from_the_choices_own_label():
    screen = "  [0] Skip  [1] Good\n  1. Yes\n  (2) No\n  3) Later\n  version 4.2 Stay\n"
    assert acts.label_key(screen, "Skip") == "0" and acts.label_key(screen, "[1] Good") == "1"
    assert acts.label_key(screen, "Yes") == "1" and acts.label_key(screen, "No") == "2"
    assert acts.label_key(screen, "Later") == "3" and acts.label_key(screen, "Stay") is None


# -- Security's guard: what is the desk's, never a systems seat's -------------------------------------------------

DESK = "that is the desk's to answer, never a systems seat's; nothing is pressed"


@pytest.mark.parametrize("question, choice, kind", [
    ("Synthetic question from the program?", "1. Allow once", "grants a permission"),
    ("Synthetic question from the program?", "1. Yes, trust this folder", "trusts a path or folder"),
    ("Synthetic question from the program?", "1. Run without the sandbox", "widens a sandbox"),
    ("Synthetic question from the program?", "1. Yes, and don't ask again", "allows a command always"),
    ("Synthetic question from the program?", "1. I agree", "answers an account holder's or terms question"),
])
def test_a_choice_whose_own_text_is_the_desks_is_refused(ship, question, choice, kind):
    svc, seat, live = ship
    live["screen"] = DIALOG.replace("1. Yes, go on", choice)
    line = svc.answer(dict(kind="press", seat="worker", choice=choice, caller="spock"))
    assert line.startswith(f"refused: the choice {choice!r} {kind} (") and line.endswith(DESK)
    assert live["sent"] == [] and not acts.path(seat).exists()


@pytest.mark.parametrize("question, kind", [
    ("Do you want to grant access to your files?", "grants a permission"),
    ("Do you trust the authors of this project?", "trusts a path or folder"),
    ("Give the agent full access to your machine?", "widens a sandbox"),
    ("Run this command always without asking?", "allows a command always"),
    ("Help us by sharing usage data for training?", "answers an account holder's or terms question"),
    ("Please review the updated Terms of Service.", "answers an account holder's or terms question"),
])
def test_a_harmless_choice_on_a_screen_whose_question_is_the_desks_is_refused(ship, question, kind):
    svc, seat, live = ship
    live["screen"] = DIALOG.replace("Synthetic question from the program?", question)
    line = svc.answer(dict(kind="press", seat="worker", choice="Yes, go on", caller="spock"))
    assert line.startswith(f"refused: its screen reads as a question that {kind} (it says ") and line.endswith(DESK)
    assert live["sent"] == []


@pytest.mark.parametrize("screen, measured", [
    ("  Allow command?\n\n  1. Yes, go on\n  2. No, stop here\n", "approval prompt"),
    ("  Sign in with ChatGPT\n\n  1. Yes, go on\n  2. No, stop here\n", "login"),
])
def test_a_screen_the_adapter_measures_as_the_desks_is_refused_by_its_form(ship, screen, measured):
    svc, seat, live = ship
    live["screen"] = screen
    line = svc.answer(dict(kind="press", seat="worker", choice="Yes, go on", caller="spock"))
    assert line.startswith(f"refused: its screen is the measured {measured!r}, which ") and line.endswith(DESK)
    assert live["sent"] == []


def test_a_measured_control_or_fixed_answer_question_is_never_a_systems_press():
    from quarterdeck.harness.types import ControlInterruption, Interruption, Screen
    control = Screen("  Switch model?\n  1. Yes\n", None, interruption=ControlInterruption("switch-model confirmation", "", control="model"))
    assert "only its own control request answers" in acts.guard(control, choice="1. Yes")
    fixed = Screen("  [1] Good [0] Skip\n", None, interruption=Interruption("a question", "", click="[0] Skip", by_label=True))
    assert "whose answer the orders fix" in acts.guard(fixed, choice="[1] Good")
    trust = Screen("  Synthetic\n  1. Yes\n", None, interruption=Interruption("trust dialog", "", key=None))
    assert acts.guard(trust, choice="1. Yes") == "its screen is the measured 'trust dialog', which trusts a path or folder"


def test_a_screen_that_cannot_be_classified_is_refused(ship):
    svc, seat, live = ship
    live["screen"] = DIALOG.replace("Synthetic question", "Synthetic\u0301 question")
    line = svc.answer(dict(kind="press", seat="worker", choice="Yes, go on", caller="spock"))
    assert line.startswith("refused: its screen cannot be classified") and line.endswith(DESK)
    assert live["sent"] == []


@pytest.mark.parametrize("text", ["always allow", "/permissions", "yes, I trust it", "accept the terms"])
def test_a_line_that_answers_what_is_the_desks_is_refused(ship, text):
    svc, seat, live = ship
    live["screen"] = "› \n\n  test low · /work\n"
    line = svc.answer(dict(kind="type", seat="worker", line=text, caller="spock"))
    assert line.startswith(f"refused: the line {text!r} ") and line.endswith("never a systems seat's; nothing is typed")
    assert live["sent"] == []


def test_a_line_into_a_box_under_a_question_that_is_the_desks_is_refused(ship):
    svc, seat, live = ship
    live["screen"] = "  Why do you want network access?\n› \n\n  test low · /work\n"
    line = svc.answer(dict(kind="type", seat="worker", line="for the build", caller="spock"))
    assert line.startswith("refused: its screen reads as a question that widens a sandbox") and live["sent"] == []
