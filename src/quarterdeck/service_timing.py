"""One numeric service summary per minute; no message, seat or record identities."""

from contextlib import contextmanager
from inspect import isroutine
import time

INTERVAL = 60.0
FIXED_NAMES = frozenset(('pass', 'sleep', 'status', 'status_post', 'status_checks',
                         'drain_drops', 'drain_accounting', 'drain_requests'))


def duty_name(name):
    # Imported when measuring, after Service's module has finished loading.
    from .service import Service
    if isinstance(name, str) and name.isidentifier():
        if name in FIXED_NAMES or isroutine(getattr(Service, name, None)):
            return name
    return 'other'


class Timings:
    def __init__(self, changes=None):
        """``changes``: the service's ledger write count (its connection's total_changes). Each duty's row then counts
        the ledger rows it wrote, a number under its fixed name: which duties write the ledger outside the accounting
        job, read before any narrowing of the accounting gate's other-writers barrier is built on them."""
        self.since = time.monotonic()
        self.changes = changes
        self.rows = {}
        self.status_checks = 0
        self.accounting_waits = 0
        self.parse_cache = None  # the record cache, read each pass: {records, record_bytes, max_record_bytes, max_records}

    def cache_reading(self, records: int, held: int) -> None:
        """The record cache's size after a pass (harness/json_record): the last, and the most bytes in the minute."""
        before = self.parse_cache or {}
        self.parse_cache = dict(records=records, record_bytes=held,
                                max_record_bytes=max(held, before.get('max_record_bytes', 0)),
                                max_records=max(records, before.get('max_records', 0)))

    @contextmanager
    def measure(self, name):
        # New Service methods are carried automatically; other labels cannot
        # put runtime identities into the summary or create unbounded rows.
        name = duty_name(name)
        wall, cpu = time.monotonic(), time.process_time()
        written = self.written()
        try:
            yield
        finally:
            elapsed, used = max(0.0, time.monotonic() - wall), max(0.0, time.process_time() - cpu)
            row = self.rows.setdefault(name, dict(calls=0, wall=0.0, cpu=0.0, max_wall=0.0, max_cpu=0.0))
            after = self.written()
            if written is not None and after is not None and after >= written:
                row['ledger_writes'] = row.get('ledger_writes', 0) + after - written
            row['calls'] += 1
            row['wall'] += elapsed
            row['cpu'] += used
            row['max_wall'] = max(row['max_wall'], elapsed)
            row['max_cpu'] = max(row['max_cpu'], used)

    def written(self):
        if self.changes is None:
            return None
        try:
            return int(self.changes())
        except Exception:  # a ledger being reopened or closed: this duty's count is simply not taken
            return None

    def publish(self, log):
        now = time.monotonic()
        if now < self.since:
            self.since = now
        if now - self.since < INTERVAL:
            return
        duties = {name: dict(row) for name, row in self.rows.items()}
        if self.accounting_waits:
            # Count-only row: its time is already included in the account duty.
            duties['accounting_waits'] = dict(calls=self.accounting_waits, wall=0.0, cpu=0.0,
                                              max_wall=0.0, max_cpu=0.0)
        extra = {} if self.parse_cache is None else {'parse_cache': self.parse_cache}
        log('service-timing', seconds=now - self.since,
            passes=self.rows.get('pass', {}).get('calls', 0), status_checks=self.status_checks,
            duties=duties, **extra)
        self.rows.clear()
        self.parse_cache = None
        self.status_checks = 0
        self.accounting_waits = 0
        self.since = now
