"""Ordinary storage stays whole without sync; three fixed law records retain it."""
import ast
import os
from pathlib import Path
import stat

import pytest

from quarterdeck import compaction, durable, post, post_dir, refusal
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
    for destination in (tmp_path / 'outside.json', tmp_path / 'runtime/post/inbox/worker.md'):
        post_dir.replace_bytes(destination, 'complete post Ω'.encode())
        assert destination.read_text() == 'complete post Ω'
        durable.replace_file(destination, 'complete replacement Ω')
        assert destination.read_text() == 'complete replacement Ω'
        assert list(destination.parent.glob('.' + destination.name + '.*.tmp')) == []


def test_exactly_the_initial_attempt_refusal_and_transition_records_keep_file_and_folder_sync(tmp_path, monkeypatch):
    # law: context-is-cache; law: one-door -- storage only, no refusal or door behavior.
    runtime = tmp_path / 'runtime'
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
    first = refusal.path(runtime, 'worker')
    expected[0] = first
    refusal.save(runtime, 'worker', [])
    second = compaction.path(runtime, 'worker')
    expected[0] = second
    compaction.save(runtime, 'worker', compaction.new())
    expected[0] = None
    message = post.Store(runtime).new('order', 'lead', 'worker', 'synthetic storage fixture')
    attempts = post.Attempts(runtime)
    third = attempts.dir / (message.id + '-1.json')
    expected[0] = third
    attempt = attempts.open(message, 'worker', None, None, None)
    expected[0] = None
    attempts.close(attempt, 'delivered', 'synthetic completion')
    attempts.close(attempt, 'delivered', 'synthetic completion')
    durable.replace_file(runtime / 'ordinary.json', 'complete')
    post_dir.replace_bytes(runtime / 'post/inbox/worker.md', b'ordinary post bytes')
    assert events == [(first, 'file'), (first, 'folder'), (second, 'file'), (second, 'folder'),
                      (third, 'file'), (third, 'folder')]
    assert first.is_file() and second.is_file() and third.is_file()


def test_only_the_three_fixed_call_sites_can_reach_the_forced_storage_helper():
    root = Path(durable.__file__).parent
    callers, syncs, post_law, private_writes, folder_callers = [], [], [], [], []
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
                if node.func.attr == '_replace_law_bytes':
                    post_law.append(site)
                if node.func.attr == 'sync_folder':
                    folder_callers.append(site)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'sync_folder':
                folder_callers.append(relative + ':' + '.'.join(scope))
            if (relative == 'post_dir.py' and isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == '_replace_bytes'):
                setting = next(k.value for k in node.keywords if k.arg == 'law_record')
                assert isinstance(setting, ast.Constant) and isinstance(setting.value, bool)
                private_writes.append(('.'.join(scope), setting.value))
            if relative == 'post_dir.py' and isinstance(node, ast.FunctionDef) and node.name == 'replace_bytes':
                assert not {'sync', 'law_record'} & {a.arg for a in [*node.args.args, *node.args.kwonlyargs]}
            for child in ast.iter_child_nodes(node):
                visit(child, scope)
        visit(ast.parse(path.read_text()))
    assert sorted(callers) == ['compaction.py:save', 'post.py:Attempts.open', 'refusal.py:save']
    # A seat's folder takes the same shape as the post folder's walk: a private writer whose syncs only the law
    # records' entry reaches (seat_files._replace_law_bytes, from durable._replace_law_record alone).
    assert sorted(syncs) == ['durable.py:_replace_law_record', 'durable.py:sync_folder',
                             'post_dir.py:_replace_bytes', 'post_dir.py:_replace_bytes',
                             'seat_files.py:_write', 'seat_files.py:_write']
    assert post_law == ['durable.py:_replace_law_record', 'durable.py:_replace_law_record']
    assert folder_callers == ['durable.py:_replace_law_record']
    assert sorted(private_writes) == [('_replace_law_bytes', True), ('replace_bytes', False)]


@pytest.mark.parametrize('after_rename', [False, True])
def test_post_replacement_stays_whole_across_publication_interruption(tmp_path, monkeypatch, after_rename):
    path = tmp_path / 'runtime/post/inbox/worker.md'
    post_dir.replace_bytes(path, b'old complete bytes')
    def forbidden(_):
        pytest.fail('ordinary post replacement forced a sync')
    monkeypatch.setattr(os, 'fsync', forbidden)
    monkeypatch.setattr(os, 'fdatasync', forbidden)
    real = os.replace
    def interrupted(source, destination, **kwargs):
        if after_rename:
            real(source, destination, **kwargs)
        raise OSError('publication interruption')
    monkeypatch.setattr(os, 'replace', interrupted)
    with pytest.raises(OSError, match='publication interruption'):
        post_dir.replace_bytes(path, b'new complete bytes')
    assert path.read_bytes() == (b'new complete bytes' if after_rename else b'old complete bytes')
    assert list(path.parent.iterdir()) == [path]
    monkeypatch.setattr(os, 'replace', real)
    post_dir.replace_bytes(path, b'new complete bytes')
    assert path.read_bytes() == b'new complete bytes'
