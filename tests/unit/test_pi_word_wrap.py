"""Scrubbed 0.74.2 evidence: pane 348, box 347, next word 33 cells."""

from types import SimpleNamespace
from datetime import timedelta
import json

import pytest

from quarterdeck.door import Door
from quarterdeck import post
from quarterdeck.harness.types import Box
from quarterdeck.harness.pi import PiHarness
from tests.conftest import FIXTURES

BODY = 'In /srv/ship/work/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx run exactly one final full suite on the committed correction: .venv/bin/python -m pytest -q -rs -n 8 > work/full-suite-card-recovery.log 2>&1. First report git rev-parse HEAD. No other suite is running; Pilot owns the sole slot. Report command exit status and raw tail -n 120 work/full-suite-card-recovery.log including summary. No edits, installs, live runtime inspection, repairs, retry, restart or upgrade. Focused lifecycle/standing/service/guards/private-tmux checks pass 47 tests. Done is the raw final result.'
PAYLOAD = "qd > order from pilot · ord-1234 · re ord-5678\n" + BODY + "\nqd > reply: qd post report --re ord-1234 · inbox: post/inbox/samplehand.md\n"


def test_observed_greedy_wrap_of_33_character_word_in_347_column_box():
    harness=PiHarness()
    screen=harness.screen((FIXTURES/'pi/0.74.2/paste-greedy-348.txt').read_text())
    assert len(BODY)==568 and len('work/full-suite-card-recovery.log')==33
    assert len(screen.box.splitlines()[1].rstrip())==327
    seat=SimpleNamespace(harness=harness,tmux=None,pane=lambda:SimpleNamespace(width=348))
    assert Door(seat).matches(harness.box(PAYLOAD),screen)


@pytest.mark.parametrize('text,columns,shown', [
    # Literal rows from installed pi-tui wordWrapLine; no model or pane involved.
    ('first work/full-suite-card.log', 20, 'first\nwork/full-suite-card\n.log'),
    ('abc abcdefghijklmnop tail', 10, 'abc\nabcdefghij\nklmnop\ntail'),
    ('alpha beta gamma', 10, 'alpha\nbeta gamma'),
    ('alpha  beta gamma', 10, 'alpha\nbeta gamma'),
    ('xx abcdefghijklmnop', 10, 'xx\nabcdefghij\nklmnop'),
    ('one-two-three', 10, 'one-two-th\nree'),
    ('alpha-beta tail', 10, 'alpha-beta\n tail'),
])
def test_source_established_greedy_and_hard_word_breaks(text, columns, shown):
    box = PiHarness().box(text)
    assert box.matches(shown, columns)
    assert box.matches(shown.replace('\n', '   \n'), columns), 'painted right padding'


@pytest.mark.parametrize('text,columns,shown', [
    ('one-two-three', 10, 'one-two-\nthree'),  # hyphens are not pi break opportunities
    ('alpha beta gamma', 10, 'alpha beta\ngamma'),  # overflowing space backtracks
    ('alpha beta gamma', 20, 'alpha\nbeta gamma'),  # next word fits
    ('abc abcdefghijklmnop tail', 10, 'abc\nabcdefghi\njklmnop\ntail'),
    ('alpha  beta gamma', 10, 'alpha\nbeta  gamma'),  # changed visible spaces
    ('alpha beta gamma', 10, 'alpha\nbeta_gamma'),
    ('alpha beta gamma', 10, 'alpha\nbeta gam'),
    ('alpha\nbeta gamma', 10, 'alpha beta\ngamma'),  # explicit newline is not a wrap
    ('abc\tdefghijk', 5, 'abc\t\ndefgh\nijk'),
    ('e\u0301abcdefgh', 5, 'e\u0301abcd\nefgh'),
    ('🧑\u200d💻abcdef', 5, '🧑\u200d💻abc\ndef'),
])
def test_early_breaks_edits_and_unestablished_grapheme_layout_are_refused(text, columns, shown):
    assert not PiHarness().box(text).matches(shown, columns)


@pytest.mark.parametrize('columns', [None, 0, -1])
def test_wrapped_text_requires_a_supported_width(columns):
    assert not PiHarness().box('one-two-three').matches('one-two-th\nree', columns)


def test_pi_rule_is_opt_in_and_folding_still_names_this_payload():
    assert Box('one-two-three').matches('one-two-\nthree', 10), 'existing generic hyphen rule is unchanged'
    assert not PiHarness().box('one-two-three').matches('one-two-\nthree', 10)
    text = 'long ' * 250
    box = PiHarness().box(text)
    assert box.matches(f'[paste #1 {len(text)} chars]')
    assert not box.matches(f'[paste #1 {len(text) - 1} chars]')


@pytest.mark.parametrize('receipt,result', [('exact', 'delivered'), ('stale', 'blind'),
                                          ('edited', 'blind'), ('missing', 'blind')])
def test_observed_box_verification_never_replaces_a_fresh_exact_receipt(tmp_path, receipt, result):
    harness = PiHarness()
    screen = harness.screen((FIXTURES / 'pi/0.74.2/paste-greedy-348.txt').read_text())
    record = tmp_path / 'synthetic.jsonl'
    at = post.now()
    text = PAYLOAD.replace('final full suite', 'final  full suite') if receipt == 'edited' else PAYLOAD
    stamp = at - timedelta(seconds=1) if receipt == 'stale' else at
    record.write_text('' if receipt == 'missing' else json.dumps({
        'type': 'message', 'timestamp': stamp.isoformat(),
        'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]}}) + '\n')
    seat = SimpleNamespace(harness=harness, tmux=None, pane=lambda: SimpleNamespace(width=348),
                           record=lambda: record)
    door = Door(seat)
    door.since = at
    assert door.matches(harness.box(PAYLOAD), screen)
    assert door.confirm(PAYLOAD, seconds=0).result == result
