"""Reference lookup shares each ordinary message listing."""
from datetime import datetime, timezone

import pytest

from quarterdeck import post
from quarterdeck.ship import ShipError


def item(identifier):
    return post.Message(identifier, 'order', 'issuer', 'recipient',
                        datetime(2032, 1, 1, tzinfo=timezone.utc), 'synthetic lookup fixture')


def test_repeated_full_and_short_lookups_index_one_listing_once(tmp_path, monkeypatch):
    store = post.Store(tmp_path)
    messages = [item(f'ord-synthetic-{number:04d}-{number:04x}') for number in range(30)]
    for message in messages:
        store.write(message)
    refs = [(message.id, message.short) for message in messages]
    calls = []
    original = post.Message.short.fget
    def short(message):
        calls.append(message.id)
        return original(message)
    with post.reading_pass():
        assert store.all() == messages
        monkeypatch.setattr(post.Message, 'short', property(short))
        for _ in range(3):
            for message, (full, brief) in zip(messages, refs):
                assert store.get(full) == store.get(brief) == message
        with pytest.raises(ShipError, match='no message'):
            store.get('ord-missing')
    assert calls == [message.id for message in messages]


def test_new_listing_refreshes_the_index_and_preserves_short_ambiguity(tmp_path):
    store, publisher = post.Store(tmp_path), post.Store(tmp_path)
    first, second = item('ord-first-cafe'), item('ord-second-cafe')
    publisher.write(first)
    assert store.get(first.short) == first
    publisher.write(second)
    assert store.get(first.id) == first
    assert store.get(second.id) == second
    with pytest.raises(ShipError, match='names 2 messages'):
        store.get(first.short)


def test_one_reference_that_is_also_the_full_id_names_one_message(tmp_path):
    store = post.Store(tmp_path)
    message = item('ref-fixture')
    store.write(message)
    assert message.id == message.short
    assert store.get(message.id) == message


def test_index_follows_the_existing_presentation_snapshot(tmp_path):
    store, publisher = post.Store(tmp_path), post.Store(tmp_path)
    first, second = item('ord-first-0001'), item('ord-second-0002')
    publisher.write(first)
    with post.reading_pass():
        assert store.get(first.id) == first
        publisher.write(second)
        with pytest.raises(ShipError, match='no message'):
            store.get(second.id)
    assert store.get(second.short) == second
