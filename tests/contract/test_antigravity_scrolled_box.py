"""Positive 1.5.9 viewport shapes from zero-turn synthetic pastes."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck.door import Door
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.ship import SeatSpec
from tests.antigravity_shapes import AntigravityShape


ROOT = Path(__file__).parents[1] / 'fixtures/antigravity/1.5.9'


@pytest.mark.parametrize('height', [12, 11])
def test_native_counted_bottom_window_is_submitted_once_and_record_confirms(tmp_path, height):
    example = json.loads((ROOT / f'scrolled-112x{height}.json').read_text())
    reader = AntigravityHarness()
    payload = example['payload']
    pane = SimpleNamespace(id='%1', pid=123, width=112, height=height, dead=False, in_mode=False)
    home = tmp_path / 'home'
    saved = {}
    events = []
    rule = '─' * 112
    idle = f'{rule}\n>\n{rule}\n? for shortcuts       Gemini Synthetic · medium\n'

    class Composer:
        surface = idle

        def paste(self, pane_id, text):
            events.append(('paste', pane_id, text))
            self.surface = example['capture']

        def pane(self, pane_id):
            return pane

        def keys(self, pane_id, *keys):
            events.append(('keys', pane_id, keys))
            shape = AntigravityShape(home, tmp_path, 'synthetic', 'Gemini Synthetic')
            shape.begin(payload, 'Gemini Synthetic', 'medium')
            saved['record'] = shape.path
            self.surface = idle

    composer = Composer()
    seat = SimpleNamespace(
        name='probe', harness=reader, tmux=composer, folder=tmp_path / 'probe', trusted=tmp_path / 'probe', home=home,
        ship=SimpleNamespace(runtime=tmp_path / 'runtime'),
        session=SimpleNamespace(human=False), pool=SimpleNamespace(name='google'),
        spec=SeatSpec('probe', 'probe', 'worker', 'antigravity', ('google',)),
        pane=lambda: pane, read_pane=lambda: (pane, composer.surface),
        launched=lambda: None, record=lambda: saved.get('record'),
    )
    door = Door(seat)
    shown = reader.screen(example['capture'])
    assert len(reader.box(payload).wrapped_rows(109, reader.wrap_slack)) == 8
    assert shown.box.splitlines()[0].rstrip() == f"↑ {example['hidden_rows']} more lines"
    assert door.matches(reader.box(payload), shown)

    result = door.deliver(payload)

    assert result.result == 'delivered' and 'exact payload in the record' in result.detail
    assert events == [('paste', '%1', payload), ('keys', '%1', ('Enter',))]
    assert [entry.text for entry in reader.inputs(saved['record'])] == [payload]
