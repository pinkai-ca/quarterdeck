"""Explicit disk-frozen status cost proof; copies are owned and removed by testrun.py."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import statistics
import subprocess
import sys
import time

from quarterdeck import cli, output, post, presentation, service, servicectl, ship as shipmod, status_snapshot, trusted_files
from quarterdeck.tmux import Tmux


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)


def copy_file(source, destination):
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError('nonregular copied input')
        with os.fdopen(os.dup(descriptor), 'rb') as stream:
            data = stream.read()
        if identity(os.fstat(descriptor)) != identity(before) or identity(source.lstat()) != identity(before):
            raise RuntimeError('copied input changed during read')
    finally:
        os.close(descriptor)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    os.utime(destination, ns=(before.st_atime_ns, before.st_mtime_ns))
    return len(data), hashlib.sha256(data).digest()


def frozen_copy(source, destination):
    copy_file(source / 'ship.toml', destination / 'ship.toml')
    counts, sizes, digest = {}, {}, hashlib.sha256()
    for family in ('store', 'imports', 'forwarded', 'addressed'):
        old, new = source / 'runtime/post' / family, destination / 'runtime/post' / family
        new.mkdir(parents=True, exist_ok=True)
        counts[family], sizes[family] = 0, 0
        if not old.exists():
            continue
        for name in sorted(os.listdir(old)):
            if name.endswith('.json'):
                length, hashed = copy_file(old / name, new / name)
                counts[family] += 1
                sizes[family] += length
                digest.update(family.encode() + name.encode() + hashed)
    ship = shipmod.load(source)
    for name in ship.seats:
        old = trusted_files.path(source / 'runtime', name, 'compaction.json')
        if old.exists():
            copy_file(old, trusted_files.path(destination / 'runtime', name, 'compaction.json'))
    return dict(files=counts, bytes=sizes, opaque_digest=digest.hexdigest(), native_records=0)


def measured(function):
    cpu, wall = time.process_time(), time.perf_counter()
    value = function()
    return dict(cpu_seconds=time.process_time() - cpu, wall_seconds=time.perf_counter() - wall), value


def cost_child(folder, cold_first):
    ship = shipmod.load(Path(folder))
    # Age only Store's file observations. The clock continues to advance, and
    # snapshot/demand freshness retains its real wall/monotonic clock pair.
    native_time = post.time
    class AgedStoreClock:
        def time_ns(self):
            return native_time.time_ns() + 20 * post.POST_TIMESTAMP_TICK_NS
        def __getattr__(self, name):
            return getattr(native_time, name)
    post.time = AgedStoreClock()
    actor = service.Service(ship, Tmux.for_ship(ship))
    lock = servicectl.acquire_lock(ship.runtime)
    try:
        actor.first_beat()
        with post.reading_pass(store=actor.store):
            actor.store.all()
        request = status_snapshot.request
        def call(cold):
            status_snapshot.request = (lambda ship: False) if cold else request
            stream, diagnostic = io.StringIO(), io.StringIO()
            try:
                with redirect_stdout(stream), redirect_stderr(diagnostic):
                    code = cli.main(['--ship', str(ship.folder), '--json', 'status'])
                if code:
                    raise RuntimeError('copied status command refused')
                value = json.loads(stream.getvalue())
                if value.pop('reading')['mode'] != ('cold' if cold else 'served'):
                    raise RuntimeError('copied status command used the wrong reader')
                return value
            finally:
                status_snapshot.request = request
        result = {}
        if cold_first:
            result['cold'], cold = measured(lambda: call(True))
        assert request(ship)
        result['headless_whole_reading'], _ = measured(actor.status_due)
        if actor.status_done != 'full' or not status_snapshot.path(ship.runtime).exists():
            raise RuntimeError('headless whole reading did not publish')
        if not cold_first:
            result['cold'], cold = measured(lambda: call(True))
        result['served'], served = measured(lambda: call(False))
        if served != cold:
            raise RuntimeError('copied served fields differ from independent fields')
        result['equal'] = True
        result['reading_period_seconds'] = ship.status_refresh
        result['headless_cpu_seconds_per_minute'] = result['headless_whole_reading']['cpu_seconds'] * 60 / ship.status_refresh
        result['facts_digest'] = hashlib.sha256(json.dumps(cold, sort_keys=True).encode()).hexdigest()
        result['messages'] = len(actor.store.all())
    finally:
        actor.ledger.close()
        lock.close()
    print(json.dumps(result, sort_keys=True), flush=True)


def test_disk_frozen_status_service_cost(tmp_path, monkeypatch, capsys, tmux):
    try:
        filesystem = subprocess.run(['stat', '-f', '-c', '%T', str(tmp_path)], capture_output=True,
                                    text=True, check=True).stdout.strip()
        if filesystem in ('tmpfs', 'ramfs') or tmp_path.is_relative_to('/tmp'):
            raise RuntimeError('copied proof requires disk scratch')
        source = Path(os.environ['QD_STATUS_MEASURE_SOURCE'])
        frozen = tmp_path / 'frozen' / source.name
        result = dict(snapshot=frozen_copy(source, frozen), filesystem=filesystem, samples=[],
                      absent=['native records', 'credential homes', 'live panes', 'original ledger', 'attempts and pending'])
        monkeypatch.setenv('QD_TMUX_SOCKET', tmux.socket)
        for key in ('QD_SHIP', 'QD_SEAT', 'TMUX', 'TMUX_PANE'):
            monkeypatch.delenv(key, raising=False)
        code = ('import sys; sys.path.insert(0,sys.argv[1]); '
                'from tests.measure_status_service import cost_child; '
                'cost_child(sys.argv[2],sys.argv[3]=="cold-first")')
        for number in range(3):
            folder = tmp_path / ('trial' + str(number)) / source.name
            shutil.copytree(frozen, folder)
            done = subprocess.run([sys.executable, '-I', '-c', code, str(Path.cwd()), str(folder),
                                   'cold-first' if number % 2 == 0 else 'reading-first'],
                                  capture_output=True, text=True, check=True)
            result['samples'].append(json.loads(done.stdout))
        result['medians'] = {part: {field: statistics.median(sample[part][field] for sample in result['samples'])
                                    for field in ('cpu_seconds', 'wall_seconds')}
                             for part in ('cold', 'headless_whole_reading', 'served')}
        result['headless_cpu_seconds_per_minute'] = statistics.median(
            sample['headless_cpu_seconds_per_minute'] for sample in result['samples'])
    except Exception as error:
        raise RuntimeError('disk-frozen status cost proof failed: ' + type(error).__name__
                           + '; opaque output withheld') from None
    with capsys.disabled():
        print('COPIED_STATUS_SERVICE ' + json.dumps(result, sort_keys=True), flush=True)
