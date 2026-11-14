"""Read-only replay planning in a low-priority child; bounded service-owned commits."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
import pickle
import resource
import select
import signal
import sqlite3
import struct
import subprocess
import sys
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
from .provenance import Provenance
from .usage_cache import UsageCache

BUDGET = 2.0
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
    service.pane_died = lambda *args: None  # the parent already observed and logged dead panes

    def send(value):
        sys.stdout.buffer.write(frame(value))
        sys.stdout.buffer.flush()

    while (job := receive(sys.stdin.buffer)) is not None:
        book = None
        try:
            if job.get('reset'):
                service.read_marks, service.account_errors = {}, {}
            service.runtime = Path(job['runtime'])
            service.store = post.Store(service.runtime)
            service.status_framework = job['epoch']
            book = planning_book(service.runtime / 'ledger.sqlite', send)
            service.ledger = book
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
            with book.record_pass() as staged:
                staged.attributions.replay_stream(service.store, service.runtime, job['epoch'], attribution_context,
                                                  lambda: send(('heartbeat',)), attribution_errors)
                context(None, [])
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

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=0.2)
            self.process.stdin.close()
            self.process.stdout.close()
            self.process = None

    def collect(self, service):
        for seat in service.resolved_seats():
            pane, launch = seat.pane(), seat.launched()
            if pane is not None and pane.dead:
                service.pane_died(seat, pane)
                pane = None
            record = seat.record()
            sources = [(str(item.path), item.name, item.reason, item.transient) for item in record_owner.records(seat)]
            screen = Door(seat).screen() if pane is not None and seat.harness.screen_walls else None
            self.screens.append((seat, screen, launch))
            self.seats.append(dict(name=seat.name, home=str(seat.home), harness=seat.harness.name,
                                   human=seat.session.human, record=str(record) if record else None,
                                   pane=dict(pid=pane.pid, dead=False) if pane else None,
                                   launch=dict(at=launch.at, pool=launch.pool, harness=record_owner.launch_reader(launch),
                                               price=getattr(launch, 'price', None)) if launch else None,
                                   sources=sources))
            yield

    def start(self, service):
        if self.process is None:
            environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', OPENBLAS_NUM_THREADS='1')
            for name in ('QD_SEAT', 'QD_SHIP', 'QD_TMUX_SOCKET', 'TMUX', 'TMUX_PANE'):
                environment.pop(name, None)
            self.process = subprocess.Popen([sys.executable, '-m', __name__, '--worker', str(os.getpid())],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, env=environment)
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
        self.outgoing.extend(frame(dict(runtime=str(service.runtime), epoch=service.status_framework,
                                        seats=self.seats, reset=self.reset_child)))
        self.reset_child = False
        self.started = time.monotonic()

    def pump(self):
        moved = False
        if self.outgoing:
            try:
                count = os.write(self.process.stdin.fileno(), self.outgoing[:65536])
                del self.outgoing[:count]
                moved = bool(count)
            except BlockingIOError:
                pass
        try:
            raw = os.read(self.process.stdout.fileno(), 65536)
            if not raw and self.process.poll() is not None:
                raise OSError('accounting child ended')
            self.incoming.extend(raw)
            moved |= bool(raw)
        except BlockingIOError:
            pass
        while len(self.incoming) >= 4:
            length = struct.unpack('!I', self.incoming[:4])[0]
            if not 0 < length <= MAX_PACKET:
                raise ValueError('invalid accounting plan frame')
            if len(self.incoming) < length + 4:
                break
            self.ready.append(pickle.loads(self.incoming[4:length + 4]))
            del self.incoming[:length + 4]
        return moved

    @staticmethod
    def identity(runtime, name):
        if Path(name).name != name:
            raise ValueError('invalid accounting seat identity')
        folder = runtime / 'seats' / name
        return tuple(record_fingerprint(folder / part) for part in ('launch.json', record_owner.REGISTRY,
                                                                    'record-launches.jsonl'))

    def valid(self, service, names=None):
        names = self.context[1] if names is None else names
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
                for name, identity in self.identities.items():
                    if self.identity(service.runtime, name) != identity:
                        self.invalid.add(name)
                if self.invalid:
                    service.ledger.begin_catchup(service.status_framework, force=True)
                    self.reset_child = True
                self.active = False
                service.accounting_health.committed()
                if self.changed and not self.invalid:
                    service.log('accounting-caught-up', seconds=time.monotonic() - self.started)
        return not self.active

    def step(self, service):
        deadline = time.monotonic() + BUDGET
        before = service.accounting_health.shown()
        try:
            if self.events and self.callbacks(service, deadline):
                return
            if not self.active:
                self.active = True
                self.changed = False
                self.invalid = set()
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
                    row[0] for row in service.ledger.db.execute('SELECT DISTINCT seat FROM turns')}
                self.identities = {name: self.identity(service.runtime, name) for name in names}
                self.start(service)
            with service.ledger.record_pass() as book:
                while time.monotonic() < deadline:
                    self.pump()
                    if not self.ready:
                        # Keep draining within this slice. Returning after one pipeful
                        # would stretch a cold replay over thousands of normal ticks.
                        select.select([self.process.stdout], [self.process.stdin] if self.outgoing else [], [],
                                      min(0.05, max(0, deadline - time.monotonic())))
                        continue
                    packet = self.ready.popleft()
                    if packet[0] == 'context':
                        self.context = packet[1:]
                    elif packet[0] == 'sql' and self.valid(service):
                        for sql, parameters, many in packet[1]:
                            if 'accounting_progress' not in sql or not self.invalid:
                                changes = book.db.total_changes
                                (book.db.executemany if many else book.db.execute)(sql, parameters)
                                if 'accounting_progress' not in sql and book.db.total_changes != changes:
                                    self.changed = True
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
            self.callbacks(service, deadline)
        except Exception as error:
            self.close()
            self.active, self.inventory, self.observations = False, None, None
            self.incoming.clear()
            self.outgoing.clear()
            self.ready.clear()
            self.events.clear()
            try:
                service.ledger.begin_catchup(service.status_framework, force=True)
            except Exception:
                pass  # retain the original failure and the last committed progress
            if service.accounting_health.failed(accounting_health.cause(error), post.now()):
                service.log('accounting-failing', cause=service.accounting_health.current)
        finally:
            service.publish_accounting_health(force=True)
            service.accounting_shown_changed(before)


if __name__ == '__main__':
    worker()
