"""Hold gates for Antigravity 1.5.9's measured box: only the message, wrapped as measured, is submitted.

Law one-door: the door types once, checks the box as the program shows it, and submits only what it
can match. Declaring the box's geometry may only widen what counts as the message wrapped by the
program; any box that shows other words, fewer words, more words, or rows wider than the measured box
is not the message, and the door holds without typing again or submitting.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck.door import Door
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.ship import SeatSpec

ROOT = Path(__file__).parents[1] / 'fixtures/antigravity/1.5.9'
PARAGRAPH = (
    'Please report the measured input box width and keep this synthetic paragraph together '
    'when it crosses the terminal edge. This harmless example contains ordinary words, '
    'punctuation, and enough text to wrap twice.'
)


def fixture(name):
    return (ROOT / f'{name}.txt').read_text()


def edited(text, old, new):
    changed = text.replace(old, new, 1)
    assert changed != text, f'fixture no longer contains {old!r}'
    return changed


def rewrapped_ruler(width, first):
    """The 221-character ruler drawn with rows `first` cells wide: wider than the measured box."""
    lines = fixture(f'ruler-{width}').splitlines()
    payload = 'X' * 221
    rows = [payload[i:i + first] for i in range(0, len(payload), first)]
    drawn = [f'> {rows[0]}'] + [f'  {row}' for row in rows[1:]]
    return '\n'.join(lines[:2] + drawn + lines[-2:]) + '\n'


def spoiled(width):
    paragraph = fixture(f'paragraph-{width}')
    lines = paragraph.splitlines()
    return {
        'other-word': (PARAGRAPH, edited(paragraph, 'harmless', 'harmful ')),
        'fewer-words': (PARAGRAPH, edited(paragraph, 'wrap twice.', 'wrap      ')),
        'more-words': (PARAGRAPH, '\n'.join(lines[:-2] + ["  a person's own draft"] + lines[-2:]) + '\n'),
        'rows-too-wide': ('X' * 221, rewrapped_ruler(width, width - 3 + 1)),
    }


CASES = ['other-word', 'fewer-words', 'more-words', 'rows-too-wide']


@pytest.mark.parametrize('width', [112, 80])
@pytest.mark.parametrize('case', CASES)
def test_a_box_that_is_not_the_message_wrapped_as_measured_does_not_match(width, case):
    reader = AntigravityHarness()
    payload, shown = spoiled(width)[case]
    screen = reader.screen(shown)
    assert screen.box is not None and screen.unknown is None
    assert not reader.box(payload).matches(screen.box, reader.box_columns(width), reader.wrap_slack)


@pytest.mark.parametrize('width', [112, 80])
def test_the_measured_layouts_still_match(width):
    """The control: the same reader matches the untouched fixtures, so a hold above is the box's doing."""
    reader = AntigravityHarness()
    for payload, name in ((PARAGRAPH, f'paragraph-{width}'), ('X' * 221, f'ruler-{width}')):
        screen = reader.screen(fixture(name))
        assert reader.box(payload).matches(screen.box, reader.box_columns(width), reader.wrap_slack)


@pytest.mark.parametrize('width', [112, 80])
@pytest.mark.parametrize('case', CASES)
def test_the_door_holds_a_box_that_is_not_the_message_and_never_types_again(tmp_path, monkeypatch, width, case):
    monkeypatch.setattr(Door.box_shows, '__defaults__', (0.3,))
    reader = AntigravityHarness()
    payload, shown = spoiled(width)[case]
    rule = '─' * width
    idle = f'{rule}\n>\n{rule}\n? for shortcuts       Gemini 4 Argon · medium\n'
    pane = SimpleNamespace(id='%1', pid=123, width=width, height=40, dead=False, in_mode=False)
    events = []

    class Composer:
        surface = idle

        def paste(self, pane_id, text, **_):
            events.append(('paste', pane_id, text))
            self.surface = shown

        def pane(self, pane_id):
            return pane

        def keys(self, pane_id, *keys):
            events.append(('keys', pane_id, keys))

    composer = Composer()
    seat = SimpleNamespace(
        name='probe', harness=reader, tmux=composer, folder=tmp_path / 'probe', trusted=tmp_path / 'probe',
        ship=SimpleNamespace(runtime=tmp_path / 'runtime'),
        session=SimpleNamespace(human=False), pool=SimpleNamespace(name='google'),
        spec=SeatSpec('probe', 'probe', 'worker', 'antigravity', ('google',)),
        pane=lambda: pane, read_pane=lambda: (pane, composer.surface),
        launched=lambda: None, record=lambda: None,
    )

    result = Door(seat).deliver(payload, confirm=False)

    assert result.result in ('blind', 'withheld'), result
    assert 'not the message' in result.detail or result.result == 'withheld'
    assert events == [('paste', '%1', payload)], events
    assert composer.surface == shown
