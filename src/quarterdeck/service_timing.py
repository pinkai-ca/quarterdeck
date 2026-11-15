"""One numeric service summary per minute; no message, seat or record identities."""

from contextlib import contextmanager
import time

INTERVAL = 60.0


class Timings:
    def __init__(self):
        self.since = time.monotonic()
        self.rows = {}
        self.status_checks = 0

    @contextmanager
    def measure(self, name):
        # Callers supply code's duty names, never message or runtime identities.
        # Keep new steps automatically instead of silently omitting their cost.
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
        log('service-timing', seconds=now - self.since,
            passes=self.rows.get('pass', {}).get('calls', 0), status_checks=self.status_checks,
            duties={name: dict(row) for name, row in self.rows.items()})
        self.rows.clear()
        self.status_checks = 0
        self.since = now
