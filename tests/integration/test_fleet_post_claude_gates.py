"""Claude gates for the post across ships with real services: two synthetic ships, each on its own private tmux server,
and a real bubblewrap jail. Each ship is a copy of the drydock template, so each has a captain, a desk in the person's
own session, and a worker, under the same bare names on both ships.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from quarterdeck import cli, fleet_post as fleet, human_wake, post, sandbox, seat as seatmod, service, ship as shipmod
from tests.conftest import ROOT, TEST_TICK

from .progress import service_progress
from .waiting import wait_for



def clean_path() -> str:
    """A clean PATH for a planned jail: this checkout's interpreter and the system's, never a running ship's
    runtime that the test's own shell may have first on its PATH (a seat's shell does)."""
    return os.pathsep.join([str(Path(sys.executable).parent), '/usr/bin', '/bin'])


def make_ship(project: Path, name: str) -> shipmod.Ship:
    folder = project / 'ships' / name
    shutil.copytree(ROOT / 'ships' / 'drydock', folder)
    file = folder / 'ship.toml'
    file.write_text(shipmod.edit(file.read_text(), 'ship', 'tick', TEST_TICK))
    return shipmod.load(folder)


@pytest.fixture
def two(tmp_path):
    return make_ship(tmp_path, 'alpha'), make_ship(tmp_path, 'beta')


@pytest.fixture
def fleet_cli(two, tmux, monkeypatch, capsys):
    left, right = two
    peer = tmux.private_peer('-other-ship')
    servers = {left.folder: tmux, right.folder: peer}
    monkeypatch.delenv('QD_SHIP', raising=False)
    monkeypatch.delenv('QD_SEAT', raising=False)

    def qd(ship, *args, seat=None):
        monkeypatch.setenv('QD_TMUX_SOCKET', servers[ship.folder].socket)
        if seat:
            monkeypatch.setenv('QD_SEAT', seat)
        try:
            code = cli.main(['--ship', str(ship.folder), *args])
        finally:
            monkeypatch.delenv('QD_SEAT', raising=False)
        output = capsys.readouterr()
        assert code == 0, output.err
        return output.out

    try:
        for server in servers.values():
            server.new_session('Synthetic bootstrap')
            server.kill_session('Synthetic bootstrap')
        yield qd, servers
    finally:
        for ship in (right, left):
            if service.alive(ship.runtime):
                qd(ship, 'down')
        assert peer.close(), peer.held


def inputs(seat):
    record = seat.record()
    return [entry.text.strip() for entry in seat.harness.inputs(record)] if record else []


# -- the person's seats: only the watch or the one waking line; the crew: the door's rules unchanged ----------------

def test_an_imported_message_reaches_the_persons_desk_only_by_watch_or_the_waking_line(two, fleet_cli):
    left, right = two
    qd, servers = fleet_cli
    qd(left, 'up', 'captain')
    qd(right, 'up', 'captain')
    qd(right, 'up', 'desk')
    desk = seatmod.Seat(right, right.seat('desk'), servers[right.folder])
    captain = seatmod.Seat(right, right.seat('captain'), servers[right.folder])
    qd(left, 'post', 'question', '--from', 'worker-1', '--to', 'desk@beta', 'Synthetic question for the desk.')
    qd(left, 'post', 'order', '--from', 'worker-1', '--to', 'captain@beta', 'Synthetic order for the captain.')
    source, destination = post.Store(left.runtime), post.Store(right.runtime)
    wait_for(lambda: len(source.all()) == 2, seconds=30)  # the running service files each drop on its tick
    question, order = sorted(source.all(), key=lambda m: m.kind == 'order')
    wait_for(lambda: all((destination.dir / f'{m.id}.json').exists() for m in (question, order)), seconds=30)

    # The crew seat: the door types it once and confirms it by the record.
    wait_for(lambda: [a.result for a in post.Attempts(right.runtime).for_message(order.id)] == ['delivered'], seconds=30)
    envelope = post.typed([destination.get(order.id)]).strip()
    assert inputs(captain).count(envelope) == 1

    # The desk: never typed into, but for the one fixed waking line, at most once.
    service_progress(right)
    service_progress(right)
    typed = inputs(desk)
    assert set(typed) <= {human_wake.LINE} and typed.count(human_wake.LINE) <= 1, typed  # law: one-door
    assert post.typed([destination.get(question.id)]).strip() not in typed
    assert not [a for a in post.Attempts(right.runtime).for_message(question.id) if a.result == 'delivered'], \
        "no door delivery into the person's session"
    printed = qd(right, 'inbox', '--wait', 'desk')
    assert 'Synthetic question for the desk.' in printed, 'the watch delivers it'
    assert set(inputs(desk)) <= {human_wake.LINE}


def test_an_imported_message_whose_delivery_cannot_be_confirmed_is_held_and_never_typed_again(two, fleet_cli):
    left, right = two
    qd, servers = fleet_cli
    qd(right, 'up', 'captain')
    service.stop(right.runtime)
    wait_for(lambda: not service.alive(right.runtime))
    source, destination = post.Store(left.runtime), post.Store(right.runtime)
    order = source.new('order', 'worker-1', 'captain@beta', 'Synthetic order the door never confirmed.')
    fleet.run(left, source)
    fleet.run(right, destination)
    received = destination.get(order.id)
    captain = seatmod.Seat(right, right.seat('captain'), servers[right.folder])
    launched = captain.launched()
    attempts = post.Attempts(right.runtime)
    attempts.open(received, 'captain', launched.pane, launched.pid, launched.at)  # the door's attempt, cut before typing

    assert 'service started' in qd(right, 'up').splitlines()[-1]
    wait_for(lambda: [a.result for a in attempts.for_message(received.id)] == ['blind'], seconds=30)
    for _ in range(3):  # the offer stays on alpha: repeated passes on both ships make no new delivery
        fleet.run(left, source)
        service_progress(right)
    assert [a.result for a in post.Attempts(right.runtime).for_message(received.id)] == ['blind'], \
        'an uncertain delivery is held, never retyped'  # law: one-door
    assert post.typed([received]).strip() not in inputs(captain)


# -- a jailed seat posts to another ship from inside, and reads nothing of it beyond its declaration ----------------

def namespaces_allowed() -> bool:
    if not sandbox.available():
        return False
    try:
        return subprocess.run(['bwrap', '--ro-bind', '/', '/', 'true'], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_a_jailed_seat_posts_to_another_ship_and_reads_nothing_of_it_beyond_its_declaration(tmp_path):
    home = tmp_path / 'operator-home'
    project = home / 'project'
    left, right = make_ship(project, 'alpha'), make_ship(project, 'beta')
    for ship in (left, right):
        shipmod.fence(ship)  # as at the ship's first qd up, before any seat of it launches
    secret = 'Synthetic words in the other ship.'
    post.Store(right.runtime).new('order', 'captain', 'worker-1', secret)
    workspace = home / 'work'
    workspace.mkdir()
    own = left.runtime / 'seats' / 'worker-1'
    (own / 'home').mkdir(parents=True)
    # A clean PATH: this checkout's interpreter and the system's, never a running ship's runtime from the test's shell.
    path = os.pathsep.join([str(Path(sys.executable).parent), '/usr/bin', '/bin'])
    jail = sandbox.plan(left, 'worker-1', own, own / 'home', workspace, sys.executable, path, home=home)
    probe = '''
import json, sys
from pathlib import Path
from quarterdeck import cli
beta = Path(sys.argv[2])
seen = dict(declaration=(beta / 'ship.toml').is_file(),
            store=sorted(p.name for p in (beta / 'runtime' / 'post' / 'store').glob('*')) if (beta / 'runtime').exists() else [])
code = cli.main(['--ship', sys.argv[1], 'post', 'order', '--from', 'worker-1', '--to', 'captain@beta', 'From the jail.'])
print(json.dumps(dict(seen, code=code)))
'''
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    done = subprocess.run(sandbox.command(jail, [sys.executable, '-c', probe, str(left.folder), str(right.folder)]),
                          capture_output=True, text=True, env=env, timeout=60)
    reading = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else {}
    assert reading.get('code') == 0, (done.stdout, done.stderr)
    assert reading['store'] == [], "nothing of the other ship's runtime is readable from the jail"
    assert post.file_drops(post.Store(left.runtime)) == [], "the jail's drop files as its own seat's"
    sent, = [m for m in post.Store(left.runtime).all() if m.body == 'From the jail.']
    assert sent.to == 'captain@beta' and sent.sender == 'worker-1'
    assert secret not in done.stdout + done.stderr


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_a_ship_raised_after_the_jail_launched_is_absent_inside_it(tmp_path):
    """The fleet folders are fenced whole: a ship raised later shows inside as nothing, not read-only."""
    import fcntl
    import time

    home = tmp_path / 'operator-home'
    project = home / 'project'
    left, right = make_ship(project, 'alpha'), make_ship(project, 'beta')
    for ship in (left, right):
        shipmod.fence(ship)
    workspace = home / 'work'
    workspace.mkdir()
    own = left.runtime / 'seats' / 'worker-1'
    (own / 'home').mkdir(parents=True)
    jail = sandbox.plan(left, 'worker-1', own, own / 'home', workspace, sys.executable, clean_path(), home=home)
    probe = '''
import fcntl, json, os, sys, time
from pathlib import Path
from quarterdeck import cli
alpha, beta, gamma, work = map(Path, sys.argv[1:5])
deadline = time.monotonic() + 30
while not (work / 'raised').exists() and time.monotonic() < deadline:
    time.sleep(.02)
def lock(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return 'taken'
    except OSError as error:
        return type(error).__name__
def read(path):
    try:
        return path.read_text()
    except OSError as error:
        return type(error).__name__
seen = dict(
    raised=(work / 'raised').exists(),
    gamma=gamma.exists(), gamma_declaration=read(gamma / 'ship.toml'),
    gamma_lock=lock(gamma / 'runtime' / 'locks' / 'credential-reseed.lock'),
    gamma_home=read(gamma / 'runtime' / 'seats' / 's1' / 'home' / '.credentials.json'),
    ships=sorted(p.name for p in beta.parent.iterdir()),
    beta_declaration=(beta / 'ship.toml').is_file(), beta_runtime=(beta / 'runtime').exists(),
    own_declaration=(alpha / 'ship.toml').is_file(), own_runtime=(alpha / 'runtime' / 'post').is_dir())
(work / 'checked').write_text('')
while not (work / 'done').exists() and time.monotonic() < deadline:
    time.sleep(.02)
code = cli.main(['--ship', str(alpha), 'post', 'order', '--from', 'worker-1', '--to', 'captain@beta', 'From the fenced jail.'])
print(json.dumps(dict(seen, code=code)))
'''
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    gamma = project / 'ships' / 'gamma'
    child = subprocess.Popen(sandbox.command(jail, [sys.executable, '-c', probe, str(left.folder), str(right.folder),
                                                    str(gamma), str(workspace)]),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        time.sleep(.4)  # the jail has launched; the late ship is raised outside it now
        late = make_ship(project, 'gamma')
        shipmod.fence(late)
        locks = late.runtime / 'locks'
        locks.mkdir(parents=True, exist_ok=True)
        (locks / 'credential-reseed.lock').touch()
        secret = 'synthetic-late-ship-sign-in'
        credentials = late.runtime / 'seats' / 's1' / 'home' / '.credentials.json'
        credentials.parent.mkdir(parents=True)
        credentials.write_text(secret)
        (workspace / 'raised').write_text('')
        deadline = time.monotonic() + 30
        while not (workspace / 'checked').exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        # While the jail still runs, the late ship's reseed lock is free outside.
        fd = os.open(locks / 'credential-reseed.lock', os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            held = False
        except BlockingIOError:
            held = True
        finally:
            os.close(fd)
        (workspace / 'done').write_text('')
        out, err = child.communicate(timeout=60)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    reading = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    assert reading.get('code') == 0, (out, err)
    assert reading['raised'], 'the late ship was raised while the jail ran'
    assert not held, "the jail holds the late ship's reseed lock"
    assert reading['gamma'] is False, 'a ship raised after the launch is no folder inside'
    assert reading['gamma_declaration'] == reading['gamma_home'] == reading['gamma_lock'] == 'FileNotFoundError'
    assert reading['ships'] == ['alpha', 'beta'], 'only the ship itself and the peers found at launch'
    assert reading['beta_declaration'] and not reading['beta_runtime']
    assert reading['own_declaration'] and reading['own_runtime']
    assert secret not in out + err
    assert post.file_drops(post.Store(left.runtime)) == [], "the jail's drop files as its own seat's"
    sent, = [m for m in post.Store(left.runtime).all() if m.body == 'From the fenced jail.']
    assert sent.to == 'captain@beta' and sent.sender == 'worker-1'


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_an_office_raised_after_the_launch_under_a_project_seen_read_only_inside_is_absent(tmp_path):
    """A fleet folder missing at launch, under a project the jail binds back read-only, is made and fenced.

    This ship's own layout: the project is the framework's checkout under the home, bound back read-only
    because qd runs from it, and a hand works in a folder of its own. Without the made folder, bwrap could
    make no mount point there, and an office raised later showed inside through the project's bind.
    """
    import time

    home = tmp_path / 'operator-home'
    project = home / 'project'
    alpha = make_ship(project, 'alpha')
    shipmod.fence(alpha)
    workspace = home / 'work'
    workspace.mkdir()
    own = alpha.runtime / 'seats' / 'hand-1'
    (own / 'home').mkdir(parents=True)
    office = project / 'office'
    assert not office.exists()
    path = os.pathsep.join([clean_path(), str(project)])  # the project is bound back, as the checkout is
    jail = sandbox.plan(alpha, 'hand-1', own, own / 'home', workspace, sys.executable, path, home=home)
    assert project in jail.read_only, 'the project is seen read-only inside, as the framework checkout is'
    assert office.is_dir() and not any(office.iterdir()), 'plan made the missing office folder, empty'
    probe = '''
import json, sys, time
from pathlib import Path
project, work = map(Path, sys.argv[1:3])
deadline = time.monotonic() + 30
while not (work / 'raised').exists() and time.monotonic() < deadline:
    time.sleep(.02)
def read(path):
    try:
        return path.read_text()
    except OSError as error:
        return type(error).__name__
office = project / 'office'
print(json.dumps(dict(
    raised=(work / 'raised').exists(), project=sorted(p.name for p in project.iterdir()),
    office=sorted(p.name for p in office.iterdir()) if office.is_dir() else None,
    declaration=read(office / 'ship.toml'),
    home=read(office / 'runtime' / 'seats' / 'desk' / 'home' / '.credentials.json'))))
'''
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    child = subprocess.Popen(sandbox.command(jail, [sys.executable, '-c', probe, str(project), str(workspace)]),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        time.sleep(.4)  # the jail has launched; the office is raised outside it now
        office.mkdir(exist_ok=True)
        (office / 'ship.toml').write_text('[ship]\nname = "office"\n')
        secret = 'synthetic-late-office-sign-in'
        credentials = office / 'runtime' / 'seats' / 'desk' / 'home' / '.credentials.json'
        credentials.parent.mkdir(parents=True)
        credentials.write_text(secret)
        (workspace / 'raised').write_text('')
        out, err = child.communicate(timeout=60)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    reading = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    assert reading.get('raised'), (out, err)
    assert 'office' in reading['project'] and 'ships' in reading['project'], 'the project itself is seen inside'
    assert reading['office'] == [], 'the office raised after the launch is an empty folder inside'
    assert reading['declaration'] == reading['home'] == 'FileNotFoundError'
    assert secret not in out + err


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_the_forum_reviewer_writes_its_work_folder_and_nothing_else_of_its_ship(tmp_path):
    """The forum's jailed reviewer works in the forum's own work folder: writable inside, the ship's declaration and the
    rest of the fleet read-only or absent. Before, the fleet's fence covered that folder read-only."""
    home = tmp_path / 'operator-home'
    project = home / 'project'
    forum_folder = project / 'ships' / 'forum'
    shutil.copytree(ROOT / 'ships' / 'forum', forum_folder)
    forum = shipmod.load(forum_folder)
    shipmod.fence(forum)
    beta = make_ship(project, 'beta')
    seat = forum.seat('reviewer-4')
    work = forum.cwd_of(seat)
    assert work == forum_folder / 'work'
    own = forum.runtime / 'seats' / seat.name
    (own / 'home').mkdir(parents=True)
    jail = sandbox.plan(forum, seat.name, own, own / 'home', work, sys.executable, clean_path(), home=home)
    probe = '''
import json, sys
from pathlib import Path
forum, work, beta = map(Path, sys.argv[1:4])
def write(path):
    try:
        path.write_text('written inside')
        return 'written'
    except OSError as error:
        return type(error).__name__
print(json.dumps(dict(work=write(work / 'review.md'), declaration=write(forum / 'ship.toml'),
                      beside=write(forum / 'planted.txt'), beta=(beta / 'runtime').exists(),
                      beta_declaration=(beta / 'ship.toml').is_file())))
'''
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    result = subprocess.run(sandbox.command(jail, [sys.executable, '-c', probe, str(forum_folder), str(work), str(beta.folder)]),
                            capture_output=True, text=True, timeout=60, env=env)
    reading = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
    assert reading.get('work') == 'written', (result.stdout, result.stderr)
    assert (work / 'review.md').read_text() == 'written inside'
    assert reading['declaration'] in ('OSError', 'PermissionError') and reading['beside'] in ('OSError', 'PermissionError'), reading
    assert not (forum_folder / 'planted.txt').exists() and 'written inside' not in (forum_folder / 'ship.toml').read_text()
    assert not reading['beta'], 'another ship\'s runtime stays absent'  # a template-named ship binds no peer declaration
