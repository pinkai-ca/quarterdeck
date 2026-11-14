"""Finished evidence remains usable after its unchanged files leave the hot folders."""

from types import SimpleNamespace
import json

import pytest

from quarterdeck import pending, post, post_retirement as retirement


def service(runtime):
    return SimpleNamespace(runtime=runtime, store=post.Store(runtime), attempts=post.Attempts(runtime),
                           ledger=SimpleNamespace(catching_up=lambda: False))


def delivery(s, *, closed=True, result='delivered', phase='delivered', name='builder'):
    message = s.store.new('order', 'lead', name, 'Synthetic task')
    attempt = s.attempts.open(message, name, '%1', 100, '2000-01-01T00:00:00+00:00')
    if result is not None:
        attempt = s.attempts.close(attempt, result)
    data = dict(seat=name, identity=dict(pane='%1', pid=100, harness='fake', launch=attempt.launched_at),
                at=attempt.at, since=attempt.at, payload=post.typed([message]), phase=phase,
                record=None, shown=None, attempts=[attempt.path.name])
    path = s.runtime / 'post/pending' / name / attempt.path.name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=3) + '\n')
    if closed:
        s.store.new('report', name, 'lead', 'Done', re=message.id)
    return message, attempt, path


def test_finished_settled_files_move_unchanged_and_history_still_delivers_once(tmp_path):
    s = service(tmp_path)
    message, attempt, journal = delivery(s)
    originals = {p: p.read_bytes() for p in (attempt.path, journal)}
    expected = [(a.message, a.result, a.at, a.detail) for a in s.attempts.all()]
    assert retirement.run(s) == dict(attempts=1, journals=1)
    for old, raw in originals.items():
        assert not old.exists()
        assert retirement.resolve(old, tmp_path).read_bytes() == raw
    restarted = post.Attempts(tmp_path)
    assert [(a.message, a.result, a.at, a.detail) for a in restarted.all()] == expected
    assert message.id in restarted.accepted_ids()
    assert message not in restarted.undelivered(s.store, 'builder')
    assert post.attempt_path(tmp_path, attempt.path.name).read_bytes() == originals[attempt.path]
    assert pending.journal_path(tmp_path, 'builder', journal.name).read_bytes() == originals[journal]
    assert pending._journals(tmp_path, 'builder') == ([], [])
    assert restarted.close(attempt, 'withheld').result == 'delivered'
    assert post.attempt_path(tmp_path, attempt.path.name).read_bytes() == originals[attempt.path]
    newer = restarted.open(message, 'builder', '%1', 100, attempt.launched_at)
    assert newer.path.name != attempt.path.name
    assert newer.path.name.endswith('-2.json')
    assert retirement.run(s) == dict(attempts=0, journals=0)


@pytest.mark.parametrize('closed,result,phase', [
    (False, 'delivered', 'delivered'), (True, None, 'prepared'),
    (True, 'queued', 'queued'), (True, 'blind', 'submitted'),
    (True, 'launch', 'carried'), (True, 'delivered', 'submitted'),
])
def test_open_or_unsettled_evidence_stays(tmp_path, closed, result, phase):
    s = service(tmp_path)
    _, attempt, journal = delivery(s, closed=closed, result=result, phase=phase)
    before = {p: p.read_bytes() for p in (attempt.path, journal)}
    assert retirement.run(s) == dict(attempts=0, journals=0)
    assert all(p.read_bytes() == raw for p, raw in before.items())


def test_catchup_holds_both_files_until_its_commit(tmp_path):
    s = service(tmp_path)
    _, attempt, journal = delivery(s)
    s.ledger.catching_up = lambda: True
    assert retirement.run(s)['held'] == 'ledger catch-up'
    assert attempt.path.exists() and journal.exists()
    s.ledger.catching_up = lambda: False
    assert retirement.run(s) == dict(attempts=1, journals=1)


def test_inflight_pointer_keeps_even_a_finished_journal_in_place(tmp_path):
    s = service(tmp_path)
    _, attempt, journal = delivery(s)
    pointer = tmp_path / 'service.delivery'
    pointer.write_text(json.dumps(dict(journal=str(journal.relative_to(tmp_path)))))
    assert retirement.run(s) == dict(attempts=0, journals=0)
    assert attempt.path.exists() and journal.exists()
    pointer.unlink()
    assert retirement.run(s) == dict(attempts=1, journals=1)


@pytest.mark.parametrize('stop', [0, 1, 2])
def test_interrupted_batch_has_whole_files_and_restarts_without_losing_history(tmp_path, monkeypatch, stop):
    s = service(tmp_path)
    message, attempt, journal = delivery(s)
    originals = {p: p.read_bytes() for p in (attempt.path, journal)}
    real_move = retirement.move
    calls = 0
    def interrupted(source, destination):
        nonlocal calls
        if calls == stop:
            raise KeyboardInterrupt('synthetic interrupted retirement')
        calls += 1
        result = real_move(source, destination)
        if calls == stop == 2:
            raise KeyboardInterrupt('synthetic interrupted retirement')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(retirement, 'move', interrupted)
        with pytest.raises(KeyboardInterrupt):
            retirement.run(s)
    for old, raw in originals.items():
        target = retirement.retired(old, tmp_path)
        assert old.exists() != target.exists()
        assert (old if old.exists() else target).read_bytes() == raw
    fresh = service(tmp_path)
    assert fresh.attempts.latest()[message.id] == 'delivered'
    retirement.run(fresh)
    for old, raw in originals.items():
        assert retirement.retired(old, tmp_path).read_bytes() == raw
        assert not old.exists()
    assert len(fresh.attempts.for_message(message.id)) == 1


def test_batch_limit_does_not_leave_finished_journals_stranded(tmp_path):
    s = service(tmp_path)
    cases = [delivery(s) for _ in range(3)]
    for _ in range(4):
        retirement.run(s, limit=1)
    assert len(s.attempts.all()) == 3
    assert not list((tmp_path / 'post/pending').rglob('*.json'))
    assert not list(s.attempts.dir.glob('*.json'))
    assert all(retirement.retired(journal, tmp_path).is_file() for _, _, journal in cases)


def test_current_launch_receipts_stay_until_that_launch_ends(tmp_path):
    s = service(tmp_path)
    _, attempt, journal = delivery(s)
    launch = tmp_path / 'seats/builder/launch.json'
    launch.parent.mkdir(parents=True)
    launch.write_text(json.dumps(dict(pane=attempt.pane, pid=attempt.pid, at=attempt.launched_at)))
    assert retirement.run(s) == dict(attempts=0, journals=0)
    assert attempt.path.exists() and journal.exists()
    launch.unlink()
    assert retirement.run(s) == dict(attempts=1, journals=1)


def test_service_slice_beats_yields_and_resumes_between_whole_moves(tmp_path, monkeypatch):
    s = service(tmp_path)
    originals = [delivery(s) for _ in range(8)]
    clock = [0.0]
    beats = []
    s.heartbeat = lambda: beats.append(clock[0])
    real_move = retirement.move
    def measured_move(source, target):
        moved = real_move(source, target)
        clock[0] += 0.11
        return moved
    with monkeypatch.context() as patch:
        patch.setattr(retirement.time, 'monotonic', lambda: clock[0])
        patch.setattr(retirement, 'move', measured_move)
        result = retirement.run(s, seconds=0.5)
    assert result['paused'] is True
    assert result['attempts'] + result['journals'] == 5
    assert beats == [0.0, 0.11, 0.22, 0.33, 0.44, 0.55]
    for _ in range(8):
        progress = retirement.run(s)
        if not progress['attempts'] and not progress['journals']:
            break
    for _, attempt, journal in originals:
        assert retirement.retired(attempt.path, tmp_path).is_file()
        assert retirement.retired(journal, tmp_path).is_file()
    assert len(s.attempts.all()) == 8


def test_retirement_is_once_after_delivery_and_accounting_in_a_service_pass():
    from quarterdeck.service import Service
    events = []
    class Duties:
        def __getattr__(self, name):
            return lambda *args, **kwargs: events.append(name)
    Service._tick_steps(Duties())
    assert events.count('retire_post') == 1
    assert events.index('deliveries') < events.index('account') < events.index('retire_post')


def test_yielding_while_reading_one_seat_still_gives_the_next_seat_a_turn(tmp_path, monkeypatch):
    s = service(tmp_path)
    delivery(s, name='first')
    delivery(s, name='second')
    clock = [0.0]
    journals = pending._journals
    def slow_read(*args, **kwargs):
        result = journals(*args, **kwargs)
        clock[0] += 0.6
        return result
    with monkeypatch.context() as patch:
        patch.setattr(retirement.time, 'monotonic', lambda: clock[0])
        patch.setattr(pending, '_journals', slow_read)
        assert retirement.run(s, seconds=0.5)['paused'] is True
    assert s.post_retirement_cursor == 'second'
    moved = []
    move = retirement.move
    def observed(source, target):
        moved.append(source)
        return move(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(retirement, 'move', observed)
        retirement.run(s)
    assert moved[0].parent.name == 'second'
    assert len(s.attempts.all()) == 2


def test_batch_never_syncs_and_publishes_index_before_moves_and_completion_after(tmp_path, monkeypatch):
    import os
    s = service(tmp_path)
    cases = [delivery(s) for _ in range(4)]
    originals = {old: old.read_bytes() for _, attempt, journal in cases for old in (attempt.path, journal)}
    events = []
    move = retirement.move
    write = retirement._replace_file
    def forbidden_sync(_):
        pytest.fail('retirement forced a storage sync')
    def observed_move(source, target):
        result = move(source, target)
        events.append(('move', source))
        return result
    def observed_write(path, text):
        events.append(('write', path))
        return write(path, text)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', forbidden_sync)
        patch.setattr(os, 'fdatasync', forbidden_sync)
        patch.setattr(retirement, 'move', observed_move)
        patch.setattr(retirement, '_replace_file', observed_write)
        assert retirement.run(s) == dict(attempts=4, journals=4)
        index = [i for i,(event,p) in enumerate(events) if event=='write' and p.parent.name=='index']
        done = [i for i,(event,p) in enumerate(events) if event=='write' and p.parent.name=='completed']
        renames = [i for i,(event,_) in enumerate(events) if event=='move']
        assert len(index) == len(done) == 1
        assert len(renames) == len(originals)
        assert index[0] < min(renames) <= max(renames) < done[0]
        for old, raw in originals.items():
            assert retirement.retired(old, tmp_path).read_bytes() == raw
        events.clear()
        assert retirement.run(s) == dict(attempts=0, journals=0)
        assert events == []


def test_crash_after_renames_before_completion_replays_the_unfinished_batch(tmp_path, monkeypatch):
    s = service(tmp_path)
    _, attempt, journal = delivery(s)
    originals = {p:p.read_bytes() for p in (attempt.path,journal)}
    write = retirement._replace_file
    def cut(path, text):
        if path.parent.name == 'completed':
            raise OSError('synthetic process cut before completion')
        return write(path, text)
    with monkeypatch.context() as patch:
        patch.setattr(retirement, '_replace_file', cut)
        with pytest.raises(OSError, match='synthetic process cut'):
            retirement.run(s)
    assert not list((tmp_path/'post/retired/completed').glob('*.json'))
    for old,raw in originals.items():
        new = retirement.retired(old,tmp_path)
        assert old.exists() != new.exists()
        assert (old if old.exists() else new).read_bytes()==raw
    restarted = service(tmp_path)
    assert retirement.run(restarted) == dict(attempts=0,journals=0)
    assert len(list((tmp_path/'post/retired/completed').glob('*.json'))) == 1
    for old,raw in originals.items():
        assert retirement.retired(old,tmp_path).read_bytes()==raw
    assert len(restarted.attempts.all()) == 1



def test_index_replacement_is_whole_without_any_sync(tmp_path, monkeypatch):
    import os
    path = tmp_path / 'index.json'
    path.write_text('old complete index')
    replace = os.replace
    seen = []
    def forbidden_sync(_):
        pytest.fail('index publication forced a storage sync')
    def observed_replace(temporary, target):
        seen.append((target.read_text(), temporary.read_text()))
        replace(temporary, target)
    monkeypatch.setattr(os, 'fsync', forbidden_sync)
    monkeypatch.setattr(os, 'fdatasync', forbidden_sync)
    monkeypatch.setattr(os, 'replace', observed_replace)
    retirement._replace_file(path, 'new complete index')
    assert seen == [('old complete index', 'new complete index')]
    assert path.read_text() == 'new complete index'
    assert list(tmp_path.iterdir()) == [path]
