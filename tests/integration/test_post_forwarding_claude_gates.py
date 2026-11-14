"""Claude gate for forwarding an address in the person's own session, with real services: two synthetic ships, each a
copy of the drydock template on its own private tmux server, the old desk forwarded to the other ship's desk.
"""
from __future__ import annotations

import shutil

import pytest

from quarterdeck import cli, human_wake, post, seat as seatmod, service, ship as shipmod
from tests.conftest import ROOT, TEST_TICK

from .progress import service_progress
from .waiting import wait_for


def make_ship(project, name, forward=None):
    folder = project / 'ships' / name
    shutil.copytree(ROOT / 'ships' / 'drydock', folder)
    file = folder / 'ship.toml'
    text = shipmod.edit(file.read_text(), 'ship', 'tick', TEST_TICK)
    file.write_text(text + (f'\n[forward]\ndesk = "{forward}"\n' if forward else ''))
    return shipmod.load(folder)


def inputs(seat):
    record = seat.record()
    return [entry.text.strip() for entry in seat.harness.inputs(record)] if record else []


def test_a_forwarded_desk_is_never_typed_into_on_either_ship_and_only_the_new_desks_watch_owns_it(tmp_path, tmux, monkeypatch, capsys):
    left = make_ship(tmp_path, 'alpha', forward='desk@beta')
    right = make_ship(tmp_path, 'beta')
    peer = tmux.private_peer('-other-ship')
    servers = {left.folder: tmux, right.folder: peer}
    monkeypatch.delenv('QD_SHIP', raising=False)
    monkeypatch.delenv('QD_SEAT', raising=False)

    def qd(ship, *args):
        monkeypatch.setenv('QD_TMUX_SOCKET', servers[ship.folder].socket)
        code = cli.main(['--ship', str(ship.folder), *args])
        output = capsys.readouterr()
        assert code == 0, output.err
        return output.out

    try:
        for server in servers.values():
            server.new_session('Synthetic bootstrap')
            server.kill_session('Synthetic bootstrap')
        qd(left, 'up', 'desk')
        qd(right, 'up', 'desk')
        old = seatmod.Seat(left, left.seat('desk'), tmux)
        new = seatmod.Seat(right, right.seat('desk'), peer)
        qd(left, 'post', 'question', '--from', 'worker-1', '--to', 'desk', 'Synthetic question to the old desk.')
        source, destination = post.Store(left.runtime), post.Store(right.runtime)
        question, = source.all()
        wait_for(lambda: (destination.dir / f'{question.id}.json').exists(), seconds=30)
        service_progress(left)
        service_progress(right)

        # The old desk: no line of any kind typed, no watch delivery, nothing waiting in its inbox.
        assert inputs(old) == [], inputs(old)
        assert post.Attempts(left.runtime).for_message(question.id) == []
        assert 'Synthetic question to the old desk.' not in post.inbox_text(source, 'desk')
        # The new desk: never typed but for the one waking line; its watch delivers it.
        typed = inputs(new)
        assert set(typed) <= {human_wake.LINE} and typed.count(human_wake.LINE) <= 1, typed  # law: one-door
        assert not [a for a in post.Attempts(right.runtime).for_message(question.id) if a.result == 'delivered']
        printed = qd(right, 'inbox', '--wait', 'desk')
        assert 'Synthetic question to the old desk.' in printed
    finally:
        for ship in (right, left):
            if service.alive(ship.runtime):
                qd(ship, 'down')
        assert peer.close(), peer.held
