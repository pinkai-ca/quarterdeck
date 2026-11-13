"""Ordinary checkpoint metadata only; no provider record or delivery guard fixtures."""

import json

import pytest

from quarterdeck import durable, receipt_refresh
from quarterdeck.ship import ShipError


REVISION = '1' * 40
NEXT = '2' * 40
KEY = 'a' * 64


def test_missing_checkpoint_read_creates_nothing(tmp_path):
    runtime = tmp_path / 'runtime'
    marker = receipt_refresh.Checkpoint(runtime, REVISION)
    assert not marker.complete and not marker.seen(KEY)
    assert not runtime.exists()


def test_same_revision_resumes_and_finishes_without_republishing(tmp_path, monkeypatch):
    marker = receipt_refresh.Checkpoint(tmp_path, REVISION)
    marker.mark(KEY)
    resumed = receipt_refresh.Checkpoint(tmp_path, REVISION)
    assert resumed.seen(KEY) and not resumed.complete
    resumed.finish()
    finished = receipt_refresh.Checkpoint(tmp_path, REVISION)
    assert finished.complete and finished.seen(KEY)
    monkeypatch.setattr(durable, 'replace_file', lambda *args: pytest.fail('republished completed progress'))
    finished.mark(KEY)
    finished.finish()


def test_next_revision_inherits_no_observation_and_does_not_write_on_read(tmp_path):
    marker = receipt_refresh.Checkpoint(tmp_path, REVISION)
    marker.mark(KEY)
    marker.finish()
    before = marker.path.read_bytes()
    next_marker = receipt_refresh.Checkpoint(tmp_path, NEXT)
    assert not next_marker.complete and not next_marker.seen(KEY)
    assert marker.path.read_bytes() == before
    next_marker.finish()
    assert json.loads(marker.path.read_text()) == dict(format=1, commit=NEXT, observed=[], complete=True)


@pytest.mark.parametrize('change', [dict(format=True), dict(format=2), dict(commit='unknown'),
                                  dict(observed=[KEY, KEY]), dict(observed=['unknown']),
                                  dict(complete=1), dict(extra='unsupported')])
def test_unsupported_checkpoint_is_retained_even_for_a_new_revision(tmp_path, change):
    path = tmp_path / 'post' / 'receipt-refresh.json'
    path.parent.mkdir()
    path.write_text(json.dumps(dict(format=1, commit=REVISION, observed=[], complete=False) | change))
    before = path.read_bytes()
    with pytest.raises(ShipError, match='unsupported; retained'):
        receipt_refresh.Checkpoint(tmp_path, NEXT)
    assert path.read_bytes() == before


def test_unreadable_checkpoint_is_not_reset(tmp_path):
    path = tmp_path / 'post' / 'receipt-refresh.json'
    path.parent.mkdir()
    path.write_text('{unfinished')
    with pytest.raises(ShipError, match='unreadable; retained'):
        receipt_refresh.Checkpoint(tmp_path, REVISION)
    assert path.read_text() == '{unfinished'


def test_failed_marker_publication_leaves_progress_unchanged(tmp_path, monkeypatch):
    marker = receipt_refresh.Checkpoint(tmp_path, REVISION)
    marker.mark(KEY)
    before = marker.path.read_bytes()

    def fail(*args):
        raise OSError('synthetic publication failure')

    monkeypatch.setattr(durable, 'replace_file', fail)
    with pytest.raises(OSError, match='publication failure'):
        marker.mark('b' * 64)
    assert not marker.seen('b' * 64) and marker.path.read_bytes() == before
    with pytest.raises(OSError, match='publication failure'):
        marker.finish()
    assert not marker.complete and marker.path.read_bytes() == before


def test_completed_revision_cannot_admit_new_observations(tmp_path):
    marker = receipt_refresh.Checkpoint(tmp_path, REVISION)
    marker.finish()
    with pytest.raises(ShipError, match='already complete'):
        marker.mark(KEY)


@pytest.mark.parametrize('revision', [None, 'unknown', 'A' * 40])
def test_unknown_revision_never_creates_a_checkpoint(tmp_path, revision):
    with pytest.raises(ShipError, match='revision unknown'):
        receipt_refresh.Checkpoint(tmp_path, revision)
    assert not (tmp_path / 'post').exists()
