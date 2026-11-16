"""Explicit-only proof that a stitched certificate changes no ledger row; not part of normal discovery.

QD_ACCOUNTING_MEASURE_SOURCE names the source ship; QD_STITCH_BASE names the base commit (main's engine). One copy is
made by measure_accounting_gate.copied_runtime, which leaves out the desk's and every office seat's records, metadata
and office-internal messages (tests/runtime_copy.py). Each engine runs in a child process on a fresh copy made from it
by the same tool, the same sequence: a full job during which one crew seat's identity moves (as a fresh start moves
it), its retry, then jobs until the gate asks for none. Every ledger table is compared by row count and digest, with
each copy's own folder and stamps set aside. Only counts, times and digests are emitted, never a record's content.
Every copy is removed at the end.
"""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time

HERE = Path(__file__).resolve().parent
LIMIT = 12  # jobs per engine before the sequence is called unfinished


def table_rows(book, runtime):
    """Every table's row count and digest, each copy's own runtime path read as one placeholder: the two engines run
    on two copies, and only their folders' names may differ."""
    import hashlib
    prefix, result = str(runtime), {}
    def plain(value):
        return value.replace(prefix, '<runtime>') if isinstance(value, str) else value
    for table, in book.db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        quoted = '"' + table.replace('"', '""') + '"'
        rows = [tuple(plain(value) for value in row) for row in book.db.execute('SELECT * FROM ' + quoted)]
        if table == 'records':
            # A settled record's evidence is a hash of its own file's inode and change time (service.settlement_of):
            # every copy gives it a new one. Compared here is whether a record is settled, and with which kind of
            # evidence, never the copy's own stamps.
            rows = [row[:4] + (None if row[4] is None else row[4].split(':', 1)[0],) for row in rows]
        elif table in ('attribution_records', 'record_accounting'):
            # Each row's evidence is a certificate over the record's own file stamp, the copy's own: compared is which
            # records hold one, not the hash.
            rows = [row[:1] for row in rows]
        elif table == 'wall_events':
            # observed_at is the time the job saw the wall, the run's own clock: set aside.
            names = [column[1] for column in book.db.execute('PRAGMA table_info(wall_events)')]
            rows = [tuple(value for name, value in zip(names, row) if name != 'observed_at') for row in rows]
        rows = sorted(rows, key=repr)
        raw = json.dumps(rows, ensure_ascii=False, separators=(',', ':'), default=lambda value: value.hex()).encode()
        result[table] = dict(rows=len(rows), sha256=hashlib.sha256(raw).hexdigest())
    return result


def phase(runtime):
    from quarterdeck import record_owner, trusted_files
    import tests.measure_accounting_scopes as scopes
    from quarterdeck.accounting_slices import Driver
    # The scopes proof's own service set-up, on this copy.
    service, selected, inventory = build(runtime, scopes)
    moved = next(seat for seat in selected if inventory.get(seat.name)).name
    starts = []

    class Moved(Driver):
        def start(self, service):
            starts.append(None if self.scope is None else len(self.scope))
            super().start(service)
            if len(starts) == 1:  # one crew seat starts fresh while the full job reads it
                with record_owner.registry(trusted_files.folder(runtime, moved)).open('a') as stream:
                    stream.write('\n')
    driver = Moved()
    jobs = []
    try:
        while len(jobs) < LIMIT:
            if not driver.active and starts and not driver.retry_seats and not driver.gate.allowed(service):
                break  # quiet: the gate asks for no job
            time.sleep(2.1)  # every input ages past a filesystem tick before the next job reads it
            wall, before = time.monotonic(), driver.job
            while time.monotonic() - wall < 900:
                driver.step(service)
                if driver.job != before and not driver.active:
                    break
            else:
                raise RuntimeError('copied job exceeded its guard')
            if service.accounting_health.failures:
                raise RuntimeError('copied planner failed; native details withheld')
            jobs.append(dict(scope=None if driver.scope is None else len(driver.scope),
                             seconds=round(time.monotonic() - wall, 1),
                             reset=bool(driver.retry_seats),
                             stitched=driver.gate.previous.get('stitched', 0)))
        return dict(jobs=jobs, quiet=len(jobs) < LIMIT, tables=table_rows(service.ledger, runtime))
    finally:
        driver.close()
        service.ledger.close()


def build(runtime, scopes):
    """measure_accounting_scopes.phase's set-up of a service on a copy, without its jobs."""
    from contextlib import nullcontext
    from types import SimpleNamespace
    from quarterdeck import record_owner, ship as shipmod, trusted_files
    from quarterdeck.harness import get
    from quarterdeck.service import Service
    service = Service(shipmod.load(runtime.parent), SimpleNamespace())
    service.heartbeat = lambda: None
    service.log = lambda *args, **kwargs: None
    service.publish_accounting_health = lambda **kwargs: None
    service.seat_guard = lambda *args: nullcontext()
    inventory, selected, bindings = {}, [], {}
    for record, seat, name in service.ledger.db.execute('''SELECT DISTINCT t.record,t.seat,h.harness
            FROM turns t LEFT JOIN launch_harnesses h ON h.record=t.record ORDER BY t.seat,t.record'''):
        path = Path(record)
        if not path.is_relative_to(runtime):
            continue
        if seat not in bindings:
            owner = SimpleNamespace(home=runtime / 'seats' / seat / 'home', trusted=trusted_files.folder(runtime, seat),
                                    launched=lambda: None)
            bindings[seat] = record_owner.bindings(owner, publish=False)
        names = bindings[seat].get(path, set())
        if names:
            # Saved ownership evidence decides where it exists; otherwise the ledger's own reader stands, as in
            # measure_accounting_scopes.
            name = next(iter(names)) if len(names) == 1 else None
        reader = get(name) if name else None
        inventory.setdefault(seat, []).append(record_owner.Owned(path, name, reader,
                                              None if reader else 'reader unavailable in copied history'))
    for name, sources in inventory.items():
        spec = service.ship.seats.get(name)
        harness = (get(spec.harness) if spec is not None else
                   next((item.reader for item in sources if item.reader), get('fake')))
        pool = service.ship.pools[spec.pools[0]] if spec is not None else SimpleNamespace(name='copied-history')
        session = service.ship.sessions.get(spec.session) if spec is not None else None
        selected.append(SimpleNamespace(name=name, home=runtime / 'seats' / name / 'home',
                                        trusted=trusted_files.folder(runtime, name), harness=harness,
                                        spec=spec or SimpleNamespace(model=None), pool=pool,
                                        session=session or SimpleNamespace(human=False), pane=lambda: None,
                                        launched=lambda: None, record=lambda: None))
    service.resolved_seats = lambda: selected
    record_owner.records = lambda seat: inventory[seat.name]
    return service, selected, inventory


def measure(source, base, scratch):
    from tests.measure_accounting_gate import copied_runtime, copied_selection_metadata
    pristine = scratch / 'pristine' / 'ship' / 'runtime'
    counts = copied_runtime(source, pristine, compact_sqlite=True)
    copied_selection_metadata(source, pristine)
    engines = {'main': scratch / 'base-src', 'candidate': HERE.parent / 'src'}
    archive = subprocess.check_output(['git', '-C', str(HERE.parent), 'archive', base, 'src'])
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(scratch / 'base-tree', filter='data')
    shutil.move(str(scratch / 'base-tree' / 'src'), engines['main'])
    results = {}
    for label, src in engines.items():
        work = scratch / label / 'ship'
        # Each engine's copy is made from the pristine one by the same tool, so the ledger names this copy's own paths
        # and both engines read identical inputs.
        copied_runtime(pristine, work / 'runtime', compact_sqlite=True)
        copied_selection_metadata(pristine, work / 'runtime')
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(src), str(HERE.parent))), PYTHONDONTWRITEBYTECODE='1')
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--phase', str(work / 'runtime')],
                              env=env, capture_output=True, text=True, timeout=3600, check=False)
        if done.returncode:
            raise RuntimeError(f'{label} phase exited {done.returncode}: {done.stderr.strip().splitlines()[-1:]}')
        results[label] = json.loads(done.stdout)
        shutil.rmtree(scratch / label)
    tables_equal = results['main']['tables'] == results['candidate']['tables']
    return dict(copy=counts, base=base, tables_equal=tables_equal,
                tables=len(results['main']['tables']),
                rows={name: value['rows'] for name, value in results['candidate']['tables'].items()},
                rows_equal=all(results['main']['tables'][name]['rows'] == value['rows']
                               for name, value in results['candidate']['tables'].items()),
                differing=[name for name, value in results['main']['tables'].items()
                           if results['candidate']['tables'].get(name) != value],
                main=dict(jobs=results['main']['jobs'], quiet=results['main']['quiet']),
                candidate=dict(jobs=results['candidate']['jobs'], quiet=results['candidate']['quiet']))


def test_stitched_certificate_changes_no_ledger_row(tmp_path, capsys):
    source = Path(os.environ['QD_ACCOUNTING_MEASURE_SOURCE']).resolve() / 'runtime'
    result = measure(source, os.environ['QD_STITCH_BASE'], tmp_path)
    with capsys.disabled():
        print('STITCHED_CATCHUP ' + json.dumps(result, sort_keys=True), flush=True)
    if not result['tables_equal']:
        raise RuntimeError('the stitched certificate changed ledger rows')


if __name__ == '__main__' and sys.argv[1:2] == ['--phase']:
    sys.path.insert(0, str(HERE.parent))
    try:
        print(json.dumps(phase(Path(sys.argv[2]))))
    except Exception as error:
        import traceback
        frame = traceback.extract_tb(error.__traceback__)[-1]
        # The error's kind and where it was raised, never a message that could carry native content.
        print(json.dumps(dict(error=type(error).__name__, file=Path(frame.filename).name, line=frame.lineno)),
              file=sys.stderr)
        raise SystemExit(1)
