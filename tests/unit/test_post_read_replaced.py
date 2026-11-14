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


# A complete file renamed over the name while the reader holds the old one: the old file loses its last name,
# which moves only its change time. The reader starts over and returns the new whole file. A change of the old
# file's own bytes during the read is still refused, and the refusal names the path below runtime/post.

def act_after_first_fstat(monkeypatch, path, act):
    original = post_dir.os.fstat
    done = []

    def raced(descriptor, *args, **kwargs):
        found = original(descriptor, *args, **kwargs)
        if not done and stat.S_ISREG(found.st_mode) and found.st_ino == original_inode[0]:
            done.append(True)
            act(path)
        return found

    original_inode = [path.stat().st_ino]
    monkeypatch.setattr(post_dir.os, 'fstat', raced)
    return done


def replace_whole(path):
    temporary = path.with_name('complete-temporary')
    temporary.write_bytes(REPLACED)
    temporary.replace(path)


def change_in_place(path):
    with path.open('ab') as stream:
        stream.write(b' and more')


@pytest.mark.parametrize('limit', [None, 64])
def test_a_whole_replacement_during_the_read_returns_the_new_file(tmp_path: Path, monkeypatch, limit):
    path = tmp_path / 'runtime' / 'post' / 'attempts' / 'attempt.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(ORIGINAL)
    done = act_after_first_fstat(monkeypatch, path, replace_whole)
    assert post_dir.read_bytes(path, limit=limit) == REPLACED
    assert done


def test_an_in_place_change_during_the_read_is_refused_and_names_the_path(tmp_path: Path, monkeypatch):
    path = tmp_path / 'runtime' / 'post' / 'attempts' / 'attempt.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(ORIGINAL)
    act_after_first_fstat(monkeypatch, path, change_in_place)
    with pytest.raises(ShipError) as caught:
        post_dir.read_bytes(path)
    assert str(caught.value) == 'post path changed during its read: attempts/attempt.json'
    assert ORIGINAL.decode() not in str(caught.value)


def test_a_reader_beside_a_writer_that_replaces_whole_is_never_refused(tmp_path: Path):
    """Under load: one writer replaces the file whole, a thousand times a second; the reader is never refused."""
    import time

    path = tmp_path / 'runtime' / 'post' / 'attempts' / 'attempt.json'
    post_dir.replace_bytes(path, ORIGINAL)
    stop = threading.Event()

    def writer():
        index = 0
        while not stop.is_set():
            post_dir.replace_bytes(path, b'%d complete attempt ' % index * 64)
            index += 1
            time.sleep(0.001)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    refused, reads = [], 0
    try:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                data = post_dir.read_bytes(path)
                assert data.endswith(b'complete attempt ') or data == ORIGINAL, 'every read is one whole version'
                reads += 1
            except ShipError as error:
                refused.append(str(error))
    finally:
        stop.set()
        thread.join(timeout=5)
    assert reads > 1000 and refused == [], refused[:3]
