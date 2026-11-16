"""The post's guards work on the path's text, and a cold Store reads each message's files once.

``post_dir``'s guards split a path as ``Path.parts`` does, on the string, so that a status reading of some 28,000
messages spends no time building paths; their refusals are unchanged one for one. A cold Store reads a message's
import record once, and its view and route lookup take it as read.
"""
from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from quarterdeck import fleet_post, post, post_dir

PIECES = ['', '.', '..', 'runtime', 'post', 'store', 'x.json', 'a b', 'runtime.post']


def old_split(path):
    parts = Path(path).parts
    for index in range(len(parts) - 1, 0, -1):
        if parts[index] == 'post' and parts[index - 1] == 'runtime':
            return Path(*parts[:index + 1]), parts[index + 1:]
    return None, ()


def old_under(path):
    root, relative = old_split(path)
    return root is not None and (Path(path) == root or bool(relative))


@pytest.mark.parametrize('lead', ['', '/', '//', '///', './'])
def test_the_split_on_text_is_paths_own_for_every_form(lead):
    for count in range(1, 5):
        for names in itertools.product(PIECES, repeat=count):
            for text in (lead + '/'.join(names), lead + '//'.join(names), lead + '/'.join(names) + '/'):
                assert post_dir._parts(text) == Path(text).parts, text
                assert post_dir._split_under_post(text) == old_split(text), text
                assert post_dir.under_post(text) == old_under(text), text
    assert post_dir._split_under_post(Path('/a/runtime/post/x')) == old_split('/a/runtime/post/x')


def test_a_cold_store_reads_each_import_record_once(tmp_path, monkeypatch):
    """Outside a reading pass, each message's import record was tried three times (the load, its view, its route
    lookup); now once, and the route's own file once. The messages are the same."""
    runtime = tmp_path / 'ship' / 'runtime'
    (tmp_path / 'ship' / 'ship.toml').parent.mkdir(parents=True)
    (tmp_path / 'ship' / 'ship.toml').write_text('')  # the ship owns its post: local_view's work is done
    store = post.Store(runtime)
    sent = [store.new('order', 'captain', 'worker-1', f'Synthetic order {n}.') for n in range(3)]
    tried = []
    real = fleet_post.stable_bytes

    def counted(path):
        tried.append(Path(path).relative_to(runtime / 'post').parts[0])
        return real(path)
    monkeypatch.setattr(fleet_post, 'stable_bytes', counted)
    messages = post.Store(runtime).all()
    assert sorted(m.id for m in messages) == sorted(m.id for m in sent)
    assert tried.count('imports') == 3, f'one import record tried per message: {tried}'
    assert tried.count('forwarded') == 3, f'one route file tried per message: {tried}'


from .test_group_forward_crossship import fleet_pair, imported  # noqa: E402,F401 (fleet_pair: the fixture, by name)


def test_an_imported_message_with_an_at_address_reads_its_import_record_once(fleet_pair, monkeypatch):
    """The view of an imported message, or one with an '@' address, passes the import record it was given on to the
    route lookup, so a cold load outside a reading pass reads it once, as every other message's."""
    pair = fleet_pair
    root = imported(pair, monkeypatch)
    assert '@' in root.id and root.sender == 'desk@office'
    agora = pair[1]
    tried = []
    real = fleet_post.stable_bytes

    def counted(path):
        tried.append(Path(path))
        return real(path)
    monkeypatch.setattr(fleet_post, 'stable_bytes', counted)
    messages = post.Store(agora.runtime).all()
    assert any(m.id == root.id for m in messages)
    record = fleet_post.import_path(agora.runtime, root.id)
    assert record.is_file()
    assert tried.count(record) == 1, f'the import record read {tried.count(record)} times'


def test_the_parties_of_an_imported_message_read_its_import_record_once(fleet_pair, monkeypatch):
    """The parties index reads an imported '@' message's sender and addressee with its import record read once:
    the record provenance read is passed on to the view, never read again there."""
    from quarterdeck import post_view
    pair = fleet_pair
    root = imported(pair, monkeypatch)
    assert '@' in root.id and root.sender == 'desk@office'
    agora = pair[1]
    record = fleet_post.import_path(agora.runtime, root.id)
    assert record.is_file()
    tried = []
    real = fleet_post.stable_bytes

    def counted(path):
        tried.append(Path(path))
        return real(path)
    monkeypatch.setattr(fleet_post, 'stable_bytes', counted)
    parties = post_view._parties_of(agora.runtime, agora.runtime / 'post' / 'store' / f'{root.id}.json')
    assert parties is not None and parties[0] == root.sender
    assert tried.count(record) == 1, f'the import record read {tried.count(record)} times'


def counting(monkeypatch):
    """Every read of a file by fleet_post.stable_bytes, the import record's reader, as resolved paths."""
    tried = []
    real = fleet_post.stable_bytes

    def counted(path):
        tried.append(Path(path).resolve())
        return real(path)
    monkeypatch.setattr(fleet_post, 'stable_bytes', counted)
    return tried


def declare_route(ship, seat, target):
    with ship.file.open('a') as stream:
        stream.write(f'\n[forward]\n{seat}="{target}"\n')


def test_with_a_route_declared_a_loaded_message_reads_its_import_record_once(fleet_pair, monkeypatch):
    """The route lookup of a loaded message takes the import record the load read: with a route declared for its
    addressee, an imported message's record is still read once."""
    root = imported(fleet_pair, monkeypatch)
    agora = fleet_pair[1]
    declare_route(agora, 'lead', 'moved@office')
    record = fleet_post.import_path(agora.runtime, root.id).resolve()
    tried = counting(monkeypatch)
    messages = post.Store(agora.runtime).all()
    assert any(m.id == root.id for m in messages)
    assert tried.count(record) == 1, f'the import record read {tried.count(record)} times'


def test_a_store_not_under_post_reads_each_import_record_once_with_its_route(fleet_pair, monkeypatch):
    """A store whose runtime is not named runtime loads by its own reader: the view and the route lookup there take
    the record that reader read."""
    root = imported(fleet_pair, monkeypatch)
    agora = fleet_pair[1]
    declare_route(agora, 'lead', 'moved@office')
    alias = agora.folder / 'alias'
    alias.symlink_to(agora.runtime)
    assert not post_dir.under_post(alias / 'post' / 'store'), 'the other reader'
    record = fleet_post.import_path(agora.runtime, root.id).resolve()
    tried = counting(monkeypatch)
    messages = post.Store(alias).all()
    assert any(m.id == root.id for m in messages)
    assert tried.count(record) == 1, f'the import record read {tried.count(record)} times'


def test_a_new_message_is_viewed_with_its_import_record_read_once(fleet_pair, monkeypatch):
    """After its write, a new message's view and its route lookup take one reading of its import record (none)."""
    agora = fleet_pair[1]
    store = post.Store(agora.runtime)
    tried = counting(monkeypatch)
    after_write = []
    real_write = post.Store.write

    def write(self, message):
        written = real_write(self, message)
        after_write.append(len(tried))
        return written
    monkeypatch.setattr(post.Store, 'write', write)
    message = store.new('order', 'lead', 'reviewer', 'A local order.')
    record = fleet_post.import_path(agora.runtime, message.id).resolve()
    assert tried[after_write[0]:].count(record) == 1, f'read {tried[after_write[0]:].count(record)} times after write'


def test_context_only_looks_for_a_local_messages_import_record_once(fleet_pair, monkeypatch):
    """A message with no import record: context_only looks for it once, and its route lookup takes that answer
    (none) rather than looking again."""
    agora = fleet_pair[1]
    message = post.Store(agora.runtime).new('order', 'lead', 'reviewer', 'A local order.')
    record = fleet_post.import_path(agora.runtime, message.id).resolve()
    tried = counting(monkeypatch)
    assert fleet_post.context_only(agora.runtime, message.id) is False
    assert tried.count(record) == 1, f'the import record looked for {tried.count(record)} times'


def test_an_import_views_its_message_with_the_record_it_published(fleet_pair, monkeypatch):
    """The import publishes the record and views the message with it: no view during the import reads the record
    back. (The import still reads it once itself before publishing, and context_only once, as before.)"""
    tried = counting(monkeypatch)
    inside = []
    real_view = fleet_post.local_view

    def view(runtime, message, **options):
        before = len(tried)
        try:
            return real_view(runtime, message, **options)
        finally:
            inside.extend(tried[before:])
    monkeypatch.setattr(fleet_post, 'local_view', view)
    root = imported(fleet_pair, monkeypatch)
    record = fleet_post.import_path(fleet_pair[1].runtime, root.id).resolve()
    assert record in tried, 'the import ran and read its record'
    assert inside.count(record) == 0, f'a view read the import record {inside.count(record)} times'
