"""Layout breaks are not spaces; edits are not layout breaks."""

import pytest

from quarterdeck.composer import matches
from quarterdeck.door import Door
from quarterdeck.harness import get
from types import SimpleNamespace


@pytest.mark.parametrize('name', ['codex', 'claude', 'antigravity', 'fake'])
@pytest.mark.parametrize('body', ['x' * 34 + 'fresh-launch', 'a' * 40 + '/abc.def', ('word ' * 12).rstrip()])
def test_every_adapter_accepts_exact_width_wraps_and_rejects_edits(name, body):
    harness = get(name)
    columns = 40
    # Independent renderer: fixed cell rows, not the comparator's implementation.
    rows = [body[i:i + columns] for i in range(0, len(body), columns)]
    if name == 'codex':
        text = '\n'.join(['› ' + rows[0], *['  ' + r for r in rows[1:]], '', '  model low · /work'])
    elif name == 'claude':
        text = '\n'.join(['─' * 42, '❯ ' + rows[0], *['  ' + r for r in rows[1:]], '─' * 42])
    else:
        text = '\n'.join(['─' * 42, '> ' + rows[0], *['  ' + r for r in rows[1:]], '─' * 42,
                          'fake claude · model test · effort low · idle'])
    seat = SimpleNamespace(harness=harness, tmux=None, pane=lambda: SimpleNamespace(width=columns + (3 if name == 'codex' else 2)))
    screen = harness.screen(text)
    # law: one-door
    assert Door(seat).matches(harness.box(body.rstrip()), screen)
    assert not Door(seat).matches(harness.box(body.replace('a', 'b', 1) + '!'), screen)


def test_significant_whitespace_and_unknown_layout_fail_closed():
    assert not matches('fresh-launch', 'fresh- launch', 7)
    assert not matches('alpha beta', 'alphabeta', 10)
    assert not matches('alpha  beta', 'alpha beta', 20)
    assert not matches('alpha\nbeta', 'alpha beta', 20)
    assert not matches('alpha beta', 'alpha\nbeta', 20)
    assert not matches('fresh-launch', 'fresh-\nlaunch')
    assert not matches('fresh-launch', 'fre\nsh-launch', 12)
    assert matches('fresh-launch', 'fresh-\nlaunch', 6)
    assert matches('abcdefghijkl', 'abcde\nfghij\nkl', 5)
    assert matches('first fresh-launch', 'first fresh-\nlaunch', 12)
    assert not matches('first fresh-launch', 'first fresh-\nlaunch', 30)
    assert matches('two  spaces here', 'two  spaces\nhere', 12)
    assert not matches('two  spaces here', 'two spaces\nhere', 12)


@pytest.mark.parametrize("token", ["-x", "--option", "---", "-x-y"])
def test_leading_hyphens_have_width_at_a_word_break(token):
    first = "alpha " * 3 + "z"
    assert len(first) == 19
    assert matches(first + " " + token + " tail", first + "\n" + token + " tail", 20)
    assert not matches("short " + token, "short\n" + token, 20)
    assert not matches(first + " " + token, first + "\n" + token.replace("-", "", 1), 20)
