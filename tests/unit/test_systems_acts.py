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

DIALOG = "  Synthetic question from the program?\n\n  1. Continue\n  2. Not now\n\n  test low · /work\n"
MOUSE = {"sgr": True, "button": True, "all": False, "standard": False}


@pytest.fixture
def ship(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pool = Pool("test", "codex")
    seats = {"worker": SeatSpec("worker", "bridge", "worker", "codex", ("test",), cwd=tmp_path),
             "sentry": SeatSpec("sentry", "bridge", "systems", "codex", ("test",), cwd=tmp_path),
             "maker": SeatSpec("maker", "bridge", "engineer", "codex", ("test",), cwd=tmp_path),
             "desk": SeatSpec("desk", "office", "desk", "codex", ("test",), cwd=tmp_path)}
    ship = Ship(tmp_path, "example", "operator", tmp_path, {"test": pool},
                {"bridge": Session("bridge", "Test"), "office": Session("office", "Office", human=True)}, seats)
    live = {"pane": Pane("%1", "Test", "worker", 123, 80, 24, False, False, None), "screen": DIALOG, "sent": [],
            "mouse": None, "record_at_send": []}

    def capture(*a, **k):
        return live["screen"]

    def sent(kind, value):
        path = seat.trusted / "acts.jsonl"
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
    seat.trusted.mkdir(parents=True, exist_ok=True)
    launch = {"pane": "%1", "pid": 123, "at": (post.now() - timedelta(minutes=1)).isoformat(),
              "pool": "test", "argv": ["codex"], "harness": "codex", "resume": None, "version": "synthetic"}
    (seat.trusted / "launch.json").write_text(json.dumps(launch))
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
    line = svc.answer(dict(kind="press", seat="worker", choice="Continue", caller="sentry"))
    assert live["sent"] == [("keys", ("1",))], "the key its own label names, once"
    assert live["record_at_send"] == [1], "the act was on the record before the key was sent"  # law: one-door
    first, outcome = acts_of(seat)
    assert first["verb"] == "press" and first["choice"] == "Continue" and first["caller"] == "sentry"
    assert first["outcome"] == "pressing" and "Synthetic question from the program?" in first["screen"]
    assert first["launch"] == {"pane": "%1", "pid": 123, "at": seat.launched().at}
    assert outcome["act"] == first["act"] and outcome["outcome"] == "pressed"
    assert line.startswith("worker: pressed 'Continue' by key 1 · the choice is gone from the screen")
    shown = "\n".join(watch.render_full(dict(watch.seat_reading(seat))))
    assert f"last act: pressed 'Continue' by sentry at {first['at']} · pressed (the choice is gone" in shown


def test_a_choice_named_with_its_label_and_a_click_where_the_program_takes_the_mouse(ship):
    svc, seat, live = ship
    svc.answer(dict(kind="press", seat="worker", choice="2. Not now", caller="sentry"))
    assert live["sent"] == [("keys", ("2",))]
    live.update(screen="  A notice\n  Later  Dismiss\n› \n\n  test low · /work\n", sent=[], mouse=MOUSE)
    line = svc.answer(dict(kind="press", seat="worker", choice="Dismiss", caller="sentry"))
    assert live["sent"] == [("click", (13, 2))] and "by click at 13,2" in line and "still shows" in line
    live.update(sent=[], mouse=None)
    line = svc.answer(dict(kind="press", seat="worker", choice="Dismiss", caller="sentry"))
    assert line == "refused: 'Dismiss' names no key of its own and worker's program takes no mouse; nothing is pressed"
    assert live["sent"] == []


@pytest.mark.parametrize("caller", ["maker", None, "stranger"])
def test_a_press_by_any_other_seat_is_refused(ship, caller):
    svc, seat, live = ship
    line = svc.answer(dict(kind="press", seat="worker", choice="Continue", caller=caller))
    assert line.startswith("refused:") and "only a systems seat acts on a screen" in line
    assert {"maker": "maker is a engineer seat", None: "QD_SEAT names no seat",
            "stranger": "stranger is not a seat of example"}[caller] in line
    line = svc.answer(dict(kind="type", seat="worker", line="go on", caller=caller))
    assert line.startswith("refused:") and live["sent"] == [] and not acts.path(seat).exists()


def test_a_press_or_type_into_a_human_session_is_refused_whoever_asks(ship):
    svc, seat, live = ship
    for kind, field in (("press", "choice"), ("type", "line")):
        line = svc.answer({"kind": kind, "seat": "desk", field: "Continue", "caller": "sentry"})
        assert line == "refused: desk sits in a human's session; nothing is pressed or typed there, whoever asks"
    assert live["sent"] == []


def test_an_unknown_choice_name_is_refused(ship):
    svc, seat, live = ship
    line = svc.answer(dict(kind="press", seat="worker", choice="Maybe", caller="sentry"))
    assert line == "refused: no visible choice on worker's screen is named 'Maybe'; nothing is pressed"
    live["screen"] = DIALOG.replace("2. Not now", "2. Continue")
    line = svc.answer(dict(kind="press", seat="worker", choice="Continue", caller="sentry"))
    assert line == "refused: 'Continue' shows 2 times on worker's screen, not once; nothing is pressed"
    assert live["sent"] == [] and not acts.path(seat).exists()


def test_nothing_is_pressed_if_the_act_cannot_be_recorded(ship, monkeypatch):
    svc, seat, live = ship

    def unwritable(seat, entry):
        raise OSError("synthetic: read-only")
    monkeypatch.setattr(acts, "write", unwritable)
    line = svc.answer(dict(kind="press", seat="worker", choice="Continue", caller="sentry"))
    assert line == "refused: the act could not be recorded (synthetic: read-only); nothing is pressed"
    line = svc.answer(dict(kind="type", seat="worker", line="go on", caller="sentry"))
    assert live["sent"] == []


def test_a_screen_that_changes_while_it_is_read_is_not_pressed(ship, monkeypatch):
    svc, seat, live = ship
    frames = iter([DIALOG, DIALOG + "  a later line\n"])
    monkeypatch.setattr(svc.tmux, "capture", lambda *a, **k: next(frames))
    line = svc.answer(dict(kind="press", seat="worker", choice="Continue", caller="sentry"))
    assert line == "refused: worker's screen changed while it was read; nothing is pressed" and live["sent"] == []


def test_a_type_into_a_non_empty_box_is_refused(ship):
    svc, seat, live = ship
    live["screen"] = "› a person's draft\n\n  test low · /work\n"
    line = svc.answer(dict(kind="type", seat="worker", line="go on", caller="sentry"))
    assert line == "refused: worker's box is not empty, it shows \"a person's draft\"; nothing is typed"
    live["screen"] = DIALOG
    line = svc.answer(dict(kind="type", seat="worker", line="go on", caller="sentry"))
    assert line == "refused: worker's box is not on the screen; nothing is typed"
    assert live["sent"] == [] and not acts.path(seat).exists()


def test_a_type_into_an_empty_box_is_recorded_typed_checked_and_submitted_once(ship):
    svc, seat, live = ship
    live["screen"] = "› \n\n  test low · /work\n"
    line = svc.answer(dict(kind="type", seat="worker", line="please go on", caller="sentry"))
    assert live["sent"] == [("paste", "please go on"), ("keys", ("Enter",))]
    assert live["record_at_send"] == [1, 1], "recorded before the paste"
    first, outcome = acts_of(seat)
    assert first["verb"] == "type" and first["line"] == "please go on" and first["outcome"] == "typing"
    assert outcome["outcome"] == "typed"
    assert line.startswith("worker: typed 'please go on' · submitted once; the record does not show it")
    assert "last act: typed 'please go on' by sentry" in "\n".join(watch.render_full(dict(watch.seat_reading(seat))))


def test_a_typed_line_the_box_does_not_show_is_not_submitted(ship):
    svc, seat, live = ship
    live["screen"] = "› \n\n  test low · /work\n"
    live["after"] = lambda kind, value: live.update(screen="› something else\n\n  test low · /work\n")
    line = svc.answer(dict(kind="type", seat="worker", line="please go on", caller="sentry"))
    assert live["sent"] == [("paste", "please go on")], "never submitted"
    assert "type 'please go on': not submitted" in line and acts_of(seat)[-1]["outcome"] == "not submitted"


@pytest.mark.parametrize("text", ["two\nlines", "a \x1b[31m colour", "", "   "])
def test_a_type_takes_one_plain_line(ship, text):
    svc, seat, live = ship
    live["screen"] = "› \n\n  test low · /work\n"
    assert svc.answer(dict(kind="type", seat="worker", line=text, caller="sentry")).startswith("refused:")
    assert live["sent"] == []


def test_the_command_line_refuses_a_caller_who_is_no_systems_seat_before_any_request(ship, monkeypatch, capsys):
    from quarterdeck import cli
    svc, seat, live = ship
    filed = []
    monkeypatch.setattr(cli, "load_ship", lambda args: svc.ship)
    monkeypatch.setattr(cli, "seat_of", lambda ship, name: svc.seat(name))
    monkeypatch.setattr(cli.service, "request", lambda runtime, kind, **data: filed.append((kind, data)) or "ok")
    monkeypatch.setenv("QD_SEAT", "maker")
    args = cli.build_parser().parse_args(["press", "worker", "1. Continue"])
    with pytest.raises(cli.ShipError, match="maker is a engineer seat; only a systems seat acts on a screen"):
        args.run(args)
    monkeypatch.setenv("QD_SEAT", "sentry")
    assert args.run(args) == "ok"
    args = cli.build_parser().parse_args(["type", "worker", "go on"])
    assert args.run(args) == "ok"
    assert filed == [("press", dict(seat="worker", caller="sentry", choice="1. Continue")),
                     ("type", dict(seat="worker", caller="sentry", line="go on"))]


def test_label_keys_are_read_only_from_the_choices_own_label():
    screen = "  [0] Skip  [1] Good\n  1. Yes\n  (2) No\n  3) Later\n  version 4.2 Stay\n"
    assert acts.label_key(screen, "Skip") == "0" and acts.label_key(screen, "[1] Good") == "1"
    assert acts.label_key(screen, "Yes") == "1" and acts.label_key(screen, "No") == "2"
    assert acts.label_key(screen, "Later") == "3" and acts.label_key(screen, "Stay") is None


# -- Security's guard: what is the desk's, never a systems seat's -------------------------------------------------

DESK = "that is the desk's to answer, never a systems seat's; nothing is pressed"


@pytest.mark.parametrize("choice", ["1. Allow once", "1. Yes, trust this folder", "1. Run without the sandbox",
                                    "1. Yes, and don't ask again", "1. I agree", "1. Yes", "1. Continue anyway, it is fine"])
def test_a_choice_not_on_the_benign_list_is_refused(ship, choice):
    svc, seat, live = ship
    live["screen"] = DIALOG.replace("1. Continue", choice)
    line = svc.answer(dict(kind="press", seat="worker", choice=choice, caller="sentry"))
    assert line.startswith(f"refused: the choice {choice!r} is not on the benign list (close, continue, dismiss, ")
    assert "any other answer is the desk's" in line and DESK in line
    assert live["sent"] == [] and not acts.path(seat).exists()


@pytest.mark.parametrize("choice", ["Continue", "1. OK", "[0] Skip", "❯ 2. No", "Not now", "No (esc)", "Close.",
                                    "Later", "Wait", "Dismiss"])
def test_the_benign_list_sets_aside_label_pointer_and_key_hint(choice):
    assert acts.benign(choice)


@pytest.mark.parametrize("question, kind", [
    ("Do you want to grant access to your files?", "grants a permission"),
    ("Do you trust the authors of this project?", "trusts a path or folder"),
    ("Give the agent full access to your machine?", "widens a sandbox"),
    ("Keep this answer always, without asking?", "allows a command always"),
    ("Help us by sharing usage data for training?", "answers an account holder's, money, data or terms question"),
    ("Please review the updated Terms of Service.", "answers an account holder's, money, data or terms question"),
])
def test_a_harmless_choice_on_a_screen_whose_question_is_the_desks_is_refused(ship, question, kind):
    svc, seat, live = ship
    live["screen"] = DIALOG.replace("Synthetic question from the program?", question)
    line = svc.answer(dict(kind="press", seat="worker", choice="Continue", caller="sentry"))
    assert line.startswith(f"refused: its screen reads as a question that {kind} (the whole screen says ") and DESK in line
    assert live["sent"] == []


@pytest.mark.parametrize("screen, measured", [
    ("  Allow command?\n\n  1. Continue\n  2. Not now\n", "approval prompt"),
    ("  Sign in with ChatGPT\n\n  1. Continue\n  2. Not now\n", "login"),
])
def test_a_screen_the_adapter_measures_as_the_desks_is_refused_by_its_form(ship, screen, measured):
    svc, seat, live = ship
    live["screen"] = screen
    line = svc.answer(dict(kind="press", seat="worker", choice="Continue", caller="sentry"))
    assert line.startswith(f"refused: its screen is the measured {measured!r}, which ") and DESK in line
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
    line = svc.answer(dict(kind="press", seat="worker", choice="Continue", caller="sentry"))
    assert line.startswith("refused: its screen cannot be classified") and DESK in line
    assert live["sent"] == []


@pytest.mark.parametrize("text", ["always allow", "/permissions", "yes, I trust it", "accept the terms"])
def test_a_line_that_answers_what_is_the_desks_is_refused(ship, text):
    svc, seat, live = ship
    live["screen"] = "› \n\n  test low · /work\n"
    line = svc.answer(dict(kind="type", seat="worker", line=text, caller="sentry"))
    assert line.startswith(f"refused: the line {text!r} ") and "never a systems seat's; nothing is typed; " in line
    assert live["sent"] == []


def test_a_line_into_a_box_under_a_question_that_is_the_desks_is_refused(ship):
    svc, seat, live = ship
    live["screen"] = "  Why do you want network access?\n› \n\n  test low · /work\n"
    line = svc.answer(dict(kind="type", seat="worker", line="for the build", caller="sentry"))
    assert line.startswith("refused: its screen reads as a question that widens a sandbox") and live["sent"] == []


def to_desk(svc):
    return [m for m in svc.store.all() if m.to == "desk"]


def test_the_service_itself_tells_the_desk_once_per_seat_and_screen(ship):
    svc, seat, live = ship
    live["screen"] = DIALOG.replace("Synthetic question from the program?", "Do you trust the authors of this project?")
    first = svc.answer(dict(kind="press", seat="worker", choice="Continue", caller="sentry"))
    [notice] = to_desk(svc)
    assert first.endswith(f"nothing is pressed; the desk is told ({notice.short})")
    assert notice.kind == "report" and notice.sender == "ship"
    assert "on worker: sentry asked qd to press 'Continue' there" in notice.body
    assert "trusts a path or folder" in notice.body and "Do you trust the authors of this project?" in notice.body
    again = svc.answer(dict(kind="press", seat="worker", choice="Not now", caller="sentry"))
    assert again.startswith("refused:") and again.endswith(f"the desk was told already ({notice.id})")
    assert len(to_desk(svc)) == 1, "deduplicated: one notice for one seat and screen"
    live["screen"] = live["screen"].replace("authors", "makers")
    svc.answer(dict(kind="press", seat="worker", choice="Continue", caller="sentry"))
    assert len(to_desk(svc)) == 2, "another screen is another notice"
    assert live["sent"] == [] and not acts.path(seat).exists()


_BODY = "Q7wM2xC9zT4lP8rV3nH6Q7wM2xC9zT4lP8r"  # synthetic; each shape is put together here so none stands whole in the file
KEY_SHAPES = [*(start + _BODY for start in ("sk-", "sk-" + "ant-", "sk-" + "or-", "xai-", "gh" + "p_", "gh" + "o_",
                                            "gh" + "s_", "gh" + "u_", "github" + "_pat_", "gl" + "pat-", "AI" + "za")),
              "AK" + "IA" + "Q7WM2XC9ZT4LP8RV", "ey" + "Jabcdefgh12.abcdefgh12.abcdefgh12"]


@pytest.mark.parametrize("shape", KEY_SHAPES)
def test_a_key_drawn_on_a_screen_is_masked_in_the_acts_record(ship, shape):
    svc, seat, live = ship
    live["screen"] = DIALOG.replace("Synthetic question from the program?", f"Synthetic note {shape}")
    live["after"] = lambda kind, value: live.update(screen="• Working (1s • esc to interrupt)\n› \n\n  test low · /work\n")
    svc.answer(dict(kind="press", seat="worker", choice="Continue", caller="sentry"))
    assert "[masked]" in acts_of(seat)[0]["screen"]
    assert shape not in acts.path(seat).read_text() and _BODY[:8] not in acts.path(seat).read_text().replace("Q7WM", "")


@pytest.mark.parametrize("shape", KEY_SHAPES)
def test_a_key_drawn_on_a_refused_screen_is_masked_in_the_desks_post(ship, shape):
    svc, seat, live = ship
    live["screen"] = DIALOG.replace("Synthetic question from the program?",
                                    f"Do you trust the authors of this project? {shape}")
    svc.answer(dict(kind="press", seat="worker", choice="Continue", caller="sentry"))
    [notice] = to_desk(svc)
    assert "Do you trust the authors of this project? [masked]" in notice.body and shape not in notice.body


def _box_after_paste(live, drawn):
    live["screen"] = "› \n\n  test low · /work\n"
    live["after"] = lambda kind, value: live.update(screen=f"› {drawn}\n\n  test low · /work\n")


def _last_act_line(seat):
    """Watch's line of the last act alone: its live reading of the box is the screen, shown as it is."""
    rows = watch.render_full(dict(watch.seat_reading(seat)))
    return next(row for row in "\n".join(rows).split("\n") if "last act:" in row)


def _no_part_of(key, *texts):
    for text in texts:
        assert key not in text and key[:12] not in text and key[-8:] not in text


@pytest.mark.parametrize("shape", KEY_SHAPES)
def test_a_key_the_box_shows_after_a_paste_is_masked_in_the_record_the_reply_and_watch(ship, shape):
    svc, seat, live = ship
    _box_after_paste(live, f"note {shape}")
    reply = svc.answer(dict(kind="type", seat="worker", line="please go on", caller="sentry"))
    shown = _last_act_line(seat)
    assert "not submitted" in reply and "[masked]" in reply and "[masked]" in acts_of(seat)[-1]["detail"]
    assert "[masked]" in shown
    _no_part_of(shape, reply, acts.path(seat).read_text(), shown)


@pytest.mark.parametrize("shape", KEY_SHAPES)
def test_a_key_in_a_box_that_refuses_a_type_is_masked_in_the_refusal(ship, shape):
    svc, seat, live = ship
    live["screen"] = f"› a draft {shape} and more\n\n  test low · /work\n"
    line = svc.answer(dict(kind="type", seat="worker", line="go on", caller="sentry"))
    assert line.startswith("refused: worker's box is not empty") and "[masked]" in line
    _no_part_of(shape, line)
    assert live["sent"] == []


def test_the_refusals_cut_cannot_leave_part_of_a_key(ship):
    svc, seat, live = ship
    key = "sk-" + _BODY * 3
    live["screen"] = f"› {'a' * 70} {key}\n\n  test low · /work\n"
    line = svc.answer(dict(kind="type", seat="worker", line="go on", caller="sentry"))
    assert line.startswith("refused: worker's box is not empty")
    _no_part_of(key, line)
    assert key[:6] not in line


@pytest.mark.parametrize("width", [5, 9, 20, 40])
def test_a_key_folded_across_the_boxs_rows_leaves_no_row_of_it(ship, width):
    svc, seat, live = ship
    key = "sk-" + "ant-" + _BODY * 3
    rows = [key[at:at + width] for at in range(0, len(key), width)]
    drawn = "\n  ".join(rows)
    live["screen"] = f"› {drawn}\n\n  test low · /work\n"
    assert "\n" in seat.harness.screen(live["screen"]).box, "the program's box holds the fold"
    refused = svc.answer(dict(kind="type", seat="worker", line="go on", caller="sentry"))
    _box_after_paste(live, drawn)
    reply = svc.answer(dict(kind="type", seat="worker", line="please go on", caller="sentry"))
    record = acts.path(seat).read_text()
    shown = _last_act_line(seat)
    for text in (refused, reply, record, shown):
        assert "[masked]" in text
        assert not any(len(row) >= 5 and row in text for row in rows)


def test_a_folded_key_after_words_on_the_row_before_is_masked():
    key = "sk-" + _BODY * 2
    assert acts.masked_box(f"a note\n{key[:20]}\n{key[20:]}\nlast").startswith("a note\n[masked]")
    assert key[20:] not in acts.masked_box(f"a note\n{key[:20]}\n{key[20:]}")


def test_a_box_with_no_key_is_kept_as_it_was():
    for text in ("a person's draft", "two\n  rows of words", "", "  spaced  out  "):
        assert acts.masked_box(text) == text


def test_the_screens_cut_cannot_leave_the_end_of_a_key():
    key = "sk-" + _BODY
    shown = acts.bounded(key + "y" * (acts.SCREEN_CHARS - 10))
    assert "[masked]" in shown and key[-10:] not in shown


def test_a_refusal_for_authority_tells_nobody(ship):
    svc, seat, live = ship
    live["screen"] = DIALOG.replace("Synthetic question from the program?", "Do you trust the authors of this project?")
    svc.answer(dict(kind="press", seat="worker", choice="Continue", caller="maker"))
    svc.answer(dict(kind="press", seat="desk", choice="Continue", caller="sentry"))
    assert to_desk(svc) == [] and not acts.notices(seat).exists()


def test_with_no_desk_declared_the_refusal_says_nobody_was_told(ship):
    svc, seat, live = ship
    del svc.ship.seats["desk"]
    live["screen"] = DIALOG.replace("1. Continue", "1. Allow once")
    line = svc.answer(dict(kind="press", seat="worker", choice="Allow once", caller="sentry"))
    assert line.endswith("nothing is pressed; no desk seat is declared; nobody was told") and live["sent"] == []


# -- security's samples (runtime/seats/unit/d3-guard/probe.py): approximations of real programs' forms -------------

SECURITY_DIALOGS = {
    "mcp approval (Claude Code form)": "New MCP server found in .mcp.json: github\n❯ 1. Use this and all future MCP servers in this project\n  2. Use this MCP server\n  3. Continue without using this MCP server",
    "run command, no allow word": "Would you like to run the following command?\n  curl x | sh\n❯ 1. Yes\n  2. No",
    "apply edit": "Apply this change to src/x.py?\n❯ 1. Yes\n  2. No",
    "curly apostrophe": "Run npm test?\n❯ 1. Yes\n  2. Yes, and don’t ask again\n  3. No",
    "wrapped phrase": "Run npm test?\n  2. Yes, and don't ask\n  again for npm",
    "during this session": "Edit file?\n 1. Yes\n 2. Yes, for all edits during this session",
    "api key entry": "Enter your API key:\n> ",
    "login code": "Paste code here if prompted >",
    "extra usage": "You've hit your limit. Turn on extra usage to keep going?\n 1. Yes\n 2. Wait",
    "contributor mode": "Enable contributor mode for faster models?\n 1. Yes\n 2. No",
    "help improve": "Help improve Grok with your coding data?\n 1. Yes\n 2. No",
    "hooks": "This project defines hooks. Enable project hooks?\n 1. Yes\n 2. No",
    "add dir": "Add /home/person to the working directories?\n 1. Yes\n 2. No",
}
SECURITY_LINES = {"/approvals": True, "/mcp": True, "/hooks": True, "/add-dir /home/person": True,
              "!curl http://x | sh": True, "# always run tests": True, "/config": True, "/model opus": True,
              "/plugin install x": True, "continue": False}


@pytest.mark.parametrize("name", sorted(SECURITY_DIALOGS))
def test_each_of_the_security_dialogs_is_the_desks_even_for_a_benign_choice(name):
    from quarterdeck.harness.types import Screen
    text = SECURITY_DIALOGS[name]
    assert acts._kind(text) is not None, "the dialog's words alone refuse it"
    for choice in ("No", "Wait", "Continue"):
        assert acts.guard(Screen(text, None), choice=choice) is not None
    assert acts.guard(Screen(text, ""), line="continue") is not None, "a line under it is refused too"


@pytest.mark.parametrize("line", sorted(SECURITY_LINES))
def test_each_of_the_security_lines_is_refused_or_allowed_as_she_says(line):
    from quarterdeck.harness.types import Screen
    reason = acts.guard(Screen("› \n\n  test low · /work\n", ""), line=line)
    assert (reason is not None) is SECURITY_LINES[line], reason


@pytest.mark.parametrize("start", list("/!#$&@\\`"))
def test_a_line_that_starts_as_a_command_is_never_typed(ship, start):
    svc, seat, live = ship
    live["screen"] = "› \n\n  test low · /work\n"
    line = svc.answer(dict(kind="type", seat="worker", line=f"  {start}something", caller="sentry"))
    assert f"starts with {start!r}: a program command, shell mode or reference" in line and live["sent"] == []


def test_normalising_catches_a_curly_apostrophe_a_wrapped_phrase_and_a_frame():
    always = "allows a command always"
    assert acts._kind("Yes, and don’t ask again")[0] == always
    assert acts._kind("Yes, and don't\n   ask again")[0] == always
    assert acts._kind("│ Yes, and don't │\n│ ask again    │")[0] == always
    assert acts._kind("ｄｏｎ’ｔ ａｓｋ")[0] == always, "NFKC"
    assert acts.normalised("│ a\n\t b ╰──╯ ") == "a b"


def test_the_region_is_the_dialogs_top_frame_or_else_the_whole_screen(ship):
    svc, seat, live = ship
    above = "  earlier: run the tests and edit the code\n"
    framed = "╭──────────────────────╮\n│ Limit reached        │\n│ 1. Wait  2. Not now  │\n╰──────────────────────╯\n"
    live["screen"] = above + framed
    line = svc.answer(dict(kind="press", seat="worker", choice="2. Not now", caller="sentry"))
    assert live["sent"] == [("keys", ("2",))], line
    live.update(screen=above + framed.replace("╭", "─").replace("╰", "─"), sent=[])
    line = svc.answer(dict(kind="press", seat="worker", choice="2. Not now", caller="sentry"))
    assert "(the whole screen says 'run')" in line and live["sent"] == []
    live.update(screen=above + framed.replace("Limit reached", "Run the hooks?"), sent=[])
    line = svc.answer(dict(kind="press", seat="worker", choice="2. Not now", caller="sentry"))
    assert "(the dialog's frame says 'Run')" in line and live["sent"] == []


# -- security's second probe (runtime/seats/unit/d3-guard/probe2.py) -------------------------------------------------

def test_a_benign_choice_that_skips_safety_checks_is_refused(ship):
    svc, seat, live = ship
    live["screen"] = "  Skip safety checks for this folder?\n\n  1. Skip\n  2. Cancel\n"
    line = svc.answer(dict(kind="press", seat="worker", choice="1. Skip", caller="sentry"))
    assert line.startswith("refused: its screen reads as a question that widens a sandbox (the whole screen says 'safety')")
    assert live["sent"] == []


@pytest.mark.parametrize("word", ["safety", "security", "protection", "guardrails", "checks", "confirmation",
                                  "warnings", "verification"])
def test_the_words_of_a_lowered_guard_are_the_desks(word):
    assert acts._kind(f"Turn off {word} here?") == ("widens a sandbox", word)


@pytest.mark.parametrize("text", ["​/config", "please​ continue", "go on⁠", "﻿continue", "con­tinue"])
def test_a_line_with_an_invisible_format_character_is_never_typed(ship, text):
    svc, seat, live = ship
    live["screen"] = "› \n\n  test low · /work\n"
    line = svc.answer(dict(kind="type", seat="worker", line=text, caller="sentry"))
    assert "holds an invisible format character (U+" in line and line.startswith("refused:") and live["sent"] == []


def test_a_fullwidth_command_start_is_read_as_the_command_it_shows():
    from quarterdeck.harness.types import Screen
    assert "starts with '/': a program command" in acts.guard(Screen("› \n", ""), line="／config")
