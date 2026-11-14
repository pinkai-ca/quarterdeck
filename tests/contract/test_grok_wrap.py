"""Synthetic drafts preserve native 1.0.77 wrapping at measured widths and states."""

import json
from pathlib import Path

import pytest

from quarterdeck.harness.grok import GrokHarness

FIXTURES = Path(__file__).parents[1] / 'fixtures/grok/1.0.77'
SAMPLES = json.loads((FIXTURES / 'wrap-punctuation.json').read_text())['samples']


@pytest.mark.parametrize('sample', SAMPLES, ids=lambda sample: sample['name'])
def test_measured_punctuation_wrap_preserves_the_complete_payload(sample):
    harness = GrokHarness()
    screen = harness.screen('\n'.join(sample['lines']) + '\n')
    payload, columns = sample['payload'], harness.box_columns(sample['width'])
    assert screen.unknown is None and screen.interruption is None
    assert screen.working is sample['working']
    assert harness.box(payload).matches(screen.box, columns)
    for different in ('different first line\n' + payload, payload + '!', 'Q' + payload[1:]):
        assert not harness.box(different).matches(screen.box, columns)
    # Short rows are wrapping only at their measured width, not at any newline.
    assert not harness.box(payload).matches(screen.box, columns + 1000)


def test_recovered_padded_box_needs_its_slash_break_and_explicit_hard_newlines():
    sample = json.loads((FIXTURES / 'padded-wrap.json').read_text())
    harness = GrokHarness()
    payload, box = sample['payload'], sample['box']
    columns = harness.box_columns(sample['pane_columns'])
    assert columns == sample['box_columns']
    assert [len(row) for row in box.split('\n')] == [columns + 1] * 6
    assert len(payload) == 1307
    assert harness.box(payload).matches(box, columns)
    assert not harness.box(payload.replace('\n', ' ')).matches(box, columns)
    assert not harness.box(payload.replace('/', ' ', 1)).matches(box, columns)
    assert not harness.box('different header\n' + payload.split('\n', 1)[1]).matches(box, columns)


VIEWPORTS = json.loads((FIXTURES / 'scrolled-viewport.json').read_text())['samples']


@pytest.mark.parametrize('sample', VIEWPORTS, ids=lambda sample: sample['name'])
def test_each_scrolled_view_exposes_only_part_of_the_draft(sample):
    harness = GrokHarness()
    screen = harness.screen('\n'.join(sample['lines']) + '\n')
    assert screen.box is None and screen.unknown
    assert not harness.box(sample['payload']).matches(screen.box, harness.box_columns(sample['width']))
    assert len([line for line in screen.text.split('\n') if line.startswith('  │')]) == 15
    assert ('Synthetic viewport header.' in screen.text) == sample['name'].endswith('-home')
    if 'viewport3' in sample['name']:
        assert ('Synthetic ending.' in screen.text) != sample['name'].endswith('-home')
