"""A damaged ordinary run journal names the ring that keeps a new start waiting."""

import errno
import json

import pytest

from quarterdeck import cascade
from quarterdeck.ship import ShipError
from .test_cascade import report, ring  # noqa: F401


@pytest.fixture
def done(ring):
    for _ in range(9):
        ring.runner.advance(ring.path)
        report(ring)
        ring.runner.advance(ring.path)
    assert cascade.load(ring.path)['status'] == 'done'
    return ring


@pytest.mark.parametrize('damage,reason', [
    ('json', 'invalid JSON'),
    ('mismatch', 'unknown or mismatched run journal'),
    ('missing-field', "KeyError: 'declaration'"),
    ('unreadable', 'PermissionError'),
    ('encoding', 'invalid text encoding'),
])
def test_damaged_journal_beside_done_run_refuses_by_path_without_changing_evidence(done, monkeypatch, damage, reason):
    broken = done.ship.runtime / 'ring' / 'damaged-ring'
    broken.mkdir()
    data = cascade.load(done.path)
    data['question'] = broken.name
    if damage == 'mismatch':
        data['question'] = 'another-fictional-question'
    elif damage == 'missing-field':
        del data['declaration']
    state = broken / 'state.json'
    state.write_bytes(b'{' if damage == 'json' else b'\xff' if damage == 'encoding'
                      else json.dumps(data).encode())
    if damage == 'unreadable':
        read = cascade.cascade_runtime.read_file
        def unavailable(path):
            if path == state:
                raise PermissionError(errno.EACCES, 'synthetic journal unavailable', str(path))
            return read(path)
        monkeypatch.setattr(cascade.cascade_runtime, 'read_file', unavailable)
    question = done.store.new('question', 'operator', 'conductor', 'Another fictional garden plan.')
    before = {str(path.relative_to(done.ship.runtime)): path.read_bytes()
              for path in (done.ship.runtime / 'ring').rglob('*') if path.is_file()}
    posts = len(done.store.all())
    with pytest.raises(ShipError) as caught:
        cascade.start(done.service, question.id, 'conductor')
    assert str(state) in str(caught.value)
    assert reason in str(caught.value)
    assert 'retained' in str(caught.value)
    assert len(done.store.all()) == posts
    assert not cascade.folder(done.ship.runtime, question.id).exists()
    assert before == {str(path.relative_to(done.ship.runtime)): path.read_bytes()
                      for path in (done.ship.runtime / 'ring').rglob('*') if path.is_file()}


def test_completed_run_alone_still_allows_a_new_question(done):
    before = (done.path / 'state.json').read_bytes()
    question = done.store.new('question', 'operator', 'conductor', 'A further fictional garden plan.')
    assert 'running' in cascade.start(done.service, question.id, 'conductor')
    assert cascade.load(cascade.folder(done.ship.runtime, question.id))['status'] == 'running'
    assert (done.path / 'state.json').read_bytes() == before


def test_resuming_its_own_damaged_journal_names_the_same_file(ring):
    state = ring.path / 'state.json'
    state.write_bytes(b'{')
    posts = len(ring.store.all())
    with pytest.raises(ShipError) as caught:
        cascade.start(ring.service, ring.question.id, 'conductor')
    assert str(state) in str(caught.value) and 'invalid JSON' in str(caught.value)
    assert state.read_bytes() == b'{' and len(ring.store.all()) == posts
