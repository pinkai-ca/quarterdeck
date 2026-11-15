"""Hold gates for a box the program scrolls: only the message's own anchored window, under its mark, is submitted.

Law one-door: the door types once, checks the box as the program shows it, and submits only what it
can match. A scrolled box may count as the message only when its visible rows are the one contiguous
window of the message, as the program wraps it, at the declared end, with the program's own mark in
its declared place. A box showing any other window, other words, a gap, no mark, a mark inside the
text, or more rows than the pane holds is not the message: the door holds after one paste, without
Enter, and never types again. An adapter that declares no scroll gains no scrolled match at all.
"""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck.door import Door
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.ship import SeatSpec

ROOT = Path(__file__).parents[1] / 'fixtures/codex/0.166.0'
CASES = ['wrapped-112x42', 'wrapped-112x28', 'plain-112x28', 'plain-112x24']


def example(case):
    return json.loads((ROOT / f'scrolled-{case}.json').read_text())


def box_rows(capture):
    """Indexes of the capture's box rows: from the prompt row to the row before the first blank one."""
    lines = capture.split('\n')
    first = next(i for i, line in enumerate(lines) if line.startswith('› '))
    last = next(i for i in range(first, len(lines)) if not lines[i].strip()) - 1
    return lines, first, last


def changed(capture, edit):
    lines, first, last = box_rows(capture)
    result = edit(lines, first, last)
    assert result != capture.split('\n'), 'the spoiling edit changed nothing'
    return '\n'.join(result)


def other_word(lines, first, last):
    lines = list(lines)
    row = first + 2
    text = lines[row]
    at = len(text) - len(text[2:].lstrip()) if text[2:].strip() else None
    assert at is not None, 'the third box row holds no text'
    lines[row] = text[:at] + ('Q' if text[at] != 'Q' else 'Z') + text[at + 1:]
    return lines


def window_one_row_up(lines, first, last):
    """The rows shown are one earlier than the bottom: the last message row is missing."""
    lines = list(lines)
    body = [line[2:] for line in lines[first:last + 1]]
    mark = body[0][-1]
    body[0] = body[0][:-1]
    shifted = [body[0].replace(body[0].strip(), 'R08xxx', 1)] + body[:-1]
    shifted[0] = shifted[0].rstrip().ljust(len(body[0])) + mark
    prefixes = ['› '] + ['  '] * (len(shifted) - 1)
    lines[first:last + 1] = [p + row for p, row in zip(prefixes, shifted)]
    return lines


def a_gap(lines, first, last):
    lines = list(lines)
    del lines[first + 3]
    return lines


def no_mark(lines, first, last):
    lines = list(lines)
    lines[first] = lines[first].replace('↑', ' ')
    return lines


def mark_inside_the_text(lines, first, last):
    lines = list(lines)
    row = lines[first].replace('↑', ' ')
    at = len(row.rstrip()) + 2
    lines[first] = row[:at] + '↑' + row[at + 1:]
    return lines


def a_person_s_draft_below(lines, first, last):
    lines = list(lines)
    lines.insert(last + 1, "  a person's own draft")
    return lines


SPOILERS = {
    'other-word': other_word,
    'window-one-row-up': window_one_row_up,
    'a-gap': a_gap,
    'no-mark': no_mark,
    'mark-inside-the-text': mark_inside_the_text,
    'a-person-s-draft-below': a_person_s_draft_below,
}


def seat_for(reader, width, height, surface_after_paste, tmp_path, events):
    pane = SimpleNamespace(id='%1', pid=123, width=width, height=height, dead=False, in_mode=False)
    idle = '› Ask Codex to do anything\n\n  Synthetic-Model low · /synthetic/workspace\n'

    class Composer:
        surface = idle

        def paste(self, pane_id, text, **_):
            events.append(('paste', pane_id, text))
            self.surface = surface_after_paste

        def pane(self, pane_id):
            return pane

        def keys(self, pane_id, *keys):
            events.append(('keys', pane_id, keys))

    composer = Composer()
    seat = SimpleNamespace(
        name='probe', harness=reader, tmux=composer, folder=tmp_path / 'probe', trusted=tmp_path / 'probe',
        home=tmp_path / 'probe/home', ship=SimpleNamespace(runtime=tmp_path / 'runtime'),
        session=SimpleNamespace(human=False), pool=SimpleNamespace(name='codex'),
        spec=SeatSpec('probe', 'probe', 'worker', 'codex', ('codex',)),
        pane=lambda: pane, read_pane=lambda: (pane, composer.surface),
        launched=lambda: None, record=lambda: None,
    )
    return seat, composer


@pytest.mark.parametrize('case', CASES)
def test_the_measured_scrolled_windows_still_match(tmp_path, case):
    """The control: the untouched captures match, so every hold below is the spoiled box's doing."""
    data, reader = example(case), CodexHarness()
    seat, _ = seat_for(reader, data['width'], data['height'], data['capture'], tmp_path, [])
    assert Door(seat).matches(reader.box(data['payload']), reader.screen(data['capture']))


@pytest.mark.parametrize('case', CASES)
@pytest.mark.parametrize('spoiler', sorted(SPOILERS))
def test_a_scrolled_box_that_is_not_the_message_s_window_does_not_match(tmp_path, case, spoiler):
    data, reader = example(case), CodexHarness()
    shown = changed(data['capture'], SPOILERS[spoiler])
    seat, _ = seat_for(reader, data['width'], data['height'], shown, tmp_path, [])
    screen = reader.screen(shown)
    assert not Door(seat).matches(reader.box(data['payload']), screen)


@pytest.mark.parametrize('case', CASES)
def test_another_message_whose_last_rows_differ_does_not_match(tmp_path, case):
    data, reader = example(case), CodexHarness()
    lines = data['payload'].split('\n')
    lines[-1] = lines[-1].replace('R', 'Q', 1)
    seat, _ = seat_for(reader, data['width'], data['height'], data['capture'], tmp_path, [])
    assert not Door(seat).matches(reader.box('\n'.join(lines)), reader.screen(data['capture']))


@pytest.mark.parametrize('case', CASES)
def test_a_viewport_taller_than_the_pane_does_not_match(tmp_path, case):
    data, reader = example(case), CodexHarness()
    rows = len(reader.screen(data['capture']).box.splitlines())
    seat, _ = seat_for(reader, data['width'], rows - 1, data['capture'], tmp_path, [])
    assert not Door(seat).matches(reader.box(data['payload']), reader.screen(data['capture']))


@pytest.mark.parametrize('case', CASES)
def test_an_adapter_that_declares_no_scroll_gains_no_scrolled_match(tmp_path, case):
    data = example(case)

    class Undeclared(CodexHarness):
        box_scroll = None

    reader = Undeclared()
    seat, _ = seat_for(reader, data['width'], data['height'], data['capture'], tmp_path, [])
    assert not Door(seat).matches(reader.box(data['payload']), reader.screen(data['capture']))


@pytest.mark.parametrize('case', CASES)
def test_a_top_anchor_does_not_accept_a_bottom_window(tmp_path, case):
    data = example(case)

    class TopAnchored(CodexHarness):
        box_scroll = replace(CodexHarness.box_scroll, anchor='top')

    reader = TopAnchored()
    seat, _ = seat_for(reader, data['width'], data['height'], data['capture'], tmp_path, [])
    assert not Door(seat).matches(reader.box(data['payload']), reader.screen(data['capture']))


@pytest.mark.parametrize('case', ['wrapped-112x28', 'plain-112x24'])
@pytest.mark.parametrize('spoiler', sorted(SPOILERS))
def test_the_door_holds_a_scrolled_box_that_is_not_the_message_and_never_types_again(
        tmp_path, monkeypatch, case, spoiler):
    monkeypatch.setattr(Door.box_shows, '__defaults__', (0.3,))
    data, reader = example(case), CodexHarness()
    shown = changed(data['capture'], SPOILERS[spoiler])
    events = []
    seat, composer = seat_for(reader, data['width'], data['height'], shown, tmp_path, events)

    result = Door(seat).deliver(data['payload'], confirm=False)

    assert result.result in ('blind', 'withheld'), result
    assert events == [('paste', '%1', data['payload'])], events
    assert composer.surface == shown
