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
    jail = sandbox.plan(left, 'worker-1', own, own / 'home', workspace, sys.executable, os.environ['PATH'], home=home)
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
