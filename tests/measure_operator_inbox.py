"""Compare exact CLI output and elapsed times on a generated post store.

Run from the candidate worktree: python3 tests/measure_operator_inbox.py BASE
BASE is the completed waiting commit. The benchmark accepts no live ship path.
"""

import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time


ROOT = Path(__file__).resolve().parent.parent
CLOCK = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
COUNT = 50_000
OPERATOR = 'operator'
MODES = ('text', 'json', 'all-text', 'all-json', 'wait-text', 'wait-json')


class Fixture:
    def __init__(self, folder):
        from quarterdeck import post

        self.folder = folder
        self.runtime = folder / 'runtime'
        self.store = post.Store(self.runtime)
        self.store.dir.mkdir(parents=True)
        self.attempts = post.Attempts(self.runtime)
        self.attempts.dir.mkdir()
        self.retired = {}
        self.count = 0

    def message(self, kind, sender, to, body, *, re=None, at=CLOCK, **fields):
        from quarterdeck.post import Message

        self.count += 1
        identifier = f'{kind[:3]}-{at:%Y%m%d-%H%M}-{sender}-{self.count:06x}'
        message = Message(identifier, kind, sender, to, at, body, re=re.id if re else None, **fields)
        (self.store.dir / (identifier + '.json')).write_text(message.to_json(), encoding='utf-8')
        return message

    def receipt(self, message, *, result='watch', seat=None, detail='', retired=False):
        name = f'{message.id}-1.json'
        data = dict(message=message.id, seat=seat or message.to, at=(CLOCK - timedelta(seconds=1)).isoformat(),
                    result=result, detail=detail)
        if retired:
            self.retired[name] = data
        else:
            (self.attempts.dir / name).write_text(json.dumps(data), encoding='utf-8')

    def populate(self):
        from quarterdeck.desk_request import PRINTED_REPLY

        old = CLOCK - timedelta(days=2)
        for number in range(260):
            report = self.message('report', 'captain', OPERATOR, f'Received {number} — café\nIndented   ',
                                  at=old if number < 30 else CLOCK)
            self.receipt(report, retired=number % 2 == 0)

        self.open = self.message('order', 'captain', OPERATOR, 'Pending work', subject='Open',
                                 due='2026-01-03', speed='now')
        closed = self.message('order', 'captain', OPERATOR, 'Closed work', subject='Closed')
        self.message('report', 'desk', 'captain', 'Wrong closer', re=closed)
        self.message('report', OPERATOR, 'captain', 'Correct closer', re=closed)
        self.receipt(closed)
        question = self.message('question', 'captain', OPERATOR, 'Answered question')
        self.message('answer', 'desk', 'captain', 'Answer', re=question)
        self.receipt(question, retired=True)

        request = self.message('order', OPERATOR, 'captain', 'Terminal request')
        direct = self.message('report', 'captain', OPERATOR, 'Already printed directly', re=request)
        self.receipt(direct, detail=PRINTED_REPLY, retired=True)
        read = self.message('report', 'desk', OPERATOR, 'Already settled by the recipient')
        self.message('report', OPERATOR, 'desk', 'Read', re=read)

        notice = "ship's orders changed · read runtime/brief/operator.md"
        self.message('report', 'ship', OPERATOR, notice, at=old)
        self.message('report', 'ship', OPERATOR, notice)
        root = self.message('order', 'captain', OPERATOR, 'Group root')
        copy = self.message('order', OPERATOR, 'desk', 'Group copy', re=root)
        self.message('report', OPERATOR, 'captain', 'Group closed', re=root)
        self.receipt(root)
        terminal = self.message('report', 'ship', OPERATOR, 'Group terminal', re=root)
        groups = self.runtime / 'post' / 'groups'
        groups.mkdir()
        (groups / (root.id + '.json')).write_text(json.dumps(dict(
            format=1, lead=OPERATOR, group='synthetic', root=json.loads(root.to_json()),
            notice=json.loads(terminal.to_json()), copies=[json.loads(copy.to_json())])), encoding='utf-8')

        repeated = self.message('report', 'desk', OPERATOR, 'Eligible repeat')
        self.receipt(repeated, result='cancelled', retired=True)
        different = self.message('report', 'desk', OPERATOR, 'Attempted for a different reader')
        self.receipt(different, seat='desk')
        self.message('ack', 'captain', OPERATOR, 'Only a claim', re=request)

        while self.count < COUNT:
            report = self.message('report', 'captain', 'desk', 'Unrelated stored message', at=old)
            if self.count < 1800:
                self.receipt(report, retired=self.count % 2 == 0)
        retired = self.runtime / 'post' / 'retired' / 'index'
        retired.mkdir(parents=True)
        (retired / 'sample.json').write_text(json.dumps(dict(format=1, attempts=self.retired)), encoding='utf-8')

    def append(self):
        self.message('report', OPERATOR, 'captain', 'Closed after append', re=self.open)
        self.message('report', 'captain', OPERATOR, 'Received after append')


class Capture(io.StringIO):
    def flush(self):
        self.flushed = time.perf_counter()
        super().flush()


def sample(folder, mode):
    from quarterdeck import cli, post

    post.now = lambda: CLOCK
    args = ['--ship', str(folder)]
    if mode.endswith('json'):
        args.append('--json')
    args.append('inbox')
    if mode.startswith('all'):
        args.append('--all')
    elif mode.startswith('wait'):
        args.append('--wait')

    receipts = []
    printed = post.printed

    def track(runtime, attempts, *, proof=None):
        printed(runtime, attempts, proof=proof)
        receipts.extend(attempts)

    post.printed = track
    destination = Capture()
    with contextlib.redirect_stdout(destination):
        started = time.perf_counter()
        code = cli.main(args)
        finished = time.perf_counter()
    if code or not destination.getvalue():
        raise RuntimeError(f'{mode} exited {code} or emitted no output')
    if mode.startswith('wait') and not receipts:
        raise RuntimeError('wait emitted no receipt')
    for attempt in receipts:
        if json.loads(attempt.path.read_bytes())['result'] != 'watch':
            raise RuntimeError('attempt was not receipted')
        attempt.path.unlink()
    sys.stdout.buffer.write(destination.getvalue().encode('utf-8'))
    print(json.dumps(dict(flush=destination.flushed - started, exit=finished - started,
                          receipts=[attempt.message for attempt in receipts])), file=sys.stderr)


def engine_env(source, folder):
    env = {key: value for key, value in os.environ.items() if not key.startswith('QD_')}
    env.pop('PYTHONDONTWRITEBYTECODE', None)
    env.update(PYTHONPATH=str(source), PYTHONPYCACHEPREFIX=str(folder.parent.parent / 'bytecode'), PYTHONUTF8='1')
    return env


def run_sample(source, folder, mode):
    args = [sys.executable, str(Path(__file__).resolve()), '--sample', str(folder), mode]
    started = time.perf_counter()
    result = subprocess.run(args, env=engine_env(source, folder), capture_output=True, check=False, timeout=180)
    elapsed = time.perf_counter() - started
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8'))
    measurements = json.loads(result.stderr)
    measurements['process'] = elapsed
    return result.stdout, measurements


def compare(expected, candidate, mode, phase):
    original, before = expected
    current, after = candidate
    if original != current or before['receipts'] != after['receipts']:
        raise RuntimeError(f'{phase} {mode}: output or receipts differ from base')
    return dict(phase=phase, mode=mode, sha256=hashlib.sha256(current).hexdigest(), bytes=len(current),
                base={key: before[key] for key in ('flush', 'exit', 'process')},
                candidate={key: after[key] for key in ('flush', 'exit', 'process')},
                receipted=len(after['receipts']))


def measure(base, scratch, repeats):
    from quarterdeck import post_index, ship

    folder = scratch / 'ships' / 'drydock'
    shutil.copytree(ROOT / 'ships' / 'drydock', folder)
    fixture = Fixture(folder)
    fixture.populate()
    ship.fence(ship.load(folder))
    archive = subprocess.check_output(['git', '-C', str(ROOT), 'archive', base, 'src'])
    archive_root = scratch / 'parent'
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(archive_root, filter='data')
    parent = archive_root / 'src'
    candidate = ROOT / 'src'
    cache = fixture.store.dir.parent / post_index.CACHE

    # Keep Python's bytecode cache warm for both versions so "cold" measures the index build.
    for source in (parent, candidate):
        subprocess.run([sys.executable, '-c', 'from quarterdeck import cli'],
                       env=engine_env(source, folder), check=True)
    print(json.dumps(dict(messages=fixture.count, attempts=len(fixture.retired) + len(list(fixture.attempts.dir.iterdir())),
                          parent=base, clock=CLOCK.isoformat())), flush=True)
    reference = run_sample(parent, folder, 'text')
    print(json.dumps(compare(reference, run_sample(candidate, folder, 'text'), 'text', 'cold')), flush=True)
    if not cache.is_file():
        raise RuntimeError('cold reading did not build the index')
    for mode in MODES:
        if mode != 'text':
            reference = run_sample(parent, folder, mode)
        for _ in range(repeats):
            result = compare(reference, run_sample(candidate, folder, mode), mode, 'warm')
            print(json.dumps(result), flush=True)

    fixture.append()
    for mode in ('text', 'json', 'wait-text', 'wait-json'):
        observed = run_sample(candidate, folder, mode)
        reference = run_sample(parent, folder, mode)
        result = compare(reference, observed, mode, 'append')
        print(json.dumps(result), flush=True)

    cache.unlink()
    cache.mkdir()
    for mode in ('text', 'json', 'wait-text', 'wait-json'):
        reference = run_sample(parent, folder, mode)
        result = compare(reference, run_sample(candidate, folder, mode), mode, 'unavailable-cache')
        print(json.dumps(result), flush=True)
    cache.rmdir()

    reference = run_sample(parent, folder, 'text')
    result = compare(reference, run_sample(candidate, folder, 'text'), 'text', 'rebuild')
    if not cache.read_bytes().startswith(b'SQLite format 3\0'):
        raise RuntimeError('the deleted cache was not rebuilt')
    print(json.dumps(result), flush=True)
    rollback, _ = run_sample(parent, folder, 'text')
    if rollback != reference[0]:
        raise RuntimeError('rollback output changed with the cache present')
    print(json.dumps(dict(phase='rollback', mode='text', sha256=hashlib.sha256(rollback).hexdigest(),
                          bytes=len(rollback))), flush=True)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--sample']:
        sample(Path(sys.argv[2]), sys.argv[3])
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('base')
        parser.add_argument('--repeats', type=int, default=3)
        args = parser.parse_args()
        sys.path.insert(0, str(ROOT / 'src'))
        base = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', args.base], text=True).strip()
        with tempfile.TemporaryDirectory(prefix='qd-operator-inbox-') as scratch:
            measure(base, Path(scratch), args.repeats)
