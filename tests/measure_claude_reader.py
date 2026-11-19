"""Run with python3 -B -m tests.measure_claude_reader on a test checkout.

Each sample is a separate local process with a freshly generated record.
The benchmark reports process peak RSS, including any warm-up in that process,
and process CPU and elapsed wall for the last complete observation.
"""

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter, process_time
from types import SimpleNamespace

from quarterdeck import input_receipt, post, record_owner, trusted_files
from quarterdeck.harness import claude, json_record, record_file
from quarterdeck.ledger import Ledger
from quarterdeck.service import Service
from tests.compaction_shapes import AT, Native


MIB = 1024 * 1024
CHUNK = 64 * 1024
RECIPIENT = 'synthetic-reader'


class Case:
    def __init__(self, root):
        self.runtime = root / 'runtime'
        self.home = self.runtime / 'seats' / RECIPIENT / 'home'
        self.directory = self.home / 'projects' / 'synthetic-project'
        self.directory.mkdir(parents=True)
        self.record = self.directory / 'synthetic.jsonl'
        self.store = post.Store(self.runtime)
        self.trusted = trusted_files.make(self.runtime, RECIPIENT)
        self.messages = [post.Message(f'ord-20300101-0000-fixture-{i:06x}', 'order', 'synthetic-issuer',
                                      RECIPIENT, AT, f'Fictional task {i} — exact text.')
                         for i in (1, 2)]
        for message in self.messages:
            self.store.write(message)

    def turns(self, number):
        """Two completed native turns with known token counts and a compaction marker."""
        shape = Native(self.directory / 'shape.jsonl', 'claude')
        shape.rows[0]['message']['content'] = post.typed([self.messages[number]])
        shape.finish()
        shape.compact()
        initial = len(shape.rows)
        shape.begin(post.typed([self.messages[1 - number]]))
        shape.finish()
        for row in shape.rows:
            row['version'] = '2.1.317'
            if row['type'] == 'assistant':
                row['message'].update(model='claude-sonnet-4-6', role='assistant',
                                      usage=dict(input_tokens=3, output_tokens=5,
                                                 cache_read_input_tokens=7, cache_creation_input_tokens=11))
        shape.path.unlink()
        encoded = [json.dumps(row, ensure_ascii=False).encode() + b'\n' for row in shape.rows]
        return b''.join(encoded[:initial]), b''.join(encoded[initial:])

    def observe(self, source):
        """Measure the regular Claude consumers and ledger with rows kept."""
        adapter = claude.ClaudeHarness()
        state, turns = adapter.read(source)
        inputs = adapter.inputs(source)
        receipts = [input_receipt.confirmed(adapter, source, post.typed([message]), AT)
                    for message in self.messages]
        foreign = input_receipt.confirmed(adapter, source,
                                          post.typed([self.messages[0]]).replace('Fictional', 'Edited'), AT)
        history = claude.compactions(source)
        totals, attributions, errors = self.account(source, adapter)
        return dict(state=state, turns=turns, inputs=list(inputs), diagnostics=inputs.diagnostics(),
                    receipts=receipts, foreign=foreign, history=history, totals=totals, attribution=attributions,
                    account_errors=errors)

    def account(self, source, adapter):
        record_owner.register(self.trusted, source, 'claude')
        with TemporaryDirectory(dir=self.runtime, prefix='ledger-') as ledger, Ledger(Path(ledger) / 'ledger.sqlite') as book:
            actor = Service.__new__(Service)
            actor.runtime, actor.store, actor.ledger = self.runtime, self.store, book
            actor.read_marks, actor.account_errors, actor.meters = {}, {}, {}
            seat = SimpleNamespace(name=RECIPIENT, trusted=self.trusted, home=self.home, harness=adapter,
                                   session=SimpleNamespace(human=False), pane=lambda: None,
                                   record=lambda: source, launched=lambda: None)
            actor.resolved_seats = lambda: [seat]
            owned = record_owner.Owned(source, 'claude', adapter)
            actor.account_records(source_records={RECIPIENT: [owned]}, process_table={},
                                  source_screens={}, replay=False)
            book.attributions.replay_stream(self.store, self.runtime, None, lambda *_: None, lambda: None)
            totals = book.db.execute('SELECT turn, read_cached, cache_written, written, unmeasured FROM turns ORDER BY turn').fetchall()
            attributions = book.attributions.details()
            assert all(row['record'] == str(source) for row in attributions)
            errors = [(seat_name, pool, reason) for _, seat_name, pool, reason in book.incomplete_records()]
        return totals, attributions, errors


def write_value(stream, size):
    block = b'x' * CHUNK
    for _ in range(size // CHUNK):
        stream.write(block)
    stream.write(block[:size % CHUNK])


def write_record(path, size, shape, core):
    left = b'{"snapshot":{"syntheticValue":"'
    right = b'"},"type":"file-history-snapshot"}\n'
    minimum = len(left) + len(right)
    skipped = b'! synthetic malformed row\n' if shape == 'dense-malformed' else b''
    padding = size - len(core) - len(skipped)
    if padding < minimum:
        raise ValueError('fixture is too small')
    with path.open('wb') as stream:
        if shape.startswith('dense-'):
            prefix = b'{"type":"file-history-snapshot","message":{"content":[{"type":"text","text":"'
            suffix = b'"}]}}\n'
            width = 16 * 1024
            full, rest = divmod(padding, width)
            for _ in range(full):
                stream.write(prefix + b'x' * (width - len(prefix) - len(suffix)) + suffix)
            stream.write(b' ' * (rest - 1) + b'\n' if rest else b'')
        elif shape == 'large-value':
            stream.write(left)
            write_value(stream, padding - minimum)
            stream.write(right)
        else:
            width = 16 * 1024
            full, remainder = divmod(padding, width)
            if 0 < remainder < minimum:
                full -= 1
                remainder += width
            line = left + b'x' * (width - minimum) + right
            for _ in range(full):
                stream.write(line)
            if remainder:
                stream.write(left)
                write_value(stream, remainder - minimum)
                stream.write(right)
        stream.write(skipped)
        stream.write(core)
    assert path.stat().st_size == size


def semantic_digest(result):
    state = asdict(result['state'])
    state.pop('last_write')  # independently generated fixtures have different file times
    history = {name: getattr(result['history'], name) for name in
               ('conversation', 'count', 'reason', 'extent', 'digest', 'frontier', 'turn',
                'complete', 'last_call', 'origin')}
    attribution = [{key: value for key, value in row.items() if key != 'record'}
                   for row in result['attribution']]
    # The ledger asserts the real canonical record path separately in Case.account.
    facts = {**result, 'state': state, 'history': history, 'attribution': attribution,
             'inputs': [asdict(item) for item in result['inputs']],
             'turns': [asdict(item) for item in result['turns']]}
    return sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def sample(mib, shape, phase, mode):
    with TemporaryDirectory(prefix='synthetic-streaming-') as directory:
        case = Case(Path(directory))
        core, extra = case.turns(0)
        alternate, _ = case.turns(1)
        target = mib * MIB
        write_record(case.record, target - (len(extra) if phase == 'append' else 0), shape, core)
        json_record.keep_rows()
        work = dict(raw_work_bytes=0, slice_max=0, full_bytes=0, full_reads=0)
        full_reader = record_file.read_from

        def counted_full(*args, **kwargs):
            data = full_reader(*args, **kwargs)
            work['full_reads'] += 1
            work['full_bytes'] += len(data)
            return data

        record_file.read_from = counted_full
        if mode == 'production':
            from quarterdeck.harness import claude_record
            original = claude_record.read_piece

            def counted(descriptor, remaining):
                data = original(descriptor, remaining)
                work['raw_work_bytes'] += len(data)
                work['slice_max'] = max(work['slice_max'], len(data))
                return data

            claude_record.read_piece = counted
            if mib > 128 and shape != 'dense-evidence':
                # Synthetic-only comparison. No runtime or native records are involved.
                record_file.LINES_BOUND = target

        def observe():
            return case.observe(case.record)

        if phase != 'cold':
            try:
                observe()
            except (OSError, ValueError):
                if mode != 'baseline' or mib <= 128:
                    raise
            if phase == 'append':
                with case.record.open('ab') as stream:
                    stream.write(extra)
            else:
                replaced = case.record.with_suffix('.replacement')
                write_record(replaced, target, shape, alternate)
                os.replace(replaced, case.record)
        work.update(raw_work_bytes=0, slice_max=0, full_bytes=0, full_reads=0)
        wall, cpu = perf_counter(), process_time()
        try:
            result = observe()
        except record_file.PastBound as error:
            if mode != 'baseline' or mib <= 128:
                raise
            work.update(outcome='past-bound', raw_size=error.size, bound=error.bound)
        else:
            expected = [0, 1] if phase == 'append' else [1] if phase == 'replacement' else [0]
            assert result['foreign'] is None
            assert result['state'].turn_open is False
            if shape == 'dense-malformed':
                assert result['diagnostics']['total'] == 1
                assert result['history'].reason is not None
                assert result['state'].pending_tools is None
            else:
                assert result['receipts'] == [
                    'delivered' if i in expected else None for i in range(2)]
                assert result['history'].count == 1 and result['history'].extent == target
                assert result['history'].complete
                assert [row['orders'] for row in result['attribution']] == [
                    [case.messages[i].id] for i in expected]
            assert len(result['turns']) == len(result['totals']) == len(expected)
            assert all(row[1:] == (7, 11, 5, 0) for row in result['totals'])
            work.update(outcome='read', turns=len(result['turns']), semantic_digest=semantic_digest(result))
        work.update(mib=mib, shape=shape, phase=phase, mode=mode,
                    wall_s=round(perf_counter() - wall, 5), cpu_s=round(process_time() - cpu, 5),
                    rss_mib=peak_rss_mib())
        print(json.dumps(work), flush=True)


def peak_rss_mib():
    # VmHWM belongs to this executable's address space, unlike the rusage
    # high-water value that can survive a launcher exec on Linux.
    row = next(line for line in Path('/proc/self/status').read_text().splitlines()
               if line.startswith('VmHWM:'))
    return round(int(row.split()[1]) / 1024, 2)


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument('--baseline-src', type=Path, help='src/ from the exact production parent')
    args.add_argument('--shape', choices=('large-value', 'many-rows', 'dense-evidence', 'dense-malformed'))
    args.add_argument('--sample', nargs=4, metavar=('SIZE_MIB', 'SHAPE', 'PHASE', 'MODE'))
    given = args.parse_args()
    if given.sample:
        mib, shape, phase, mode = given.sample
        sample(int(mib), shape, phase, mode)
        return
    if given.baseline_src is None:
        args.error('--baseline-src is required for a comparison')
    paths = {'baseline': given.baseline_src.absolute(), 'production': Path('src').absolute()}
    sizes = {'large-value': (128, 295, 512), 'many-rows': (128, 295, 512), 'dense-evidence': (8, 24),
             'dense-malformed': (24,)}
    for shape, amounts in sizes.items():
        if given.shape and shape != given.shape:
            continue
        for mib in amounts:
            for phase in ('cold', 'append', 'replacement'):
                samples = {}
                for mode, source in paths.items():
                    raw = subprocess.check_output([sys.executable, '-B', '-m', 'tests.measure_claude_reader',
                                                   '--sample', str(mib), shape, phase, mode],
                                                  env=dict(os.environ, PYTHONPATH=str(source)), text=True)
                    samples[mode] = json.loads(raw)
                    print(raw.strip(), flush=True)
                if samples['baseline']['outcome'] == 'read':
                    assert samples['baseline']['semantic_digest'] == samples['production']['semantic_digest'], (
                        shape, mib, phase, 'reading and accounting evidence differ')



if __name__ == '__main__':
    main()
