"""One numeric service summary per minute; no message, seat or record identities."""

from contextlib import contextmanager
from inspect import isroutine
import time

INTERVAL = 60.0
FIXED_NAMES = frozenset(('pass', 'sleep', 'status', 'status_checks',
                         'drain_drops', 'drain_accounting', 'drain_requests'))


def duty_name(name):
    # Imported when measuring, after Service's module has finished loading.
    from .service import Service
    if isinstance(name, str) and name.isidentifier():
        if name in FIXED_NAMES or isroutine(getattr(Service, name, None)):
            return name
    return 'other'


class Timings:
    def __init__(self):
        self.since = time.monotonic()
        self.rows = {}
        self.status_checks = 0
        self.accounting_waits = 0

    @contextmanager
    def measure(self, name):
        # New Service methods are carried automatically; other labels cannot
        # put runtime identities into the summary or create unbounded rows.
        name = duty_name(name)
        wall, cpu = time.monotonic(), time.process_time()
        try:
            yield
        finally:
            elapsed, used = max(0.0, time.monotonic() - wall), max(0.0, time.process_time() - cpu)
            row = self.rows.setdefault(name, dict(calls=0, wall=0.0, cpu=0.0, max_wall=0.0, max_cpu=0.0))
            row['calls'] += 1
            row['wall'] += elapsed
            row['cpu'] += used
            row['max_wall'] = max(row['max_wall'], elapsed)
            row['max_cpu'] = max(row['max_cpu'], used)

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
        log('service-timing', seconds=now - self.since,
            passes=self.rows.get('pass', {}).get('calls', 0), status_checks=self.status_checks,
            duties=duties)
        self.rows.clear()
        self.status_checks = 0
        self.accounting_waits = 0
        self.since = now
