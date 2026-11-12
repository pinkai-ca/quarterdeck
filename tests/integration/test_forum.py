"""A five-seat forum on the fake program and a private tmux server, never a provider."""

from pathlib import Path
import shutil

import pytest

from quarterdeck import cli, post, seat as seatmod, service, ship as shipmod

from .waiting import wait_for


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def forum(tmp_path, tmux, monkeypatch):
    folder = tmp_path / 'forum'
    folder.mkdir()
    shutil.copytree(ROOT / 'ships/forum/roles', folder / 'roles')
    shutil.copy(ROOT / 'ships/forum/orders.md', folder / 'orders.md')
    names = ['lead', 'reviewer-1', 'reviewer-2', 'reviewer-3', 'reviewer-4']
    text = ('[ship]\nname="forum"\noperator="operator"\nworkspace="."\n'
            '[pools.fake]\nprovider="synthetic"\n'
            '[sessions.forum]\ntitle="Synthetic forum"\n'
            '[groups.reviewers]\nlead="lead"\n'
            'members=["reviewer-1","reviewer-2","reviewer-3","reviewer-4"]\n')
    for name in names:
        role = 'lead' if name == 'lead' else 'reviewer'
        shape = 'codex' if name == 'reviewer-2' else 'claude'
        text += (f'\n[seats.{name}]\nsession="forum"\nrole="{role}"\nharness="fake"\n'
                 f'pool="fake"\nmodel="fake-1"\nsettings={{as="{shape}", step_seconds="0.05"}}\n')
    (folder / 'ship.toml').write_text(text)
    monkeypatch.setenv('QD_TMUX_SOCKET', tmux.socket)
    monkeypatch.setenv('QD_SHIP', str(folder))
    monkeypatch.delenv('QD_SEAT', raising=False)
    tmux.new_session('Synthetic bootstrap')
    tmux.kill_session('Synthetic bootstrap')
    ship = shipmod.load(folder)
    yield ship
    service.stop(ship.runtime)
    for spec in ship.seats.values():
        seatmod.Seat(ship, spec, tmux).down()


def qd(capsys, *args):
    code = cli.main(list(args))
    output = capsys.readouterr()
    assert code == 0, output.err
    return output.out


def test_fake_forum_files_four_identical_reviews_and_one_notice(forum, tmux, capsys):
    # All five receive the same brief; the fake lead does not synthesize a final
    # report. The test later files that report explicitly, as the real card does.
    body = ('Review this fictional change. Preserve dissent.\n'
            '$ if [ "$QD_SEAT" != lead ]; then '
            'qd post report --re {this} "review:$QD_SEAT; dissent preserved"; fi')
    qd(capsys, 'post', 'order', '--to', 'reviewers', body)
    store = post.Store(forum.runtime)
    root, = store.open_for('lead')
    copies = [m for m in store.all() if m.re == root.id and m.kind == 'order']
    assert len(copies) == 4 and all(m.body.encode() == root.body.encode() for m in copies)
    qd(capsys, 'up')
    wait_for(lambda: all(store.closing_report(m) for m in copies), seconds=30)
    notices = wait_for(lambda: [m for m in store.for_seat('lead') if m.sender == post.SHIP
                                and m.re == root.id and 'all filed' in m.body], seconds=30)
    assert len(notices) == 1 and store.is_open(root)
    reports = [store.closing_report(m) for m in copies]
    assert all(r.id in notices[0].body and 'dissent preserved' in r.body for r in reports)
    for copy in copies:
        reviewer = seatmod.Seat(forum, forum.seat(copy.to), tmux)
        wait_for(lambda: reviewer.record() and reviewer.harness.turns(reviewer.record()), seconds=15)
        inputs = reviewer.harness.inputs(reviewer.record())
        assert any(copy.id in item.text for item in inputs)
        inbox = post.inbox_reading(store, reviewer.name)
        assert all(message.to == reviewer.name for message in inbox['open'] + inbox['received'])
    qd(capsys, 'post', 'report', '--from', 'lead', '--re', root.id,
       'Combined only the four filed reports; dissent preserved.')
    assert not store.is_open(root)
    # A new service instance recovers its journals without a second notice.
    service.stop(forum.runtime)
    reread = service.Service(forum, tmux)
    try:
        reread.groups()
        assert [m for m in store.for_seat('lead') if m.sender == post.SHIP and m.re == root.id] == notices
    finally:
        reread.ledger.close()


def test_fake_forum_names_an_absent_saved_member_once(forum, tmux, capsys):
    qd(capsys, 'up')
    member = seatmod.Seat(forum, forum.seat('reviewer-4'), tmux)
    pane = member.pane()
    tmux.kill_window(pane.id, expected_pid=pane.pid)
    qd(capsys, 'post', 'order', '--to', 'reviewers', 'Review this fictional proposal independently.')
    store = post.Store(forum.runtime)
    root, = store.open_for('lead')
    notices = wait_for(lambda: [m for m in store.for_seat('lead') if m.sender == post.SHIP
                                and m.re == root.id and 'member failed' in m.body], seconds=30)
    assert len(notices) == 1
    assert 'reviewer-4: saved launch pane is absent' in notices[0].body
    assert store.is_open(root)
    service.stop(forum.runtime)
    reread = service.Service(forum, tmux)
    try:
        reread.groups()
        assert [m for m in store.for_seat('lead') if m.sender == post.SHIP and m.re == root.id] == notices
    finally:
        reread.ledger.close()
