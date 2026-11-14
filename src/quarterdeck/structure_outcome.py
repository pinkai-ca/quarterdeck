"""Read-only ended-turn proof and one completed service pass of report grace.

Only Service advances Passes. Status reads fixed derived waits; it cannot age a
candidate. Restarting the service forgets candidates and grants grace again.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path

from . import durable, input_receipt, pending, post, post_dir, record_owner, recovery
from .door import Door
from .ship import ShipError
from .tmux import TmuxError

NORMAL = 'turn ended without a report'
LENGTH = 'turn ended on a length cap without a report'
REASONS = frozenset({NORMAL, LENGTH})
UNKNOWN = frozenset({'ownership unknown', 'record proof unknown', 'seat active',
                     'tools or queue unknown', 'input association unknown',
                     'thinking recovery unresolved', 'service pass unknown'})
READ_ERRORS = (ShipError, OSError, ValueError, TypeError, KeyError, IndexError,
               AttributeError, RecursionError, TmuxError)


@dataclass
class Passes:
    current: int = 0
    completed: int = 0
    active: bool = False
    candidates: dict[str, tuple[tuple, int]] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)

    def begin(self):
        self.current += 1
        self.active = True
        self.seen.clear()

    def finish(self):
        self.candidates = {key: value for key, value in self.candidates.items() if key in self.seen}
        self.completed = self.current
        self.active = False

    def observe(self, request, signature):
        if not self.active:
            self.candidates.pop(request, None)
            return False
        saved = self.candidates.get(request)
        if saved is None or saved[0] != signature:
            self.candidates[request] = signature, self.current
            return False
        return self.current > saved[1] and self.completed >= saved[1]


def _path(runtime, request):
    if not isinstance(request, str) or not request:
        raise ValueError('invalid wait request')
    return runtime / 'post' / 'structure-waits' / (hashlib.sha256(request.encode()).hexdigest() + '.json')


def clear(service, order):
    service.structure_passes.candidates.pop(order.id, None)
    clear_wait(service.runtime, order.id)


def clear_wait(runtime, request):
    path = _path(runtime, request)
    try:
        post_dir.remove(path)
    except FileNotFoundError:
        return


def _wait_data(data):
    if (not isinstance(data, dict) or set(data) != {'format', 'request', 'member', 'reason', 'observation'}
            or type(data['format']) is not int or data['format'] != 1
            or not isinstance(data['request'], str) or not data['request']
            or not isinstance(data['member'], str) or not data['member']
            or not isinstance(data['reason'], str) or data['reason'] not in UNKNOWN | {'turn ended, no report'}):
        raise ValueError('invalid structure wait')
    saved = data['observation']
    if data['reason'] == 'turn ended, no report':
        if (not isinstance(saved, list) or len(saved) != 8
                or any(not isinstance(saved[index], str) or not saved[index] for index in (0, 2, 3, 4, 5))
                or type(saved[1]) is not int or type(saved[7]) is not int
                or not isinstance(saved[6], list) or len(saved[6]) != 5
                or any(type(value) is not int for value in saved[6])):
            raise ValueError('invalid structure observation')
    elif saved is not None:
        raise ValueError('unknown wait has no observation authority')
    return data


def _wait(service, name, order, reason, signature=None):
    data = _wait_data(dict(format=1, request=order.id, member=name, reason=reason,
                     observation=json.loads(json.dumps(signature[:8])) if signature is not None else None))
    path = _path(service.runtime, order.id)
    try:
        if _wait_data(json.loads(post_dir.read_text(path))) == data:
            return
    except READ_ERRORS:
        pass  # a fresh source observation replaces only derived display metadata
    post_dir.ensure_directory(path.parent)
    durable.replace_file(path, json.dumps(data) + '\n')


def _active_requests(data, kind):
    if not isinstance(data, dict) or type(data.get('format')) is not int or data['format'] != 1:
        raise ValueError('invalid structure metadata')
    if kind == 'group':
        from .group_post import FAILURE_REASONS
        copies, failed, notice = data['copies'], data.get('failed', {}), data['notice']
        if (not isinstance(copies, list) or not copies or not isinstance(failed, dict)
                or (notice is not None and not isinstance(notice, dict))
                or any(not isinstance(copy, dict) or not isinstance(copy.get('id'), str) or not copy['id']
                       or not isinstance(copy.get('to'), str) or not copy['to'] for copy in copies)):
            raise ValueError('invalid group metadata')
        members = {copy['id']: copy['to'] for copy in copies}
        if (len(members) != len(copies) or any(
                identity not in members or not isinstance(outcome, dict)
                or outcome.get('member') != members[identity]
                or not isinstance(outcome.get('reason'), str) or outcome['reason'] not in FAILURE_REASONS
                for identity, outcome in failed.items())):
            raise ValueError('invalid group outcomes')
        return set(members) - set(failed) if notice is None else set()
    status, steps = data['status'], data['steps']
    if not isinstance(status, str) or status not in ('running', 'done', 'stopped') or not isinstance(steps, list):
        raise ValueError('invalid cascade metadata')
    if not steps:
        return set()
    step = steps[-1]
    if (not isinstance(step, dict) or not isinstance(step.get('seat'), str)
            or (step.get('order') is not None and not isinstance(step['order'], str))
            or ('report' in step and (not isinstance(step['report'], str) or not step['report']))):
        raise ValueError('invalid cascade step')
    return {step['order']} if status == 'running' and step.get('order') and 'report' not in step else set()


def _outstanding(runtime):
    active, unreadable = set(), False
    for kind, folder, pattern in (('group', runtime / 'post' / 'groups', '*.json'),
                                  ('cascade', runtime / 'ring', '*/state.json')):
        try:
            paths = post_dir.json_paths(folder) if kind == 'group' else sorted(folder.glob(pattern))
        except READ_ERRORS:
            unreadable = True
            continue
        for path in paths:
            try:
                active.update(_active_requests(json.loads(path.read_text()), kind))
            except READ_ERRORS:
                unreadable = True
    return active, unreadable


def waits(ship, selected=()):
    """Read fixed waits, invalidating changed generations without advancing grace."""
    runtime = ship.runtime
    lines = []
    try:
        active, unreadable = _outstanding(runtime)
    except READ_ERRORS:
        return ['structure wait evidence unreadable']
    if unreadable:
        lines.append('structure wait evidence unreadable')
    store = post.Store(runtime)
    seats = {seat.name: seat for seat in selected}
    try:
        paths = post_dir.json_paths(runtime / 'post' / 'structure-waits')
    except READ_ERRORS:
        return lines + ['structure wait evidence unreadable']
    for path in paths:
        try:
            data = _wait_data(json.loads(path.read_text()))
            if _path(runtime, data['request']) != path or data['member'] not in ship.seats:
                raise ValueError('invalid structure wait')
            order = store.get(data['request'])
            if order.kind != 'order' or order.to != data['member']:
                raise ValueError('wait addressee differs')
            if data['request'] not in active or store.closing_report(order) is not None:
                continue
            reason = data['reason']
            if reason == 'turn ended, no report':
                saved = data['observation']
                seat = seats.get(data['member'])
                current = None
                if seat is not None and hasattr(seat, '_lifecycle'):
                    try:
                        with _status_lock(seat):
                            found, _ = _proof(seat, order, readonly=True)
                            current = json.loads(json.dumps(found[0][:8])) if found is not None else None
                    except READ_ERRORS:
                        pass
                if current != saved:
                    reason = 'record proof unknown'
            if reason == 'turn ended, no report':
                lines.append(f"waiting on {data['member']}: turn ended, no report")
            else:
                lines.append(f"waiting on {data['member']}: turn outcome unknown ({reason})")
        except READ_ERRORS:
            lines.append('structure wait evidence unreadable')
    return lines


@contextmanager
def _status_lock(seat):
    """Read the existing lifecycle inode without creating or repairing it."""
    path = seat.ship.runtime / 'post' / 'lifecycle' / f'{seat.name}.lock'
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError('lifecycle inode unknown')
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        current = path.lstat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError('lifecycle inode changed')
        yield
    finally:
        os.close(descriptor)


class _SavedSeat:
    """A status reading cannot join a record or register an inferred reader."""

    def __init__(self, seat):
        self.seat = seat

    def __getattr__(self, name):
        return getattr(self.seat, name)

    def record(self):
        named = (self.folder / 'record').read_text().strip()
        return Path(named) if named else None

    def reader(self, record):
        rows = record_owner.events(self.folder / record_owner.REGISTRY)
        rows += record_owner.events(self.folder / 'record-launches.jsonl')
        names = {row['harness'] for row in rows if row['record'] == str(record) and row.get('harness')}
        return self.harness.name if names == {self.harness.name} else None


def _stamp(record):
    info = record.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _associated(turn, text, since, inputs):
    """A direct input, or explicit per-turn membership supplied by the reader."""
    if (not turn.timed or turn.started is None or turn.ended is None or turn.failure is not None
            or turn.started > turn.ended or turn.end_reason not in (None, 'stop', 'length')):
        return False
    members = turn.inputs
    if members is None:
        # Old readers can prove a direct input only. A steered input needs
        # explicit membership; presence elsewhere in a record is insufficient.
        if not input_receipt.same_text(turn.input, text):
            return False
        members = tuple(item for item in inputs if item.at == turn.started)
    matches = [item for item in members if input_receipt.same_text(item.text, text)]
    return (len(matches) == 1 and matches[0].at is not None and matches[0].at >= since
            and turn.started <= matches[0].at <= turn.ended
            and (matches[0].kind == 'accepted' or matches[0].kind == 'consumed'
                 and matches[0].consumed_at is not None
                 and matches[0].at <= matches[0].consumed_at <= turn.ended))


def _target(seat, record, state, conversation, order, proofs):
    """Resolve a finished continuation through its already reserved source only."""
    canonical = post.typed([order])
    matches = []
    for proof in proofs:
        meta = proof.data.get('recovery')
        if meta is None:
            continue
        if meta['mode'] != 'done':
            return None  # active, queued, working and held modes all retain their hold
        if (proof.data['record'] != str(record) or not proof.same_launch(seat)
                or meta['source']['conversation'] != conversation):
            continue
        client = recovery._client(seat)
        if client is None:
            return None
        original = client.accepted_input(record, canonical, order.at, conversation)
        if original != meta['source']['work']:
            continue
        if (not recovery._publication(seat, proof) or meta['error'] is not None
                or client.signature != meta['client'] or proof.data['phase'] != 'delivered'
                or not meta['recovery_input']):
            return None
        since = datetime.fromisoformat(proof.data['since'])
        native = client.accepted_input(record, proof.data['payload'], since, conversation)
        if native != meta['recovery_input'] or state.work_id != native:
            return None
        matches.append((proof.data['payload'], since, False))
    if len(matches) > 1:
        return None
    if matches:
        return matches[0]
    launched = seat.launched()
    client = recovery._client(seat) if launched.recovery is not None else None
    if launched.recovery is not None and client is None:
        return None  # declared recovery evidence changed or became unreadable
    due = client is not None and state.capped is not None and client.eligible(state.capped)
    return canonical, order.at, due


def _proof(seat, order, *, readonly=False):
    if readonly:
        seat = _SavedSeat(seat)
    bound_reader = seat.reader if readonly else lambda record: record_owner.pinned_reader(seat, record)
    launched, pane = seat.launched(), seat.pane()
    if (launched is None or pane is None or pane.dead or seat.session.human
            or pane.id != launched.pane or pane.pid != launched.pid
            or record_owner.launch_reader(launched) != seat.harness.name):
        return None, 'ownership unknown'
    record = seat.record()
    if (record is None or seat.home not in record.parents or record.resolve() != record.absolute()
            or bound_reader(record) != seat.harness.name):
        return None, 'record proof unknown'
    before = _stamp(record)
    state = seat.harness.state(record)
    conversation = seat.harness.session_id(record)
    if (not isinstance(conversation, str) or not conversation
            or state.conversation not in (None, conversation) or not state.completed_turns
            or (state.conversation is not None and (not state.input_id or not state.work_id))):
        return None, 'record proof unknown'
    if state.turn_open is not False:
        return None, 'seat active'
    if state.pending_tools != () or state.queued or state.queue_error or state.failure is not None:
        return None, 'tools or queue unknown'
    proofs, errors = pending.journals(seat)
    if (errors or any(p.data['phase'] not in pending.TERMINAL or p.data.get('screen_queue') for p in proofs)):
        return None, 'tools or queue unknown'
    target = _target(seat, record, state, conversation, order, proofs)
    if target is None:
        return None, 'thinking recovery unresolved'
    inputs = seat.harness.inputs(record)
    if not hasattr(inputs, 'diagnostics') or inputs.diagnostics()['total']:
        return None, 'input association unknown'
    turn = state.completed_turns[-1]
    text, since, recovery_due = target
    if (not _associated(turn, text, max(since, launched.when), inputs)
            or state.last_input_at is None or turn.started is None or turn.ended is None
            or not turn.started <= state.last_input_at <= turn.ended
            or not input_receipt.same_text(state.last_input or '', text)):
        return None, 'input association unknown'
    door = Door(seat)
    screen = door.screen()
    if (screen is None or screen.box != '' or screen.working is not False or screen.queued
            or screen.unknown or screen.interruption or screen.starting
            or (seat.harness.synthetic and not screen.shows) or not door.reading(screen).open):
        return None, 'seat active'
    # The screen and input reader must describe the same unchanged native file
    # and live identity as the completion, not an earlier idle snapshot.
    if (seat.launched() != launched or seat.pane() != pane or seat.record() != record
            or _stamp(record) != before or seat.harness.session_id(record) != conversation
            or bound_reader(record) != seat.harness.name):
        return None, 'ownership unknown'
    signature = (launched.pane, launched.pid, launched.at, seat.harness.name, str(record),
                 conversation, before, turn.index, turn.started.isoformat(), turn.ended.isoformat())
    return (signature, LENGTH if turn.end_reason == 'length' else NORMAL, recovery_due), None


@contextmanager
def observe(service, name, order):
    """Keep ownership/admission through the caller's final report check and save.

    The caller saves only its existing terminal intent inside this scope, then
    publishes its notice afterward so recipient admission locks never nest.
    """
    tracker = getattr(service, 'structure_passes', None)
    if tracker is None:
        tracker = service.structure_passes = Passes()  # no pass is active; cannot settle
    if order is not None:
        tracker.seen.add(order.id)
    if order is None:
        yield None
        return
    with ExitStack() as held:
        try:
            seat = service.seat(name)
            held.enter_context(seat._lifecycle(automatic=True))
            held.enter_context(post.admission(service.runtime, name))
        except READ_ERRORS:
            tracker.candidates.pop(order.id, None)
            _wait(service, name, order, 'ownership unknown')
            yield None
            return
        if service.store.closing_report(order) is not None:
            clear(service, order)
            yield None
            return
        try:
            found, unknown = _proof(seat, order)
        except READ_ERRORS:
            found, unknown = None, 'record proof unknown'
        if found is None or not tracker.active:
            tracker.candidates.pop(order.id, None)
            _wait(service, name, order, unknown or 'service pass unknown')
            yield None
            return
        signature, reason, recovery_due = found
        ready = tracker.observe(order.id, signature)
        _wait(service, name, order, 'turn ended, no report', signature)
        if ready and recovery_due:
            # A supported cap still awaiting its existing recovery reservation
            # cannot be mistaken for a final outcome after that opportunity ran.
            _wait(service, name, order, 'thinking recovery unresolved')
            ready = False
        if ready:
            # Recheck immediately before the coordinator saves its outcome.
            try:
                latest, unknown = _proof(seat, order)
            except READ_ERRORS:
                latest, unknown = None, 'record proof unknown'
            if latest != found:
                tracker.candidates.pop(order.id, None)
                _wait(service, name, order, unknown or 'record proof unknown')
                ready = False
        yield reason if ready else None
