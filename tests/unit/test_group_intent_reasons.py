"""Each way a staged group intent is held names its own cause, never its contents, on the status line too."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from quarterdeck import group_post, post, post_dir, ship as shipmod

ROOT = Path(__file__).resolve().parents[2]


def ident(suffix: str) -> str:
    """A synthetic post id, built from its parts so that no id literal is tracked (law: nothing-leaks)."""
    return '-'.join(('ord', '20261114', '1300', 'operator', suffix))


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
    root = ident('aaaa')
    stage(forum, 'operator', root, raw='{not json')
    assert held(forum, 'operator') == [group_post.INTENT_UNREADABLE]
    stage(forum, 'operator', root, {'format': 2, 'group': 'reviewers', 'root': root})
    assert held(forum, 'operator') == [group_post.INTENT_MALFORMED]


def test_a_refused_intent_read_keeps_the_regular_file_reason(forum, monkeypatch):
    intent = stage(forum, 'operator', ident('aaaa'))
    read = post._read_regular
    monkeypatch.setattr(post, '_read_regular', lambda path, **kwargs: None if path == intent else read(path, **kwargs))
    assert held(forum, 'operator') == ['not a regular file']
    assert intent.is_file()


def test_an_intent_naming_another_root_or_an_undeclared_group(forum):
    root = ident('bbbb')
    stage(forum, 'operator', root, {'format': 1, 'group': 'reviewers', 'root': ident('cccc')})
    assert held(forum, 'operator') == [group_post.INTENT_OTHER_ROOT]
    stage(forum, 'operator', root, {'format': 1, 'group': 'nobody', 'root': root})
    assert held(forum, 'operator') == [group_post.NO_GROUP]


def test_a_root_that_is_nowhere_or_not_the_leads_order(forum):
    store = post.Store(forum.runtime)
    stage(forum, 'operator', ident('dddd'))
    assert held(forum, 'operator') == [group_post.ROOT_MISSING]
    other = store.new('order', 'operator', 'reviewer-1', 'not to the lead')
    stage(forum, 'operator', other.id)
    assert group_post.ROOT_NOT_LEADS_ORDER in held(forum, 'operator')


@pytest.mark.parametrize('location', ['drop', 'store'])
@pytest.mark.parametrize(('outcome', 'reason'), [
    ('unreadable', group_post.ROOT_UNREADABLE),
    ('link', group_post.LINK_HELD),
    ('invalid-encoding', group_post.ROOT_MALFORMED),
    ('invalid-json', group_post.ROOT_MALFORMED),
    ('wrong-shape', group_post.ROOT_MALFORMED),
    ('wrong-field-type', group_post.ROOT_MALFORMED),
    ('wrong-id', group_post.ROOT_ID_MISMATCH),
])
def test_unusable_root_evidence_holds_the_intent(forum, monkeypatch, location, outcome, reason):
    root = post.Message(ident('aaaa'), 'order', 'operator', 'lead', post.now(), 'synthetic body')
    intent = stage(forum, 'operator', root.id)
    folder = intent.parent if location == 'drop' else post.Store(forum.runtime).dir
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{root.id}.json'
    content = {
        'invalid-encoding': b'\xff',
        'invalid-json': b'{not json',
        'wrong-shape': b'null',
        'wrong-field-type': replace(root, body=[]).to_json().encode(),
        'wrong-id': replace(root, id=ident('bbbb')).to_json().encode(),
    }.get(outcome, root.to_json().encode())
    path.write_bytes(content)
    read_text = post_dir.read_text
    if outcome in ('unreadable', 'link'):
        def read(candidate, **kwargs):
            if candidate == path:
                if outcome == 'link':
                    raise shipmod.ShipError(post_dir.LINK_HELD + 'synthetic path')
                raise PermissionError('synthetic read refusal')
            return read_text(candidate, **kwargs)

        monkeypatch.setattr(post_dir, 'read_text', read)

    assert held(forum, 'operator') == [reason]
    assert post.drop_alarms(forum.runtime) == [f'post drop held: drop/operator: {reason}']
    assert intent.is_file() and path.read_bytes() == content
    assert not list((forum.runtime / 'post' / 'groups').glob('*.json'))


def test_root_id_syntax_has_its_own_reason(forum):
    root = post.Message('synthetic-root', 'order', 'operator', 'lead', post.now(), 'synthetic body')
    intent = stage(forum, 'operator', root.id)
    intent.with_name(f'{root.id}.json').write_text(root.to_json())
    assert held(forum, 'operator') == [group_post.ROOT_ID_MALFORMED]


def test_a_different_root_with_an_already_stored_id_holds_as_a_conflict(forum):
    root = post.Store(forum.runtime).new('order', 'operator', 'lead', 'already filed')
    intent = stage(forum, 'operator', root.id)
    intent.with_name(f'{root.id}.json').write_text(replace(root, body='different body').to_json())
    assert held(forum, 'operator') == ['id already stored']
    assert intent.is_file()


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


def forwarded_with_journal(forum, text=None, *, link_to=None):
    """An open order to the lead, its forward staged, and a journal already saved for its root as given."""
    store = post.Store(forum.runtime)
    root = store.new('order', 'operator', 'lead', 'forward me')
    stage(forum, 'lead', root.id)
    journal = forum.runtime / 'post' / 'groups' / f'{root.id}.json'
    journal.parent.mkdir(parents=True, exist_ok=True)
    if link_to is not None:
        journal.symlink_to(link_to)
    else:
        journal.write_text(text)
    return root, journal


@pytest.mark.parametrize('content', [
    '{not json',
    json.dumps({'format': 2}),  # an unknown format: _load refuses it as a ShipError
    json.dumps({'format': 1, 'group': 'reviewers', 'lead': 'lead', 'root': {}, 'copies': [], 'notice': None}),
    json.dumps([]),
], ids=['not-json', 'unknown-format', 'inconsistent', 'not-an-object'])
def test_a_malformed_saved_journal_holds_as_malformed_never_as_a_refused_publication(forum, content):
    root, journal = forwarded_with_journal(forum, content)
    assert held(forum, 'lead') == [group_post.JOURNAL_MALFORMED]
    assert journal.read_text() == content, 'the journal is kept as found'


def test_an_inconsistent_saved_journal_holds_as_malformed(forum):
    store = post.Store(forum.runtime)
    root = store.new('order', 'operator', 'lead', 'forward me')
    other = store.new('order', 'operator', 'lead', 'another root')
    stage(forum, 'lead', root.id)
    journal = forum.runtime / 'post' / 'groups' / f'{root.id}.json'
    journal.parent.mkdir(parents=True, exist_ok=True)
    copy = json.loads(other.to_json()) | {'to': 'reviewer-1', 'sender': 'lead', 're': other.id}
    journal.write_text(json.dumps({'format': 1, 'group': 'reviewers', 'lead': 'lead', 'root': json.loads(other.to_json()),
                                   'copies': [copy], 'notice': None, 'failed': {}}))
    with pytest.raises(shipmod.ShipError, match='inconsistent group intent'):
        group_post._load(journal)  # its stem names another root: the ShipError the filing must not call a refusal
    assert held(forum, 'lead') == [group_post.JOURNAL_MALFORMED]


def test_a_link_at_the_saved_journal_holds_as_a_held_link(forum, tmp_path):
    target = tmp_path / 'elsewhere.json'
    target.write_text('{}')
    root, journal = forwarded_with_journal(forum, link_to=target)
    assert held(forum, 'lead') == [group_post.LINK_HELD]
    assert journal.is_symlink() and target.read_text() == '{}', 'the link is held in place, never followed'
