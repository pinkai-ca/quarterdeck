"""Explicit-only copied-data scheduler measurement; select through testrun.py -n 0.

QD_ACCOUNTING_MEASURE_SOURCE names the source ship. Nothing is launched there.
The copied ledger/records/post stay in test-owned scratch outside repositories.
Only aggregate CPU/count data reaches the console; the runner removes the copy.
"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import time
from types import SimpleNamespace

from quarterdeck import post, record_owner, ship as shipmod
from quarterdeck.accounting_gate import Gate, capture
from quarterdeck.harness import get, reader_stamp
from quarterdeck.harness.json_record import release
from quarterdeck.harness.sqlite_snapshot import snapshot
from quarterdeck.ledger import Ledger
from quarterdeck.order_attribution import Threads, moment
from quarterdeck.seat import Seat, seats

STAGE = ['copy']

def copied_runtime(source, destination, *, compact_sqlite=False):
    """No original SQLite connection or native reader; rewrite copied paths only."""
    destination.mkdir(parents=True)
    _, raw = snapshot(source / 'ledger.sqlite')
    db = sqlite3.connect(':memory:')
    db.deserialize(raw)
    records = [row[0] for row in db.execute('SELECT DISTINCT record FROM turns')]
    translated = {}
    copied, size = 0, 0
    for record in records:
        path = Path(record)
        if not path.is_relative_to(source):
            continue  # legacy sources outside runtime stay unread, as in replay
        target = destination / path.relative_to(source)
        translated[record] = str(target)
        if compact_sqlite and path.suffix == '.db' and path.exists():
            # A coherent native SQLite snapshot replaces accumulated WAL
            # versions; it preserves the current database, not obsolete pages.
            if not path.resolve().is_relative_to(source):
                raise RuntimeError('source record resolves outside runtime')
            _, data = snapshot(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            copied += 1
            size += len(data)
            continue
        for suffix in ('', '-wal'):
            old, new = Path(str(path) + suffix), Path(str(target) + suffix)
            if not old.exists():
                continue
            if not old.resolve().is_relative_to(source):
                raise RuntimeError('source record resolves outside runtime')
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, new)
            copied += 1
            size += new.stat().st_size
    # Remap every copied table's record identity without firing accounting
    # invalidations merely because the experiment moved the files.
    triggers = list(db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
    for name, _ in triggers:
        db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
    for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        quoted = '"' + table.replace('"', '""') + '"'
        if any(row[1] == 'record' for row in db.execute('PRAGMA table_info(' + quoted + ')')):
            db.executemany('UPDATE ' + quoted + ' SET record=? WHERE record=?',
                           [(new, old) for old, new in translated.items()])
    for _, sql in triggers:
        db.execute(sql)
    db.commit()
    (destination / 'ledger.sqlite').write_bytes(db.serialize())
    db.close()
    # Only saved binding metadata is needed from seats; never credential homes.
    for folder in (source / 'seats').iterdir():
        if not folder.is_dir():
            continue
        for name in (record_owner.REGISTRY, 'record-launches.jsonl'):
            path = folder / name
            if not path.is_file():
                continue
            target = destination / 'seats' / folder.name / name
            target.parent.mkdir(parents=True, exist_ok=True)
            rows = []
            for line in path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    row['record'] = translated.get(row['record'], row['record'])
                    rows.append(json.dumps(row))
            target.write_text('\n'.join(rows) + '\n')
    # Preserve the actual store's local projections/import provenance too.
    for name in ('store', 'imports', 'forwarded', 'addressed'):
        old = source / 'post' / name
        if old.exists():
            shutil.copytree(old, destination / 'post' / name)
    shutil.copy2(source.parent / 'ship.toml', destination.parent / 'ship.toml')
    return dict(ledger_records=len(records), copied_files=copied, copied_bytes=size)


def copied_selection_metadata(source, runtime):
    for folder in (source / 'seats').iterdir():
        if not folder.is_dir():
            continue
        for name in ('launch.json', 'launch-price.json', 'record', 'pool'):
            path = folder / name
            if path.is_file():
                target = runtime / 'seats' / folder.name / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        path = folder / 'home' / 'conversations.owned'
        if path.is_file():
            target = runtime / 'seats' / folder.name / 'home' / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def append_synthetic_native_turn(book, runtime, store):
    """One ordinary synthetic completed pi turn, parsed by its actual reader."""
    seat = book.db.execute('''SELECT t.seat FROM turns t JOIN launch_harnesses h ON h.record=t.record
        WHERE h.harness='pi' GROUP BY t.seat ORDER BY count(DISTINCT t.record) DESC LIMIT 1''').fetchone()[0]
    record = runtime / 'seats' / seat / 'home' / 'ordinary-measured-turn.jsonl'
    record.parent.mkdir(parents=True, exist_ok=True)
    start = datetime.now(timezone.utc) + timedelta(days=1)
    rows = [dict(type='session', version=1, id='ordinary-measured-session', timestamp=start.isoformat()),
            dict(type='message', timestamp=start.isoformat(),
                 message=dict(role='user', content='Ordinary synthetic measured work.')),
            dict(type='message', timestamp=(start + timedelta(seconds=1)).isoformat(),
                 message=dict(role='assistant', content=[dict(type='text', text='Ordinary synthetic result.')],
                              model='ordinary-measured-model', usage=dict(input=0, output=0), stopReason='stop'))]
    record.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    state, turns = get('pi').read(record)
    if len(turns) != 1 or state.turn_open is not False:
        raise RuntimeError('synthetic native fixture did not yield exactly one completed turn')
    folder = runtime / 'trusted' / seat
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'record-launches.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(record=str(record), harness='pi')) + '\n')
    book.ingest(seat, 'measurement-only', record, turns, store)
    release(record)
    return record


def measured_copy(source, root):
    runtime = root / 'ship' / 'runtime'
    counts = copied_runtime(source, runtime)
    copied_selection_metadata(source, runtime)
    STAGE[0] = 'selection'
    ship = shipmod.load(runtime.parent)
    book = Ledger(runtime / 'ledger.sqlite')
    store = post.Store(runtime)
    graph = Threads(store)
    epoch = reader_stamp.attributions()
    service = SimpleNamespace(runtime=runtime, ship=ship, ledger=book, accounting_epoch=epoch,
                              resolved_seats=lambda: [seat for seat in seats(ship, None, False)
                                                      if isinstance(seat, Seat)])
    current = [None]
    def context(record, owners):
        if current[0] is not None:
            release(current[0])
        current[0] = Path(record) if record else None
    cache = book.attributions
    gate_work_cpu = [0.0]
    def job(gate=None):
        if gate is not None:
            started = time.process_time()
            gate.begin(service, [])
            gate_work_cpu[0] += time.process_time() - started
        before = book.db.total_changes
        wall, cpu = time.monotonic(), time.process_time()
        written = cache.replay_stream(store, runtime, epoch, context, lambda: None, graph=graph)
        book.db.commit()
        elapsed, consumed = time.monotonic() - wall, time.process_time() - cpu
        if gate is not None:
            started = time.process_time()
            gate.finish(service, before, full=True)
            gate_work_cpu[0] += time.process_time() - started
        return dict(wall_seconds=elapsed, cpu_seconds=consumed, changed_rows=written)
    try:
        STAGE[0] = 'warm-copied-history'
        job()  # relocation changes source identities; warm once, outside comparison
        warm_state = book.db.serialize()
        initial = job()
        result = dict(counts, initial_warm_job=initial, span_seconds=30, opportunities=6,
                      opportunity_spacing_seconds=5, clock='real monotonic spans and process CPU')
        # Same frozen ledger/native/post inputs in both variants. The one-turn
        # fixture is created only in its private scenario; no provider is called.
        for scenario in ('quiet', 'one_new_native_turn'):
            variants = {}
            for label, gated in (('before', False), ('after', True)):
                STAGE[0] = scenario + '-' + label
                book.db.deserialize(warm_state)
                cache = book.attributions
                gate = Gate() if gated else None
                # Both variants get an initial completed job; new services are
                # never allowed to skip their first job.
                for _ in range(4):
                    job(gate)
                    if gate is None or gate.completed is not None:
                        break
                    time.sleep(2.01)  # a relocated/recent input must age past the filesystem tick
                if gate is not None and gate.completed is None:
                    raise RuntimeError('frozen copy could not establish a completed starting certificate')
                gate_work_cpu[0] = 0.0
                worker_cpu = worker_wall = check_cpu = 0.0
                jobs, added = 0, None
                valid_readings = 0
                span_started = time.monotonic()
                for opportunity in range(6):
                    time.sleep(max(0.0, span_started + opportunity * 5 - time.monotonic()))
                    if scenario == 'one_new_native_turn' and opportunity == 3:
                        STAGE[0] = 'synthetic-native-turn'
                        added = append_synthetic_native_turn(book, runtime, store)
                        STAGE[0] = scenario + '-' + label
                    started = time.process_time()
                    allowed = gate.allowed(service) if gate is not None else True
                    check_cpu += time.process_time() - started
                    if allowed:
                        reading = job(gate)
                        worker_cpu += reading['cpu_seconds']
                        worker_wall += reading['wall_seconds']
                        jobs += 1
                    if gate is not None and gate.completed is not None:
                        valid_readings += 1
                time.sleep(max(0.0, span_started + 30 - time.monotonic()))
                variants[label] = dict(worker_cpu_seconds=worker_cpu, worker_wall_seconds=worker_wall,
                                       span_wall_seconds=time.monotonic() - span_started,
                                       certificate_cpu_seconds=check_cpu + gate_work_cpu[0], jobs=jobs,
                                       completed_certificates=valid_readings)
                if added is not None:
                    added.unlink()
                    # Restore the binding journal too: only this fixture's
                    # synthetic final row was appended to it.
                    journal = added.parent.parent / 'record-launches.jsonl'
                    lines = journal.read_text().splitlines()
                    journal.write_text('\n'.join(lines[:-1]) + '\n')
                    release(added)
            result[scenario] = variants
        book.db.deserialize(warm_state)
        STAGE[0] = 'certificate-cost'
        gate = Gate()
        for _ in range(4):
            job(gate)
            if gate.completed is not None:
                break
            time.sleep(2.01)
        checks = []
        for _ in range(20):
            started = time.process_time()
            reading = capture(service, gate.records)
            checks.append(time.process_time() - started)
            if reading is None:
                raise RuntimeError('copied inputs could not certify a quiet check')
        result['one_certificate_check_cpu_seconds'] = dict(mean=statistics.mean(checks),
                                                          minimum=min(checks), maximum=max(checks))
        return result
    finally:
        if current[0] is not None:
            release(current[0])
        book.close()


def test_copied_accounting_job_gate(tmp_path, capsys):
    try:
        source = Path(os.environ['QD_ACCOUNTING_MEASURE_SOURCE']).resolve() / 'runtime'
        result = measured_copy(source, tmp_path)
    except Exception as error:
        raise RuntimeError('copied accounting gate measurement failed at ' + STAGE[0]
                           + ': ' + type(error).__name__ + '; no native text displayed') from None
    with capsys.disabled():
        print('COPIED_ACCOUNTING_GATE ' + json.dumps(result, sort_keys=True), flush=True)
    if result['quiet']['after']['jobs'] != 0:
        raise RuntimeError('quiet copied ship started an unnecessary accounting job')
    if result['one_new_native_turn']['after']['jobs'] == 0:
        raise RuntimeError('new copied native turn did not start accounting')

