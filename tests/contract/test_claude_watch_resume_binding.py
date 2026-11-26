"""Synthetic watch acknowledgements, retained generations and launch boundaries."""

import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from quarterdeck import human_wake, inbox_watch, post, trusted_files, watch_broker
from quarterdeck.harness import claude, claude_watch, json_record
from quarterdeck.harness.base import Join
from quarterdeck.harness.claude_background import STOPPED_RESUME
from quarterdeck.service import Service

from .test_claude_own_watch import (AT, CALL, NEW, OLD, SESSION, TASK, origin, record, resume, resume_notice, row,
                                   stamp, started_watch, typed)


UNRESOLVED = 'task notification queue unresolved'


def launch(path, second, *, version=NEW, resume=False, proved=True):
    folder = trusted_files.make(path.parents[5], 'quill')
    data = dict(at=stamp(second), pane=f'%{int(second) + 20}', pid=52345 + int(second),
                pool='synthetic', harness='claude', version=version, resume=SESSION if resume else None)
    (folder / trusted_files.LAUNCH).write_text(json.dumps(data))
    (folder / trusted_files.RECORD).write_text(str(path) + '\n')
    if proved:
        with (folder / trusted_files.LAUNCHES).open('a') as stream:
            stream.write(json.dumps(dict(record=str(path), at=data['at'], pool=data['pool'], harness='claude')) + '\n')
    return SimpleNamespace(**data, when=datetime.fromisoformat(data['at']))


def current_seat(path, current):
    return SimpleNamespace(name='quill', ship=SimpleNamespace(runtime=path.parents[5]), launched=lambda: current[0],
                           session=SimpleNamespace(human=True),
                           harness=claude.ClaudeHarness(), lookup_record=lambda: path,
                           join=lambda: Join(path), pane=lambda: SimpleNamespace(
                               id=current[0].pane, pid=current[0].pid, dead=False, in_mode=False))


def register(seat, second, monkeypatch, *, admitted=True):
    parent = 65234
    process = os.getpid()
    root = seat.launched().pid
    table = {process: (parent, '1000'), parent: (root, '999'), root: (1, '998')}
    monkeypatch.setattr(inbox_watch, 'process', table.get)
    monkeypatch.setattr(inbox_watch, 'children', lambda pid: {process} if pid == parent else set())
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=second))
    return inbox_watch.register(seat.ship.runtime, seat.name, admitted=admitted)


def bind(seat):
    with post.admission(seat.ship.runtime, seat.name):
        inbox_watch.retain_binding(seat)


def append(path, *rows, complete=True):
    with path.open('a') as stream:
        stream.write('\n'.join(json.dumps(row) for row in rows) + ('\n' if complete else ''))


def next_watch():
    rows = started_watch()
    for entry in rows:
        entry['timestamp'] = (datetime.fromisoformat(entry['timestamp']) + timedelta(seconds=8)).isoformat()
    return [json.loads(json.dumps(entry).replace(CALL, CALL + 'B').replace(TASK, TASK + 'b'))
            for entry in rows]


@pytest.mark.parametrize('registered', [1.5, 2.5], ids=['before-acknowledgement', 'after-acknowledgement'])
def test_exact_binding_survives_retirement_rearm_and_reader_restart(tmp_path, monkeypatch, registered):
    path = record(tmp_path, started_watch())
    current = [launch(path, -1)]
    seat = current_seat(path, current)
    first = register(seat, registered, monkeypatch)
    monkeypatch.setattr(watch_broker, 'check_seats', lambda ship: [])
    monkeypatch.setattr(post, 'watch_reading', lambda *_: post.WatchReading(True, first['generation']))
    errors = []
    service = SimpleNamespace(ship=seat.ship, runtime=seat.ship.runtime, resolved_seats=lambda: [seat],
                              log=lambda *args, **kwargs: errors.append((args, kwargs)))
    Service.jailed_watches(service)
    assert errors == []
    folder = claude_watch.bindings_path(path)
    saved = folder / (first['generation'] + '.json')
    binding = json.loads(saved.read_text())
    assert (binding['generation'], binding['task'], binding['call'], binding['launch_at']) == (
        first['generation'], TASK, CALL, current[0].at)
    inbox_watch.retire(seat.ship.runtime, seat.name, first)

    native = resume_notice()
    append(path, native)
    current[0] = launch(path, 5, resume=True)
    monkeypatch.setattr(claude_watch, '_watch_ended', lambda pid, start: (pid, start) == (os.getpid(), '1000'))
    before = path.read_bytes()
    assert claude.state(path).queue_error is None
    assert claude.state(path).pending_tools == claude.state(path).consumed == ()
    assert path.read_bytes() == before
    monkeypatch.setattr(human_wake, 'candidate_hold', lambda *args: None)
    assert human_wake.eligible(seat) is None

    append(path, *next_watch())
    second = register(seat, 9.5, monkeypatch)
    bind(seat)
    assert second['generation'] != first['generation']
    rearmed = folder / (second['generation'] + '.json')
    assert json.loads(rearmed.read_text())['task'] == TASK + 'b'
    assert json.loads(saved.read_text()) == binding
    inbox_watch.retire(seat.ship.runtime, seat.name, second)

    json_record.release(path)
    reread = claude.state(path)
    assert reread.queue_error is None and reread.pending_tools == ('Bash',)
    current[0] = launch(path, 12, resume=True)
    duplicate = resume_notice()
    duplicate.update(timestamp=stamp(13), uuid='new-notice', promptId='new-prompt')
    append(path, duplicate)
    assert claude.state(path).queue_error == STOPPED_RESUME


def test_rearm_cannot_reuse_an_earlier_acknowledgement(tmp_path, monkeypatch):
    path = record(tmp_path, started_watch())
    seat = current_seat(path, [launch(path, -1)])
    first = register(seat, 2.5, monkeypatch)
    bind(seat)
    inbox_watch.retire(seat.ship.runtime, seat.name, first)
    second = register(seat, 3.5, monkeypatch)
    bind(seat)
    folder = claude_watch.bindings_path(path)
    assert [entry.stem for entry in folder.glob('*.json')] == [first['generation']]
    inbox_watch.retire(seat.ship.runtime, seat.name, second)


@pytest.mark.parametrize('fault', [
    'ack', 'different-call', 'different-task', 'different-generation', 'different-launch', 'missing-marker',
    'missing-binding', 'running-waiter', 'opened', 'watch', 'abandoned', 'no-join', 'partial', 'duplicate',
    'brokered', 'legacy',
])
def test_incomplete_watch_evidence_preserves_the_latch(tmp_path, monkeypatch, fault):
    rows = started_watch()
    if fault == 'ack':
        rows[2]['sourceToolAssistantUUID'] = 'some-other-assistant'
    path = record(tmp_path, rows)
    current = [launch(path, -1)]
    seat = current_seat(path, current)
    first = register(seat, 1.5, monkeypatch, admitted=fault != 'brokered')
    folder = claude_watch.bindings_path(path)
    marker = inbox_watch.unoffered_path(seat.ship.runtime, first['generation'])
    binding = folder / (first['generation'] + '.json')
    if fault == 'legacy':
        marker.unlink()
    assert marker.exists() is (fault not in ('brokered', 'legacy'))
    bind(seat)
    assert binding.exists() is (fault not in ('ack', 'brokered', 'legacy'))
    if fault == 'missing-binding':
        binding.unlink()
    elif fault == 'missing-marker':
        marker.unlink()
    elif fault in ('different-generation', 'different-launch'):
        data = json.loads(marker.read_text())
        if fault == 'different-generation':
            data['generation'] = 'a' * 32
        else:
            data['launch'] = 'a' * 64
        marker.write_text(json.dumps(data))
    elif fault in ('opened', 'watch', 'abandoned'):
        attempts = post.Attempts(seat.ship.runtime)
        message = SimpleNamespace(id='synthetic-attempt')
        opened = attempts.open(message, seat.name, None, None, None, watch_generation=first['generation'])
        if fault != 'opened':
            attempts.close(opened, fault)
        assert not marker.exists() and marker.with_name(f'.watch-{first["generation"]}-offered').exists()
    inbox_watch.retire(seat.ship.runtime, seat.name, first, admitted=fault != 'brokered')
    current[0] = launch(path, 5, resume=True, proved=fault != 'no-join')
    monkeypatch.setattr(claude_watch, '_watch_ended', lambda *_: fault != 'running-waiter')
    fields = {'task': 'another-task'} if fault == 'different-task' else (
        {'call': 'another-call'} if fault == 'different-call' else {})
    native = resume_notice(**fields)
    rows = [native, dict(native, uuid='second-notice', timestamp=stamp(6.03))] if fault == 'duplicate' else [native]
    append(path, *rows, complete=fault != 'partial')
    assert claude.state(path).queue_error == STOPPED_RESUME
    assert claude.state(path).pending_tools is None


def interrupted_watch():
    partial = resume()[:5]
    # A dated transcript-only row whose exact continuation is unmeasured.
    breaker = row('user', 6.02, OLD, queueTranscriptOnly=True, queueSkipAttachments=True,
                  message=dict(role='user', content='<local-command-stdout>Synthetic</local-command-stdout>'))
    return [*origin(OLD), *partial, breaker]


def test_historical_bash_latch_is_scoped_only_by_a_proved_successor(tmp_path):
    path = record(tmp_path, interrupted_watch())
    launch(path, -1, version=OLD)
    original = path.read_bytes()
    assert claude.state(path).queue_error == UNRESOLVED
    launch(path, 7, resume=True, proved=False)
    assert claude.state(path).queue_error == UNRESOLVED
    launch(path, 7, resume=True)
    assert claude.state(path).queue_error is None
    assert claude.state(path).pending_tools == ()
    assert path.read_bytes() == original
    append(path, dict(resume_notice(task='new-task'), timestamp=stamp(8)))
    assert claude.state(path).queue_error == STOPPED_RESUME


def test_the_successor_keeps_unresolved_bash_work_and_its_own_latch(tmp_path):
    from .test_claude_331_bash_resume import before_restart, resume_rows

    broken = [*before_restart(), resume_rows()[0], typed(4.1, 'break', 'Synthetic interruption.'),
              row('system', 4.2, NEW, subtype='turn_duration')]
    path = record(tmp_path, broken)
    launch(path, -1)
    launch(path, 5, resume=True)
    assert claude.state(path).queue_error == UNRESOLVED  # no end for the old ordinary Bash task

    path = record(tmp_path, interrupted_watch())
    launch(path, 7, resume=True, proved=False)
    # In the same conversation, the only proved current boundary precedes the failure.
    (trusted_files.folder(path.parents[5], 'quill') / trusted_files.LAUNCHES).unlink()
    launch(path, -1, version=OLD)
    launch(path, 5, resume=True)
    assert claude.state(path).queue_error == UNRESOLVED
