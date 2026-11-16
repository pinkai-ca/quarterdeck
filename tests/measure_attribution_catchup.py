"""Explicit-only copied attribution measurement; not part of normal discovery.

Run through testrun.py with this path and -n 0. Set
QD_ATTRIBUTION_MEASURE_SOURCE to the ship folder and
QD_ATTRIBUTION_MEASURE_BASELINE to the previous installed quarterdeck package.
Copies stay in the run's scratch, outside any repository, and the test tool
removes that scratch whole. Only aggregate numbers reach the console.
The copy leaves out the desk's and every human-session seat's records, their
rows and their seat folders: the crew does not read them (left_out).
"""

import importlib.util
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from types import SimpleNamespace

from quarterdeck import attribution_cache, post, record_owner
from tests import runtime_copy
from quarterdeck.harness import reader_stamp
from quarterdeck.harness.json_record import release
from quarterdeck.harness.sqlite_snapshot import snapshot
from quarterdeck.ledger import Ledger
from quarterdeck.order_attribution import Threads
from quarterdeck.order_attribution import moment


def left_out(source):
    """The seats whose records no copy takes (tests/runtime_copy.py, the one rule)."""
    return runtime_copy.people(Path(source).parent)


def copied_runtime(source, destination):
    """No original SQLite connection or native reader; rewrite copied paths only. The people's records are left out:
    not copied, their rows dropped from the copied ledger, their seat folders skipped."""
    destination.mkdir(parents=True)
    people = left_out(source)
    _, raw = snapshot(source / 'ledger.sqlite')
    db = sqlite3.connect(':memory:')
    db.deserialize(raw)
    records = [row[0] for row in db.execute('SELECT DISTINCT record FROM turns')]
    translated, dropped = {}, set()
    copied, size = 0, 0
    for record in records:
        path = Path(record)
        if not path.is_relative_to(source):
            continue  # legacy sources outside runtime stay unread, as in replay
        if runtime_copy.theirs(path, source, people):
            dropped.add(record)
            continue  # a person's record: never copied, never read
        target = destination / path.relative_to(source)
        translated[record] = str(target)
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
            db.executemany('DELETE FROM ' + quoted + ' WHERE record=?', [(old,) for old in dropped])
            db.executemany('UPDATE ' + quoted + ' SET record=? WHERE record=?',
                           [(new, old) for old, new in translated.items()])
    for _, sql in triggers:
        db.execute(sql)
    db.commit()
    (destination / 'ledger.sqlite').write_bytes(db.serialize())
    db.close()
    # Only saved binding metadata is needed from seats; never credential homes.
    for folder in (source / 'seats').iterdir():
        if not folder.is_dir() or folder.name in people:
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
    return dict(ledger_records=len(records) - len(dropped), copied_files=copied, copied_bytes=size,
                left_out_seats=sorted(people), left_out_records=len(dropped))


def growth_comparison(book, runtime, store, old_module, old_epoch, new_epoch,
                      old_state, new_state, context, monkeypatch):
    """Paired private-memory jobs, as in the worker; never live service CPU.

    Whitespace growth introduces no input or completed turn. A second, clearly
    synthetic ledger-only append isolates the whole-seat invalidation cost.
    It is not a claim that the native program produced another measured turn.
    """
    histories = dict(book.db.execute('SELECT seat,count(DISTINCT record) FROM turns GROUP BY seat'))
    candidates = list(book.db.execute('''SELECT t.record,t.seat,max(t.ended),h.harness
        FROM turns t JOIN launch_harnesses h ON h.record=t.record
        WHERE h.harness IN ('pi','claude','codex') GROUP BY t.record,t.seat,h.harness'''))
    candidates.sort(key=lambda row: (histories[row[1]], moment(row[2]) is not None,
                                     moment(row[2]).timestamp() if moment(row[2]) else 0), reverse=True)
    chosen = next((row for row in candidates if Path(row[0]).suffix == '.jsonl' and Path(row[0]).is_file()), None)
    if chosen is None:
        raise RuntimeError('copy has no JSON-lines history for the growth control')
    record, seat, _, program = chosen
    graph = Threads(store)
    factory, reads = attribution_cache.harness.get, [0]
    def counted(name):
        reader = factory(name)
        def inputs(path):
            reads[0] += 1
            return reader.attribution_inputs(path)
        return SimpleNamespace(attribution_inputs=inputs)
    monkeypatch.setattr(attribution_cache.harness, 'get', counted)
    results = dict(program=program, seat_history_records=histories[seat])
    for label, cls, epoch, saved in (
        ('before', old_module.Attribution, old_epoch, old_state),
        ('after', attribution_cache.Attribution, new_epoch, new_state),
    ):
        book.db.deserialize(saved)
        monkeypatch.setattr(reader_stamp, 'attributions', lambda epoch=epoch: epoch)
        cache, rows = cls(book.db, create=False), [0]
        actual = cache.replay_row
        def replayed(*args, **kwargs):
            rows[0] += 1
            return actual(*args, **kwargs)
        cache.replay_row = replayed
        def job():
            reads[0] = rows[0] = 0
            changes = book.db.total_changes
            wall, cpu = time.monotonic(), time.process_time()
            written = cache.replay_stream(store, runtime, epoch, context, lambda: None, graph=graph)
            book.db.commit()
            return dict(wall_seconds=time.monotonic() - wall, cpu_seconds=time.process_time() - cpu,
                        native_reads=reads[0], replayed_turns=rows[0], changed_rows=written,
                        database_changes=book.db.total_changes - changes)
        job()  # settle any relocated/grown source stamp before the paired control
        steady = job()
        with Path(record).open('ab') as stream:
            stream.write(b'\n')
        growth = job()
        settled = [job() for _ in range(3)]
        latest = max(moment(row[0]) for row in book.db.execute('SELECT ended FROM turns')
                     if moment(row[0]) is not None)
        index = book.db.execute('SELECT max(turn)+1 FROM turns WHERE record=?', (record,)).fetchone()[0]
        start = latest + timedelta(days=1)
        book.db.execute('''INSERT INTO turns (record,turn,seat,pool,order_id,started,ended)
            VALUES (?,?,?,?,?,?,?)''', (record, index, seat, 'measurement-only', '-', start.isoformat(),
                                       (start + timedelta(seconds=1)).isoformat()))
        appended = job()
        results[label] = dict(steady=steady, growth_without_new_turns=growth,
                              subsequent_unchanged_jobs=settled, synthetic_disjoint_turn=appended)
    return results


def measure(source, baseline, root, monkeypatch):
    runtime = root / 'ship' / 'runtime'
    counts = copied_runtime(source, runtime)
    old_epoch = reader_stamp.attributions(baseline)
    new_epoch = reader_stamp.attributions()
    native_equal = reader_stamp.readers(baseline) == reader_stamp.readers()
    if not native_equal or old_epoch == new_epoch:
        raise RuntimeError('measurement requires unchanged native readers and a changed attribution engine')
    old_file = root / 'previous_attribution.py'
    old_file.write_bytes((baseline / 'attribution_cache.py').read_bytes())
    spec = importlib.util.spec_from_file_location('quarterdeck._previous_attribution', old_file)
    old_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old_module)
    book = Ledger(runtime / 'ledger.sqlite')
    messages = post.Store(runtime).all()
    store = SimpleNamespace(all=lambda: messages)
    current = [None]
    def context(record, owners):
        if current[0] is not None:
            release(current[0])
        current[0] = Path(record) if record else None
    try:
        # Warm the OLD implementation on the same immutable copied inputs.
        # Moving copies changes inode/path evidence; that copying cost must not
        # be confused with the engine epoch's one real invalidation.
        monkeypatch.setattr(reader_stamp, 'attributions', lambda: old_epoch)
        old = old_module.Attribution(book.db, create=False)
        book.begin_catchup(old_epoch)
        old.replay_stream(store, runtime, old_epoch, context, lambda: None)
        book.finish_catchup(old_epoch)
        book.db.commit()
        old_state = book.db.serialize()
        monkeypatch.setattr(reader_stamp, 'attributions', lambda: new_epoch)
        new = attribution_cache.Attribution(book.db, create=False)
        book.begin_catchup(new_epoch)
        wall, cpu = time.monotonic(), time.process_time()
        written = new.replay_stream(store, runtime, new_epoch, context, lambda: None)
        book.finish_catchup(new_epoch)
        book.db.commit()
        result = dict(counts, wall_seconds=time.monotonic() - wall,
                      cpu_seconds=time.process_time() - cpu, changed_rows=written,
                      turn_rows=book.db.execute('SELECT count(*) FROM turns').fetchone()[0],
                      certificates=book.db.execute('SELECT count(*) FROM attribution_records').fetchone()[0],
                      native_readers_unchanged=native_equal, attribution_epoch_changed=True,
                      pending=book.catching_up())
        new_state = book.db.serialize()
        result['growth_comparison'] = growth_comparison(
            book, runtime, store, old_module, old_epoch, new_epoch, old_state, new_state, context, monkeypatch)
        return result
    finally:
        if current[0] is not None:
            release(current[0])
        book.close()


def test_copied_epoch_catchup(tmp_path, monkeypatch, capsys):
    # No production path or native text can become an assertion representation.
    try:
        source = Path(os.environ['QD_ATTRIBUTION_MEASURE_SOURCE']).resolve() / 'runtime'
        baseline = Path(os.environ['QD_ATTRIBUTION_MEASURE_BASELINE']).resolve()
        result = measure(source, baseline, tmp_path, monkeypatch)
    except Exception:
        raise RuntimeError('copied attribution measurement failed; inspect with an authorized reader') from None
    with capsys.disabled():
        print('COPIED_ATTRIBUTION_CATCHUP ' + json.dumps(result, sort_keys=True), flush=True)
    if result['pending']:
        raise RuntimeError('copied attribution catch-up did not complete')
