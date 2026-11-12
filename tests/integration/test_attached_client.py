"""The attached terminal client a test owns: its output is kept while it lives, embedded whole when it never
attaches, and its file is gone on every exit, so plain pytest leaves nothing in the system's temporary folder."""

import os
import tempfile

import pytest

from .waiting import attached_client

MARK = 'synthetic client output'


@pytest.fixture
def own_tmp(tmp_path, monkeypatch):
    """The helper's temporary files land in this test's own folder, where they can be counted."""
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    return tmp_path


def logs(folder):
    return sorted(folder.glob('tmux-client-*.log'))


def test_a_ready_client_keeps_its_output_while_attached_and_leaves_no_file(own_tmp):
    term = os.environ.get('TERM')
    with attached_client('sleep 30', lambda: True) as client:
        assert client.poll() is None and len(logs(own_tmp)) == 1
    assert client.returncode is not None and logs(own_tmp) == []
    assert os.environ.get('TERM') == term, 'only the client is given its terminal type'


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


def test_a_failure_inside_an_attached_client_is_raised_untouched_and_leaves_no_file(own_tmp):
    with pytest.raises(AssertionError) as failure:
        with attached_client('sleep 30', lambda: True):
            raise AssertionError('synthetic failure inside the attached test')
    assert str(failure.value) == 'synthetic failure inside the attached test'
    assert logs(own_tmp) == []


def test_a_client_that_cannot_start_leaves_no_file(own_tmp, monkeypatch):
    monkeypatch.setenv('PATH', str(own_tmp / 'no-programs'))
    with pytest.raises(FileNotFoundError):
        with attached_client('sleep 30', lambda: True):
            pytest.fail('a client that never started was handed to the test')
    assert logs(own_tmp) == []
