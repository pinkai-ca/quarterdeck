"""Explicit-only proof that judging a seat's launch files seat by seat changes no ledger row; not part of normal
discovery.

QD_ACCOUNTING_MEASURE_SOURCE names the source ship; QD_LAUNCH_FILES_BASE names the base commit (the engine before this
change). One copy is made by measure_accounting_gate.copied_runtime, which leaves out the desk's and every office seat's
records, metadata and office-internal messages (tests/runtime_copy.py) and holds the crew's trusted launch files. One
crew seat's last launch event and reader binding are set aside, as before its last fresh start: the seat with
the most launch events in the copy, a hand that starts fresh on every order. Each engine runs in a
child process on its own copy made from it by the same tool: jobs until the gate asks for none, then the lines are put
back (appended, as a fresh start appends them), then jobs until it asks for none again. Every ledger table is compared
(measure_stitched_catchup.table_rows). Only counts, times and decisions are emitted, never a record's content. Every
copy is removed at the end.
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
LIMIT = 12  # jobs per stage before the sequence is called unfinished
REGISTRIES = ('record-launches.jsonl', 'record-readers.jsonl')


def until_quiet(service, driver, jobs, stage):
    """Jobs until the gate asks for none: every job's scope size and its decision's kind, never a seat or path."""
    count = 0
    while count < LIMIT:
        if not driver.active and not driver.retry_seats and not driver.gate.allowed(service):
            return True
        time.sleep(2.1)  # every input ages past a filesystem tick before the next job reads it
        wall, before = time.monotonic(), driver.job
        while time.monotonic() - wall < 1800:
            driver.step(service)
            if driver.job != before and not driver.active:
                break
        else:
            raise RuntimeError('copied job exceeded its guard')
        if service.accounting_health.failures:
            raise RuntimeError('copied planner failed; native details withheld')
        decision = driver.gate.decision or {}
        jobs.append(dict(stage=stage, scope=None if driver.scope is None else len(driver.scope),
                         seconds=round(time.monotonic() - wall, 1), reset=bool(driver.retry_seats),
                         decision=sorted(key for key in decision if key != 'previous-job'),
                         stitched=driver.gate.previous.get('stitched', 0)))
        count += 1
    return False


def phase(runtime, aside, hand):
    import tests.measure_accounting_scopes as scopes
    from tests.measure_stitched_catchup import build, table_rows
    from quarterdeck.accounting_slices import Driver
    service, _, _ = build(runtime, scopes)
    driver = Driver()
    jobs = []
    try:
        settled = until_quiet(service, driver, jobs, 'settle')
        for name in REGISTRIES:
            with (runtime / 'trusted' / hand / name).open('a') as stream:  # appended, as a fresh start appends them
                stream.write((Path(aside) / name).read_text())
        quiet = until_quiet(service, driver, jobs, 'launch')
        return dict(jobs=jobs, settled=settled, quiet=quiet, tables=table_rows(service.ledger, runtime))
    finally:
        driver.close()
        service.ledger.close()


def measure(source, base, scratch):
    from tests.measure_accounting_gate import copied_runtime, copied_selection_metadata
    pristine = scratch / 'pristine' / 'ship' / 'runtime'
    counts = copied_runtime(source, pristine, compact_sqlite=True)
    copied_selection_metadata(source, pristine)
    aside = scratch / 'aside'
    aside.mkdir()
    def launches(folder):
        path = folder / REGISTRIES[0]
        return len(path.read_text().splitlines()) if path.is_file() else 0
    hand = max((folder for folder in (pristine / 'trusted').iterdir() if folder.is_dir()), key=launches).name
    for name in REGISTRIES:  # the seat's last line of each registry, set aside
        path = pristine / 'trusted' / hand / name
        lines = path.read_text().splitlines(keepends=True)
        (aside / name).write_text(''.join(lines[-1:]))
        path.write_text(''.join(lines[:-1]))
    engines = {'main': scratch / 'base-src', 'candidate': HERE.parent / 'src'}
    archive = subprocess.check_output(['git', '-C', str(HERE.parent), 'archive', base, 'src'])
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(scratch / 'base-tree', filter='data')
    shutil.move(str(scratch / 'base-tree' / 'src'), engines['main'])
    results = {}
    for label, src in engines.items():
        work = scratch / label / 'ship'
        copied_runtime(pristine, work / 'runtime', compact_sqlite=True)
        copied_selection_metadata(pristine, work / 'runtime')
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(src), str(HERE.parent))), PYTHONDONTWRITEBYTECODE='1')
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--phase', str(work / 'runtime'),
                               str(aside), hand], env=env, capture_output=True, text=True, timeout=7200, check=False)
        if done.returncode:
            raise RuntimeError(f'{label} phase exited {done.returncode}: {done.stderr.strip().splitlines()[-1:]}')
        results[label] = json.loads(done.stdout)
        shutil.rmtree(scratch / label)
    tables = {label: result['tables'] for label, result in results.items()}
    return dict(copy={key: counts[key] for key in ('copied_files', 'ledger_records', 'left_out_records',
                                                   'left_out_messages', 'trusted_seats')},
                base=base, set_aside=len(REGISTRIES), tables_equal=tables['main'] == tables['candidate'],
                differing=[name for name in tables['main'] if tables['candidate'].get(name) != tables['main'][name]],
                rows_equal=all(tables['main'][name]['rows'] == tables['candidate'][name]['rows']
                               for name in tables['main']),
                main={key: results['main'][key] for key in ('jobs', 'settled', 'quiet')},
                candidate={key: results['candidate'][key] for key in ('jobs', 'settled', 'quiet')})


def test_launch_files_change_no_ledger_row(tmp_path, capsys):
    source = Path(os.environ['QD_ACCOUNTING_MEASURE_SOURCE']).resolve() / 'runtime'
    result = measure(source, os.environ['QD_LAUNCH_FILES_BASE'], tmp_path)
    with capsys.disabled():
        print('LAUNCH_FILES ' + json.dumps(result, sort_keys=True), flush=True)
    if not result['tables_equal']:
        raise RuntimeError('judging launch files seat by seat changed ledger rows')


if __name__ == '__main__' and sys.argv[1:2] == ['--phase']:
    sys.path.insert(0, str(HERE.parent))
    try:
        print(json.dumps(phase(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])))
    except Exception as error:
        import traceback
        frame = traceback.extract_tb(error.__traceback__)[-1]
        # The error's kind and where it was raised, never a message that could carry native content.
        print(json.dumps(dict(error=type(error).__name__, file=Path(frame.filename).name, line=frame.lineno)),
              file=sys.stderr)
        raise SystemExit(1)
