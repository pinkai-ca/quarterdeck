"""Synthetic 1.5.5 surfaces based on private native check and ordinary queue captures."""

from pathlib import Path

from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.harness.types import Screen

ROOT = Path(__file__).parents[1] / 'fixtures/antigravity/1.5.5'


def test_current_version_fixture_set_and_known_footer_forms():
    reader = AntigravityHarness()
    assert {p.stem for p in ROOT.glob('*.txt')} == {
        'idle', 'working', 'working-queued', 'paste-shown', 'paste-check-folded'}
    for path in ROOT.glob('*.txt'):
        screen = reader.screen(path.read_text())
        assert screen.unknown is None and screen.interruption is None
        assert screen.shows == dict(model='Gemini 3.8 Flash', effort='medium')
    assert reader.screen((ROOT/'idle.txt').read_text()).box == ''
    assert reader.screen((ROOT/'working.txt').read_text()).working
    assert reader.screen((ROOT/'paste-shown.txt').read_text()).box == 'Synthetic paste\nsecond line'


def test_current_check_multiline_fold_and_queue_preview_remain_distinct():
    reader = AntigravityHarness()
    text = '\n'.join(['Synthetic check content']*43)
    folded = reader.screen((ROOT/'paste-check-folded.txt').read_text())
    assert reader.box(text).matches(folded.box)
    queued = reader.screen((ROOT/'working-queued.txt').read_text())
    wanted = 'qd > order from issuer · ord-1234 · Synthetic task\nSynthetic body'
    assert reader.queued_input(wanted, Screen('matched paste', wanted, working=True), queued)
    assert not reader.queued_input(wanted.replace('ord-1234','ord-5678'), Screen('', wanted), queued)
    assert not reader.box(wanted).matches(queued.box)
