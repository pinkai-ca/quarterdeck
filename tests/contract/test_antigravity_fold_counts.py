"""Measured 1.5.9 paste counters and the earlier compatible counted forms."""
import json
from pathlib import Path

import pytest

from quarterdeck.harness.antigravity import AntigravityHarness


READINGS = json.loads((Path(__file__).parents[1] /
                      'fixtures/antigravity/1.5.9/fold-readings.json').read_text())
CASES = {value['name']: value for value in READINGS['cases']}
FOLDED = [value for value in READINGS['cases'] if value['folded']]


@pytest.mark.parametrize('case', FOLDED, ids=lambda case: case['name'])
def test_measured_native_fold_counter_matches_without_changing_payload(case):
    counter = case['counter']
    label = f"+{counter['lines']} lines" if 'lines' in counter else f"{counter['chars']} chars"
    box = AntigravityHarness().box(case['payload'])

    assert box.matches(f'[Pasted text #7 {label}]')
    assert box.text == case['payload']


@pytest.mark.parametrize('name, new_counter', [
    ('lines-1', '+1 lines'),
    ('lines-2', '+2 lines'),
    ('boundary-15-trail-0', '+15 lines'),
    ('extra-long-15-lines', '+15 lines'),
    ('extra-long-two-lines', '+2 lines'),
])
def test_expanded_boundary_cases_do_not_acquire_an_unconditional_line_plus_one_form(name, new_counter):
    case = CASES[name]
    assert not case['folded']  # these native pastes stayed expanded, including the long multiline controls
    assert not AntigravityHarness().box(case['payload']).matches(f'[Pasted text #7 {new_counter}]')


@pytest.mark.parametrize('name, wrong_counter', [
    ('boundary-22-trail-0', '+20 lines'),
    ('boundary-22-trail-0', '+23 lines'),
    ('boundary-22-trail-1', '+21 lines'),
    ('boundary-22-trail-1', '+23 lines'),
    ('two-trailing-newlines', '+16 lines'),
    ('two-trailing-newlines', '+19 lines'),
    ('extra-long-one-two-trailing-newlines', '1199 chars'),
    ('extra-long-one-two-trailing-newlines', '1202 chars'),
    ('unicode-unicode-1001-two-newlines', '2002 chars'),
])
def test_counters_outside_the_measured_and_legacy_sizes_are_not_accepted(name, wrong_counter):
    assert not AntigravityHarness().box(CASES[name]['payload']).matches(f'[Pasted text #7 {wrong_counter}]')


def test_legacy_newline_and_one_terminal_lf_character_counts_are_retained():
    reader = AntigravityHarness()
    payload = CASES['boundary-22-trail-0']['payload']
    assert reader.box(payload).matches('[Pasted text #7 +21 lines]')
    assert reader.box(payload).matches('[Pasted text #7 +22 lines]')
    payload = CASES['extra-long-one-two-trailing-newlines']['payload']
    assert reader.box(payload).matches('[Pasted text #7 1201 chars]')
    assert reader.box(payload).matches('[Pasted text #7 1200 chars]')


def test_new_character_counter_is_not_added_at_the_unfolded_length_boundary():
    reader = AntigravityHarness()
    payload = 'x' * 1000 + '\n\n'
    assert reader.box(payload).matches('[Pasted text #7 1001 chars]')  # earlier measured form
    assert not reader.box(payload).matches('[Pasted text #7 1000 chars]')
