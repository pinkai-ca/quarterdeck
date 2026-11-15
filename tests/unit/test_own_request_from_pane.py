"""A seat's own restart request is the seat's only when its filer descends from that seat's own pane process.

Who files is read from /proc at enqueue: the filing process, then its parents, until the pane process the seat's
launch records, by its pid and its start time, so a reused pid never counts. Anything else naming the seat as its
own caller is refused as not the seat's own, with the reason, and nothing is filed. The asked time is stamped at
enqueue, never taken from the filer. The service, in turn, counts a request as the seat's own only with that proof,
for the launch the seat still runs: a request file written by hand is never the seat's own.

Real processes stand in for the pane and the filer; nothing else is faked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from quarterdeck import request_queue
from quarterdeck.seat import process_start
from quarterdeck.ship import ShipError

from .test_request_human_claude_validation import SEAT, desk, state  # noqa: F401 (desk: the fixture)

ROOT = Path(__file__).resolve().parents[2]

# The filer: enqueues the seat's own relaunch, as qd relaunch does, and prints what came of it.
FILE = r'''
import json, sys
from pathlib import Path
from quarterdeck import request_queue
runtime, seat, origin = Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
data = dict(seat=seat, resume=False, caller=seat, origin=origin, requested_at='2001-01-01T00:00:00+00:00')
try:
    path = request_queue.enqueue(runtime, 'relaunch', data, name='own-' + sys.argv[4])
except Exception as error:
    print(json.dumps(dict(refused=str(error))))
else:
    print(json.dumps(dict(filed=str(path))))
'''

def environment() -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(ROOT / 'src'), env.get('PYTHONPATH')]))
    return env


def origin_of(pid: int, start: str | None) -> dict:
    return dict(pane='%7', pid=pid, at='2032-01-01T00:00:00+00:00', harness='claude', start=start)


def run_filer(runtime: Path, seat: str, origin: dict, name: str, *, parent: list[str] | None = None) -> dict:
    """Run the filer, directly or below a given parent command line; return what it printed."""
    argv = [sys.executable, '-c', FILE, str(runtime), seat, json.dumps(origin), name]
    done = subprocess.run([*(parent or []), *argv], capture_output=True, text=True, env=environment(), timeout=60)
    return json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else dict(error=done.stderr)


@pytest.fixture
def pane():
    """A live stand-in pane process that is no ancestor of any filer here, and is closed afterwards."""
    child = subprocess.Popen([sys.executable, '-c', 'import sys; print("up", flush=True); sys.stdin.read()'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == 'up'
    yield child
    child.stdin.close()
    child.wait(timeout=10)


def requests(runtime: Path) -> list[Path]:
    folder = runtime / 'requests'
    return sorted(folder.glob('*.json')) if folder.is_dir() else []


def test_a_foreign_process_filing_as_the_seat_is_refused_and_files_nothing(tmp_path, pane):
    runtime = tmp_path / 'runtime'
    origin = origin_of(pane.pid, process_start(pane.pid))  # the seat's real, live pane process
    seen = run_filer(runtime, 'desk', origin, 'foreign')  # QD_SEAT=desk from a process outside the pane
    assert "refused as not the seat's own" in seen.get('refused', ''), seen
    assert f"does not descend from desk's pane process {pane.pid}" in seen['refused']
    assert requests(runtime) == []


def test_the_seats_own_descendant_is_accepted_with_its_proof_and_an_asked_time_of_enqueue(tmp_path):
    runtime = tmp_path / 'runtime'
    # The pane process runs a shell, which runs the filer: two generations below the pane, as a program's tool is.
    shell = [sys.executable, '-c', 'import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))']
    pane = subprocess.Popen([sys.executable, '-c', 'import sys; print("up", flush=True); sys.stdin.readline(); '
                             'import json, subprocess; args = json.loads(sys.stdin.readline()); '
                             'print(subprocess.run(args, capture_output=True, text=True).stdout.strip(), flush=True); '
                             'sys.stdin.read()'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                            env=environment())
    try:
        assert pane.stdout.readline().strip() == 'up'
        origin = origin_of(pane.pid, process_start(pane.pid))
        before = datetime.now().astimezone()
        argv = [*shell, sys.executable, '-c', FILE, str(runtime), 'desk', json.dumps(origin), 'own']
        pane.stdin.write('\n' + json.dumps(argv) + '\n')
        pane.stdin.flush()
        seen = json.loads(pane.stdout.readline())
        after = datetime.now().astimezone()
    finally:
        pane.stdin.close()
        pane.wait(timeout=10)
    assert 'filed' in seen, seen
    [path] = requests(runtime)
    meta = request_queue.metadata(path)
    assert meta['caller'] == 'desk' and meta['origin'] == origin
    proof = meta['own']
    assert (proof['pane'], proof['pane_start']) == (origin['pid'], origin['start'])
    assert proof['filer'] != pane.pid, 'the filer is a descendant, not the pane process itself'
    asked = datetime.fromisoformat(meta['created_at'])
    assert before <= asked <= after, 'the asked time is stamped at enqueue'
    assert not meta['created_at'].startswith('2001'), "never the filer's own asked time"
    assert 'requested_at' not in json.loads(path.read_text())


def test_the_pane_process_itself_filing_is_accepted(tmp_path):
    runtime = tmp_path / 'runtime'
    origin = origin_of(os.getpid(), process_start(os.getpid()))  # this process is the pane process
    path = request_queue.enqueue(runtime, 'relaunch', dict(seat='desk', resume=False, caller='desk', origin=origin))
    assert request_queue.metadata(path)['own']['pane'] == os.getpid()


def test_a_reused_pid_is_refused(tmp_path):
    runtime = tmp_path / 'runtime'
    # This process is an ancestor of the filer, by pid; the launch recorded another start: the pid was reused.
    origin = origin_of(os.getpid(), str(int(process_start(os.getpid())) - 1))
    seen = run_filer(runtime, 'desk', origin, 'reused')
    assert "refused as not the seat's own" in seen.get('refused', ''), seen
    assert requests(runtime) == []


def test_a_pane_process_that_is_gone_is_refused(tmp_path):
    runtime = tmp_path / 'runtime'
    gone = subprocess.Popen([sys.executable, '-c', 'pass'])
    gone.wait(timeout=10)
    seen = run_filer(runtime, 'desk', origin_of(gone.pid, '12345'), 'gone')
    assert "refused as not the seat's own" in seen.get('refused', ''), seen
    assert requests(runtime) == []


@pytest.mark.parametrize('origin', [None, origin_of(4242, None), dict(pane='%7')])
def test_a_launch_without_a_recorded_pane_process_and_start_proves_nothing(tmp_path, origin):
    with pytest.raises(ShipError, match="refused as not the seat's own: its launch records no pane process"):
        request_queue.enqueue(tmp_path / 'runtime', 'relaunch',
                              dict(seat='desk', resume=False, caller='desk', origin=origin))
    assert requests(tmp_path / 'runtime') == []


def test_another_callers_request_needs_no_proof_and_is_not_the_seats_own(tmp_path):
    runtime = tmp_path / 'runtime'
    path = request_queue.enqueue(runtime, 'relaunch', dict(seat='desk', resume=False, caller='captain', origin=None))
    meta = request_queue.metadata(path)
    assert 'own' not in meta and not request_queue.proved_own('desk', meta, None)


def test_the_service_counts_a_request_as_the_seats_own_only_with_its_proof_for_the_launch_it_runs():
    origin = origin_of(4242, '777')
    proof = dict(pane=4242, pane_start='777', filer=4343, filer_start='778')
    own = dict(caller='desk', origin=origin, own=proof)
    assert request_queue.proved_own('desk', own, origin)
    assert not request_queue.proved_own('desk', {**own, 'own': None}, origin), 'written by hand, with no proof'
    assert not request_queue.proved_own('desk', own, {**origin, 'start': '999'}), 'the seat relaunched since'
    assert not request_queue.proved_own('desk', own, None), 'no launch to prove it by'
    assert not request_queue.proved_own('desk', {**own, 'caller': 'captain'}, origin)
    assert not request_queue.proved_own('desk', {**own, 'own': {**proof, 'pane_start': '1'}}, origin)


def test_a_hand_written_own_request_is_not_held_for_the_seats_asking_turn(desk):
    """Filed by another caller, then rewritten by hand to name the seat: the service treats it as another caller's,
    refused at once during the person's turn, never held for later."""
    desk.state = state(open_=True, input_at=-5)
    path = desk.ask(caller='captain-larch', seconds=0)
    meta = request_queue.metadata(path)
    request_queue.save_metadata(path, {**meta, 'caller': SEAT})  # forged: the seat's name, no proof
    desk.tick(1)
    assert desk.applied == [] and 'not proved at an immediate break' in desk.answer(path)
    assert not path.exists()


def test_a_proved_own_request_is_held_for_its_asking_turn(desk):
    desk.state = state(open_=True, input_at=-5)
    path = desk.ask(seconds=0)  # filed by this process, which stands in for the desk's pane process
    assert request_queue.metadata(path)['own']['pane'] == os.getpid()
    desk.tick(1)
    assert desk.applied == [] and path.exists() and desk.answer(path) is None
