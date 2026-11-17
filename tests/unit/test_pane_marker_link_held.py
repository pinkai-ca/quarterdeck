"""A link planted at a seat's paste-obstruction marker: the door's optional diagnostic reads unknown and a journal
save completes, the link and its target untouched, and the link is named on the status line. Any other ShipError at
those sites still raises. A throwaway runtime under tmp_path; every file and identity invented."""
import os
from types import SimpleNamespace

import pytest

from quarterdeck import pending, pane_obstruction as pane_meta, post_dir, watch
from quarterdeck.door import Door
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.ship import ShipError
from .test_short_pane_obstruction import TEXT, seat_for


@pytest.fixture
def diagnostic(tmp_path):
    """As the short-pane tests' fixture, but under a real ship's shape, runtime/post, where the post folder's
    no-follow reader applies, as it does on every ship."""
    runtime = tmp_path / 'runtime'
    seat, pane = seat_for(CodexHarness())
    seat.name = 'invented-seat'
    seat.ship = SimpleNamespace(runtime=runtime)
    seat.launched = lambda: SimpleNamespace(pane=pane.id, pid=pane.pid, at='invented-launch')
    data = dict(phase='pasting', payload=TEXT, paste_geometry=[100, 16], attempts=['synthetic-attempt'],
                identity=dict(pane=pane.id, pid=pane.pid, launch='invented-launch', harness='codex'),
                since='2001-01-01T00:00:01+00:00')
    path = runtime / 'post' / 'pending' / seat.name / 'synthetic-journal.json'
    proof = pending.Proof(path, {})
    proof.save(**data)
    pane_meta.publish(proof)
    door = Door(seat)
    door.captured_pane = pane
    assert post_dir.under_post(path) and door.pane_obstruction(), 'the diagnostic reads through the post reader'
    return door, pane, proof, path


def planted(tmp_path, path):
    """The marker replaced by a link to a file outside the post folder, holding a valid marker's bytes."""
    marker = pane_meta.marker_path(path)
    target = tmp_path / 'outside-target.json'
    target.write_bytes(marker.read_bytes())
    marker.unlink()
    marker.symlink_to(target)
    return marker, target, target.read_bytes()


def untouched(marker, target, data):
    assert os.path.islink(marker) and os.readlink(marker) == str(target), 'the link itself is untouched'
    assert target.read_bytes() == data, 'its target is untouched'


def test_a_link_at_the_marker_leaves_the_doors_diagnostic_unknown_and_never_raises(tmp_path, diagnostic):
    door, _, _, path = diagnostic
    marker, target, data = planted(tmp_path, path)
    assert door.pane_obstruction() is None
    untouched(marker, target, data)


def test_a_link_at_the_marker_lets_a_journal_save_complete(tmp_path, diagnostic):
    _, _, proof, path = diagnostic
    marker, target, data = planted(tmp_path, path)
    proof.save(phase='delivered')  # a phase change reaches the marker's retirement, which must not raise
    assert proof.data['phase'] == 'delivered'
    import json
    assert json.loads(path.read_text())['phase'] == 'delivered', 'the journal was written whole'
    untouched(marker, target, data)


def test_a_link_at_the_marker_is_named_on_the_status_line(tmp_path, diagnostic):
    _, _, _, path = diagnostic
    marker, target, data = planted(tmp_path, path)
    ship = SimpleNamespace(runtime=tmp_path / 'runtime')
    store = SimpleNamespace(all=lambda: [])
    _, holds = watch._post_listing(ship, store)
    assert f'{post_dir.LINK_HELD}{marker}' in holds
    untouched(marker, target, data)


def test_without_a_link_nothing_is_named(tmp_path, diagnostic):
    ship = SimpleNamespace(runtime=tmp_path / 'runtime')
    _, holds = watch._post_listing(ship, SimpleNamespace(all=lambda: []))
    assert holds == []


def test_any_other_refusal_at_the_marker_still_raises(diagnostic, monkeypatch):
    door, _, proof, _ = diagnostic
    def refused(*args, **kwargs):
        raise ShipError('synthetic other refusal')
    monkeypatch.setattr(post_dir, 'read_bytes', refused)
    with pytest.raises(ShipError, match='synthetic other refusal'):
        door.pane_obstruction()
    with pytest.raises(ShipError, match='synthetic other refusal'):
        proof.save(phase='delivered')
