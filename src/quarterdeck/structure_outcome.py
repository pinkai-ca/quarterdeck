"""Read-only ended-turn proof and one completed service pass of report grace.

Only Service advances Passes. Status reads fixed derived waits; it cannot age a
candidate. Restarting the service forgets candidates and grants grace again.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json

from . import durable, input_receipt, pending, post, record_owner, recovery
from .door import Door
from .ship import ShipError
from .tmux import TmuxError

NORMAL = 'turn ended without a report'
LENGTH = 'turn ended on a length cap without a report'
REASONS = frozenset({NORMAL, LENGTH})
UNKNOWN = frozenset({'ownership unknown', 'record proof unknown', 'seat active',
                     'tools or queue unknown', 'input association unknown',
                     'thinking recovery unresolved', 'service pass unknown'})


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
    return runtime / 'post' / 'structure-waits' / (hashlib.sha256(request.encode()).hexdigest() + '.json')


def clear(service, order):
    service.structure_passes.candidates.pop(order.id, None)
    clear_wait(service.runtime, order.id)


def clear_wait(runtime, request):
    path = _path(runtime, request)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    durable.sync_folder(path.parent)


def _wait(service, name, order, reason, signature=None):
    data = dict(format=1, request=order.id, member=name, reason=reason,
                observation=json.loads(json.dumps(signature[:8])) if signature is not None else None)
    path = _path(service.runtime, order.id)
    try:
        if json.loads(path.read_text()) == data:
            return
    except (OSError, ValueError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    durable.replace_file(path, json.dumps(data) + '\n')


def _outstanding(runtime):
    active, unreadable = set(), False
    for path in (runtime / 'post' / 'groups').glob('*.json'):
        try:
            data = json.loads(path.read_text())
            if data['notice'] is None:
                active.update(item['id'] for item in data['copies'] if item['id'] not in data.get('failed', {}))
        except (OSError, ValueError, KeyError, TypeError):
            unreadable = True
    for path in (runtime / 'ring').glob('*/state.json'):
        try:
            data = json.loads(path.read_text())
            if data['status'] == 'running' and data['steps']:
                step = data['steps'][-1]
                if step.get('order') and 'report' not in step:
                    active.add(step['order'])
        except (OSError, ValueError, KeyError, TypeError):
            unreadable = True
    return active, unreadable


def waits(ship, selected=()):
    """Read fixed waits, invalidating changed generations without advancing grace."""
    runtime = ship.runtime
    lines = []
    try:
        active, unreadable = _outstanding(runtime)
    except (OSError, ValueError, KeyError, TypeError):
        return ['structure wait evidence unreadable']
    if unreadable:
        lines.append('structure wait evidence unreadable')
    store = post.Store(runtime)
    seats = {seat.name: seat for seat in selected}
    for path in sorted((runtime / 'post' / 'structure-waits').glob('*.json')):
        try:
            data = json.loads(path.read_text())
            if (type(data['format']) is not int or data['format'] != 1
                    or _path(runtime, data['request']) != path
                    or data['member'] not in ship.seats
                    or data['reason'] not in UNKNOWN | {'turn ended, no report'}):
                raise ValueError('invalid structure wait')
            order = store.get(data['request'])
            if order.kind != 'order' or order.to != data['member']:
                raise ValueError('wait addressee differs')
            if data['request'] not in active or store.closing_report(order) is not None:
                continue
            reason = data['reason']
            if reason == 'turn ended, no report':
                saved = data['observation']
                if not isinstance(saved, list) or len(saved) != 8:
                    raise ValueError('missing observation')
                seat = seats.get(data['member'])
                current = None
                if seat is not None and hasattr(seat, '_lifecycle'):
                    try:
                        with seat._lifecycle(automatic=True):
                            found, _ = _proof(seat, order)
                            current = json.loads(json.dumps(found[0][:8])) if found is not None else None
                    except (ShipError, OSError, ValueError, TypeError, KeyError, TmuxError):
                        pass
                if current != saved:
                    reason = 'record proof unknown'
            if reason == 'turn ended, no report':
                lines.append(f"waiting on {data['member']}: turn ended, no report")
            else:
                lines.append(f"waiting on {data['member']}: turn outcome unknown ({reason})")
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            lines.append('structure wait evidence unreadable')
    return lines


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


def _proof(seat, order):
    launched, pane = seat.launched(), seat.pane()
    if (launched is None or pane is None or pane.dead or seat.session.human
            or pane.id != launched.pane or pane.pid != launched.pid
            or record_owner.launch_reader(launched) != seat.harness.name):
        return None, 'ownership unknown'
    record = seat.record()
    if (record is None or seat.home not in record.parents or record.resolve() != record.absolute()
            or record_owner.pinned_reader(seat, record) != seat.harness.name):
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
    if (screen is None or screen.box != '' or screen.working or screen.queued
            or screen.unknown or screen.interruption or screen.starting
            or (seat.harness.synthetic and not screen.shows) or not door.reading(screen).open):
        return None, 'seat active'
    # The screen and input reader must describe the same unchanged native file
    # and live identity as the completion, not an earlier idle snapshot.
    if (seat.launched() != launched or seat.pane() != pane or seat.record() != record
            or _stamp(record) != before or seat.harness.session_id(record) != conversation):
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
        except (ShipError, OSError, TmuxError):
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
        except (ShipError, OSError, ValueError, TypeError, KeyError, TmuxError):
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
            except (ShipError, OSError, ValueError, TypeError, KeyError, TmuxError):
                latest, unknown = None, 'record proof unknown'
            if latest != found:
                tracker.candidates.pop(order.id, None)
                _wait(service, name, order, unknown or 'record proof unknown')
                ready = False
        yield reason if ready else None
