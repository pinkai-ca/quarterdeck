"""Synthetic payloads measured in the native 0.166.0 composer; positive delivery contracts."""
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck.door import Door
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.ship import SeatSpec


ROOT = Path(__file__).parents[1] / 'fixtures/codex/0.166.0'
CASES = ['wrapped-112x42', 'wrapped-112x28', 'plain-112x28', 'plain-112x24']


@pytest.mark.parametrize('case', CASES)
def test_scrolled_22_line_paste_is_submitted_once_and_confirmed_by_record(tmp_path, case):
    example = json.loads((ROOT / f'scrolled-{case}.json').read_text())
    reader = CodexHarness()
    payload, width, height = example['payload'], example['width'], example['height']
    assert len(payload) == 839 and len(payload.splitlines()) == 22
    shown = reader.screen(example['capture'])
    assert len(shown.box.splitlines()) == example['visible_rows']
    assert shown.box.splitlines()[0].endswith('↑')
    original_box = shown.box
    pane = SimpleNamespace(id='%1', pid=123, width=width, height=height, dead=False, in_mode=False)
    record = tmp_path / 'synthetic.jsonl'
    events = []
    idle = '› Ask Codex to do anything\n\n  Synthetic-Model low · /synthetic/workspace\n'

    class Composer:
        surface = idle

        def paste(self, pane_id, text):
            events.append(('paste', pane_id, text))
            self.surface = example['capture']

        def pane(self, pane_id):
            return pane

        def keys(self, pane_id, *keys):
            events.append(('keys', pane_id, keys))
            row = dict(timestamp=datetime.now(timezone.utc).isoformat(), type='response_item',
                       payload=dict(type='message', role='user', content=[dict(type='input_text', text=payload)]))
            record.write_text(json.dumps(row) + '\n')
            self.surface = idle

    composer = Composer()
    seat = SimpleNamespace(
        name='probe', harness=reader, tmux=composer, folder=tmp_path / 'probe',
        home=tmp_path / 'probe/home', ship=SimpleNamespace(runtime=tmp_path / 'runtime'),
        session=SimpleNamespace(human=False), pool=SimpleNamespace(name='codex'),
        spec=SeatSpec('probe', 'probe', 'worker', 'codex', ('codex',)),
        pane=lambda: pane, read_pane=lambda: (pane, composer.surface),
        launched=lambda: None, record=lambda: record if record.exists() else None,
    )
    door = Door(seat)

    assert door.matches(reader.box(payload), shown)
    assert shown.box == original_box, 'the screen reading remains the native observation'
    result = door.deliver(payload)

    assert result.result == 'delivered'
    assert 'exact payload in the record' in result.detail
    assert events == [('paste', '%1', payload), ('keys', '%1', ('Enter',))]
    assert [entry.text for entry in reader.inputs(record)] == [payload]


@pytest.mark.parametrize('payload, columns, expected', [
    ('alpha beta gamma', 12, ['alpha beta', 'gamma']),
    ('header\n' + 'x' * 12 + '\nreply', 12, ['header', 'x' * 12, '', 'reply']),
    ('header\n' + 'x' * 13 + '\nreply', 12, ['header', 'x' * 12, 'x', 'reply']),
    ('qd > sender · synthetic\n\nreply', 109, ['qd > sender · synthetic', '', 'reply']),
])
def test_codex_rows_preserve_word_wrap_and_the_measured_exact_width_editor_row(payload, columns, expected):
    assert CodexHarness().box(payload).wrapped_rows(columns) == expected
