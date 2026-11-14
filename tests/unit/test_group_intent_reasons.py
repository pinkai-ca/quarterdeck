"""Each way a staged group intent is held names its own cause, never its contents, on the status line too."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quarterdeck import group_post, post, ship as shipmod

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def forum(tmp_path):
    (tmp_path / 'ship.toml').write_text((ROOT / 'ships/forum/ship.toml').read_text())
    return shipmod.load(tmp_path)


def stage(forum, seat, root_id, data=None, *, raw=None):
    folder = post.drop_dir(forum.runtime, seat)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{root_id}.group.json'
    path.write_text(raw if raw is not None else json.dumps(data or {'format': 1, 'group': 'reviewers', 'root': root_id}))
    return path


def held(forum, seat):
    store = post.Store(forum.runtime)
    return [reason for folder, reason in post.file_drops(store, forum) if folder == f'drop/{seat}']


def test_every_intent_reason_is_one_the_status_line_names():
    assert set(group_post.INTENT_REASONS) <= set(post.DROP_HOLD_REASONS)
    assert len(set(group_post.INTENT_REASONS)) == len(group_post.INTENT_REASONS)
    assert 'not a message' not in group_post.INTENT_REASONS


def test_unreadable_and_malformed_intents_are_named_apart(forum):
    root = 'ord-20261114-1300-operator-aaaa'
    stage(forum, 'operator', root, raw='{not json')
    assert held(forum, 'operator') == [group_post.INTENT_UNREADABLE]
    stage(forum, 'operator', root, {'format': 2, 'group': 'reviewers', 'root': root})
    assert held(forum, 'operator') == [group_post.INTENT_MALFORMED]


def test_an_intent_naming_another_root_or_an_undeclared_group(forum):
    root = 'ord-20261114-1300-operator-bbbb'
    stage(forum, 'operator', root, {'format': 1, 'group': 'reviewers', 'root': 'ord-20261114-1300-operator-cccc'})
    assert held(forum, 'operator') == [group_post.INTENT_OTHER_ROOT]
    stage(forum, 'operator', root, {'format': 1, 'group': 'nobody', 'root': root})
    assert held(forum, 'operator') == [group_post.NO_GROUP]


def test_a_root_that_is_nowhere_or_not_the_leads_order(forum):
    store = post.Store(forum.runtime)
    stage(forum, 'operator', 'ord-20261114-1300-operator-dddd')
    assert held(forum, 'operator') == [group_post.ROOT_MISSING]
    other = store.new('order', 'operator', 'reviewer-1', 'not to the lead')
    stage(forum, 'operator', other.id)
    assert group_post.ROOT_NOT_LEADS_ORDER in held(forum, 'operator')


def test_a_forward_of_a_closed_order_is_named(forum):
    store = post.Store(forum.runtime)
    root = store.new('order', 'operator', 'lead', 'closed first')
    store.new('report', 'lead', 'operator', 'done', re=root.id)
    path = stage(forum, 'lead', root.id)
    assert held(forum, 'lead') == [group_post.FORWARD_NOT_OPEN_STORED]
    assert path.is_file()
    assert any('drop/lead: forward of an order not open in the store' in line for line in post.drop_alarms(forum.runtime))


def test_a_publication_refused_by_the_store_is_named_and_not_a_message(forum, monkeypatch):
    store = post.Store(forum.runtime)
    root = store.new('order', 'operator', 'lead', 'forward me')
    stage(forum, 'lead', root.id)

    def refuse(*args, **kwargs):
        raise shipmod.ShipError('synthetic refusal')

    monkeypatch.setattr(group_post, '_fanout', refuse)
    assert held(forum, 'lead') == [group_post.PUBLICATION_REFUSED]

    def broken(*args, **kwargs):
        raise OSError('synthetic I/O')

    monkeypatch.setattr(group_post, '_fanout', broken)
    assert held(forum, 'lead') == [group_post.PUBLICATION_IO]
