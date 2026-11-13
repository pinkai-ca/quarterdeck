"""Independent gates for the seat's own inbox watch at idle decisions (captain ord-23da, on ord-3d12).

Where the only work a turn left running is the seat's own inbox watch, the turn's work counts as ended for a
relaunch, for a move, for a control and for readiness after compactions. Both proofs are required together: the
record's background call is exactly `qd inbox --wait`, and this seat's watch registration is current (its watch
file, proof, canonical generation and a live process descended from the seat's launch). Any other background work,
any unregistered or stale watch, and any unknown tool state still hold, and are named.

Every case reads a Claude Code 2.1.317-shaped record through the real adapter. The watch is the real
`qd inbox --wait` command, run as its own process against a real drydock ship, so its registration is the
framework's own (law: record-first). Identities and text are synthetic.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import compaction, idle, inbox_watch, post, quota, record_owner, recovery
from quarterdeck import service as servicemod
from quarterdeck import ship as shipmod
from quarterdeck.harness.claude import ClaudeHarness

QD = Path(sys.executable).parent / 'qd'  # this checkout's own qd, never an installed copy
VERSION = '2.1.317'
WATCH = 'qd inbox --wait'
SEAT = 'desk'  # drydock's desk sits in its human session, where `qd inbox --wait` is the seat's own path
WATCH_LABEL = 'inbox watch (registration unproved)'


class Case:
    """One seat with a launch, a record and, when asked, a running watch; consumers are the service's own."""

    def __init__(self, ship, tmp_path, monkeypatch):
        self.ship, self.tmp, self.monkeypatch = ship, tmp_path, monkeypatch
        self.runtime = ship.runtime
        self.folder = self.runtime / 'seats' / SEAT
        self.folder.mkdir(parents=True, exist_ok=True)
        self.base = datetime.now(timezone.utc) - timedelta(seconds=60)
        self.record = tmp_path / 'record-hazel.jsonl'
        self.session = 'session-hazel'
        self.harness = ClaudeHarness()
        self.watches: list[subprocess.Popen] = []
        self.actions: list[str] = []
        self.launch(os.getpid(), self.base)
        self.seat = SimpleNamespace(
            name=SEAT, folder=self.folder, ship=ship, session=SimpleNamespace(human=True),
            spec=SimpleNamespace(pools=('pool-alder', 'pool-birch'), pool_choices=False, harness='claude'),
            pool=SimpleNamespace(name='pool-alder'), is_up=True, harness=self.harness,
            launched=lambda: self.launched, record=lambda: self.record,
            join=lambda: SimpleNamespace(record=self.record, reason=None),
            pane=lambda: SimpleNamespace(id='%41', pid=self.launched.pid, dead=False),
            is_working=lambda: False,
            restart=lambda: self.actions.append('restart') or 'synthetic restart proof',
            fresh=lambda: self.actions.append('fresh') or 'synthetic fresh proof',
            failover_to=lambda pool: self.actions.append(f'move {pool}') or 'synthetic move proof')

    def at(self, seconds: float) -> datetime:
        return self.base + timedelta(seconds=seconds)

    def launch(self, pid: int, at: datetime) -> None:
        (self.folder / 'launch.json').write_text(json.dumps(dict(pid=pid, at=at.isoformat())) + '\n')
        self.launched = SimpleNamespace(pane='%41', pid=pid, at=at.isoformat(), harness='claude', resume=None, version=None)

    def write(self, calls, *, unresolved_delegation=False, extra=()) -> None:
        """A prompt, background Bash calls with their returned task handles, and an ended turn."""
        rows, parent = [], None

        def add(kind, seconds, **fields):
            nonlocal parent
            key = str(uuid.uuid4())
            rows.append(dict(type=kind, timestamp=self.at(seconds).isoformat(), version=VERSION, isSidechain=False,
                             sessionId=self.session, uuid=key, parentUuid=parent, **fields))
            parent = key

        add('user', 10, message=dict(role='user', content='synthetic order hazel'))
        second = 11
        for call, command, background, task in calls:
            add('assistant', second, message=dict(id=f'message-{call}', role='assistant', stop_reason='tool_use',
                                                  content=[dict(type='tool_use', id=call, name='Bash', input=dict(
                                                      command=command, description='synthetic step',
                                                      run_in_background=background))]))
            add('user', second + 1, message=dict(role='user', content=[dict(
                type='tool_result', tool_use_id=call, content='synthetic handle', is_error=False)]),
                toolUseResult=dict(stdout='', stderr='', interrupted=False, isImage=False, noOutputExpected=False,
                                   **({'backgroundTaskId': task} if background else {})))
            second += 2
        if unresolved_delegation:
            add('assistant', second, message=dict(id='message-delegation', role='assistant', stop_reason='tool_use',
                                                  content=[dict(type='tool_use', id='call-delegation', name='Task',
                                                                input=dict(prompt='synthetic delegation'))]))
            second += 1
        for row in extra:
            add(row.pop('type'), second, **row)
            second += 1
        add('assistant', 40, message=dict(id='message-end', role='assistant', stop_reason='end_turn',
                                          content=[dict(type='text', text='synthetic reply')]))
        add('system', 41, subtype='turn_duration', durationMs=31000)
        self.record.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def watch(self) -> subprocess.Popen:
        """The real command, as the seat runs it, waiting for this ship's desk."""
        env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
        env.update(QD_SHIP=str(self.ship.folder), QD_SEAT=SEAT)
        process = subprocess.Popen([str(QD), 'inbox', '--wait'], env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.watches.append(process)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f'the watch ended before registering: {process.communicate()}')
            try:
                proof = json.loads(inbox_watch.proof_path(self.runtime, SEAT).read_text())
                if (proof['generation'] == inbox_watch.generation(self.runtime, SEAT)
                        and post.watch_file(self.runtime, SEAT).read_text().split()[0] == str(process.pid)):
                    return process
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.05)
        pytest.fail('the watch never registered')

    def stop(self, process, sig=signal.SIGINT) -> None:
        if process.poll() is None:
            process.send_signal(sig)
            try:
                process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

    def close(self) -> None:
        for process in self.watches:
            self.stop(process)

    # The consumers, as the service runs them

    def service(self):
        service = servicemod.Service.__new__(servicemod.Service)
        service.ledger = object()
        service.store = SimpleNamespace(new=lambda *args, **kwargs: None)
        service.log = lambda *args, **kwargs: None
        service.resolved_seats = lambda: [self.seat]
        service.known_open = lambda target, pool: True
        self.monkeypatch.setattr(recovery, 'held', lambda seat: None)
        self.monkeypatch.setattr(quota, 'current_walled', lambda *args: True)
        self.monkeypatch.setattr(quota, 'candidate_pools', lambda seat: ['pool-birch'])
        return service

    def consumers(self) -> dict:
        """What each consumer decides now: True where it acted, False where it held."""
        service, decided = self.service(), {}
        self.actions.clear()
        decided['relaunch'] = service.relaunch(self.seat, False) is not None
        service.failover()
        decided['move'] = any(action.startswith('move') for action in self.actions)
        decided['not_idle'] = service.not_idle(self.seat) is None
        reached = []

        class Door:
            def __init__(self, seat):
                pass

            def deliver(self, text, confirm=True):
                reached.append(text)
                return SimpleNamespace(result='synthetic stop before typing')
        self.monkeypatch.setattr(servicemod, 'Door', Door)
        service.control(self.seat, 'effort', 'low')
        decided['control'] = bool(reached)
        self.monkeypatch.setattr(record_owner, 'pinned_reader', lambda seat, record: 'claude')
        _, reading, _ = compaction.current(self.seat)
        decided['readiness'] = reading.state.pending_tools == ()
        return decided


@pytest.fixture
def case(drydock, tmp_path, monkeypatch):
    monkeypatch.setenv('QD_SHIP', str(drydock))
    monkeypatch.delenv('QD_SEAT', raising=False)
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    built = Case(ship, tmp_path, monkeypatch)
    yield built
    built.close()


EVERY = {'relaunch': True, 'move': True, 'not_idle': True, 'control': True, 'readiness': True}
NONE = {name: False for name in EVERY}


def held(case) -> idle.Hold:
    _, hold = idle.reading(case.seat)
    assert hold is not None, 'held'
    return hold


def test_only_the_registered_own_watch_left_running_lets_every_consumer_apply(case):
    case.write([('call-hazel', WATCH, True, 'task-hazel')])
    raw = case.harness.state(case.record)
    assert raw.turn_open is False and raw.pending_tools == ('Bash',), 'the adapter alone still sees work running'
    assert [tool.inbox_watch_started for tool in raw.active_tools] == [case.at(11)], 'the exact native candidate'
    assert held(case).dimension == 'tools-pending', 'with no registration the candidate grants nothing'
    assert case.consumers() == NONE

    watch = case.watch()
    state, hold = idle.reading(case.seat)
    assert hold is None and state.pending_tools == () and state.active_tools == ()
    assert case.consumers() == EVERY, 'relaunch, move, control and readiness all apply'

    case.stop(watch)  # the watch ends; Claude has not yet said the task ended, so its work is running again
    assert held(case).dimension == 'tools-pending'
    assert WATCH_LABEL in held(case).reason
    assert case.consumers() == NONE


def test_other_background_work_beside_the_watch_holds_and_is_named(case):
    case.write([('call-hazel', WATCH, True, 'task-hazel'), ('call-rowan', 'synthetic other command', True, 'task-rowan')])
    case.watch()
    hold = held(case)
    assert hold.dimension == 'tools-pending' and hold.reason.startswith('tools pending: Bash since ')
    assert case.at(14).isoformat() in hold.reason, 'the other work is named from its own returned handle'
    assert WATCH_LABEL not in hold.reason and 'synthetic other command' not in hold.reason and WATCH not in hold.reason
    assert case.consumers() == NONE


def test_other_background_work_alone_holds_whatever_watch_is_registered(case):
    case.write([('call-rowan', 'synthetic other command', True, 'task-rowan')])
    case.watch()
    assert held(case).dimension == 'tools-pending' and case.consumers() == NONE


@pytest.mark.parametrize('command', [
    'qd inbox', 'qd inbox --wait ', ' qd inbox --wait', 'qd inbox --wait; true', 'qd inbox --wait &',
    'qd inbox --wait --seat captain', 'cd /work && qd inbox --wait', 'qd  inbox --wait', 'QD inbox --wait',
])
def test_the_background_call_must_be_exactly_the_watch_command(case, command):
    case.write([('call-hazel', command, True, 'task-hazel')])
    case.watch()
    hold = held(case)
    assert hold.dimension == 'tools-pending' and WATCH_LABEL not in hold.reason
    assert case.consumers() == NONE


def stale(case, how):
    if how == 'absent':
        return
    if how == 'foreign launch':  # bound before the watch registers: only the watch's ancestry can refuse it
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        case.watches.append(other)
        case.launch(other.pid, case.base)
        case.watch()
        return
    if how == 'killed':  # killed without retiring: every file stays, the process is gone
        case.stop(case.watch(), signal.SIGKILL)
        assert post.watch_file(case.runtime, SEAT).exists()
        return
    watch = case.watch()
    if how == 'generation replaced':
        inbox_watch.generation_path(case.runtime, SEAT).write_text('0' * 32 + '\n')
    elif how == 'proof removed':  # the two-field watch file alone, as an older watch wrote it
        inbox_watch.proof_path(case.runtime, SEAT).unlink()
    elif how == 'watch rewritten':
        path = post.watch_file(case.runtime, SEAT)
        path.write_text(f'{watch.pid} {post.now().isoformat(timespec="microseconds")}\n')
    elif how == 'launch replaced':  # a relaunch after the call: the watch belongs to the earlier launch
        case.launch(os.getpid(), case.at(30))


@pytest.mark.parametrize('how', ['absent', 'killed', 'generation replaced', 'proof removed', 'watch rewritten',
                                 'launch replaced', 'foreign launch'])
def test_a_background_watch_without_a_current_registration_holds_and_is_named(case, how):
    case.write([('call-hazel', WATCH, True, 'task-hazel')])
    stale(case, how)
    hold = held(case)
    assert hold.dimension == 'tools-pending' and WATCH_LABEL in hold.reason
    assert case.consumers() == NONE


def test_a_watch_registered_before_the_call_is_an_earlier_watch(case):
    case.watch()
    registered = datetime.fromisoformat(post.watch_file(case.runtime, SEAT).read_text().split()[1])
    case.base = registered - timedelta(seconds=11) + timedelta(milliseconds=50)  # the call 50 ms after it
    case.write([('call-hazel', WATCH, True, 'task-hazel')])
    assert case.at(11) > registered >= datetime.fromisoformat(case.launched.at), 'same launch, call after the watch'
    hold = held(case)
    assert hold.dimension == 'tools-pending' and WATCH_LABEL in hold.reason
    assert case.consumers() == NONE


def test_unknown_tool_state_is_never_cleared_by_a_registered_watch(case):
    case.write([('call-hazel', WATCH, True, 'task-hazel')], unresolved_delegation=True)
    case.watch()
    state, hold = idle.reading(case.seat)
    assert hold.dimension == 'tools-unknown' and state.pending_tools is None
    assert case.consumers() == NONE


def test_an_unknown_ending_of_the_watch_task_is_never_cleared(case):
    notice = ('<task-notification>\n<task-id>task-hazel</task-id>\n<tool-use-id>call-hazel</tool-use-id>\n'
              '<output-file>/synthetic/output</output-file>\n<status>stopped</status>\n'
              '<summary>synthetic summary</summary>\n<note>synthetic resume uncertainty</note>\n</task-notification>')
    case.write([('call-hazel', WATCH, True, 'task-hazel')],
               extra=[dict(type='user', origin=dict(kind='task-notification'),
                           message=dict(role='user', content=notice))])
    case.watch()
    assert held(case).dimension == 'tools-unknown' and case.consumers() == NONE


def test_two_watch_calls_are_never_one_watch(case):
    case.write([('call-hazel', WATCH, True, 'task-hazel'), ('call-aspen', WATCH, True, 'task-aspen')])
    case.watch()
    assert held(case).dimension == 'tools-pending' and case.consumers() == NONE


def test_a_watch_replaced_during_the_reading_supplies_no_idle(case):
    case.write([('call-hazel', WATCH, True, 'task-hazel')])
    case.watch()
    real = case.harness.state
    replaced = []

    def state(record):
        if not replaced:
            replaced.append(case.watch())  # a second watch takes the registration while the record is read
        return real(record)
    case.monkeypatch.setattr(case.harness, 'state', state)
    reading = idle.reading(case.seat)
    assert reading.state is None and reading.hold.dimension == 'identity'


def test_the_registration_keeps_its_two_field_watch_and_shares_the_canonical_generation(case):
    watch = case.watch()
    path = post.watch_file(case.runtime, SEAT)
    pid, registered = path.read_text().split()
    assert int(pid) == watch.pid and datetime.fromisoformat(registered).tzinfo is not None
    assert post.watch_armed(case.runtime, SEAT), 'existing readers of the two-field watch still see it armed'
    token = inbox_watch.generation(case.runtime, SEAT)
    proof = json.loads(inbox_watch.proof_path(case.runtime, SEAT).read_text())
    assert proof['generation'] == token and proof['watch'] == path.read_text(), 'one generation, never a second identity'
    case.stop(watch)
    assert not path.exists() and not inbox_watch.proof_path(case.runtime, SEAT).exists(), 'retired with the watch'
    assert inbox_watch.generation(case.runtime, SEAT) == token, 'the canonical generation survives retirement'
    following = case.watch()
    assert inbox_watch.generation(case.runtime, SEAT) != token, 'each watch registers a new generation'
    case.stop(following)
