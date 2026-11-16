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
