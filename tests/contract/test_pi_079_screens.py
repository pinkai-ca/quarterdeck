"""Synthetic screen text in the forms measured on installed pi 0.79.0.

Private native captures cover 120, 256 and 425 columns; the last case also
measures the renderer's provider-prefix fallback at 52 columns. Paths, model,
transcript and statistics below are invented rather than copied from a session.
"""

from pathlib import Path

import pytest

from quarterdeck.harness.pi import PiHarness
from quarterdeck.endpoint import Choice
from quarterdeck.ship import Pool, SeatSpec


FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'pi' / '0.79.0'
SHORT = 'sample line one\nsample line two\nsample line three\nsample line four'
WRAPPED = '\n'.join([' '.join(['sample words'] * 9)] * 7 + [' '.join(['sample words'] * 7)])


@pytest.mark.parametrize('name,box,working,effort', [
    ('fresh-120', '', False, 'medium'),
    ('idle-256', '', False, 'medium'),
    ('working-425', '', True, 'medium'),
    ('paste-short-120', SHORT, False, 'medium'),
    ('paste-wrapped-120', WRAPPED, False, 'medium'),
    ('paste-folded-lines-256', '[paste #1 +44 lines]', False, 'medium'),
    ('paste-folded-chars-425', '[paste #2 1200 chars]', False, 'medium'),
    ('single-provider-120', '', False, 'off'),
    ('thinking-off-425', '', False, 'off'),
    ('no-reasoning-425', '', False, None),
    ('prefix-does-not-fit-52', '', False, 'off'),
])
def test_measured_pi_screen(name, box, working, effort):
    screen = PiHarness().screen((FIXTURES / f'{name}.txt').read_text())

    assert screen.box == box
    assert screen.working is working
    assert screen.shows == {'model': 'sample-model', **({'effort': effort} if effort else {})}
    assert screen.unknown is None
    assert screen.interruption is None
    assert screen.queued == 0
    assert screen.echoes == ()


def test_xai_key_startup_draws_the_editor_after_the_migration_warning():
    harness = PiHarness()
    warning = harness.screen((FIXTURES / 'xai-key-warning-80.txt').read_text())
    ready = harness.screen((FIXTURES / 'xai-key-startup-80.txt').read_text())

    assert warning.box is None and warning.unknown == 'pi footer not found'
    assert warning.interruption is None
    assert ready.box == '' and ready.working is False
    assert ready.unknown is None and ready.interruption is None
    assert ready.shows == {'model': 'grok-4.20-0309-non-reasoning', 'effort': 'off'}


def test_xai_key_editor_is_open_after_the_footer_clips_at_the_pane_edge():
    screen = PiHarness().screen((FIXTURES / 'xai-key-after-turn-80.txt').read_text())

    assert screen.box == '' and screen.working is False
    assert screen.unknown is None and screen.interruption is None
    assert screen.shows == {'model': 'grok-4.20-0309-non-reasoning'}


@pytest.mark.parametrize('old,new', [
    ('thinking of', 'thinking o'),
    ('thinking of', 'thinking of '),
    ('grok-4.20-0309-non-reasoning', 'grok-4.21-0309-non-reasoning'),
    ('CH99.2%', 'XX99.2%'),
    ('─' * 80, '─' * 79),
    ('<test folder>/workspace', '<test folder>/workspace\nExtra row'),
])
def test_xai_key_clipped_footer_needs_the_measured_stats_and_editor_edges(old, new):
    text = (FIXTURES / 'xai-key-after-turn-80.txt').read_text()
    assert old in text

    screen = PiHarness().screen(text.replace(old, new, 1))

    assert screen.box is None and screen.unknown == 'pi footer not found'


@pytest.mark.parametrize('name,payload,width', [
    ('paste-short-120', SHORT, 120),
    ('paste-wrapped-120', ' '.join(['sample words'] * 70), 120),
    ('paste-folded-lines-256', '\n'.join(f'sample line {i}' for i in range(44)), 256),
    ('paste-folded-chars-425', 'x' * 1200, 425),
], ids=['short', 'wrapped', 'folded-lines', 'folded-chars'])
def test_measured_pi_paste_display(name, payload, width):
    harness = PiHarness()
    screen = harness.screen((FIXTURES / f'{name}.txt').read_text())

    assert harness.box(payload).matches(screen.box, harness.box_columns(width))


def test_pi_launch_ignores_project_files_without_first_launch_question(tmp_path):
    pool = Pool('sample-local', 'local', endpoint='http://sample.invalid/v1')
    seat = SeatSpec('sample-seat', 'sample-session', 'worker', 'pi', (pool.name,), model='sample-model')
    Choice(pool.name, seat.model, 32768, 1024).write(tmp_path)
    brief = tmp_path / 'brief.md'
    brief.write_text('Sample brief.\n')

    launch = PiHarness().launch(seat, pool, tmp_path, brief, None, None)

    for flag in ('--no-approve', '--no-context-files', '--no-extensions'):
        assert launch.argv.count(flag) == 1
    assert launch.argv[launch.argv.index('--append-system-prompt') + 1] == str(brief)
