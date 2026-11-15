"""Reply lookup and allocation share a reading; publication and the repeat check do not."""

from pathlib import Path

import pytest

from quarterdeck import cli, durable, post, ship as shipmod
from .test_post_repeat import operator, qd, drops  # noqa: F401


def observe(monkeypatch):
    readings, writes = [], []
    load = post.Store._load_current
    write = post.write_drop
    def read(store):
        readings.append(post._READING.get() is not None)
        return load(store)
    def publish(*args, **kwargs):
        assert post._READING.get() is None
        writes.append(True)
        return write(*args, **kwargs)
    monkeypatch.setattr(post.Store, '_load_current', read)
    monkeypatch.setattr(post, 'write_drop', publish)
    return readings, writes


@pytest.mark.parametrize('kind', ['order', 'question', 'report', 'answer', 'ack'])
def test_reply_lookup_and_id_allocation_share_one_reading(drydock, capsys, operator, monkeypatch, kind):
    ship = shipmod.load(drydock)
    root = post.Store(ship.runtime).new('order', 'captain', ship.operator.lower(), 'Synthetic work')
    readings, writes = observe(monkeypatch)
    _, output = qd(capsys, drydock, kind, '--re', root.short, 'Synthetic reply')
    assert output.startswith('Posted ')
    # Repeatable reply kinds still need their later fresh store check, after reading drops.
    assert readings == ([True, False] if kind in post.REPEATS_REFUSED else [True])
    assert writes == [True]
    (path,) = drops(drydock, ship.operator.lower())
    message = post.Message.from_json(path.read_text())
    assert message.re == root.id and message.to == 'captain'


def test_post_without_reply_reads_the_store_once(drydock, capsys, operator, monkeypatch):
    readings, writes = observe(monkeypatch)
    qd(capsys, drydock, 'order', '--to', 'captain', 'Synthetic work')
    assert readings == [True] and writes == [True]


def test_repeat_guard_sees_a_drop_filed_after_the_early_reply_read(drydock, capsys, operator, monkeypatch):
    ship = shipmod.load(drydock)
    store = post.Store(ship.runtime)
    root = store.new('order', 'captain', ship.operator.lower(), 'Synthetic work')
    prior = store.prepare('report', ship.operator.lower(), 'captain', 'Synthetic result', re=root.id)
    post.write_drop(ship.runtime, prior)
    own_drops = post._own_drops
    filed = []
    def handoff(runtime, seat):
        assert post._READING.get() is None, 'the repeat guard cannot use the early planning snapshot'
        if not filed:
            filed.append(True)
            post.file_drops(post.Store(runtime))  # filed after lookup, absent from the guard's drop read
        return own_drops(runtime, seat)
    monkeypatch.setattr(post, '_own_drops', handoff)
    _, output = qd(capsys, drydock, 'report', '--re', root.short, 'Synthetic result')
    assert output.startswith(f'Already posted as {prior.short};')
    assert filed == [True] and drops(drydock, ship.operator.lower()) == []
    assert [m.id for m in post.Store(ship.runtime).replies(root)] == [prior.id]


@pytest.mark.parametrize('forward', [False, True])
def test_group_planning_has_one_reading_and_all_writes_follow_it(tmp_path, capsys, operator, monkeypatch, forward):
    template = Path(__file__).parents[2] / 'ships/forum/ship.toml'
    (tmp_path / 'ship.toml').write_text(template.read_text())
    ship = shipmod.load(tmp_path)
    body = 'Synthetic group work'
    argv = []
    if forward:
        root = post.Store(ship.runtime).new('order', 'issuer', 'lead', body)
        argv = ['--re', root.short]
    readings, writes = observe(monkeypatch)
    replace = durable.replace_file
    def publish(*args, **kwargs):
        assert post._READING.get() is None, 'a group intent is an action too'
        return replace(*args, **kwargs)
    monkeypatch.setattr(durable, 'replace_file', publish)
    qd(capsys, tmp_path, 'order', '--from', 'lead', '--to', 'reviewers', *argv, body)
    assert readings == [True]
    assert writes == ([] if forward else [True])
    assert len(list(post.drop_dir(ship.runtime, 'lead').glob('*.group.json'))) == 1
