"""The attached terminal client a test owns: its output is kept while it lives, embedded whole when it never
attaches, and its file is gone on every exit, so plain pytest leaves nothing in the system's temporary folder."""

import os
import shutil
import tempfile

import pytest

from . import waiting
from .waiting import attached_client

MARK = 'synthetic client output'
# As the other attach tests: a client is attached through the pty helper; without it there is nothing to attach.
needs_script = pytest.mark.skipif(shutil.which('script') is None, reason='no pty helper to attach a client')


@pytest.fixture
def own_tmp(tmp_path, monkeypatch):
    """The helper's temporary files land in this test's own folder, where they can be counted."""
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    return tmp_path


def logs(folder):
    return sorted(folder.glob('tmux-client-*.log'))


@needs_script
def test_a_ready_client_keeps_its_output_while_attached_and_leaves_no_file(own_tmp):
    term = os.environ.get('TERM')
    with attached_client('sleep 30', lambda: True) as client:
        assert client.poll() is None and len(logs(own_tmp)) == 1
        assert not client.stdin.closed
    assert client.returncode is not None and logs(own_tmp) == []
    assert client.stdin.closed
    assert os.environ.get('TERM') == term, 'only the client is given its terminal type'


@needs_script
def test_a_client_that_never_attaches_fails_with_its_whole_output_and_leaves_no_file(own_tmp):
    def attached():
        if any(MARK in path.read_text(errors='replace') for path in logs(own_tmp)):
            raise AssertionError('synthetic client never attached')
        return False

    with pytest.raises(AssertionError) as failure:
        with attached_client(f"printf '%s\\n' '{MARK}'; sleep 30", attached):
            pytest.fail('a client that never attached was handed to the test')
    text = str(failure.value)
    assert 'synthetic client never attached' in text and MARK in text
    assert "client TERM='xterm-256color'" in text and 'tmux attach script exit=' in text
    assert logs(own_tmp) == []


def test_readiness_timeout_names_the_expected_attachment_with_no_client_output(own_tmp, monkeypatch):
    script = own_tmp / 'script'
    script.write_text('#!/bin/sh\nexit 0\n')
    script.chmod(0o700)
    monkeypatch.setenv('PATH', str(own_tmp))
    wait_for = waiting.wait_for
    monkeypatch.setattr(waiting, 'wait_for', lambda predicate: wait_for(predicate, seconds=0))
    event = 'one attached client for Bridge ($42) in list-sessions'
    with pytest.raises(AssertionError) as failure:
        with attached_client('synthetic-client', lambda: False, event=event):
            pytest.fail('a client that never attached was handed to the test')
    text = str(failure.value)
    assert f'condition not met in time while waiting for {event}' in text
    assert 'tmux attach script exit=' in text and "client TERM='xterm-256color'" in text
    assert text.endswith('output:\n')
    assert logs(own_tmp) == []


@needs_script
def test_a_failure_inside_an_attached_client_is_raised_untouched_and_leaves_no_file(own_tmp):
    with pytest.raises(AssertionError) as failure:
        with attached_client('sleep 30', lambda: True, event='a synthetic attachment'):
            raise AssertionError('synthetic failure inside the attached test')
    assert str(failure.value) == 'synthetic failure inside the attached test'
    assert logs(own_tmp) == []


def test_a_client_that_cannot_start_leaves_no_file(own_tmp, monkeypatch):
    monkeypatch.setenv('PATH', str(own_tmp / 'no-programs'))
    with pytest.raises(FileNotFoundError):
        with attached_client('sleep 30', lambda: True):
            pytest.fail('a client that never started was handed to the test')
    assert logs(own_tmp) == []
