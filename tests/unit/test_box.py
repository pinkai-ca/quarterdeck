"""Layout breaks are not spaces; edits are not layout breaks."""

import pytest

from quarterdeck.harness.types import Box
from quarterdeck.door import Door
from quarterdeck.harness import get
from types import SimpleNamespace


@pytest.mark.parametrize("name", ["codex", "claude", "antigravity", "fake"])
@pytest.mark.parametrize("body", ["x" * 34 + "fresh-launch", "a" * 40 + "/abc.def", ("word " * 12).rstrip()])
def test_every_adapter_accepts_exact_width_wraps_and_rejects_edits(name, body) -> None:
    harness = get(name)
    columns = 40
    # Independent renderer: fixed cell rows, not the comparator's implementation.
    rows = [body[i:i + columns] for i in range(0, len(body), columns)]
    if name == "codex":
        text = "\n".join(["› " + rows[0], *["  " + r for r in rows[1:]], "", "  model low · /work"])
    elif name == "claude":
        text = "\n".join(["─" * 42, "❯ " + rows[0], *["  " + r for r in rows[1:]], "─" * 42])
    else:
        text = "\n".join(["─" * 42, "> " + rows[0], *["  " + r for r in rows[1:]], "─" * 42,
                          "fake claude · model test · effort low · idle"])
    seat = SimpleNamespace(harness=harness, tmux=None, pane=lambda: SimpleNamespace(width=columns + {"codex": 3, "claude": 4, "antigravity": 3}.get(name, 2)))
    screen = harness.screen(text)
    # law: one-door
    assert Door(seat).matches(harness.box(body.rstrip()), screen)
    assert not Door(seat).matches(harness.box(body.replace("a", "b", 1) + "!"), screen)


def test_significant_whitespace_and_unknown_layout_fail_closed() -> None:
    assert not Box("fresh-launch").matches("fresh- launch", 7)
    assert not Box("alpha beta").matches("alphabeta", 10)
    assert not Box("alpha  beta").matches("alpha beta", 20)
    assert not Box("alpha\nbeta").matches("alpha beta", 20)
    assert not Box("alpha beta").matches("alpha\nbeta", 20)
    assert not Box("fresh-launch").matches("fresh-\nlaunch")
    assert not Box("fresh-launch").matches("fre\nsh-launch", 12)
    assert Box("fresh-launch").matches("fresh-\nlaunch", 6)
    assert Box("abcdefghijkl").matches("abcde\nfghij\nkl", 5)
    assert Box("first fresh-launch").matches("first fresh-\nlaunch", 12)
    assert not Box("first fresh-launch").matches("first fresh-\nlaunch", 30)
    assert Box("two  spaces here").matches("two  spaces\nhere", 12)
    assert not Box("two  spaces here").matches("two spaces\nhere", 12)


@pytest.mark.parametrize("token", ["-x", "--option", "---", "-x-y"])
def test_leading_hyphens_have_width_at_a_word_break(token) -> None:
    first = "alpha " * 3 + "z"
    assert len(first) == 19
    assert Box(first + " " + token + " tail").matches(first + "\n" + token + " tail", 20)
    assert not Box("short " + token).matches("short\n" + token, 20)
    assert not Box(first + " " + token).matches(first + "\n" + token.replace("-", "", 1), 20)
