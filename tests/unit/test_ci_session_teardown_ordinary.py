"""The CI recorder must finish inside the session fixture's existing cleanup scope."""

import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

from tests.conftest import socket_container
from tests import conftest
from tests.testrunner import evidence_plugin, layout, owned, sessions


def session_root(parent, native_child):
    with socket_container(parent, 'session-') as folder:
        native = folder / f'tmux-{os.getuid()}'
        if native_child:
            native.mkdir()
        try:
            yield folder
        finally:
            if native_child:
                native.rmdir()


@pytest.mark.parametrize('native_child', [False, True])
def test_ci_registration_finishes_after_the_fixture_removes_its_native_child(tmp_path, monkeypatch, native_child):
    # Use the real owner marker/unmark checks, without requiring a repository
    # or starting any program. Registration itself is already tested separately.
    support = ModuleType('_qd_testrun_ci_support')
    support.sessions = SimpleNamespace(register=lambda parent, folder, descriptor, **context: owned.mark(descriptor),
                                       unmark_empty=sessions.unmark_empty)
    support_layout = ModuleType('_qd_testrun_ci_support.layout')
    support_layout.DIRECTORY = layout.DIRECTORY
    monkeypatch.setitem(sys.modules, support.__name__, support)
    monkeypatch.setitem(sys.modules, support_layout.__name__, support_layout)

    finalizers = []
    request = SimpleNamespace(addfinalizer=finalizers.append)
    fixturedef = SimpleNamespace(argname='private_socket_root', func=session_root)
    recorder = SimpleNamespace(ci_sessions=evidence_plugin.CISessions({}))
    hook = evidence_plugin.Recorder.pytest_fixture_setup(recorder, fixturedef, request)
    next(hook)
    generator = session_root(tmp_path, native_child)
    folder = next(generator)

    def finish_fixture():
        with pytest.raises(StopIteration):
            next(generator)

    # This is pytest's order: install the yield fixture finalizer before the
    # hook wrapper receives its successful result, then run finalizers LIFO.
    finalizers.append(finish_fixture)
    with pytest.raises(StopIteration):
        hook.send(SimpleNamespace(get_result=lambda: folder))
    errors = []
    for finish in reversed(finalizers):
        try:
            finish()
        except (Exception, pytest.fail.Exception) as error:
            errors.append(str(error))
    assert errors == []
    assert not folder.exists()


def test_socket_entry_diagnostics_name_types_and_mtime_ages(tmp_path, monkeypatch):
    folder = tmp_path / 'native'
    folder.mkdir()
    entry = tmp_path / 'empty-file'
    entry.touch()
    instant = 1893456000000000000
    os.utime(folder, ns=(instant - 5_000_000_000, instant - 5_000_000_000))
    os.utime(entry, ns=(instant - 7_000_000_000, instant - 7_000_000_000))
    monkeypatch.setattr(conftest, 'time', SimpleNamespace(time_ns=lambda: instant))
    descriptor = os.open(tmp_path, layout.DIRECTORY)
    try:
        assert conftest.socket_container_entries(descriptor) == [
            dict(name='empty-file', lstat_type='regular', mtime_age_seconds=7.0),
            dict(name='native', lstat_type='directory', mtime_age_seconds=5.0),
        ]
    finally:
        os.close(descriptor)
