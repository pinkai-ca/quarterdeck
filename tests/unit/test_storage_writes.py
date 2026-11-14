"""Ordinary storage stays whole without sync; three fixed law records retain it."""
import ast
import os
from pathlib import Path
import stat

import pytest

from quarterdeck import compaction, durable, post, refusal
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


def test_exactly_the_initial_attempt_refusal_and_transition_records_keep_file_and_folder_sync(tmp_path, monkeypatch):
    # law: context-is-cache; law: one-door -- storage only, no refusal or door behavior.
    events, expected = [], [None]
    real = os.fsync
    def sync(descriptor):
        actual = Path(os.readlink(f'/proc/self/fd/{descriptor}'))
        target = expected[0]
        assert target is not None, 'sync outside the three named records'
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
    message = post.Store(tmp_path).new('order', 'lead', 'worker', 'synthetic storage fixture')
    attempts = post.Attempts(tmp_path)
    third = attempts.dir / (message.id + '-1.json')
    expected[0] = third
    attempt = attempts.open(message, 'worker', None, None, None)
    expected[0] = None
    attempts.close(attempt, 'delivered', 'synthetic completion')
    attempts.close(attempt, 'delivered', 'synthetic completion')
    durable.replace_file(tmp_path / 'ordinary.json', 'complete')
    assert events == [(first, 'file'), (first, 'folder'), (second, 'file'), (second, 'folder'),
                      (third, 'file'), (third, 'folder')]
    assert first.is_file() and second.is_file() and third.is_file()


def test_only_the_three_fixed_call_sites_can_reach_the_forced_storage_helper():
    root = Path(durable.__file__).parent
    callers, syncs = [], []
    paths = [*root.rglob('*.py'), *Path(layout.__file__).parent.rglob('*.py')]
    for path in paths:
        relative = path.relative_to(root).as_posix() if path.is_relative_to(root) else 'testrunner/' + path.name
        def visit(node, scope=()):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = (*scope, node.name)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                site = relative + ':' + '.'.join(scope)
                if node.func.attr == '_replace_law_record':
                    callers.append(site)
                if node.func.attr in ('fsync', 'fdatasync'):
                    syncs.append(site)
            for child in ast.iter_child_nodes(node):
                visit(child, scope)
        visit(ast.parse(path.read_text()))
    assert sorted(callers) == ['compaction.py:save', 'post.py:Attempts.open', 'refusal.py:save']
    assert sorted(syncs) == ['durable.py:_replace_law_record', 'durable.py:sync_folder']
