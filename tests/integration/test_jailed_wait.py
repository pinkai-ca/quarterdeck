"""A jailed seat in a person's session hears the crew: its watch arms, the waking line wakes it, and its wait receives.

Real bubblewrap. Inside the jail runtime/post is read-only: the wait takes no admission lock and writes no attempt.
The service opens the attempts on its behalf and closes them from the receipts the wait leaves in its own seat
folder. The jailed seat still cannot write the store, another seat's drop, admission or attempts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from quarterdeck import cli, post, sandbox, seat as seatmod, service, ship as shipmod
from quarterdeck.tmux import Tmux

from .progress import service_progress
from .test_sandbox import namespaces_allowed
from .waiting import await_drop, wait_for

pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap is not installed, or cannot make a namespace here (inside a jail)")

QD = Path(sys.executable).parent / 'qd'
EXACT = 'qd > ship: messages are waiting. Run qd inbox --wait now to read them.'


@pytest.fixture
def ship(drydock: Path, tmux: Tmux, tmp_path: Path, monkeypatch) -> shipmod.Ship:
    workspace = tmp_path / 'desk-work'
    workspace.mkdir()
    monkeypatch.setenv('PATH', f"{QD.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    text = (drydock / 'ship.toml').read_text()
    text = shipmod.edit(text, 'seats.desk', 'sandbox', True)
    text = shipmod.edit(text, 'seats.desk', 'cwd', str(workspace))
    text += '\n[seats.captain.settings]\nstep_seconds = "0.05"\n\n[seats.desk.settings]\nstep_seconds = "0.05"\n'
    (drydock / 'ship.toml').write_text(text)
    monkeypatch.setenv('QD_TMUX_SOCKET', tmux.socket)
    monkeypatch.setenv('QD_SHIP', str(drydock))
    monkeypatch.delenv('QD_SEAT', raising=False)
    ship = shipmod.load(drydock)
    tmux.new_session('Synthetic bootstrap')
    tmux.kill_session('Synthetic bootstrap')
    yield ship
    service.stop(ship.runtime)


def qd(capsys, *argv: str) -> str:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    assert code == 0, out.err
    text = out.out.rstrip('\n')
    if argv and argv[0] == 'post':
        await_drop(text)
    return text


def inputs(seat: seatmod.Seat) -> list[str]:
    record = seat.record()
    return [i.text.strip() for i in seat.harness.inputs(record)] if record else []


def desk_jail(ship: shipmod.Ship) -> sandbox.Jail:
    """The desk's own jail, as its launch builds it."""
    own = ship.runtime / 'seats' / 'desk'
    workspace = Path(ship.seat('desk').cwd)
    return sandbox.plan(ship, 'desk', own, own / 'home', workspace, sys.executable, os.environ['PATH'])


def jailed(ship: shipmod.Ship, argv: list[str], **kwargs):
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    env.update(QD_SEAT='desk', QD_SHIP=str(ship.folder))
    return subprocess.Popen(sandbox.command(desk_jail(ship), argv), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, **kwargs)


def test_a_jailed_desk_arms_its_watch_is_woken_and_its_wait_receives(ship, tmux, capsys):
    qd(capsys, 'up')
    desk = seatmod.Seat(ship, ship.seat('desk'), tmux)
    launched = desk.launched()
    assert sandbox.seats_hidden(launched.argv) is True, 'the desk runs jailed'
    order = post.Store(ship.runtime).all()  # nothing yet
    qd(capsys, 'post', 'order', '--from', 'captain', '--to', 'desk', '--subject', 'Synthetic', 'Synthetic: for the jailed desk.')
    order = next(m for m in post.Store(ship.runtime).all() if m.to == 'desk')
    assert not post.watch_armed(ship.runtime, 'desk')

    woken = wait_for(lambda: inputs(desk) or None, seconds=60)
    assert woken == [EXACT], 'the fixed waking line, typed once into the empty box'

    # The desk's agent does what the line says, from inside its own jail.
    wait = jailed(ship, [str(QD), 'inbox', '--wait'])
    try:
        out, err = wait.communicate(timeout=90)
    finally:
        if wait.poll() is None:
            wait.kill()
    assert wait.returncode == 0, err
    assert order.short in out and 'Synthetic: for the jailed desk.' in out
    attempts = post.Attempts(ship.runtime).for_message(order.id)
    assert len(attempts) == 1 and attempts[0].seat == 'desk', 'the service opened one attempt on its behalf'
    wait_for(lambda: [a.result for a in post.Attempts(ship.runtime).for_message(order.id)] == ['watch'], seconds=60)
    assert post.Attempts(ship.runtime).for_message(order.id)[0].detail == 'printed by qd inbox --wait'
    service_progress(ship)
    service_progress(ship)
    assert inputs(desk) == [EXACT], 'one wake, never repeated: the receipt is a delivery'


def test_a_jailed_watch_reads_armed_while_it_waits_and_holds_the_waking_line(ship, tmux, capsys):
    qd(capsys, 'up')
    desk = seatmod.Seat(ship, ship.seat('desk'), tmux)
    wait = jailed(ship, [str(QD), 'inbox', '--wait'])
    try:
        wait_for(lambda: post.watch_armed(ship.runtime, 'desk'), seconds=30)
        qd(capsys, 'post', 'order', '--from', 'captain', '--to', 'desk', 'Synthetic: to an armed jailed watch.')
        out, err = wait.communicate(timeout=90)
    finally:
        if wait.poll() is None:
            wait.kill()
    assert wait.returncode == 0, err
    assert 'Synthetic: to an armed jailed watch.' in out
    assert inputs(desk) == [], 'an armed watch holds the waking line'
    wait_for(lambda: not post.watch_armed(ship.runtime, 'desk'), seconds=10)  # its lease went with it


def test_from_inside_the_desks_jail_nothing_of_the_post_is_writable(ship, capsys):
    store = post.Store(ship.runtime)
    victim = store.new('order', 'captain', 'worker-1', 'VICTIM')
    for folder in ('admission', 'attempts', 'drop/worker-1', 'watch-offers'):
        (ship.runtime / 'post' / folder).mkdir(parents=True, exist_ok=True)
    (ship.runtime / 'post' / 'admission' / 'existing.lock').write_text('')
    (ship.runtime / 'seats' / 'desk' / 'home').mkdir(parents=True, exist_ok=True)
    probe = r"""
import json, os, sys
from pathlib import Path
post, desk = map(Path, sys.argv[1:3])
def write(path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('forged')
        return True
    except OSError:
        return False
def open_rw(path):
    try:
        os.close(os.open(path, os.O_RDWR))
        return True
    except OSError:
        return False
print(json.dumps(dict(
    store=write(post / 'store' / 'forged.json'),
    other_drop=write(post / 'drop' / 'worker-1' / 'forged.json'),
    admission=write(post / 'admission' / 'forged.lock') or open_rw(post / 'admission' / 'existing.lock'),
    attempts=write(post / 'attempts' / 'forged.json'),
    offers=write(post / 'watch-offers' / 'desk.json'),
    own_receipts=write(desk / 'watch-receipts' / 'own.json'),
)))
"""
    process = jailed(ship, [sys.executable, '-c', probe, str(ship.runtime / 'post'), str(ship.runtime / 'seats' / 'desk')])
    out, err = process.communicate(timeout=60)
    assert process.returncode == 0, err
    seen = json.loads(out.strip().splitlines()[-1])
    assert seen == dict(store=False, other_drop=False, admission=False, attempts=False, offers=False, own_receipts=True), seen
    assert (store.dir / f'{victim.id}.json').is_file() and not (store.dir / 'forged.json').exists()


AUDITED = r"""
import json, os, sys
writes, locks = [], []
announced = {}  # a seat-folder write opened by name within a folder's handle: its full path, named just before
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

def hook(event, args):
    if event == 'quarterdeck.seat_files.open':
        announced[os.path.basename(args[0])] = args[0]
    elif event == 'open':
        path, mode, flags = args
        if isinstance(path, int):
            return
        writing = (isinstance(flags, int) and flags & WRITE) or (isinstance(mode, str) and any(c in mode for c in 'wax+'))
        if writing:
            name = os.fsdecode(path)
            writes.append(announced.pop(name, name) if not os.path.isabs(name) else name)
    elif event == 'fcntl.flock':
        try:
            locks.append(os.readlink(f'/proc/self/fd/{args[0]}'))
        except OSError:
            locks.append('unknown descriptor')

sys.addaudithook(hook)
from quarterdeck import cli
code = cli.main(['inbox', '--wait'])
sys.stdout.flush()
sys.stderr.write('AUDIT ' + json.dumps(dict(writes=writes, locks=locks)) + '\n')
sys.exit(code)
"""


def test_a_woken_jailed_wait_writes_and_locks_only_its_own_seat_folder(ship, tmux, capsys):
    """Every write-open and every flock the jailed wait makes, from Python's own audit events, is in its seat folder."""
    qd(capsys, 'up')
    desk = seatmod.Seat(ship, ship.seat('desk'), tmux)
    qd(capsys, 'post', 'order', '--from', 'captain', '--to', 'desk', 'Synthetic: audited.')
    assert wait_for(lambda: inputs(desk) or None, seconds=60) == [EXACT], 'woken by the message'
    wait = jailed(ship, [sys.executable, '-B', '-c', AUDITED])
    try:
        out, err = wait.communicate(timeout=90)
    finally:
        if wait.poll() is None:
            wait.kill()
    assert wait.returncode == 0, err
    assert 'Synthetic: audited.' in out
    audit = json.loads(next(line for line in err.splitlines() if line.startswith('AUDIT '))[6:])
    own = str(ship.runtime / 'seats' / 'desk') + os.sep
    post_folder = str(ship.runtime / 'post') + os.sep
    assert audit['locks'] and all(path.startswith(own) for path in audit['locks']), audit['locks']
    assert audit['writes'] and all(os.path.isabs(path) and path.startswith(own) for path in audit['writes']), audit['writes']
    assert not any(path.startswith(post_folder) for path in audit['writes'] + audit['locks'])


def test_a_jailed_check_seat_held_by_a_live_check_lease_is_served(ship, capsys):
    """qd check's human-wake seat is a temporary seat no ship file declares: only the check's live lease admits it.
    Jailed, its wait must still be served by the service (worker7's pi check on 97f1055c: no watch receipt)."""
    from .test_human_wake_inbox_lease_claude_validation import Lease, SEAT

    qd(capsys, 'up')
    lease = Lease(ship)
    try:
        message = post.Store(ship.runtime).new('order', 'ship', SEAT, 'Synthetic: for the jailed check seat.')
        own = ship.runtime / 'seats' / SEAT
        (own / 'home').mkdir(parents=True, exist_ok=True)
        workspace = Path(ship.seat('desk').cwd)
        jail = sandbox.plan(ship, SEAT, own, own / 'home', workspace, sys.executable, os.environ['PATH'])
        env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
        env.update(QD_SEAT=SEAT, QD_SHIP=str(ship.folder))
        wait = subprocess.Popen(sandbox.command(jail, [str(QD), 'inbox', '--wait']), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            out, err = wait.communicate(timeout=60)
        finally:
            if wait.poll() is None:
                wait.kill()
        assert wait.returncode == 0, err
        assert 'Synthetic: for the jailed check seat.' in out
        wait_for(lambda: [a.result for a in post.Attempts(ship.runtime).for_message(message.id)] == ['watch'], seconds=60)
    finally:
        lease.close()
