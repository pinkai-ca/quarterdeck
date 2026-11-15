"""A live private service's complete reading equals the independent reading at its publication barrier."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time

from quarterdeck import cli, output, post, service, ship as shipmod, status_snapshot, watch
from quarterdeck.seat import Seat


def run_service(folder, release_fd):
    from quarterdeck.tmux import Tmux
    class PausedAfterReading(service.Service):
        def publish_status(self):
            written = super().publish_status()
            # Pause on an event after a completed, healthy whole reading. The
            # heartbeat thread continues; no duty or native source can move
            # during the two client readings of this same publication tick.
            if (written and self.status_done == 'full' and not self.ledger.catching_up()
                    and not getattr(getattr(self, 'account_driver', None), 'active', False)
                    and status_snapshot.path(self.runtime).exists()):
                print('STATUS_READY', flush=True)
                os.read(release_fd, 1)
            return written
    ship = shipmod.load(Path(folder))
    actor = PausedAfterReading(ship, Tmux.for_ship(ship))
    try:
        actor.run()
    finally:
        actor.ledger.close()


def status_command(folder, cold):
    if cold:
        status_snapshot.request = lambda ship: False
    captured = io.StringIO()
    cpu, wall = time.process_time(), time.perf_counter()
    with redirect_stdout(captured):
        code = cli.main(['--ship', folder, '--json', 'status'])
    data = json.loads(captured.getvalue())
    print(json.dumps(dict(exit=code, cpu_seconds=time.process_time() - cpu,
                          wall_seconds=time.perf_counter() - wall, data=data)), flush=True)


def test_live_service_reading_matches_cold_at_the_same_tick(tmp_path, tmux, monkeypatch, capsys):
    (tmp_path / 'ship.toml').write_text('''[ship]
name="synthetic-status"
code="checkout"
tick=0.05
status_refresh=5
[pools.fake]
provider="synthetic"
[sessions.crew]
title="Synthetic status crew"
[seats.worker]
session="crew"
role="worker"
harness="fake"
pool="fake"
up=false
''')
    ship = shipmod.load(tmp_path)
    tmux.new_session(ship.sessions['crew'].title)
    seat = Seat(ship, ship.seat('worker'), tmux)
    seat.up()
    post.Store(ship.runtime).new('report', 'issuer', 'ship', 'Invented status history')
    monkeypatch.setenv('QD_TMUX_SOCKET', tmux.socket)
    monkeypatch.delenv('QD_SHIP', raising=False)
    monkeypatch.delenv('QD_SEAT', raising=False)
    assert status_snapshot.request(ship)
    reader, writer = os.pipe()
    code = ('import sys; sys.path.insert(0,sys.argv[1]); '
            'from tests.integration.test_status_service_reading_ordinary import run_service; '
            'run_service(sys.argv[2],int(sys.argv[3]))')
    process = subprocess.Popen([sys.executable, '-I', '-c', code, str(Path.cwd()), str(tmp_path), str(reader)],
                               pass_fds=(reader,), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    os.close(reader)
    try:
        # This is an event barrier, not a machine-speed assertion. The test
        # tool owns the outer timeout and all interrupted-process cleanup.
        assert process.stdout.readline().strip() == 'STATUS_READY'
        snapshot = status_snapshot.served(ship)
        assert snapshot is not None
        cold = output.value(watch.status_reading(ship, tmux))
        snapshot.pop('reading')
        assert snapshot == cold
        samples = dict(cold=[], served=[])
        command = ('import sys; sys.path.insert(0,sys.argv[1]); '
                   'from tests.integration.test_status_service_reading_ordinary import status_command; '
                   'status_command(sys.argv[2],sys.argv[3]=="cold")')
        for number in range(3):
            for label in (('cold', 'served') if number % 2 == 0 else ('served', 'cold')):
                before = resource.getrusage(resource.RUSAGE_CHILDREN)
                started = time.perf_counter()
                done = subprocess.run([sys.executable, '-I', '-c', command, str(Path.cwd()), str(tmp_path), label],
                                      capture_output=True, text=True, check=True)
                elapsed = time.perf_counter() - started
                after = resource.getrusage(resource.RUSAGE_CHILDREN)
                result = json.loads(done.stdout)
                assert result['exit'] == 0
                assert result['data'].pop('reading')['mode'] == label
                assert result.pop('data') == cold
                result.update(process_wall_seconds=elapsed,
                              process_cpu_seconds=after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime)
                samples[label].append(result)
        medians = {label: {field: statistics.median(sample[field] for sample in values)
                           for field in ('cpu_seconds', 'wall_seconds', 'process_cpu_seconds', 'process_wall_seconds')}
                   for label, values in samples.items()}
        with capsys.disabled():
            print('LIVE_STATUS_SERVICE ' + json.dumps(dict(equal=True, samples=samples, medians=medians,
                                                          native_programs=0, synthetic_programs=1), sort_keys=True), flush=True)
    finally:
        os.write(writer, b'x')
        os.close(writer)
        process.terminate()
        process.communicate()
        seat.down()
