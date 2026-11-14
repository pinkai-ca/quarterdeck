"""Complete ordinary post writes racing a read at its final name check."""

import os
import stat
import threading
from pathlib import Path

import pytest

from quarterdeck import post_dir
from quarterdeck.ship import ShipError


def replace_at_final_stat(monkeypatch, path, replacements):
    """Rename complete regular files just before the reader's final name stat."""
    original = post_dir.os.stat
    calls = []
    payloads = [b'first complete attempt', b'second complete attempt', b'third complete attempt']

    def raced(name, *args, **kwargs):
        if name == path.name and kwargs.get('dir_fd') is not None:
            assert kwargs.get('follow_symlinks') is False
            calls.append(name)
            index = len(calls) // 2 - 1
            if len(calls) % 2 == 0 and index < replacements:
                temporary = path.with_name('complete-temporary')
                temporary.write_bytes(payloads[index])
                temporary.replace(path)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(post_dir.os, 'stat', raced)
    return calls, payloads


@pytest.mark.parametrize('replacements', [1, 2])
@pytest.mark.parametrize('limit', [None, 64])
def test_atomic_replacement_returns_the_new_complete_bytes(tmp_path: Path, monkeypatch, replacements, limit):
    path = tmp_path / 'runtime' / 'post' / 'attempts' / 'attempt.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'original complete attempt')
    calls, payloads = replace_at_final_stat(monkeypatch, path, replacements)

    assert post_dir.read_bytes(path, limit=limit) == payloads[replacements - 1]
    assert len(calls) == 2 * (replacements + 1)
    assert path.read_bytes() == payloads[replacements - 1]


def test_continuous_atomic_replacement_stops_after_three_reads(tmp_path: Path, monkeypatch):
    path = tmp_path / 'runtime' / 'post' / 'attempts' / 'attempt.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'original complete attempt')
    calls, payloads = replace_at_final_stat(monkeypatch, path, 3)

    with pytest.raises(ShipError, match='post path changed during its read'):
        post_dir.read_bytes(path)
    assert len(calls) == 6
    assert path.read_bytes() == payloads[-1]


# A link or a FIFO swapped in at each check of the retry is refused: nothing is returned, neither the
# link target's bytes nor the old ones, and a FIFO is never waited on. These pin the checks inside the loop.

SECRET = b'link target bytes must stay unread'
ORIGINAL = b'original complete attempt'
REPLACED = b'replaced complete attempt'


def put(path, kind, target):
    """Rename a complete regular file, a link to target, or a FIFO onto path."""
    temporary = path.with_name(f'swap-{kind}')
    if kind == 'regular':
        temporary.write_bytes(REPLACED)
    elif kind == 'link':
        temporary.symlink_to(target)
    else:
        os.mkfifo(temporary)
    temporary.replace(path)


def swap_at(monkeypatch, path, target, before=(), after=()):
    """Swap path at the reader's numbered name stats: ``before`` the stat runs, or ``after`` it returned."""
    original = post_dir.os.stat
    calls = []
    before, after = dict(before), dict(after)

    def raced(name, *args, **kwargs):
        if name != path.name or kwargs.get('dir_fd') is None:
            return original(name, *args, **kwargs)
        assert kwargs.get('follow_symlinks') is False
        calls.append(name)
        if len(calls) in before:
            put(path, before[len(calls)], target)
        found = original(name, *args, **kwargs)
        if len(calls) in after:
            put(path, after[len(calls)], target)
        return found

    monkeypatch.setattr(post_dir.os, 'stat', raced)
    return calls


def refused_at_once(path):
    """The read's refusal. It must come within two seconds; a read that returns fails the test."""
    box = []

    def run():
        try:
            box.append(('returned', post_dir.read_bytes(path)))
        except Exception as error:
            box.append(('raised', error))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(2)
    assert not thread.is_alive(), 'the read waited on the swapped name'
    outcome, value = box[0]
    assert outcome == 'raised', f'the read returned {value!r}'
    return value


SWAPS = {
    # the first read's final stat: a link, or a FIFO
    'link at the final stat': (dict(before={2: 'link'}), 2, ShipError, 'post path changed during its read'),
    'fifo at the final stat': (dict(before={2: 'fifo'}), 2, ShipError, 'post path changed during its read'),
    # a regular replacement starts the retry; its own final stat then meets a link, or a FIFO
    'regular then link at the second final stat': (
        dict(before={2: 'regular', 4: 'link'}), 4, ShipError, 'post path changed during its read'),
    'regular then fifo at the second final stat': (
        dict(before={2: 'regular', 4: 'fifo'}), 4, ShipError, 'post path changed during its read'),
    # a regular replacement starts the retry; a link or a FIFO lands between the retry's lstat and its open
    'link between the retry lstat and its open': (
        dict(before={2: 'regular'}, after={3: 'link'}), 3, ShipError, 'post link held at '),
    'fifo between the retry lstat and its open': (
        dict(before={2: 'regular'}, after={3: 'fifo'}), 3, post_dir.NotRegular, 'post path is not a regular file'),
}


@pytest.mark.parametrize('case', sorted(SWAPS))
def test_a_link_or_fifo_swapped_in_during_the_retry_is_refused(tmp_path: Path, monkeypatch, case):
    swaps, stats, kind, message = SWAPS[case]
    path = tmp_path / 'runtime' / 'post' / 'attempts' / 'attempt.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(ORIGINAL)
    target = tmp_path / 'outside-target'
    target.write_bytes(SECRET)
    calls = swap_at(monkeypatch, path, target, **swaps)

    error = refused_at_once(path)

    assert type(error) is kind and str(error).startswith(message)
    assert len(calls) == stats  # the refusal came at the swapped check, not at a later one
    assert SECRET.decode() not in str(error) and str(target) not in str(error)
    assert ORIGINAL.decode() not in str(error) and REPLACED.decode() not in str(error)
    assert target.read_bytes() == SECRET  # the target is untouched, and the swapped name is left in place
    assert path.is_symlink() if 'link' in case else stat.S_ISFIFO(path.lstat().st_mode)
