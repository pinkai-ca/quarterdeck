"""Bounded reason-marker reads and writer lifecycle; all files and identities invented."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from quarterdeck import pending, pane_obstruction as pane_meta
from .test_short_pane_obstruction import diagnostic, REASON


@pytest.mark.parametrize('marked', [False, True])
def test_many_settled_journals_are_never_opened_by_the_diagnostic(diagnostic, monkeypatch, marked):
    door, _, proof, path = diagnostic
    for index in range(512):
        (path.parent / f'settled-{index}.json').write_text('{"phase":"delivered"}')
    marker = pane_meta.marker_path(path)
    if not marked:
        marker.unlink()
    opened = []
    real_open = os.open

    def counted(name, flags, *args, **kwargs):
        opened.append(Path(name))
        return real_open(name, flags, *args, **kwargs)

    def no_history(*args, **kwargs):
        raise AssertionError('a reason reading must not enumerate journal or attempt history')

    monkeypatch.setattr(os, 'open', counted)
    monkeypatch.setattr(pending, 'journals', no_history)
    result = door.pane_obstruction()
    assert result == (REASON if marked else None)
    assert opened == ([marker, path] if marked else [marker])
    assert not any(p.name.startswith('settled-') for p in opened)


def test_marker_is_small_and_contains_facts_not_the_message(diagnostic):
    _, _, proof, path = diagnostic
    marker = pane_meta.marker_path(path)
    value = json.loads(marker.read_text())
    assert marker.stat().st_size <= pane_meta.MARKER_BYTES
    assert value['journal'] == path.name and value['identity'] == proof.data['identity']
    assert value['geometry'] == [100, 16] and 'payload' not in value
    assert proof.data['payload'] not in marker.read_text()


@pytest.mark.parametrize('phase', ['prepared', 'submitting', 'submitted', 'queued', 'delivered', 'unacted', 'uncertain', 'cancelled'])
def test_journal_writer_retires_its_marker_when_it_leaves_failed_paste_phases(diagnostic, phase):
    door, _, proof, path = diagnostic
    proof.save(phase=phase)
    assert not pane_meta.marker_path(path).exists()
    assert door.pane_obstruction() is None
    assert proof.data['phase'] == phase


@pytest.mark.parametrize('phase', ['pasting', 'withheld'])
def test_failed_paste_phases_keep_the_marker_and_the_original_outcome(diagnostic, phase):
    door, _, proof, path = diagnostic
    proof.save(phase=phase)
    assert pane_meta.marker_path(path).is_file()
    assert door.pane_obstruction() == REASON and proof.data['phase'] == phase


@pytest.mark.parametrize('phase,screen_queue,removed', [('delivered', False, True), ('queued', False, True),
                                                      ('queued', True, False), ('submitted', False, False)])
def test_only_a_later_record_confirmed_paste_retires_another_journals_marker(diagnostic, phase, screen_queue, removed):
    _, _, proof, path = diagnostic
    data = {**proof.data, 'phase': phase, 'since': '2001-01-01T00:00:02+00:00', 'screen_queue': screen_queue}
    later = pending.Proof(path.with_name('later-journal.json'), {})
    later.save(**data)
    assert pane_meta.marker_path(path).exists() is not removed
    assert proof.data['phase'] == 'pasting'


def test_an_older_late_confirmation_keeps_the_current_failed_paste_reason(diagnostic):
    door, _, proof, path = diagnostic
    older = pending.Proof(path.with_name('older-journal.json'), {})
    older.save(**{**proof.data, 'phase': 'delivered', 'since': '2001-01-01T00:00:00+00:00'})
    assert door.pane_obstruction() == REASON


def test_record_confirmation_of_an_already_queued_paste_retires_the_marker(diagnostic):
    _, _, proof, path = diagnostic
    later = pending.Proof(path.with_name('later-journal.json'), {})
    later.save(**{**proof.data, 'phase': 'queued', 'since': '2001-01-01T00:00:02+00:00', 'screen_queue': True})
    assert pane_meta.marker_path(path).exists()
    later.save(screen_queue=False)
    assert not pane_meta.marker_path(path).exists()


@pytest.mark.parametrize('problem', ['malformed', 'oversized', 'missing-journal', 'settled-journal', 'different-identity'])
def test_an_unchecked_marker_reads_unknown_without_repairing_any_file(diagnostic, problem):
    door, _, proof, path = diagnostic
    marker = pane_meta.marker_path(path)
    if problem == 'malformed':
        marker.write_text('{ malformed')
    elif problem == 'oversized':
        marker.write_text('x' * (pane_meta.MARKER_BYTES + 1))
    elif problem == 'missing-journal':
        path.unlink()
    else:
        data = deepcopy(proof.data)
        if problem == 'settled-journal':
            data['phase'] = 'delivered'  # a writer cut before optional marker retirement
        else:
            data['identity']['launch'] = 'another-launch'
        path.write_text(json.dumps(data))
    before = marker.read_bytes()
    assert door.pane_obstruction() is None
    assert marker.read_bytes() == before
