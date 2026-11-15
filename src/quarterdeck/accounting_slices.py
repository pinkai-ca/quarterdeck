"""Read-only replay planning in a low-priority child; bounded service-owned commits."""

from __future__ import annotations

from collections import Counter, deque
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
import pickle
import resource
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

from . import accounting_health, machine, post, quota, record_owner
from .attribution_cache import Attribution
from .cost_evidence import CostEvidence
from .door import Door
from .harness import get
from .harness import reader_stamp
from .harness.sqlite_snapshot import snapshot
from .harness.json_record import release
from .harness.base import record_fingerprint
from .ledger import Ledger
from .order_attribution import Threads
from .provenance import UNKNOWN, Provenance
from .seat import launch_pool_refusal
from .usage_cache import UsageCache
from .accounting_gate import Gate

BUDGET = 2.0
DRAIN_BUDGET = 0.02
PROGRESS_RECORDS = 200
MAX_PACKET = 8 * 1024 * 1024


def frame(value):
    raw = pickle.dumps(value, protocol=4)
    if len(raw) > MAX_PACKET:
        raise ValueError('accounting plan exceeds packet bound')
    return struct.pack('!I', len(raw)) + raw


def receive(stream):
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4 or not 0 < (length := struct.unpack('!I', header)[0]) <= MAX_PACKET:
        raise ValueError('invalid accounting packet')
    raw = stream.read(length)
    if len(raw) != length:
        raise ValueError('incomplete accounting packet')
    return pickle.loads(raw)


class PlanningDB:
    """Execute against private memory and emit only complete logical write groups.

    Turn, cost and tool changes travel together. SQLite's triggers run in both
    databases; emitting their effects separately would apply them twice.
    """

    def __init__(self, db, send):
        self.raw, self.send, self.batch, self.turn = db, send, [], False

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def flush(self):
        if self.batch:
            self.send(('sql', self.batch))
            self.batch = []

    def execute(self, sql, parameters=()):
        writing = sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE'))
        if writing and sql.startswith('INSERT OR REPLACE INTO turns '):
            self.flush()
            self.turn = True
        result = self.raw.execute(sql, parameters)
        if writing:
            self.batch.append((sql, parameters, False))
            if not self.turn and (len(self.batch) >= 32 or 'INTO turn_attribution ' in sql
                                  or 'INTO record_accounting ' in sql or 'INTO attribution_records ' in sql):
                self.flush()
        return result

    def executemany(self, sql, parameters):
        values = list(parameters)
        result = self.raw.executemany(sql, values)
        self.batch.append((sql, values, True))
        self.turn = False
        self.flush()
        return result

    def __enter__(self):
        self.raw.__enter__()
        return self

    def __exit__(self, *error):
        result = self.raw.__exit__(*error)
        if error[0] is None:
            self.flush()
        else:
            self.batch = []
        return result


def planning_book(path, send):
    # Never open the operational ledger through SQLite, even read-only: a WAL
    # connection could create a shared-memory index beside it.
    _, data = snapshot(path)
    memory = sqlite3.connect(':memory:')
    memory.deserialize(data)
    book = Ledger.__new__(Ledger)
    book.path, book.db = path, PlanningDB(memory, send)
    book._record_active = book._usage_staging = book._usage_commit_pending = False
    book.attributions = Attribution(book.db, create=False)
    book.costs = CostEvidence(book.db, create=False)
    book.provenance = Provenance(book.db, create=False)
    book.quota = quota.Quotas(book.db, create=False)
    book.usage = UsageCache(book.db, create=False)
    return book


def worker():
    """No provider calls, native launches, source writes or operational ledger writes."""
    os.nice(10)
    if sys.platform == 'linux':
        import ctypes
        parent = int(sys.argv[-1])
        if ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != parent:
            return
    limit = 8 * 1024**3
    _, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    from .service import Service

    service = Service.__new__(Service)
    service.read_marks, service.account_errors, service.meters = {}, {}, {}
    attribution_errors = {}
    graph, graph_messages = None, None
    store_runtime = None
    service.pane_died = lambda *args: None  # the parent already observed and logged dead panes

    def send(value):
        sys.stdout.buffer.write(frame(value))
        sys.stdout.buffer.flush()

    while (job := receive(sys.stdin.buffer)) is not None:
        book = None
        try:
            if job.get('reset'):
                if job.get('scope') is None:
                    service.read_marks, service.account_errors = {}, {}
                    attribution_errors = {}
                else:
                    # Rejected plans must be read again, without losing other seats' progress.
                    for item in job['seats']:
                        for path, *_ in item['sources']:
                            service.read_marks.pop(Path(path), None)
                            service.account_errors.pop(Path(path), None)
                            attribution_errors.pop(path, None)
            service.runtime = Path(job['runtime'])
            if service.runtime != store_runtime:
                service.store = post.Store(service.runtime)
                store_runtime = service.runtime
                graph, graph_messages = None, None
            service.accounting_epoch = job['epoch']
            book = planning_book(service.runtime / 'ledger.sqlite', send)
            service.ledger = book
            if job.get('reset') and job.get('scope') is not None:
                for name in job['scope']:
                    service.meters.pop(name, None)
                    for record, in book.db.execute('SELECT DISTINCT record FROM turns WHERE seat=?', (name,)):
                        service.read_marks.pop(Path(record), None)
                        service.account_errors.pop(Path(record), None)
                        attribution_errors.pop(record, None)
            selected, sources = [], {}
            for item in job['seats']:
                current = Path(item['record']) if item['record'] else None
                pane = SimpleNamespace(**item['pane']) if item['pane'] else None
                launch = SimpleNamespace(**item['launch']) if item['launch'] else None
                seat = SimpleNamespace(name=item['name'], home=Path(item['home']), harness=get(item['harness']),
                                       session=SimpleNamespace(human=item['human']), pane=lambda pane=pane: pane,
                                       launched=lambda launch=launch: launch, record=lambda current=current: current)
                selected.append(seat)
                sources[seat.name] = [record_owner.Owned(Path(path), name, get(name) if name else None, reason, transient)
                                      for path, name, reason, transient in item['sources']]
            table = machine.processes()
            reader_keys = reader_stamp.readers()
            owners = {str(item.path): seat.name for seat in selected for item in sources[seat.name]}
            def context(record, names):
                book.db.flush()
                send(('context', str(record) if record else None, names))
            def walls(record, name, events, at):
                book.db.flush()
                send(('walls', str(record), name, events, at))
            for seat in selected:
                service.resolved_seats = lambda seat=seat: [seat]
                for item in sources[seat.name]:
                    names = {seat.name} | {row[0] for row in book.db.execute(
                        'SELECT DISTINCT seat FROM turns WHERE record=?', (str(item.path),))}
                    context(item.path, sorted(names))
                    observations, notices, human = service.account_records(source_records={seat.name: [item]}, source_screens={},
                                                               source_walls=walls, replay=False, process_table=table,
                                                               source_reader_keys=reader_keys)
                    for event, data in notices:
                        send(('notice', event, data))
                    for reading in human:
                        person, record, state, inputs, stamp = reading
                        failure = replace(state.failure, inputs=()) if state.failure else None
                        send(('human', person.name, str(record), failure, stamp))
                        del person, record, state, inputs, failure, stamp, reading
                    release(item.path)
                    del notices, human, observations
            last_attribution = [None]
            def attribution_context(record, names):
                if last_attribution[0] is not None:
                    release(last_attribution[0])
                last_attribution[0] = Path(record)
                context(record, sorted(set(names) | ({owners[record]} if record in owners else set())))
            messages = service.store._loaded()[0]
            if messages is not graph_messages:
                graph = Threads(service.store)
                graph_messages = messages
            with book.record_pass() as staged:
                staged.attributions.replay_stream(service.store, service.runtime, job['epoch'], attribution_context,
                                                  lambda: send(('heartbeat',)), attribution_errors,
                                                  seats=set(job['scope']) if job.get('scope') is not None else None,
                                                  records=owners, graph=graph)
                context(None, [])
                if job.get('scope') is None:
                    staged.finish_catchup(job['epoch'])
            if last_attribution[0] is not None:
                release(last_attribution[0])
            send(('done',))
        except Exception as error:
            send(('error', accounting_health.cause(error)))
            service.read_marks, service.account_errors, service.meters = {}, {}, {}
        finally:
            if book is not None:
                book.close()


class Driver:
    """One child and one resumable incoming plan; no database transaction spans calls."""

    def __init__(self):
        self.process = None
        self.plan = None
        self.plan_offset = 0
        self.incoming, self.outgoing = bytearray(), bytearray()
        self.ready = deque()
        self.events = deque()
        self.inventory = None
        self.observations = None
        self.seats, self.screens = [], []
        self.active = False
        self.started = None
        self.context = (None, [])
        self.identities, self.invalid = {}, set()
        self.progress = 0
        self.reset_child = False
        self.changed = False
        self.retry_seats = set()
        self.scope = None
        self.watched = set()
        self.completion = []
        self.job = 0
        self.counts, self.records = Counter(), set()
        self.completed_records = self.reported_records = 0
        self.gate = Gate()
        self.step_changes = 0

    def close(self):
        self.gate.reject()
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=0.2)
            self.process.stdin.close()
            self.process = None
        if self.plan is not None:
            self.plan.close()
            self.plan = None
        self.plan_offset = 0

    def collect(self, service):
        for seat in service.resolved_seats():
            if self.scope is not None and seat.name not in self.scope:
                continue
            # One seat's bad file (its folder is its jail's to write) holds that seat alone, never the job.
            guard = getattr(service, 'seat_guard', None)
            collected = False
            with guard('accounting', seat) if guard is not None else nullcontext():
                pane, launch = seat.pane(), seat.launched()
                if pane is not None and pane.dead:
                    service.pane_died(seat, pane)
                    pane = None
                record = seat.record()
                sources = [(str(item.path), item.name, item.reason, item.transient) for item in record_owner.records(seat)]
                screen = Door(seat).screen() if pane is not None and seat.harness.screen_walls else None
                # law: every-turn-accounted -- a pool not declared for the seat is never charged; its turns count unattributed
                pool = launch.pool if launch is not None and launch_pool_refusal(seat, launch) is None else UNKNOWN
                entry = dict(name=seat.name, home=str(seat.home), harness=seat.harness.name,
                             human=seat.session.human, record=str(record) if record else None,
                             pane=dict(pid=pane.pid, dead=False) if pane else None,
                             launch=dict(at=launch.at, pool=pool, harness=record_owner.launch_reader(launch),
                                         price=getattr(launch, 'price', None)) if launch else None,
                             sources=sources)
                collected = True
            if not collected:
                continue
            self.screens.append((seat, screen, launch))
            self.seats.append(entry)
            yield

    def start(self, service):
        self.gate.configure(service, self.seats, self.scope)
        if self.gate.begin(service, (path for item in self.seats for path, *_ in item['sources'])) is False:
            self.active = False
            return False  # inventory raced a barrier/outside seat; the next duty starts full
        if self.process is None:
            environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', OPENBLAS_NUM_THREADS='1')
            for name in ('QD_SEAT', 'QD_SHIP', 'QD_TMUX_SOCKET', 'TMUX', 'TMUX_PANE'):
                environment.pop(name, None)
            # An anonymous regular file cannot fill a pipe while the service is
            # occupied by a long duty. Only this parent and its child hold it.
            self.plan = tempfile.TemporaryFile(dir=service.runtime, buffering=0)
            self.process = subprocess.Popen([sys.executable, '-m', __name__, '--worker', str(os.getpid())],
                                            stdin=subprocess.PIPE, stdout=self.plan,
                                            stderr=subprocess.DEVNULL, env=environment)
            os.set_blocking(self.process.stdin.fileno(), False)
        else:
            # 'done' is the last flushed frame; the child now waits for its next
            # job. Reset the shared file offset only at this job boundary.
            self.plan.truncate(0)
            os.lseek(self.plan.fileno(), 0, os.SEEK_SET)
        self.plan_offset = 0
        self.outgoing.extend(frame(dict(runtime=str(service.runtime), epoch=service.accounting_epoch,
                                        seats=self.seats, reset=self.reset_child,
                                        scope=sorted(self.scope) if self.scope is not None else None)))
        self.reset_child = False
        self.started = time.monotonic()
        self.job += 1
        service.log('accounting-gate-start', job=self.job, decision=self.gate.decision)
        service.log('accounting-job-start', job=self.job, seats=len(self.seats),
                    scope=sorted(self.scope) if self.scope is not None else None,
                    sources=sum(len(item['sources']) for item in self.seats))
        return True

    def pump(self):
        moved = False
        if self.outgoing:
            try:
                count = os.write(self.process.stdin.fileno(), self.outgoing[:65536])
                del self.outgoing[:count]
                moved = bool(count)
            except BlockingIOError:
                pass
        if self.ready:
            return moved  # retain at most one input chunk (or one bounded large frame)
        raw = os.pread(self.plan.fileno(), 65536, self.plan_offset)
        self.plan_offset += len(raw)
        if not raw and self.process.poll() is not None and not self.ready:
            raise OSError('accounting child ended')
        self.incoming.extend(raw)
        moved |= bool(raw)
        while len(self.incoming) >= 4:
            length = struct.unpack('!I', self.incoming[:4])[0]
            if not 0 < length <= MAX_PACKET:
                raise ValueError('invalid accounting plan frame')
            if len(self.incoming) < length + 4:
                break
            self.ready.append(pickle.loads(self.incoming[4:length + 4]))
            del self.incoming[:length + 4]
        return moved

    def drain(self, service):
        """Apply ready groups between duties, without waiting or starting a job."""
        if self.active:
            self.step(service, seconds=DRAIN_BUDGET, wait=False)

    def report_progress(self, service):
        if self.completed_records - self.reported_records >= PROGRESS_RECORDS:
            service.log('accounting-job-progress', job=self.job, records=self.completed_records,
                        unique_records=len(self.records), counts=dict(self.counts),
                        seconds=time.monotonic() - self.started,
                        plan_bytes=os.fstat(self.plan.fileno()).st_size)
            self.reported_records = self.completed_records

    @staticmethod
    def identity(runtime, name):
        if Path(name).name != name:
            raise ValueError('invalid accounting seat identity')
        folder = runtime / 'seats' / name
        return tuple(record_fingerprint(folder / part) for part in ('launch.json', record_owner.REGISTRY,
                                                                    'record-launches.jsonl'))

    def valid(self, service, names=None):
        names = self.context[1] if names is None else names
        self.watched.update(names)
        for name in names:
            if name not in self.identities or self.identity(service.runtime, name) != self.identities[name]:
                self.invalid.add(name)
        return not self.invalid.intersection(names)

    def callbacks(self, service, deadline):
        while self.events and time.monotonic() < deadline:
            packet, names = self.events.popleft()
            if packet[0] != 'done' and not self.valid(service, names):
                continue
            if packet[0] == 'notice':
                service.log(packet[1], **packet[2])
            elif packet[0] == 'human':
                from .turn_failure import publish
                seat = next((s for s in service.resolved_seats() if s.name == packet[1]), None)
                if seat is not None and str(seat.record()) == packet[2]:
                    publish(seat, Path(packet[2]), SimpleNamespace(failure=packet[3]), (), packet[4])
            elif packet[0] == 'done':
                self.valid(service, tuple(self.watched))
                if self.invalid:
                    self.retry_seats = set(self.invalid)
                    self.reset_child = True
                    service.log('accounting-job-reset', job=self.job, seats=sorted(self.invalid))
                else:
                    # Completion is global. Validate even identities changed after the last
                    # record group before committing it; never finish and then reopen history.
                    with service.ledger.record_pass() as book:
                        for sql, parameters, many in self.completion:
                            (book.db.executemany if many else book.db.execute)(sql, parameters)
                self.active = False
                service.accounting_health.committed()
                service.log('accounting-job-end', job=self.job, seconds=time.monotonic() - self.started,
                            records=len(self.records), counts=dict(self.counts),
                            retry=sorted(self.invalid), pending=service.ledger.catching_up())
                if self.scope is None and self.changed and not self.invalid:
                    service.log('accounting-caught-up', seconds=time.monotonic() - self.started)
                self.gate.finish(service, self.step_changes, full=self.scope is None and not self.invalid
                                 and not service.ledger.catching_up(),
                                 committed=not self.invalid and not service.ledger.catching_up())
        return not self.active

    def step(self, service, *, seconds=None, wait=True):
        deadline = time.monotonic() + (BUDGET if seconds is None else seconds)
        before = service.accounting_health.shown()
        self.step_changes = service.ledger.db.total_changes
        try:
            if (self.active and self.scope is not None
                    and (self.gate.full_due() or service.ledger.catching_up())):
                # Each prior slice is already committed. Discard only the remaining
                # private plan, so a long scoped job cannot postpone the full scan.
                self.close()
                self.active = False
                self.inventory = self.observations = None
                self.incoming.clear()
                self.outgoing.clear()
                self.ready.clear()
                self.events.clear()
                self.retry_seats.clear()
                self.reset_child = True
                service.log('accounting-job-reset', job=self.job, seats=[], reason='full-scan-due')
            if self.events and self.callbacks(service, deadline):
                return
            if not self.active:
                if self.process is not None and self.process.poll() is not None:
                    raise OSError('accounting child ended')
                if not self.gate.allowed(service, retry=bool(self.retry_seats)):
                    timings = getattr(service, 'timings', None)
                    if timings is not None:
                        timings().accounting_waits += 1
                    return
                self.active = True
                self.started = None
                self.changed = False
                self.invalid = set()
                self.scope = None if self.gate.force_full else (self.retry_seats or self.gate.scope)
                self.retry_seats = set()
                self.completion = []
                self.counts, self.records = Counter(), set()
                self.completed_records = self.reported_records = 0
                self.context = (None, [])
                self.seats, self.screens = [], []
                self.inventory = iter(self.collect(service))
            if self.inventory is not None:
                while time.monotonic() < deadline:
                    try:
                        next(self.inventory)
                    except StopIteration:
                        self.inventory = None
                        break
                if self.inventory is not None:
                    return
                self.observations = iter(self.screens)
            if self.observations is not None:
                with service.ledger.record_pass() as book:
                    while time.monotonic() < deadline:
                        try:
                            seat, screen, launch = next(self.observations)
                        except StopIteration:
                            self.observations = None
                            break
                        quota.observe_screen(book, seat, screen, launch, commit=False)
                if self.observations is not None:
                    return
                self.screens = []
                names = {item['name'] for item in self.seats} | {
                    row[0] for row in service.ledger.db.execute('SELECT DISTINCT seat FROM turns')} | (self.scope or set())
                self.identities = {name: self.identity(service.runtime, name) for name in names}
                self.watched = set(names) if self.scope is None else set(self.scope)
                if self.start(service) is False:
                    return
            with service.ledger.record_pass() as book:
                while time.monotonic() < deadline:
                    self.pump()
                    if not self.ready:
                        if not wait:
                            break
                        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
                        continue
                    packet = self.ready.popleft()
                    self.counts[packet[0]] += 1
                    if packet[0] == 'context':
                        if self.context[0] is not None:
                            self.completed_records += 1
                        self.context = packet[1:]
                        self.watched.update(self.context[1])
                        if packet[1] is not None:
                            self.records.add(packet[1])
                    elif packet[0] == 'sql':
                        valid = self.valid(service)
                        for sql, parameters, many in packet[1]:
                            if 'accounting_progress' in sql:
                                self.completion.append((sql, parameters, many))
                            elif valid:
                                changes = book.db.total_changes
                                (book.db.executemany if many else book.db.execute)(sql, parameters)
                                self.counts['applied_statements'] += 1
                                if book.db.total_changes != changes:
                                    self.changed = True
                            else:
                                self.counts['discarded_statements'] += 1
                    elif packet[0] == 'walls' and self.valid(service):
                        changes = book.db.total_changes
                        book.quota.record(Path(packet[1]), packet[2], packet[3], book.provenance, packet[4], commit=False)
                        self.changed |= book.db.total_changes != changes
                    elif packet[0] == 'heartbeat':
                        service.heartbeat()
                    elif packet[0] == 'error':
                        raise RuntimeError(packet[1])
                    elif packet[0] == 'done':
                        self.events.append((packet, self.context[1]))
                        break
                    elif packet[0] in ('notice', 'human') and self.valid(service):
                        self.events.append((packet, self.context[1]))
                    self.progress += 1
                    if self.progress % 200 == 0:
                        service.heartbeat()
            self.report_progress(service)  # after commit, never inside the write transaction
            self.callbacks(service, deadline)
        except Exception as error:
            if self.started is not None and self.active:
                service.log('accounting-job-end', job=self.job, seconds=time.monotonic() - self.started,
                            records=len(self.records), counts=dict(self.counts), error=accounting_health.cause(error))
            self.close()
            self.active, self.inventory, self.observations = False, None, None
            self.incoming.clear()
            self.outgoing.clear()
            self.ready.clear()
            self.events.clear()
            try:
                service.ledger.begin_catchup(service.accounting_epoch, force=True)
            except Exception:
                pass  # retain the original failure and the last committed progress
            if service.accounting_health.failed(accounting_health.cause(error), post.now()):
                service.log('accounting-failing', cause=service.accounting_health.current)
        finally:
            self.gate.own(self.step_changes, service.ledger.db.total_changes)
            service.publish_accounting_health(force=True)
            service.accounting_shown_changed(before)


if __name__ == '__main__':
    worker()
