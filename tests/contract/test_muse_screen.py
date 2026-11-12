"""Synthetic layouts constrained to the safe offline TUI description."""

from quarterdeck.harness.muse_screen import TRUST, VisibleBox, read


def frame(text="", status="", working=False):
    return ("Muse Code 1.4.15\n" + ("Thinking (2s · esc to interrupt)\n" if working else "")
            + "────────────────────\n❯ " + text + "\n────────────────────\n"
            + "echo · synthetic-folder · Launch overrides" + status)


def test_working_indicator_survives_reply_text_and_gate_until_terminal_frame():
    screen = read("A fictional reply.\n" + frame(working=True))
    assert screen.box == "" and screen.working and screen.unknown is None
    idle = read(frame())
    assert idle.box == "" and not idle.working and idle.unknown is None
    assert read(frame(status=" interrupting run")).unknown is not None


def test_no_placeholder_is_discarded_and_queue_is_only_screen_evidence():
    screen = read(frame("A fictional draft.", status="\nQueued input · ↑ to edit", working=True))
    assert screen.box == "A fictional draft." and screen.queued == 1
    assert VisibleBox("A fictional draft.").matches(screen.box)
    assert not VisibleBox("A fictional draft.\nAnother line.").matches(screen.box)
    assert not VisibleBox("A fictional draft. ").matches(screen.box)


def test_trust_requires_a_person_and_unknown_layout_never_supplies_a_box():
    screen = read("Do you trust this workspace?\n1 Trust and continue\n2 Quit\nEsc quits")
    assert screen.interruption == TRUST and not TRUST.dismissable
    assert read(frame().replace("────────────────────", "unmeasured border")).unknown is not None
    assert read("An unfamiliar screen.").box is None
