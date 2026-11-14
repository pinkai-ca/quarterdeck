"""Complete ordinary post writes racing a read at its final name check."""

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
