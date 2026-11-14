"""Ordinary synthetic pane-height reason metadata and status text; no native program launch."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from quarterdeck import watch
from quarterdeck.door import Door, Reading
from quarterdeck.harness.pi import PiHarness


def frame(height):
    rows = ['─' * 80, ' ' * 80, '─' * 80, '/synthetic-project',
            '0.0%/100k  synthetic-model • thinking off']
    return '\n'.join(([''] * max(0, height - len(rows)) + rows)[-height:])


def display(height=4, version='0.79.0', original=None):
    pane = SimpleNamespace(id='synthetic-pane', pid=71, dead=False, height=height, width=80)
    launched = SimpleNamespace(pane=pane.id, pid=pane.pid, version=version)
    seat = SimpleNamespace(harness=PiHarness(), tmux=SimpleNamespace(), launched=lambda: launched)
    door = Door(seat)
    door.captured_pane = pane
    screen = seat.harness.screen(frame(height))
    original = original or Reading(False, f'unknown dialog: {screen.unknown}', 'needs a human')
    door._base_reading = lambda captured: original
    door.pane_obstruction = lambda: None
    return door, screen, original, pane, launched


@pytest.mark.parametrize('height', [1, 2, 3, 4])
def test_short_empty_box_reading_names_actual_height_and_measured_version(height):
    door, screen, original, _, _ = display(height)
    shown = door._reading(screen)
    assert shown.diagnostic == f'pane {height} rows; pi 0.79.0 draws its box from 5 rows'
    assert shown.display_reason == 'input box needs a taller pane'
    assert 'unknown dialog' not in str(shown) and '0.74.2' not in str(shown)
    assert shown.diagnostic in str(shown)
    assert replace(shown, diagnostic='', display_reason='') == original


@pytest.mark.parametrize('height', [5, 7, 14])
def test_measured_minimum_and_taller_frames_have_no_small_pane_label(height):
    door, screen, original, _, _ = display(height, original=Reading(True))
    assert screen.box == '' and screen.unknown is None
    assert door._reading(screen) == original


@pytest.mark.parametrize('version', [None, '0.74.2', 'unmeasured-version'])
def test_an_unmeasured_version_is_named_without_claiming_a_height(version):
    door, screen, original, _, _ = display(version=version)
    shown = door._reading(screen)
    assert shown.diagnostic == ''
    assert shown.display_reason == f'pi {version or "(version unknown)"}: input box not found'
    assert replace(shown, display_reason='') == original


def test_a_taller_unknown_frame_names_the_actual_launch_version():
    door, _, _, _, _ = display(height=7)
    screen = door.harness.screen(frame(4))
    door._base_reading = lambda captured: Reading(False, f'unknown dialog: {captured.unknown}', 'needs a human')
    shown = door._reading(screen)
    assert shown.display_reason == 'pi 0.79.0: input box not found'
    assert shown.diagnostic == ''
    assert '0.74.2' not in str(shown)


@pytest.mark.parametrize('change', ['no-launch', 'other-pane', 'other-pid', 'no-capture', 'dead',
                                   'unknown-height', 'missing-height', 'missing-launch-reader', 'missing-pid'])
def test_height_reason_requires_this_captured_launch_and_known_geometry(change):
    door, screen, original, pane, launched = display()
    if change == 'no-launch':
        door.seat.launched = lambda: None
    elif change == 'other-pane':
        launched.pane = 'earlier-pane'
    elif change == 'other-pid':
        launched.pid += 1
    elif change == 'no-capture':
        door.captured_pane = None
    elif change == 'dead':
        pane.dead = True
    elif change == 'missing-height':
        del pane.height
    elif change == 'missing-launch-reader':
        del door.seat.launched
    elif change == 'missing-pid':
        del pane.pid
    else:
        pane.height = None
    assert door._reading(screen) == original


@pytest.mark.parametrize('original', [Reading(False, 'prior delivery pending', 'time', waiting=True),
                                      Reading(False, 'ordinary existing reason', 'needs a human')])
def test_geometry_does_not_replace_an_unrelated_reason(original):
    door, screen, _, _, _ = display(original=original)
    shown = door._reading(screen)
    assert shown.display_reason == ''
    assert original.reason in str(shown)
    assert replace(shown, diagnostic='') == original


@pytest.mark.parametrize('overflow', [False, True])
def test_status_names_the_short_pane_even_when_alerts_overflow(overflow):
    door, screen, _, _, _ = display()
    shown = door._reading(screen)
    facts = watch.Facts(service='up', waiting=0, oldest=0, blind=0,
                        pane_obstructions=['synthetic-seat: ' + shown.diagnostic],
                        unknown=['synthetic alert ' * 30] if overflow else [])
    line = facts.line()
    assert 'synthetic-seat: pane 4 rows; pi 0.79.0 draws its box from 5 rows' in line
    assert 'unknown dialog' not in line and '0.74.2' not in line
    if overflow:
        assert 'details: qd status' in line


def test_missing_pi_screen_parts_do_not_claim_the_old_renderer_version():
    harness = PiHarness()
    assert harness.screen('synthetic unrecognized frame').unknown == 'pi footer not found'
    assert harness.screen(frame(4)).unknown == 'pi input box not found'


def test_an_unknown_dialog_keeps_its_ordinary_display_and_no_height_annotation():
    door, _, _, _, _ = display()
    screen = door.harness.screen('synthetic unrecognized frame')
    original = Reading(False, f'unknown dialog: {screen.unknown}', 'needs a human')
    door._base_reading = lambda captured: original
    assert door._reading(screen) == original


@pytest.mark.parametrize('error', [OSError, ValueError])
def test_unreadable_launch_metadata_leaves_the_original_reading(error):
    door, screen, original, _, _ = display()
    def unavailable():
        raise error('synthetic unavailable metadata')
    door.seat.launched = unavailable
    assert door._reading(screen) == original


@pytest.mark.parametrize('text,rows', [('', 5), ('a', 5), ('a\nb\nc', 7), ('a\nb\nc\n', 8),
    ('a\nb\nc\nd\ne', 9), ('a\nb\nc\nd\ne\nf', 20), ('x\n' * 6, 24),
    ('x\n' * 8, 30), ('x\n' * 9, 34), ('x\n' * 10, 5), ('x' * 1001, 5),
    ('x' * 120, 6), ('header · synthetic\nbody\nfooter\n', 8)])
def test_payload_height_counts_cursor_chrome_viewport_and_native_folding(text, rows):
    assert PiHarness().minimum_pane_rows(text, 120, '0.79.0') == rows


@pytest.mark.parametrize('text,width,version', [('a\tb', 120, '0.79.0'), ('e\u0301', 120, '0.79.0'),
    ('é' * 200, 120, '0.79.0'), ('abc', 120, None), ('abc', 120, '0.74.2'), ('abc', 20, '0.79.0')])
def test_unmeasured_payload_layout_has_no_pane_height_claim(text, width, version):
    assert PiHarness().minimum_pane_rows(text, width, version) is None


def test_payload_reason_is_computed_for_the_box_not_the_empty_editor():
    door, screen, original, _, _ = display(height=7)
    door.seat.session = SimpleNamespace(human=False)
    payload = 'header\nbody\nfooter\n'
    reason = door._short_pane_geometry(payload, 120, 7)
    assert reason == 'pane 7 rows; pi 0.79.0 draws this box from 8 rows'
    assert door._short_pane_geometry(payload, 120, 8) == ''
    door.pane_obstruction = lambda: reason
    screen = door.harness.screen(frame(4))
    door._base_reading = lambda captured: Reading(False, f'unknown dialog: {captured.unknown}', 'needs a human')
    shown = door._reading(screen)
    assert shown.diagnostic == reason and shown.display_reason == 'input box needs a taller pane'
    assert 'unknown dialog' not in str(shown)
    assert replace(shown, diagnostic='', display_reason='') == door._base_reading(screen)
    assert 'synthetic-seat: ' + reason in watch.Facts(service='up', waiting=0, oldest=0, blind=1,
        pane_obstructions=['synthetic-seat: ' + shown.diagnostic]).line()
