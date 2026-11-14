"""Known native notices keep their measured key without a composer underneath."""
from types import SimpleNamespace

import pytest

from quarterdeck import harness, pending
from quarterdeck.door import Door
from quarterdeck.harness.types import Screen, Interruption


@pytest.mark.parametrize('program,text,key', [
    ('codex', 'Select Model and Effort', 'Escape'),
    ('codex', 'Daybreak', 'Escape'),
    ('antigravity', 'Do you trust the contents of this project?', 'Enter'),
    ('grok', '┌─ Commands ─ [✗] ┐', 'Escape'),
    ('opencode', '┃ /models Switch model', 'Escape'),
    ('pi', 'Only showing models from configured providers', 'Escape'),
])
def test_known_notice_without_composer_is_dismissed(program, text, key, monkeypatch):
    adapter = harness.get(program)
    notice = adapter.recognize(text)
    assert notice is not None and notice.key == key
    shown = Screen(text, box=None, interruption=notice)
    assert shown.working is None
    calls = []
    seat = SimpleNamespace(harness=adapter, session=SimpleNamespace(human=False),
        tmux=SimpleNamespace(keys=lambda *args: calls.append(args)),
        pane=lambda: SimpleNamespace(id='%synthetic'))
    door = Door(seat)
    monkeypatch.setattr(door, 'screen', lambda: shown)
    monkeypatch.setattr(door, 'adapter_obstruction', lambda *args: None)
    monkeypatch.setattr(pending, 'held', lambda *args: False)
    monkeypatch.setattr('quarterdeck.door.time.sleep', lambda _: None)
    door.clear_known(shown)
    assert calls == [('%synthetic', key)]


def test_activity_restriction_is_declared_only_by_muse():
    assert harness.get('muse').dismissal_requires_activity
    assert all(not harness.get(name).dismissal_requires_activity
               for name in harness.NAMES if name != 'muse')
    door = Door(SimpleNamespace(harness=harness.get('muse'), tmux=None))
    notice = Interruption('synthetic notice', '', key='Escape')
    assert not door._dismissal_ready(Screen('', box=None, interruption=notice))
    assert not door._dismissal_ready(Screen('', box='', working=False, interruption=notice))
    assert door._dismissal_ready(Screen('', box='', working=True, interruption=notice))
