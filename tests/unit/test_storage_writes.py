"""Ordinary storage stays whole without sync; two fixed law records retain it."""
import ast
import os
from pathlib import Path
import stat

import pytest

from quarterdeck import compaction, durable, refusal
from tests.testrunner import layout


def test_ordinary_replacements_and_tool_records_stay_whole_with_both_syncs_forbidden(tmp_path, monkeypatch):
    def forbidden(_):
        pytest.fail('ordinary storage forced a sync')
    monkeypatch.setattr(os, 'fsync', forbidden)
    monkeypatch.setattr(os, 'fdatasync', forbidden)
    original = os.write
    monkeypatch.setattr(os, 'write', lambda fd, data: original(fd, data[:3]))
    path = tmp_path / 'ordinary.json'
    path.write_text('old complete text')
    durable.replace_file(path, 'new complete Ω text')
    assert path.read_text() == 'new complete Ω text'
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        layout.write_atomic(descriptor, 'ordinary.json', b'complete tool record')
    finally:
        os.close(descriptor)
    assert path.read_bytes() == b'complete tool record'
    assert list(tmp_path.iterdir()) == [path]


def test_exactly_the_refusal_and_transition_records_keep_file_and_folder_sync(tmp_path, monkeypatch):
    # law: context-is-cache -- this pins storage, not refusal or door behavior.
    events, expected = [], [None]
    real = os.fsync
    def sync(descriptor):
        actual = Path(os.readlink(f'/proc/self/fd/{descriptor}'))
        target = expected[0]
        assert target is not None, 'sync outside the two named records'
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            assert actual == target.parent
            kind = 'folder'
        else:
            assert actual.parent == target.parent and actual.name.startswith('.' + target.name + '.')
            kind = 'file'
        events.append((target, kind))
        real(descriptor)
    def forbidden(_):
        pytest.fail('no writer needs fdatasync')
    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(os, 'fdatasync', forbidden)
    first = refusal.path(tmp_path, 'worker')
    expected[0] = first
    refusal.save(tmp_path, 'worker', [])
    second = compaction.path(tmp_path, 'worker')
    expected[0] = second
    compaction.save(tmp_path, 'worker', compaction.new())
    expected[0] = None
    durable.replace_file(tmp_path / 'ordinary.json', 'complete')
    assert events == [(first, 'file'), (first, 'folder'), (second, 'file'), (second, 'folder')]
    assert first.is_file() and second.is_file()


def test_only_the_two_fixed_writers_can_reach_the_forced_storage_helper():
    root = Path(durable.__file__).parent
    callers, syncs = [], []
    paths = [*root.rglob('*.py'), *Path(layout.__file__).parent.rglob('*.py')]
    for path in paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == '_replace_law_record':
                callers.append(path.relative_to(root).as_posix())
            if node.func.attr in ('fsync', 'fdatasync'):
                syncs.append(path.relative_to(root).as_posix() if path.is_relative_to(root) else 'testrunner/' + path.name)
    assert sorted(callers) == ['compaction.py', 'refusal.py']
    assert syncs == ['durable.py', 'durable.py']
