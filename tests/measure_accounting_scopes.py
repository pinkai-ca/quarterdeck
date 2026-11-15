"""Explicit-only frozen native-copy replay proof, selected with testrun.py -n 0.

QD_ACCOUNTING_MEASURE_SOURCE names the source ship. All native parsing and
ledger writes happen in the runner's private copy, removed whole by the tool.
Only counts, CPU and table digests are emitted, never native record bodies.
"""

import hashlib
import io
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tarfile
import time

BASE = 'd2eaa04e1d927fcb9e6fe4970ef1f6ef8bd33cc2'


def table_rows(book):
    result = {}
    for table, in book.db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        quoted = '"' + table.replace('"', '""') + '"'
        rows = sorted(book.db.execute('SELECT * FROM ' + quoted).fetchall(), key=repr)
        raw = json.dumps(rows, ensure_ascii=False, separators=(',', ':'),
                         default=lambda value: value.hex()).encode()
        result[table] = dict(rows=len(rows), sha256=hashlib.sha256(raw).hexdigest())
    return result


def phase(runtime, label):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from quarterdeck import record_owner, ship as shipmod
    from quarterdeck.accounting_slices import Driver
    from quarterdeck.harness import get, reader_stamp
    from quarterdeck.service import Service

    service = Service(shipmod.load(runtime.parent), SimpleNamespace())
    service.heartbeat = lambda: None
    service.log = lambda *args, **kwargs: None
    service.publish_accounting_health = lambda **kwargs: None
    service.seat_guard = lambda *args: nullcontext()
    inventory, selected = {}, []
    for record, seat, name in service.ledger.db.execute('''SELECT DISTINCT t.record,t.seat,h.harness
            FROM turns t LEFT JOIN launch_harnesses h ON h.record=t.record ORDER BY t.seat,t.record'''):
        path = Path(record)
        if not path.is_relative_to(runtime):
            continue  # no authority to read the legacy external source
        reader = get(name) if name else None
        inventory.setdefault(seat, []).append(record_owner.Owned(path, name, reader,
                                             None if reader else 'reader unavailable in copied history'))
    for name, sources in inventory.items():
        spec = service.ship.seats.get(name)
        harness = get(spec.harness) if spec is not None else next(item.reader for item in sources if item.reader)
        pool = service.ship.pools[spec.pools[0]] if spec is not None else SimpleNamespace(name='copied-history')
        session = service.ship.sessions.get(spec.session) if spec is not None else None
        selected.append(SimpleNamespace(name=name,home=runtime/'seats'/name/'home',harness=harness,
            spec=spec or SimpleNamespace(model=None),pool=pool,
            session=session or SimpleNamespace(human=False),pane=lambda:None,
            launched=lambda:None,record=lambda:None))
    service.resolved_seats = lambda: selected
    record_owner.records = lambda seat: inventory[seat.name]
    driver = Driver()
    def job():
        parent, wall = time.process_time(), time.monotonic()
        before = driver.job
        while time.monotonic() - wall < 900:
            driver.step(service)
            if not driver.active:
                if service.accounting_health.failures:
                    raise RuntimeError('copied planner failed; native details withheld')
                break
        else:
            raise RuntimeError('copied planner exceeded outer guard')
        if driver.job != before + 1:
            raise RuntimeError('expected one copied accounting job')
        stat = Path('/proc') / str(driver.process.pid) / 'stat'
        fields = stat.read_text().rsplit(')',1)[1].split()
        child = (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')
        return dict(wall_seconds=time.monotonic()-wall,parent_cpu_seconds=time.process_time()-parent,
                    child_lifetime_cpu_seconds=child,scope_seats=None if driver.scope is None else len(driver.scope),
                    records=len(driver.records))
    try:
        if label == 'warm':
            first = job()
            driver.gate.reject()
            second = job()
            return dict(first=first,second=second,tables=table_rows(service.ledger),
                        epoch=reader_stamp.attributions(),owned_records=sum(map(len,inventory.values())))
        full = job()
        frozen = table_rows(service.ledger)
        partition = driver.gate.partitions
        if partition.baseline is None:
            raise RuntimeError('frozen copy did not establish a full baseline')
        # A stale observation represents an already-accounted publication. No
        # record or ledger input changes: replay must leave every row identical.
        path = next(path for path in partition.marks
                    if len(partition.owners.get(path, ())) == 1 and not path.endswith('-wal'))
        partition.marks[path] = ('older observation',)
        scoped = job()
        if driver.scope is None or table_rows(service.ledger) != frozen:
            raise RuntimeError('scoped frozen replay changed ledger rows')
        due = partition.due
        partition.due = time.monotonic() - 1
        backstop = job()
        if driver.scope is not None or table_rows(service.ledger) != frozen:
            raise RuntimeError('full backstop replay changed ledger rows')
        return dict(full=full,scoped=scoped,backstop=backstop,original_full_deadline=due,
                    tables=frozen,epoch=reader_stamp.attributions(),all_rows_unchanged=True)
    finally:
        driver.close()
        service.ledger.close()


def test_copied_scoped_accounting_replay(tmp_path, capsys):
    from tests.measure_accounting_gate import copied_runtime, copied_selection_metadata
    source = Path(os.environ['QD_ACCOUNTING_MEASURE_SOURCE']).resolve() / 'runtime'
    runtime = tmp_path/'ship'/'runtime'
    stage = 'copy'
    phase_error = None
    try:
        counts = copied_runtime(source,runtime,compact_sqlite=True)
        copied_selection_metadata(source,runtime)
        # This proof freezes the ledger's owned inventory, including historical
        # seats. It launches no program, reads no screen and uses no credentials.
        base = tmp_path/'base'
        base.mkdir()
        archive = subprocess.check_output(['git','archive',BASE,'src','standing-orders','pyproject.toml'])
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            stream.extractall(base,filter='data')
        time.sleep(2.1)  # copied metadata must age past the actual filesystem tick
        results = {}
        for label, source_root in [('warm',base/'src'),('candidate',Path(__file__).resolve().parents[1]/'src')]:
            stage = label
            env = dict(os.environ,PYTHONPATH=str(source_root),PYTHONDONTWRITEBYTECODE='1')
            done = subprocess.run([sys.executable,str(Path(__file__).resolve()),'--phase',str(runtime),label],
                                  env=env,capture_output=True,text=True,timeout=1200,check=False)
            if done.returncode:
                try:
                    detail = json.loads(done.stderr)
                    if (set(detail) == {'error','file','line'} and isinstance(detail['line'],int)
                            and all(isinstance(detail[key],str) for key in ('error','file'))):
                        phase_error = detail
                except (ValueError,TypeError):
                    pass
                raise RuntimeError('copy phase exited ' + str(done.returncode))
            results[label] = json.loads(done.stdout)
        if results['warm']['tables'] != results['candidate']['tables']:
            raise RuntimeError('base-to-candidate frozen replay changed ledger rows')
        if results['warm']['epoch'] != results['candidate']['epoch']:
            raise RuntimeError('reader or attribution epoch unexpectedly changed')
        results['counts'] = counts
        results['base'] = BASE
        results['all_tables_equal'] = True
        with capsys.disabled():
            print('COPIED_SCOPED_REPLAY '+json.dumps(results,sort_keys=True),flush=True)
    except Exception as error:
        raise RuntimeError('copied scoped replay failed at '+stage+': '+type(error).__name__+
                           '; phase metadata '+json.dumps(phase_error)+
                           '; no native details displayed') from None


if __name__ == '__main__' and len(sys.argv) == 4 and sys.argv[1] == '--phase':
    try:
        print(json.dumps(phase(Path(sys.argv[2]),sys.argv[3]),sort_keys=True))
    except Exception as error:
        import traceback
        frame = traceback.extract_tb(error.__traceback__)[-1]
        print(json.dumps(dict(error=type(error).__name__,file=Path(frame.filename).name,line=frame.lineno)),file=sys.stderr)
        raise SystemExit(1) from None
