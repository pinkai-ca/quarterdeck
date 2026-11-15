"""Completed CLI acts keep their outcome when a real output reader closes."""

from pathlib import Path
import os
import select
import subprocess
import sys
from types import SimpleNamespace

import pytest

from quarterdeck import cli, output


DRIVER = r'''
import os, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
import quarterdeck
from quarterdeck import cli, presentation
from quarterdeck.ship import ShipError
marker, status, mode = Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
ready, proceed = int(sys.argv[5]), int(sys.argv[6])
def done():
    with marker.open('a') as stream:
        stream.write('act\n')
    os.write(ready, b'1')
    assert os.read(proceed, 1) == b'1'
def act(args):
    done()
    if mode == 'error':
        sys.stdout.write('completed output\n')
        raise ShipError('ordinary command refusal')
    if mode == 'buffer-stderr':
        sys.stderr.reconfigure(line_buffering=False)
        sys.stderr.write('buffered diagnostic')
    if mode == 'diagnostic':
        sys.stderr.write('open diagnostic\n')
    return 'x' * 1000000 if mode == 'large' else 'completed proof'
args = SimpleNamespace(run=act, ship=None, json=False, exit_code=status, default_ship=None)
class Parser:
    def parse_args(self, argv): return args
cli.build_parser = lambda: Parser()
cli.choose_default = lambda args, argv: argv
cli.handoff = lambda *args: None
presentation.unavailable_line = lambda argv: None
if mode == 'fast':
    def fast(argv):
        done()
        return 'completed status'
    presentation.command_line = fast
else:
    presentation.command_line = lambda argv: None
sys.argv = ['qd', 'ordinary-command']
sys.exit(quarterdeck.main())
'''


def command(tmp_path, *, status=0, mode='normal', close=('stdout',), file_stream=None):
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    marker = tmp_path / 'ordinary-acts'
    target = (tmp_path / 'open-output').open('wb') if file_stream else None
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, '-I', '-c', DRIVER, str(Path(cli.__file__).resolve().parents[1]),
             str(marker), str(status), mode, str(ready_write), str(release_read)],
            stdout=target if file_stream == 'stdout' else subprocess.PIPE,
            stderr=target if file_stream == 'stderr' else subprocess.PIPE,
            pass_fds=(ready_write, release_read))
        os.close(ready_write)
        ready_write = None
        os.close(release_read)
        release_read = None
        assert select.select([ready_read], [], [], 15)[0], 'child did not finish the ordinary act'
        assert os.read(ready_read, 1) == b'1'
        assert marker.read_text() == 'act\n'
        # The side effect is complete before either reader closes, without a race.
        for name in close:
            stream = getattr(process, name)
            stream.close()
            setattr(process, name, None)
        os.write(release_write, b'1')
        stdout, stderr = process.communicate(timeout=15)
        if target is not None:
            target.close()
        return SimpleNamespace(code=process.returncode, stdout=stdout, stderr=stderr,
                               acts=marker.read_text(),
                               file=(tmp_path / 'open-output').read_bytes() if file_stream else None)
    finally:
        for descriptor in (ready_read, ready_write, release_read, release_write):
            if descriptor is not None:
                os.close(descriptor)
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for name in ('stdout', 'stderr'):
                stream = getattr(process, name)
                if stream is not None:
                    stream.close()
        if target is not None:
            target.close()


@pytest.mark.parametrize('status', [0, 7])
@pytest.mark.parametrize('mode', ['normal', 'large'])
def test_closed_stdout_preserves_the_completed_status_and_single_act(tmp_path, status, mode):
    result = command(tmp_path, status=status, mode=mode)
    assert result.code == status and result.acts == 'act\n'
    assert result.stderr == b'', 'no traceback or interpreter-flush warning'


def test_closed_stderr_preserves_the_acts_refusal_status_and_flushes_open_stdout(tmp_path):
    result = command(tmp_path, mode='error', close=('stderr',))
    assert result.code == 2 and result.acts == 'act\n'
    assert result.stdout == b'completed output\n'


def test_buffered_stderr_failure_at_exit_does_not_replace_the_status(tmp_path):
    result = command(tmp_path, status=7, mode='buffer-stderr', close=('stderr',))
    assert result.code == 7 and result.acts == 'act\n'
    assert result.stdout == b'completed proof\n'


def test_both_closed_streams_do_not_cause_shutdown_status_120(tmp_path):
    result = command(tmp_path, status=7, mode='buffer-stderr', close=('stdout', 'stderr'))
    assert result.code == 7 and result.acts == 'act\n'


@pytest.mark.parametrize('kept', ['stdout', 'stderr'])
def test_the_other_open_file_keeps_all_its_output(tmp_path, kept):
    result = command(tmp_path, status=7, mode='buffer-stderr' if kept == 'stdout' else 'diagnostic',
                     close=('stderr' if kept == 'stdout' else 'stdout',), file_stream=kept)
    assert result.code == 7 and result.acts == 'act\n'
    assert result.file == (b'completed proof\n' if kept == 'stdout' else b'open diagnostic\n')


def test_open_pipes_keep_proofs_and_buffered_diagnostics(tmp_path):
    result = command(tmp_path, mode='buffer-stderr', close=())
    assert result.code == 0 and result.acts == 'act\n'
    assert result.stdout == b'completed proof\n' and result.stderr == b'buffered diagnostic'


def test_the_lightweight_completed_status_reading_also_exits_quietly(tmp_path):
    result = command(tmp_path, mode='fast')
    assert result.code == 0 and result.acts == 'act\n' and result.stderr == b''


def parser_for(monkeypatch, act):
    args = SimpleNamespace(run=act, ship=None, json=False, default_ship=None)
    monkeypatch.setattr(cli, 'build_parser', lambda: SimpleNamespace(parse_args=lambda argv: args))
    monkeypatch.setattr(cli, 'choose_default', lambda args, argv: argv)
    monkeypatch.setattr(cli, 'handoff', lambda *args: None)


def test_a_broken_pipe_before_the_act_finishes_keeps_its_existing_exception(monkeypatch):
    calls = []
    def act(args):
        calls.append('begun')
        raise BrokenPipeError('ordinary early output, act not completed')
    parser_for(monkeypatch, act)
    with pytest.raises(BrokenPipeError, match='not completed'):
        cli.main(['ordinary-command'])
    assert calls == ['begun']


def test_renderer_broken_pipe_is_not_misclassified_as_a_closed_stdout(monkeypatch):
    def render(data):
        raise BrokenPipeError('ordinary renderer operation, not stdout')
    parser_for(monkeypatch, lambda args: output.Reading({}, render))
    with pytest.raises(BrokenPipeError, match='not stdout'):
        cli.main(['ordinary-command'])


@pytest.mark.parametrize('completed', [False, True])
def test_other_output_errors_keep_their_normal_failure(completed, monkeypatch):
    class Full:
        def write(self, text):
            raise OSError('ordinary full output device')
    monkeypatch.setattr(output.sys, 'stdout', Full())
    with pytest.raises(OSError, match='full output device'):
        output.emit('ordinary output', completed_act=completed)


def test_receipt_bearing_output_still_propagates_broken_pipes(monkeypatch):
    class Broken:
        def write(self, text):
            raise BrokenPipeError('ordinary failed receipt output')
    monkeypatch.setattr(output.sys, 'stdout', Broken())
    with pytest.raises(BrokenPipeError, match='receipt output'):
        cli.emit(SimpleNamespace(json=False), output.Reading({}, lambda data: 'ordinary batch'))
