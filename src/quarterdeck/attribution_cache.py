"""Service-written attribution beside unchanged turn rows; all turn writes invalidate it."""

from collections import defaultdict
import hashlib
import json
import time
from os.path import abspath
from pathlib import Path

from . import harness, record_owner
from .harness import reader_stamp
from .harness.base import record_fingerprint
from .order_attribution import Threads, Verdict, moment

OUTSIDE_RUNTIME = 'legacy binding outside the runtime, no saved fingerprint'


def source_evidence(source):
    stamp, inputs, error = source
    identity = ((stamp[0], tuple((part[0], part[1], part[4]) for part in stamp[1]))
                if isinstance(stamp[1], tuple) else stamp)
    diagnostics = inputs.diagnostics() if hasattr(inputs, 'diagnostics') else None
    return identity, inputs, error, diagnostics


class Attribution:
    def __init__(self, db, create=True):
        self.db = db
        self.sources = {}
        self.last = None
        self.retries = {}
        if create:
            db.execute('''CREATE TABLE IF NOT EXISTS turn_attribution (
                record TEXT NOT NULL, turn INTEGER NOT NULL, identity TEXT NOT NULL,
                received TEXT NOT NULL, status TEXT NOT NULL, orders TEXT NOT NULL,
                grouping TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL,
                valid INTEGER NOT NULL, PRIMARY KEY(record,turn))''')
            for event in ('INSERT', 'UPDATE'):
                db.execute(f'''CREATE TRIGGER IF NOT EXISTS attribution_after_{event.lower()} AFTER {event} ON turns
                    BEGIN UPDATE turn_attribution SET valid=0 WHERE record=NEW.record AND turn=NEW.turn; END''')
            db.execute('''CREATE TABLE IF NOT EXISTS attribution_records (
                record TEXT PRIMARY KEY, evidence TEXT NOT NULL)''')
            for event in ('INSERT', 'UPDATE', 'DELETE'):
                source = 'OLD' if event == 'DELETE' else 'NEW'
                previous = ' OR record=OLD.record' if event == 'UPDATE' else ''
                db.execute(f'''CREATE TRIGGER IF NOT EXISTS replay_after_{event.lower()}
                    AFTER {event} ON turn_attribution BEGIN
                    DELETE FROM attribution_records WHERE record={source}.record{previous}; END''')

    def present(self):
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_attribution'").fetchone() is not None

    def grouping(self):
        if not self.present():
            return "'pending: ' || order_id"
        return "COALESCE((SELECT a.grouping FROM turn_attribution a WHERE a.record=turns.record AND a.turn=turns.turn AND a.valid=1), 'pending: ' || order_id)"

    def details(self):
        if not self.present():
            return [{'status': 'pending', 'reason': 'attribution replay has not run'}]
        rows = self.db.execute('''SELECT t.record,t.turn,t.seat,t.order_id,a.status,a.orders,a.reason,a.evidence,a.valid
            FROM turns t LEFT JOIN turn_attribution a ON a.record=t.record AND a.turn=t.turn ORDER BY t.record,t.turn''')
        return [dict(record=r, turn=i, seat=s, legacy_order=old, status=status if valid else 'pending',
                     orders=json.loads(orders) if valid else [], reason=reason if valid else 'old/new row awaits service replay',
                     evidence=json.loads(evidence) if valid else [])
                for r,i,s,old,status,orders,reason,evidence,valid in rows]

    def put(self, row, received, verdict):
        identity = json.dumps([row['seat'], row['started'], row['ended']])
        values = (row['record'], row['turn'], identity, json.dumps(received, sort_keys=True), verdict.status,
                  json.dumps(verdict.orders), verdict.key, verdict.reason, json.dumps(verdict.evidence, sort_keys=True), 1)
        old = self.db.execute('SELECT * FROM turn_attribution WHERE record=? AND turn=?', values[:2]).fetchone()
        if old == values:
            return 0
        self.db.execute('INSERT OR REPLACE INTO turn_attribution VALUES (?,?,?,?,?,?,?,?,?,?)', values)
        return 1

    def replay(self, store, runtime, bindings=None, *, commit=True, retry_interval=0, framework=None):
        """All stored keys, including retired seats; called by the service, never a reading.

        Retained exact-envelope evidence survives a missing source if the stored
        seat and turn bounds are unchanged. Old-writer changes require rereading.
        """
        previous = self.last
        self.last = None  # source-cache updates stay dirty through every pre-gate failure
        if bindings is None:
            bindings = defaultdict(set)
            for folder in (Path(runtime)/'seats').glob('*'):
                for name in (record_owner.REGISTRY, 'record-launches.jsonl'):
                    try:
                        events = (record_owner.registry_events(folder) if name == record_owner.REGISTRY
                                  else record_owner.events(folder/name))
                    except OSError:
                        continue
                    for event in events:
                        if event.get('harness'):
                            bindings[event['record']].add(event['harness'])
        rows = [dict(zip(('record', 'turn', 'seat', 'started', 'ended'), row)) for row in self.db.execute(
            'SELECT record,turn,seat,started,ended FROM turns ORDER BY record,turn')]
        by_record, by_seat = defaultdict(list), defaultdict(list)
        for row in rows:
            by_record[row['record']].append(row)
            by_seat[row['seat']].append(row)
        excluded = {record for record in by_record
                    if not Path(abspath(record)).is_relative_to(Path(abspath(runtime)))}
        graph = Threads(store) if framework is not None else None
        invalid_records = {row[0] for row in self.db.execute('''SELECT DISTINCT t.record FROM turns t
            LEFT JOIN turn_attribution a ON a.record=t.record AND a.turn=t.turn
            WHERE a.valid IS NULL OR a.valid=0''')}
        seat_bounds = {seat: hashlib.sha256(json.dumps(bounds, sort_keys=True).encode()).hexdigest()
                       for seat, bounds in by_seat.items()} if framework is not None else {}
        certificates, reused = {}, set()
        reader_keys = reader_stamp.attributions() if framework is not None else {}
        changed = False
        for record in by_record:
            prior = self.sources.get(record)
            if record in excluded:
                stamp = (None, OUTSIDE_RUNTIME)
                changed |= record not in self.sources or self.sources[record][0] != stamp
                self.sources[record] = stamp, [], OUTSIDE_RUNTIME
                continue  # a saved reader/path is not permission to stat or open an external source
            names = bindings.get(record, set())
            reader = next(iter(names)) if len(names) == 1 else None
            stamp = (reader, None)
            try:
                stamp = (reader, record_fingerprint(Path(record))) if reader in harness.NAMES else (reader, None)
                if reader_keys.get(reader) is not None and stamp[1][0][0]:
                    ends = [moment(row['ended']) for row in by_record[record]]
                    ended = max((end for end in ends if end is not None), default=None)
                    # A record can contain several established seat identities.
                    seats = sorted({row['seat'] for row in by_record[record]})
                    scope = [(seat, seat_bounds[seat], graph.scope(seat, ended)) for seat in seats]
                    evidence = hashlib.sha256(json.dumps([reader_keys[reader], stamp, sorted(names), scope]).encode()).hexdigest()
                    old = self.db.execute('SELECT evidence FROM attribution_records WHERE record=?', (record,)).fetchone()
                    if record not in invalid_records and old == (evidence,):
                        reused.add(record)
                        continue
                    certificates[record] = evidence
                if prior is not None and prior[0] == stamp:
                    if not prior[2] or reader not in harness.NAMES or time.monotonic() < self.retries.get(record, 0):
                        if prior[2]:
                            certificates.pop(record, None)
                        continue
                if reader not in harness.NAMES:
                    raise OSError('record reader unavailable or ambiguous')
                inputs = harness.get(reader).attribution_inputs(Path(record))
                if record in certificates and stamp != (reader, record_fingerprint(Path(record))):
                    certificates.pop(record)
                error = None
            except (OSError, ValueError, KeyError, TypeError) as exc:
                inputs, error = [], f'record attribution evidence unavailable: {type(exc).__name__}'
            self.sources[record] = stamp, inputs, error
            if error:
                certificates.pop(record, None)
            self.retries[record] = time.monotonic() + retry_interval if error else 0
            changed |= prior is None or source_evidence(prior) != source_evidence(self.sources[record])
        stamp = tuple(graph.messages) if graph is not None else tuple(store.all())
        invalid = self.db.execute('''SELECT count(*) FROM turns t LEFT JOIN turn_attribution a
            ON a.record=t.record AND a.turn=t.turn WHERE a.valid IS NULL OR a.valid=0''').fetchone()[0]
        if not changed and previous == stamp and not invalid:
            self.last = stamp
            return 0
        graph = graph or Threads(store)
        written = 0
        for record, turns in by_record.items():
            if record in reused:
                continue
            if record in excluded:
                for row in turns:
                    written += self.put(row, [], Verdict('unassociated', (), OUTSIDE_RUNTIME))
                continue
            _, inputs, error = self.sources[record]
            for row in turns:
                written += self.replay_row(record, row, turns, by_seat, graph, inputs, error)
            if record in certificates:
                self.db.execute('INSERT OR REPLACE INTO attribution_records VALUES (?,?)',
                                (record, certificates[record]))
        if commit and self.db.in_transaction:
            self.db.commit()
        self.last = stamp
        return written

    def replay_stream(self, store, runtime, framework, on_record, progress, errors=None, retry_interval=60):
        """The same verdicts, releasing each record's incoming text before the next.

        The child owns this context; only compact turn identities and the post graph
        span records. Certificates remain the durable cache, rather than all inputs.
        """
        errors = {} if errors is None else errors
        bindings = defaultdict(set)
        for folder in (Path(runtime) / 'seats').glob('*'):
            for name in (record_owner.REGISTRY, 'record-launches.jsonl'):
                try:
                    events = (record_owner.registry_events(folder) if name == record_owner.REGISTRY
                              else record_owner.events(folder / name))
                except OSError:
                    continue
                for event in events:
                    if event.get('harness'):
                        bindings[event['record']].add(event['harness'])
        by_record, by_seat = defaultdict(list), defaultdict(list)
        for values in self.db.execute('SELECT record,turn,seat,started,ended FROM turns ORDER BY record,turn'):
            row = dict(zip(('record', 'turn', 'seat', 'started', 'ended'), values))
            by_record[row['record']].append(row)
            by_seat[row['seat']].append(row)
        bounds = {seat: hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
                  for seat, rows in by_seat.items()}
        invalid = {row[0] for row in self.db.execute('''SELECT DISTINCT t.record FROM turns t
            LEFT JOIN turn_attribution a ON a.record=t.record AND a.turn=t.turn
            WHERE a.valid IS NULL OR a.valid=0''')}
        graph = Threads(store)
        written, count = 0, 0
        reader_keys = reader_stamp.attributions() if framework is not None else {}
        for record, turns in by_record.items():
            seats = sorted({row['seat'] for row in turns})
            on_record(record, seats)
            certificate, inputs, error = None, [], None
            if not Path(abspath(record)).is_relative_to(Path(abspath(runtime))):
                for row in turns:
                    written += self.put(row, [], Verdict('unassociated', (), OUTSIDE_RUNTIME))
                continue
            names = bindings.get(record, set())
            reader = next(iter(names)) if len(names) == 1 else None
            stamp = (reader, None)
            try:
                if reader not in harness.NAMES:
                    raise OSError('record reader unavailable or ambiguous')
                stamp = (reader, record_fingerprint(Path(record)))
                ends = [moment(row['ended']) for row in turns]
                ended = max((end for end in ends if end is not None), default=None)
                scope = [(seat, bounds[seat], graph.scope(seat, ended)) for seat in seats]
                if reader_keys.get(reader) is not None and stamp[1][0][0]:
                    certificate = hashlib.sha256(json.dumps([reader_keys[reader], stamp, sorted(names), scope]).encode()).hexdigest()
                    old = self.db.execute('SELECT evidence FROM attribution_records WHERE record=?', (record,)).fetchone()
                    if record not in invalid and old == (certificate,):
                        continue
                prior_error = errors.get(record)
                if prior_error is not None and prior_error[0] == stamp and time.monotonic() < prior_error[2]:
                    error, certificate = prior_error[1], None
                else:
                    inputs = harness.get(reader).attribution_inputs(Path(record))
                    errors.pop(record, None)
                if stamp != (reader, record_fingerprint(Path(record))):
                    certificate = None
            except (OSError, ValueError, KeyError, TypeError) as exc:
                error, certificate = f'record attribution evidence unavailable: {type(exc).__name__}', None
                errors[record] = (locals().get('stamp'), error, time.monotonic() + retry_interval)
            for row in turns:
                written += self.replay_row(record, row, turns, by_seat, graph, inputs, error)
                count += 1
                if count % 200 == 0:
                    progress()
            if certificate is not None:
                self.db.execute('INSERT OR REPLACE INTO attribution_records VALUES (?,?)', (record, certificate))
            del inputs
        return written

    def replay_row(self, record, row, turns, by_seat, graph, inputs, error):
        received = []
        overlap = False
        a,b = moment(row['started']), moment(row['ended'])
        if a is not None and b is not None:
            for entry in inputs:
                at = entry.consumed_at if entry.kind == 'consumed' else entry.at
                if entry.kind == 'queued' or at is None or not a <= at <= b:
                    continue
                matched = graph.received(entry.text, at, row['seat'])
                if not matched:
                    continue
                if sum(moment(t['started']) is not None and moment(t['ended']) is not None
                       and moment(t['started']) <= at <= moment(t['ended']) for t in turns) != 1:
                    overlap = True
                    continue
                for item in matched:
                    received.append(dict(item, route='watch' if entry.kind == 'watch' else 'input'))
        if error:
            old = self.db.execute('SELECT identity,received FROM turn_attribution WHERE record=? AND turn=?',
                                  (record,row['turn'])).fetchone()
            identity = json.dumps([row['seat'], row['started'], row['ended']])
            if old and old[0] == identity:
                received = [dict(item, retained=True) for item in json.loads(old[1])]
        received = list({json.dumps(e, sort_keys=True):e for e in received}.values())
        verdict = graph.decide(row['seat'], row['started'], row['ended'], received, error)
        posted_overlap = any(e['route'] == 'posted' and sum(
            moment(t['started']) is not None and moment(t['ended']) is not None
            and moment(t['started']) <= moment(e['at']) <= moment(t['ended'])
            for t in by_seat[row['seat']]) > 1 for e in verdict.evidence)
        if posted_overlap:
            verdict = Verdict('unresolved', verdict.orders, 'posted message overlaps recorded turn intervals', verdict.evidence)
        if overlap:
            verdict = Verdict('unresolved', verdict.orders, 'received message overlaps recorded turn intervals', verdict.evidence)
        return self.put(row, received, verdict)
