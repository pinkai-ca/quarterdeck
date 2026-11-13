"""Synthetic 1.5.5 surfaces based on private native check and ordinary queue captures."""

from pathlib import Path

from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.harness.types import Screen

ROOT = Path(__file__).parents[1] / 'fixtures/antigravity/1.5.5'


def test_current_version_fixture_set_and_known_footer_forms():
    reader = AntigravityHarness()
    assert {p.stem for p in ROOT.glob('*.txt')} == {
        'idle', 'working', 'working-queued', 'paste-shown', 'paste-check-folded',
        'idle-notice-80', 'idle-argon-medium-200'}
    shown = {'idle-notice-80': dict(model='Gemini 3.8 Flash', effort='low'),
             'idle-argon-medium-200': dict(model='Gemini 4 Argon', effort='medium')}
    for path in ROOT.glob('*.txt'):
        screen = reader.screen(path.read_text())
        assert screen.unknown is None and screen.interruption is None
        assert screen.shows == shown.get(path.stem, dict(model='Gemini 3.8 Flash', effort='medium'))
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


def test_measured_start_up_screens_hold_an_empty_box_under_the_notice_and_the_header():
    """Measured at 80 and 200 columns on 1.5.5 without a turn: the start-up header names the model with its tier,
    a dismissable notice sits above the box, and the footer shows the family and the tier apart."""
    reader = AntigravityHarness()
    for name in ('idle-notice-80', 'idle-argon-medium-200'):
        text = (ROOT/f'{name}.txt').read_text()
        screen = reader.screen(text)
        assert screen.box == '' and not screen.working and screen.echoes == ()
        assert 'esc to dismiss' in text and 'Antigravity CLI 1.5.5' in text
        assert '@example.invalid' in text and '/synthetic/workspace' in text, 'no real account or path'
    assert 'Gemini 3.8 Flash (Low)' in (ROOT/'idle-notice-80.txt').read_text()
    assert 'Gemini 4 Argon (Medium)' in (ROOT/'idle-argon-medium-200.txt').read_text()
