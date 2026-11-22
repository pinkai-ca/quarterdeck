"""Claude Code's bottom line and working sign, as the idle proof's second witness reads them.

The original forms were measured on the ship's own seats on 15 November 2026, through `qd --json watch`: 2.1.317 on the
crew's and the office's seats, 2.1.325 on the desk's, where a helper agent was started in the background to see its
panel appear below the line and go when it ended; the same was then captured for 2.1.317 on a throwaway seat by a
builder, where no seat of the ship had ever run one. The screens here are synthetic copies of those shapes: the
composer's rules, its box, a status row and the bottom line, with synthetic words in place of every reply and name.

A form is read whole and only on the version it was measured on. Anything else says nothing: the proof then has
no second witness in the screen, and never a guessed one (law: record-first).
"""

import pytest

from quarterdeck.harness.claude import BOTTOM_LINES, ClaudeHarness

from tests.conftest import FIXTURES

RULE = '─' * 100
MODE = '  ⏵⏵ bypass permissions on'
FEEDBACK_DRAFT = '  ⏵⏵ bypass permissions on (shift+tab to cycle) · 1 feedback draft'
SHELL_AND_FEEDBACK_DRAFT = '  ⏵⏵ bypass permissions on · 1 shell · 1 feedback draft'


def fixture(version, name):
    return ClaudeHarness().screen((FIXTURES / 'claude' / version / f'{name}.txt').read_text())


def drawn(bottom, *below, box='❯\xa0', sign=None):
    """A composer at rest above ``bottom``; ``below`` is what the program draws under its bottom line."""
    lines = ['', '● synthetic reply cedar', '', *([sign, ''] if sign else []), RULE, box, RULE,
             '  Seat Cedar  role · Model 0.0 · effort · context 5%', bottom, *below]
    return ClaudeHarness().screen('\n'.join(lines) + '\n')


@pytest.mark.parametrize(('version', 'name', 'names'), [
    ('2.1.317', 'rest-nothing-running', ()),
    ('2.1.317', 'rest-one-shell', (('shell', 1),)),
    ('2.1.317', 'rest-two-shells', (('shell', 2),)),
    ('2.1.317', 'rest-one-shell-one-monitor', (('shell', 1), ('monitor', 1))),
    ('2.1.325', 'rest-nothing-running-page', ()),
    ('2.1.325', 'rest-one-shell-page', (('shell', 1),)),
])
def test_each_measured_bottom_line_names_what_runs_in_the_background(version, name, names):
    screen = fixture(version, name)
    assert screen.box == '' and screen.working is False, 'the composer at rest'
    assert ClaudeHarness().background(screen, version) == names


def test_one_feedback_draft_with_the_cycling_hint_leaves_the_composer_at_rest():
    screen = drawn(FEEDBACK_DRAFT)
    assert screen.box == '' and screen.working is False
    assert ClaudeHarness().background(screen, '2.1.317') == ()


def test_one_feedback_draft_with_one_shell_reports_the_shell():
    screen = drawn(SHELL_AND_FEEDBACK_DRAFT)
    assert screen.box == '' and screen.working is False
    assert ClaudeHarness().background(screen, '2.1.317') == (('shell', 1),)


def test_a_panel_below_the_line_is_not_a_measured_rest():
    """Both versions list a helper agent running in the background under the bottom line: the line is not the last.
    On 2.1.325 the line itself keeps its count of shells; on 2.1.317 it gains a hint, measured on a throwaway seat."""
    for version in ('2.1.317', '2.1.325'):
        screen = fixture(version, 'rest-agent-panel')
        assert screen.box == '' and ClaudeHarness().background(screen, version) is None, version


@pytest.mark.parametrize('name', [
    'rest-agent-ended-hint',  # for some thirty seconds after a helper ended: no panel, a hint beside the line
    'rest-agents-hint',       # for the first seconds after a launch, with no helper at all
])
def test_a_hint_beside_the_line_on_2_1_317_is_neither_work_nor_rest(name):
    screen = fixture('2.1.317', name)
    assert screen.box == '' and screen.working is False
    assert ClaudeHarness().background(screen, '2.1.317') is None


@pytest.mark.parametrize(('version', 'bottom'), [
    # a form of the other version, or of none measured
    ('2.1.325', MODE + ' (shift+tab to cycle)'),
    ('2.1.325', MODE + ' · 1 shell'),
    ('2.1.325', MODE + ' · 1 shell, 1 monitor · ⧉ cedar'),
    ('2.1.317', MODE + ' · 1 shell · ⧉ cedar'),
    ('2.1.317', MODE + ' (shift+tab to cycle) · ⧉ cedar'),
    ('2.1.322', MODE + ' (shift+tab to cycle)'),
    ('2.1.326', MODE + ' · 1 shell · ⧉ cedar'),
    (None, MODE + ' (shift+tab to cycle)'),
    # a count and a noun that do not agree, and a count of none
    ('2.1.317', MODE + ' · 1 shells'),
    ('2.1.317', MODE + ' · 2 shell'),
    ('2.1.317', MODE + ' · 0 shells'),
    ('2.1.317', MODE + ' · 01 shell'),
    # kinds and counts not measured
    ('2.1.317', MODE + ' · 1 monitor'),
    ('2.1.317', MODE + ' · 1 shell, 2 monitors'),
    ('2.1.317', MODE + ' · 1 shell, 1 agent'),
    ('2.1.317', MODE + ' · 1 agent'),
    ('2.1.317', MODE + ' · 1 shell, 1 monitor, 1 agent'),
    # only the measured feedback draft footer, with its original spacing
    ('2.1.325', FEEDBACK_DRAFT),
    ('2.1.325', FEEDBACK_DRAFT + ' · ⧉ cedar'),
    ('2.1.322', FEEDBACK_DRAFT),
    ('2.1.317', FEEDBACK_DRAFT[1:]),
    ('2.1.317', FEEDBACK_DRAFT.replace(' · ', '  · ')),
    ('2.1.317', FEEDBACK_DRAFT.replace('1 feedback draft', '2 feedback drafts')),
    ('2.1.317', MODE + ' · 1 feedback draft'),
    ('2.1.317', MODE + ' · 1 shell, 1 monitor, 1 feedback draft'),
    ('2.1.317', FEEDBACK_DRAFT + ' · ← for agents'),
    ('2.1.317', MODE + ' · 2 shells · 1 feedback draft'),
    ('2.1.317', MODE + ' · 1 shell, 1 monitor · 1 feedback draft'),
    ('2.1.317', MODE + ' · 1 shell · 2 feedback drafts'),
    ('2.1.317', MODE + ' · 1 shell, 1 feedback draft'),
    ('2.1.317', SHELL_AND_FEEDBACK_DRAFT[1:]),
    ('2.1.322', SHELL_AND_FEEDBACK_DRAFT),
    ('2.1.325', SHELL_AND_FEEDBACK_DRAFT),
    (None, SHELL_AND_FEEDBACK_DRAFT),
    # hints the program draws beside or in place of the line
    ('2.1.317', MODE + ' (shift+tab to cycle) · ← for agents'),
    ('2.1.317', MODE + ' (shift+tab to cycle) · esc to interrupt'),
    ('2.1.317', MODE + ' (shift+tab to cycle) · ↓ to manage'),
    ('2.1.317', MODE + ' (shift+tab to cycle) · /tasks to see subagents'),
    ('2.1.317', MODE),
    ('2.1.317', '  paste again to expand'),
    ('2.1.317', '  ? for shortcuts'),
    # another mode, another indent, text after or before the form
    ('2.1.317', '  ⏵⏵ auto mode on (shift+tab to cycle)'),
    ('2.1.317', '  ⏸ plan mode on (shift+tab to cycle)'),
    ('2.1.317', ' ⏵⏵ bypass permissions on (shift+tab to cycle)'),
    ('2.1.317', MODE + ' (shift+tab to cycle) cedar'),
    ('2.1.317', 'cedar' + MODE + ' (shift+tab to cycle)'),
    ('2.1.317', MODE + ' · 1 shell…'),
])
def test_any_other_bottom_line_says_nothing(version, bottom):  # law: record-first
    screen = drawn(bottom)
    assert screen.box == '', 'the composer itself is read'
    assert ClaudeHarness().background(screen, version) is None


def test_the_line_must_be_the_last_the_screen_draws_and_under_a_read_composer():
    harness = ClaudeHarness()
    rest = MODE + ' (shift+tab to cycle)'
    assert harness.background(drawn(rest, '', '   '), '2.1.317') == (), 'blank rows below are not a panel'
    assert harness.background(drawn(rest, '', '  cedar'), '2.1.317') is None, 'anything drawn below it'
    assert harness.background(drawn(rest, rest), '2.1.317') == (), 'only the last line is read'
    assert harness.background(drawn(MODE + ' · 1 shell', rest), '2.1.317') == ()
    assert harness.background(drawn(rest, MODE + ' · 2 shells'), '2.1.317') == (('shell', 2),)
    unread = drawn(rest, box='cedar with no composer mark')
    assert unread.box is None and harness.background(unread, '2.1.317') is None, 'no composer, no witness'


def test_the_measured_forms_are_an_exact_set_per_version():
    assert set(BOTTOM_LINES) == {'2.1.317', '2.1.325'}


@pytest.mark.parametrize(('version', 'name'), [
    ('2.1.317', 'working-under-a-minute'),  # the glyph ✽, measured on the engineer's seat
    ('2.1.317', 'working-minutes'),         # minutes lead the time past the first one
    ('2.1.325', 'working-minutes'),
    ('2.1.325', 'working-hours'),           # and hours past the first hour, measured on the desk's seat
])
def test_the_working_sign_is_read_past_the_first_minute_and_with_every_measured_glyph(version, name):
    screen = fixture(version, name)
    assert screen.box == '' and screen.working is True


@pytest.mark.parametrize('sign', [
    '· Pondering… (8m 52s · ↓ 41.7k tokens)',
    '✻ Pondering… (17m 50s · ↓ 70.3k tokens)',
    '✢ Pondering… (13m 3s · ↓ 48.3k tokens)',
    '✶ Pondering… (3s · ↓ 125 tokens)',
    '· Pondering… (1h 1m 1s · ↓ 237.2k tokens)',
    '* Pondering… (1h 1m 21s · ↓ 237.2k tokens)',
])
def test_each_measured_sign_reads_as_working(sign):
    assert drawn(MODE + ' (shift+tab to cycle)', sign=sign).working is True


@pytest.mark.parametrize('line', [
    '✻ Pondered for 8m 52s',            # what a finished turn leaves in the transcript
    '● synthetic reply (3m 2s later)',
    '  ✽ Pondering… (47s · ↓ 3.9k tokens)',  # quoted inside a reply, never at the line's start
    '✽ Pondering…',
])
def test_a_line_that_is_not_the_sign_is_not_read_as_working(line):
    assert drawn(MODE + ' (shift+tab to cycle)', sign=line).working is False
