"""Hold gates for Antigravity 1.5.9's scrolled box: only the bottom window under its own counted mark row.

Law one-door: a row mark ('↑ N more lines') may stand for the omitted rows only when N is exactly the number
of the message's wrapped rows not shown and the rows below it are the message's last rows. Any other count,
window, word, gap or a missing mark is not the message: the door holds after one paste and never types again.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck.door import Door
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.ship import SeatSpec

ROOT = Path(__file__).parents[1] / 'fixtures/antigravity/1.5.9'
HEIGHTS = [12, 11]


def example(height):
    return json.loads((ROOT / f'scrolled-112x{height}.json').read_text())


def box_span(lines):
    rules = [i for i, line in enumerate(lines) if line.startswith('─') and set(line.strip()) == {'─'}]
    assert len(rules) >= 2, 'the fixture has no box between two rules'
    return rules[0] + 1, rules[1] - 1


def spoiled(capture, spoiler):
    lines = capture.split('\n')
    first, last = box_span(lines)
    mark = lines[first]
    assert mark.startswith('> ↑ ') and 'more lines' in mark, mark
    hidden = int(mark.split()[2])
    if spoiler == 'count-too-high':
        lines[first] = mark.replace(f'↑ {hidden} more', f'↑ {hidden + 1} more', 1)
    elif spoiler == 'count-too-low':
        lines[first] = mark.replace(f'↑ {hidden} more', f'↑ {hidden - 1} more', 1) if hidden > 1 else \
            mark.replace(f'↑ {hidden} more', '↑ 0 more', 1)
    elif spoiler == 'no-mark':
        lines[first] = '> ' + lines[first + 1][2:]
    elif spoiler == 'other-word':
        row = lines[first + 2]
        lines[first + 2] = row[:2] + ('Q' if row[2] != 'Q' else 'Z') + row[3:]
    elif spoiler == 'a-gap':
        del lines[first + 2]
        lines[first] = mark.replace(f'↑ {hidden} more', f'↑ {hidden + 1} more', 1)
    elif spoiler == 'last-row-missing':
        del lines[last]
        lines[first] = mark.replace(f'↑ {hidden} more', f'↑ {hidden + 1} more', 1)
    elif spoiler == 'mark-below':
        lines.insert(last + 1, lines[first].replace('> ', '  ', 1))
        lines[first] = '> ' + lines[first + 1][2:]
        del lines[first + 1]
    else:
        raise AssertionError(spoiler)
    result = '\n'.join(lines)
    assert result != capture, spoiler
    return result


SPOILERS = ['count-too-high', 'count-too-low', 'no-mark', 'other-word', 'a-gap', 'last-row-missing', 'mark-below']


def seat_for(reader, height, after_paste, tmp_path, events):
    pane = SimpleNamespace(id='%1', pid=123, width=112, height=height, dead=False, in_mode=False)
    rule = '─' * 112
    idle = f'{rule}\n>\n{rule}\n? for shortcuts       Gemini Synthetic · medium\n'

    class Composer:
        surface = idle

        def paste(self, pane_id, text, **_):
            events.append(('paste', pane_id, text))
            self.surface = after_paste

        def pane(self, pane_id):
            return pane

        def keys(self, pane_id, *keys):
            events.append(('keys', pane_id, keys))

    composer = Composer()
    seat = SimpleNamespace(
        name='probe', harness=reader, tmux=composer, folder=tmp_path / 'probe', trusted=tmp_path / 'probe', home=tmp_path / 'home',
        ship=SimpleNamespace(runtime=tmp_path / 'runtime'),
        session=SimpleNamespace(human=False), pool=SimpleNamespace(name='google'),
        spec=SeatSpec('probe', 'probe', 'worker', 'antigravity', ('google',)),
        pane=lambda: pane, read_pane=lambda: (pane, composer.surface),
        launched=lambda: None, record=lambda: None,
    )
    return seat, composer


@pytest.mark.parametrize('height', HEIGHTS)
def test_the_measured_counted_windows_still_match(tmp_path, height):
    data, reader = example(height), AntigravityHarness()
    seat, _ = seat_for(reader, height, data['capture'], tmp_path, [])
    assert Door(seat).matches(reader.box(data['payload']), reader.screen(data['capture']))


@pytest.mark.parametrize('height', HEIGHTS)
@pytest.mark.parametrize('spoiler', SPOILERS)
def test_a_counted_window_that_is_not_the_message_s_does_not_match(tmp_path, height, spoiler):
    data, reader = example(height), AntigravityHarness()
    shown = spoiled(data['capture'], spoiler)
    seat, _ = seat_for(reader, height, shown, tmp_path, [])
    assert not Door(seat).matches(reader.box(data['payload']), reader.screen(shown))


@pytest.mark.parametrize('height', HEIGHTS)
def test_another_message_whose_last_rows_differ_does_not_match(tmp_path, height):
    data, reader = example(height), AntigravityHarness()
    payload = data['payload']
    other = payload[:-1] + ('Q' if payload[-1] != 'Q' else 'Z')  # the last visible cell differs
    seat, _ = seat_for(reader, height, data['capture'], tmp_path, [])
    assert not Door(seat).matches(reader.box(other), reader.screen(data['capture']))


@pytest.mark.parametrize('height', HEIGHTS)
@pytest.mark.parametrize('spoiler', SPOILERS)
def test_the_door_holds_a_counted_window_that_is_not_the_message_and_never_types_again(
        tmp_path, monkeypatch, height, spoiler):
    monkeypatch.setattr(Door.box_shows, '__defaults__', (0.3,))
    data, reader = example(height), AntigravityHarness()
    shown = spoiled(data['capture'], spoiler)
    events = []
    seat, composer = seat_for(reader, height, shown, tmp_path, events)

    result = Door(seat).deliver(data['payload'], confirm=False)

    assert result.result in ('blind', 'withheld'), result
    assert events == [('paste', '%1', data['payload'])], events
    assert composer.surface == shown
