"""Service-written attribution beside unchanged turn rows; old writes invalidate it."""

from collections import defaultdict
import json
from pathlib import Path

from . import harness, record_owner
from .harness.base import record_fingerprint
from .order_attribution import Threads, Verdict, moment


class Attribution:
    def __init__(self, db, create=True):
        self.db = db
        self.sources = {}
        self.last = None
        if create:
            db.execute('''CREATE TABLE IF NOT EXISTS turn_attribution (
                record TEXT NOT NULL, turn INTEGER NOT NULL, identity TEXT NOT NULL,
                received TEXT NOT NULL, status TEXT NOT NULL, orders TEXT NOT NULL,
                grouping TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL,
                valid INTEGER NOT NULL, PRIMARY KEY(record,turn))''')
            for event in ('INSERT', 'UPDATE'):
                db.execute(f'''CREATE TRIGGER IF NOT EXISTS attribution_after_{event.lower()} AFTER {event} ON turns
                    BEGIN UPDATE turn_attribution SET valid=0 WHERE record=NEW.record AND turn=NEW.turn; END''')

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

    def ingest(self, row, turn, graph):
        received = graph.received(turn.input, turn.started, row['seat'])
        return self.put(row, received, graph.decide(row['seat'], row['started'], row['ended'], received))

    def replay(self, store, runtime, bindings=None):
        """All stored keys, including retired seats; called by the service, never a reading.

        Retained exact-envelope evidence survives a missing source if the stored
        seat and turn bounds are unchanged. Old-writer changes require rereading.
        """
        graph = Threads(store)
        if bindings is None:
            bindings = defaultdict(set)
            for folder in (Path(runtime)/'seats').glob('*'):
                for name in (record_owner.REGISTRY, 'record-launches.jsonl'):
                    try:
                        events = record_owner.events(folder/name)
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
        changed = False
        for record in by_record:
            names = bindings.get(record, set())
            reader = next(iter(names)) if len(names) == 1 else None
            stamp = (reader, None)
            try:
                stamp = (reader, record_fingerprint(Path(record))) if reader in harness.NAMES else (reader, None)
                if record in self.sources and self.sources[record][0] == stamp:
                    continue
                if reader not in harness.NAMES:
                    raise OSError('record reader unavailable or ambiguous')
                inputs = harness.get(reader).attribution_inputs(Path(record))
                error = None
            except (OSError, ValueError, KeyError, TypeError) as exc:
                inputs, error = [], f'record attribution evidence unavailable: {type(exc).__name__}'
            self.sources[record] = stamp, inputs, error
            changed = True
        stamp = tuple(m.id for m in graph.messages)
        invalid = self.db.execute('''SELECT count(*) FROM turns t LEFT JOIN turn_attribution a
            ON a.record=t.record AND a.turn=t.turn WHERE a.valid IS NULL OR a.valid=0''').fetchone()[0]
        if not changed and self.last == stamp and not invalid:
            return 0
        self.last = stamp
        written = 0
        for record, turns in by_record.items():
            _, inputs, error = self.sources[record]
            for row in turns:
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
                written += self.put(row, received, verdict)
        self.db.commit()
        return written
