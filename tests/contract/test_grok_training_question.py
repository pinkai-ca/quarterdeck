"""Grok's first-run training-data question, read as measured on 1.0.77 and answered opt out.

The frames are M3's and M4's (13 November, 112 x 35), drawn in the 256-colour palette by a tmux that offers no
truecolor; only the workspace path is replaced. The question sits directly above the composer: a blank row,
its title with [Opt out] and [Opt in], its body, a blank row, then the composer's top border. No program,
tmux, credential or provider is touched here.
"""

from dataclasses import replace
import json
from pathlib import Path
import tomllib

import pytest

from quarterdeck.harness import grok_screen
from quarterdeck.harness.grok import GrokHarness
from .test_grok_home import synthetic, synthetic_native_paths  # noqa: F401 -- the guide-backed home fixture

FIXTURES = Path(__file__).parents[1] / "fixtures/grok/1.0.77"


def frame(name: str) -> str:
    return "\n".join(json.loads((FIXTURES / f"{name}.json").read_text())["lines"]) + "\n"


QUESTION = frame("training-question-256")
ANSWERED = frame("training-answered-256")
# M4: a launch carrying its first message as an argument; the turn ran under the question, then the idle hint showed.
AFTER_TURN = frame("training-question-after-turn-256")
ANSWERED_AFTER_TURN = frame("training-answered-after-turn-256")
# M4's first frame (w01), 0.6 s into that turn: the working hint under the footer, the question already drawn.
WORKING = frame("training-question-working-256")


def test_the_measured_question_is_named_and_answered_by_its_opt_out_text():
    screen = GrokHarness().screen(QUESTION)
    assert screen.interruption is grok_screen.TRAINING_QUESTION
    assert screen.interruption.click == "[Opt out]" and screen.interruption.key is None
    assert screen.unknown is None and screen.box is not None and not screen.box.strip()
    assert screen.shows == {"model": "Grok 4.7", "effort": "high"}


def test_after_a_turn_the_question_is_the_same_and_the_cleared_composer_reads_idle():
    adapter = GrokHarness()
    screen = adapter.screen(AFTER_TURN)
    assert screen.interruption is grok_screen.TRAINING_QUESTION and not screen.working and screen.box.strip() == ""
    assert adapter.question_cleared(ANSWERED_AFTER_TURN, grok_screen.TRAINING_QUESTION)
    idle = adapter.screen(ANSWERED_AFTER_TURN)
    assert idle.interruption is None and idle.unknown is None and idle.working is False and idle.box.strip() == ""


def test_the_question_under_a_running_turn_reads_working():
    screen = GrokHarness().screen(WORKING)
    assert screen.interruption is grok_screen.TRAINING_QUESTION and screen.unknown is None
    assert screen.working is True and screen.box.strip() == ""


def hint_replaced(frame_text: str, hint: str) -> str:
    """The frame without styling, its one hint row under the footer replaced."""
    lines = grok_screen.characters(frame_text)[0].split("\n")
    at = next(i for i, row in enumerate(lines) if "Shift+Tab:mode" in row)
    lines[at] = hint
    return "\n".join(lines)


def test_the_measured_hint_rows_under_the_question_read_as_measured():
    adapter = GrokHarness()
    assert adapter.screen(grok_screen.characters(WORKING)[0]).working is True
    assert adapter.screen(grok_screen.characters(AFTER_TURN)[0]).working is False
    assert adapter.screen(grok_screen.characters(QUESTION)[0]).working is False  # nothing under the footer: M1 to M3


@pytest.mark.parametrize("hint", ["  Shift+Tab:mode", "  Shift+Tab:mode  │  Ctrl+c:cancel  │  Ctrl+x:shortcuts  │  more",
                                  "  Grok is thinking", "  Esc:interrupt"])
def test_a_hint_row_that_reads_neither_leaves_the_question_named_and_unpressable(hint):
    screen = GrokHarness().screen(hint_replaced(AFTER_TURN, hint))
    assert screen.interruption is grok_screen.TRAINING_QUESTION
    assert screen.box is None and screen.unknown == "unmeasured Grok composer hint under the training question"


def test_only_nothing_under_the_footer_is_the_fresh_form_without_a_hint():
    lines = grok_screen.characters(QUESTION)[0].split("\n")
    footer = next(i for i, row in enumerate(lines) if grok_screen.FOOTER.fullmatch(row))
    assert not any(row.strip() for row in lines[footer + 1:])
    lines[footer + 2] = "  stray"
    screen = GrokHarness().screen("\n".join(lines))
    assert screen.interruption is grok_screen.TRAINING_QUESTION and screen.box is None and screen.unknown


def test_the_256_colour_palette_decodes_as_measured():
    plain, styles = grok_screen.characters("\x1b[38;5;254mA\x1b[48;5;233mB\x1b[38;5;179mC\x1b[0mD")
    assert plain == "ABCD"
    assert styles[0].foreground == (228, 228, 228)
    assert styles[1].background == (18, 18, 18)
    assert styles[2].foreground == (215, 175, 95)
    assert styles[3] == grok_screen.Style()
    assert [grok_screen.indexed(i) for i in (16, 231, 232, 255)] == [(0, 0, 0), (255, 255, 255), (8, 8, 8), (238, 238, 238)]


@pytest.mark.parametrize("code", ["38;5;0", "38;5;15", "48;5;7", "38;5;256", "38;5"])
def test_system_colours_and_malformed_indexes_stay_unknown(code):
    assert grok_screen.characters(f"\x1b[{code}mA") is None


def test_the_answered_frame_clears_the_question_and_shows_the_empty_composer():
    adapter = GrokHarness()
    assert adapter.question_cleared(ANSWERED, grok_screen.TRAINING_QUESTION)
    assert not adapter.question_cleared(QUESTION, grok_screen.TRAINING_QUESTION)
    screen = adapter.screen(ANSWERED)
    # No hint row shows yet under the footer: the fresh form. The reader alone infers no readiness from it;
    # it names the question whose answer, in the launch's own record, lets the Door read it idle.
    assert screen.interruption is None and screen.box is None and screen.unknown == grok_screen.FRESH
    assert screen.fresh == grok_screen.TRAINING_QUESTION.name


def answered_plain() -> list[str]:
    return grok_screen.characters(ANSWERED)[0].split("\n")


def footer_at(lines: list[str]) -> int:
    return max(i for i, row in enumerate(lines) if grok_screen.FOOTER.fullmatch(row))


def test_the_fresh_form_is_only_an_empty_box_with_nothing_at_all_under_the_footer():
    lines = answered_plain()
    at = footer_at(lines)
    assert at < len(lines) - 1 and not any(row.strip() for row in lines[at + 1:]), "M3: blank rows under the footer"
    assert GrokHarness().screen("\n".join(lines)).fresh == grok_screen.TRAINING_QUESTION.name
    for row in ("  x", "  Shift+Tab:mode │ Ctrl+x:shortcuts", "  Grok Build 1.0.77", "   ·"):
        under = lines[:at + 1] + [row] + lines[at + 2:]
        screen = GrokHarness().screen("\n".join(under))
        assert screen.fresh is None, f"something under the footer is no fresh form: {row!r}"
    typed = list(lines)
    typed[at - 1] = typed[at - 1].replace("❯  ", "❯ x", 1)
    screen = GrokHarness().screen("\n".join(typed))
    assert screen.fresh is None and screen.unknown == "unmeasured Grok composer hint", "a draft is no fresh form"


PLAIN = grok_screen.characters(QUESTION)[0]  # the same frame without its styling, for exact edits of its words


def test_the_question_reads_the_same_without_styling():
    assert GrokHarness().screen(PLAIN).interruption is grok_screen.TRAINING_QUESTION


def rows() -> list[str]:
    return PLAIN.split("\n")


def title_at(lines: list[str]) -> int:
    return next(i for i, row in enumerate(lines) if "Help improve Grok" in row)


def changed(change) -> str:
    lines = rows()
    change(lines, title_at(lines))
    return "\n".join(lines)


ALTERATIONS = {
    "choices swapped": lambda l, t: l.__setitem__(t, l[t].replace("[Opt out] [Opt in]", "[Opt in] [Opt out]")),
    "opt in missing": lambda l, t: l.__setitem__(t, l[t].replace(" [Opt in]", "         ")),
    "a third choice": lambda l, t: l.__setitem__(t, l[t].replace("        [Opt out] [Opt in]", "[Later] [Opt out] [Opt in]")),
    "title words": lambda l, t: l.__setitem__(t, l[t].replace("Help improve Grok", "Help improve Grok!")),
    "body words": lambda l, t: l.__setitem__(t + 1, l[t + 1].replace("Off by default", "On by default")),
    "body line missing": lambda l, t: l.__setitem__(t + 3, ""),
    "no blank row above the composer": lambda l, t: l.__setitem__(t + 4, "  An extra line."),
}


@pytest.mark.parametrize("name", sorted(ALTERATIONS))
def test_an_altered_question_is_named_held_and_never_clicked(name):
    screen = GrokHarness().screen(changed(ALTERATIONS[name]))
    assert screen.interruption is not grok_screen.TRAINING_QUESTION
    if screen.interruption is not None:
        assert screen.interruption is grok_screen.TRAINING_ALTERED
        assert screen.interruption.click is None and screen.interruption.key is None


@pytest.mark.parametrize("name", ["choices swapped", "title words", "body words", "body line missing",
                                  "no blank row above the composer"])
def test_an_altered_question_with_its_choices_reads_as_the_named_question(name):
    screen = GrokHarness().screen(changed(ALTERATIONS[name]))
    assert screen.interruption is grok_screen.TRAINING_ALTERED and screen.unknown is None


def test_the_words_quoted_in_the_transcript_are_not_the_question():
    lines = rows()
    title = title_at(lines)
    for offset in range(5):
        lines[title + offset] = ""
    lines[3] = "  someone wrote: Help improve Grok, then nothing more"
    screen = GrokHarness().screen("\n".join(lines))
    assert screen.interruption is None


def test_the_question_twice_on_the_screen_is_never_clicked():
    lines = rows()
    lines[3] = lines[title_at(lines)]
    screen = GrokHarness().screen("\n".join(lines))
    assert screen.interruption is not grok_screen.TRAINING_QUESTION


def acked(home: Path) -> object:
    return tomllib.loads((home / "config.toml").read_text()).get("privacy")


def test_a_new_home_is_never_seeded_with_an_answer(tmp_path):
    adapter, spec, pool, home, _ = synthetic(tmp_path)
    spec = replace(spec, settings={"privacy": {"privacy_banner_acked": "2026-11-13T08:49:45Z"}})
    adapter.build_home(home, spec, pool, None)
    assert acked(home) is None


def test_the_programs_own_privacy_table_survives_the_seats_rebuilds(tmp_path):
    adapter, spec, pool, home, _ = synthetic(tmp_path)
    adapter.build_home(home, spec, pool, None)
    config = home / "config.toml"
    config.write_text(config.read_text() + '\n[privacy]\nprivacy_banner_acked = "2026-11-13T08:49:45Z"\n')
    adapter.build_home(home, spec, pool, None)
    assert acked(home) == {"privacy_banner_acked": "2026-11-13T08:49:45Z"}
    adapter.build_home(home, spec, pool, None)
    assert acked(home) == {"privacy_banner_acked": "2026-11-13T08:49:45Z"}


@pytest.mark.parametrize("table", ['privacy_banner_acked = true', 'privacy_banner_acked = "not a time"',
                                   'other = "2026-11-13T08:49:45Z"'])
def test_only_the_measured_key_as_a_timestamp_is_kept(tmp_path, table):
    adapter, spec, pool, home, _ = synthetic(tmp_path)
    adapter.build_home(home, spec, pool, None)
    config = home / "config.toml"
    config.write_text(config.read_text() + f"\n[privacy]\n{table}\n")
    adapter.build_home(home, spec, pool, None)
    assert acked(home) is None


def test_an_extra_key_beside_the_answer_is_not_carried(tmp_path):
    adapter, spec, pool, home, _ = synthetic(tmp_path)
    adapter.build_home(home, spec, pool, None)
    config = home / "config.toml"
    config.write_text(config.read_text() + '\n[privacy]\nprivacy_banner_acked = "2026-11-13T08:49:45Z"\nopt_in = true\n')
    adapter.build_home(home, spec, pool, None)
    assert acked(home) == {"privacy_banner_acked": "2026-11-13T08:49:45Z"}


def test_1_0_70_draws_the_same_fresh_form_after_the_press():
    """M5 ran 1.0.70, the program a bare grok reaches on the measured machine; only its banner differs."""
    fresh = "\n".join(json.loads((FIXTURES.parent / "1.0.70" / "training-answered-256.json").read_text())["lines"]) + "\n"
    adapter = GrokHarness()
    assert adapter.question_cleared(fresh, grok_screen.TRAINING_QUESTION)
    screen = adapter.screen(fresh)
    assert screen.fresh == grok_screen.TRAINING_QUESTION.name and screen.unknown == grok_screen.FRESH
    assert "1.0.70" in fresh and grok_screen.characters(fresh)[0].split("\n")[-6:] == \
        grok_screen.characters(ANSWERED)[0].split("\n")[-6:], "the composer and below are the same as 1.0.77's"
