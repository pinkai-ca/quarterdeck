"""Real service processes fetch between two synthetic crews on private servers."""

from quarterdeck import cli, fleet_post, post, seat as seatmod, service
from tests.unit.test_fleet_post import pair

from .progress import service_progress
from .waiting import wait_for


def test_two_services_fetch_and_deliver_one_original_then_return_its_report(pair, tmux, monkeypatch, capsys):
    left, right, source, destination = pair
    peer = tmux.private_peer('-other-ship')
    servers = [(left, tmux), (right, peer)]
    monkeypatch.delenv('QD_SHIP', raising=False)
    monkeypatch.delenv('QD_SEAT', raising=False)

    def qd(ship, server, *args):
        monkeypatch.setenv('QD_TMUX_SOCKET', server.socket)
        code = cli.main(['--ship', str(ship.folder), *args])
        output = capsys.readouterr()
        assert code == 0, output.err
        return output.out

    try:
        for ship, server in servers:
            server.new_session('Synthetic bootstrap')
            server.kill_session('Synthetic bootstrap')
        qd(left, tmux, 'up', 'captain')
        qd(left, tmux, 'post', 'order', '--from', 'writer', '--to', 'captain@beta',
           'Count once.\n$ qd post report --re {this} counted')
        order, = source.all()
        wait_for(lambda: (left.runtime / 'post' / 'outbound' / 'beta' / (order.id + '.json')).exists())
        assert fleet_post.waiting(left, source) and destination.all() == [], 'a down ship has not fetched'
        qd(right, peer, 'up', 'captain')
        wait_for(lambda: source.closing_report(source.get(order.id)), seconds=60)
        report = source.closing_report(source.get(order.id))
        assert report.sender == 'captain@beta' and report.body == 'counted'
        assert report.to == 'writer'
        assert (source.dir / (order.id + '.json')).read_bytes() == (destination.dir / (order.id + '.json')).read_bytes()
        assert (source.dir / (report.id + '.json')).read_bytes() == (destination.dir / (report.id + '.json')).read_bytes()
        service_progress(right)
        attempts = post.Attempts(right.runtime).for_message(order.id)
        assert len(attempts) == 1 and attempts[0].result == 'delivered'
        captain = seatmod.Seat(right, right.seat('captain'), peer)
        record = captain.record()
        inputs = captain.harness.inputs(record)
        envelope = post.typed([destination.get(order.id)]).strip()
        assert sum(entry.text.strip() == envelope for entry in inputs) == 1
        assert fleet_post.waiting(left, source) == []
        monkeypatch.setenv('QD_SEAT', 'writer')
        thread = qd(left, tmux, 'thread', report.id)
        assert post.typed([report], whole=True) in thread
        monkeypatch.delenv('QD_SEAT')
    finally:
        for ship, server in reversed(servers):
            if service.alive(ship.runtime):
                qd(ship, server, 'down')
        assert peer.close(), peer.held
