"""The operator's terminal shortcut, using synthetic input and seats."""

from __future__ import annotations

import errno
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quarterdeck import cli, desk_request, fleet_post, output, post, post_index, post_sender, ship as shipmod, standing
from quarterdeck.ship import ShipError

USAGE = 'Send a request with qd "a sentence", echo text | qd, qd -t SEAT, or qd -n'


class Input:
    """A fake descriptor: readiness follows a schedule, and waiting advances its clock."""

    closed = False

    def __init__(self, monkeypatch, events=(), *, terminal=False, regular=False, descriptor_error=None):
        self.events = deque(events)
        self.now = 0.0
        self.terminal = terminal
        self.blocking = True
        self.mode = stat.S_IFREG if regular else stat.S_IFIFO
        self.descriptor_error = descriptor_error
        self.waits = []
        self.reads = []
        monkeypatch.setattr(desk_request, 'os', SimpleNamespace(
            fstat=self.fstat, get_blocking=lambda fd: self.blocking,
            set_blocking=self.set_blocking, read=self.read))
        monkeypatch.setattr(desk_request, 'select', SimpleNamespace(select=self.select))

    def isatty(self):
        return self.terminal

    def fileno(self):
        return 0

    def fstat(self, fd):
        if self.descriptor_error is not None:
            raise self.descriptor_error
        return SimpleNamespace(st_mode=self.mode)

    def set_blocking(self, fd, value):
        self.blocking = value

    def select(self, read, write, error, timeout):
        self.waits.append(timeout)
        if self.events and self.events[0][0] < self.now + timeout:
            self.now = self.events[0][0]
            return read, [], []
        self.now += timeout
        return [], [], []

    def read(self, fd, count):
        assert not self.blocking
        self.reads.append(count)
        when, data = self.events.popleft()
        if isinstance(data, BaseException):
            raise data
        if len(data) > count:
            self.events.appendleft((when, data[count:]))
        return data[:count]

    def text(self):
        return desk_request.read_stdin(self, clock=lambda: self.now)


@pytest.mark.parametrize('regular', [False, True])
def test_true_eof_does_not_wait_out_the_bound(monkeypatch, regular):
    stream = Input(monkeypatch, [(0, b'')], regular=regular)
    assert stream.text() == ('', False)
    assert stream.now == 0 and stream.blocking


def test_terminal_and_closed_input_need_no_readiness_check(monkeypatch):
    terminal = Input(monkeypatch, terminal=True)
    assert terminal.text() == ('', False)
    assert terminal.waits == terminal.reads == []
    closed = Input(monkeypatch, descriptor_error=OSError(errno.EBADF, 'closed'))
    assert closed.text() == ('', False)
    assert closed.waits == closed.reads == []
    assert desk_request.read_stdin(None) == ('', False)


def test_open_pipe_without_bytes_or_eof_uses_only_the_first_bound(monkeypatch):
    stream = Input(monkeypatch)
    assert stream.text() == ('', True)
    assert stream.waits == [5] and stream.reads == []
    assert stream.now == 5 and stream.blocking


def test_slow_pipe_finishes_under_one_overall_deadline(monkeypatch):
    stream = Input(monkeypatch, [(4.5, b'\xc3'), (599.5, b'\xa9\n'), (599.75, b'')])
    assert stream.text() == ('é\n', False)
    assert stream.waits == [5, 595.5, 0.5]
    assert stream.blocking


def test_progress_cannot_extend_the_total_deadline(monkeypatch):
    stream = Input(monkeypatch, [(4.5, b'one'), (599.5, b'two'), (600.5, b'')])
    with pytest.raises(ShipError, match='did not reach EOF within 10 minutes'):
        stream.text()
    assert stream.now == 600 and stream.blocking


def test_regular_input_starts_with_the_overall_bound(monkeypatch):
    stream = Input(monkeypatch, [(5.5, b'file contents'), (5.5, b'')], regular=True)
    assert stream.text() == ('file contents', False)
    assert stream.waits[0] == 600


def test_oversize_pipe_reads_at_most_one_extra_byte(monkeypatch):
    monkeypatch.setattr(post, 'DROP_MAX_BYTES', 8)
    stream = Input(monkeypatch, [(0, b'x' * 50)])
    with pytest.raises(ShipError, match='8-byte post limit'):
        stream.text()
    assert stream.reads == [9] and stream.blocking


@pytest.mark.parametrize(('value', 'reason'), [
    (b'\xff', 'stdin must be UTF-8'),
    (OSError(errno.EIO, 'failed read'), 'retry with a readable UTF-8 file'),
])
def test_bad_input_is_actionable_and_restores_the_descriptor(monkeypatch, value, reason):
    stream = Input(monkeypatch, [(0, value), (0, b'')])
    with pytest.raises(ShipError, match=reason):
        stream.text()
    assert stream.blocking


def declare(folder, *, kind='crew', role='desk', human=True):
    folder.mkdir(parents=True)
    (folder / 'ship.toml').write_text(f'''[ship]
code = "checkout"
kind = "{kind}"
operator = "Ada"
[pools.fake]
provider = "synthetic"
[sessions.office]
human = {str(human).lower()}
[seats.desk]
session = "office"
role = "{role}"
harness = "fake"
pool = "fake"
model = "fake-1"
''')
    return folder


@pytest.fixture
def shell(tmp_path, monkeypatch):
    project = tmp_path / "project with 'quotes'"
    (project / 'ships').mkdir(parents=True)
    workspace = tmp_path / 'user workspace'
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(standing, 'PROJECT_DIR', project)
    monkeypatch.delenv('QD_SHIP', raising=False)
    monkeypatch.delenv('QD_SEAT', raising=False)
    identity = Mock(return_value=None)
    monkeypatch.setattr(post_sender, 'seat_session', identity)
    monkeypatch.setattr(cli.service, 'alive', Mock(return_value=True))
    monkeypatch.setattr(cli, 'seat_of', Mock(return_value=SimpleNamespace(is_up=True)))
    return SimpleNamespace(project=project, workspace=workspace, identity=identity)


def input_text(monkeypatch, stdin='', timeout=False):
    monkeypatch.setattr(desk_request, 'read_stdin', Mock(return_value=(stdin, timeout)))
    monkeypatch.setattr(desk_request, 'stdin_is_input', Mock(return_value=bool(stdin)))


def drop(folder):
    file, = (folder / 'runtime/post/drop/ada').glob('*.json')
    return post.Message.from_json(file.read_text())


def load_drop(store):
    assert post.file_drops(store) == []
    return next(message for message in reversed(store.all()) if message.kind == 'order')


def on_reply_wait(monkeypatch, pause):
    wait = desk_request.wait_for_reply
    monkeypatch.setattr(desk_request, 'wait_for_reply', lambda *args: wait(*args, pause=pause))


@pytest.mark.parametrize(('argv', 'stdin', 'message_body'), [
    (['Look at notes.md'], '', 'Look at notes.md'),
    ([], 'Please read this\n', 'Please read this\n'),
    (['Read this file'], 'line one\nline two\n', 'Read this file\n\nAttached stdin:\nline one\nline two\n'),
    (['Read this file'], ' \t\n', 'Read this file'),
])
def test_terminal_request_uses_the_unique_desk_and_the_ordinary_operator_post(shell, capsys, monkeypatch,
                                                                     argv, stdin, message_body):
    crew = declare(shell.project / 'ships' / 'crew', human=False)
    office = declare(shell.project / 'office', kind='office')
    input_text(monkeypatch, stdin)
    assert cli.main(['-n', *argv]) == 0
    message = drop(crew)
    assert (message.kind, message.sender, message.to, message.subject) == ('order', 'ada@crew', 'desk@office', None)
    assert message.body == f'Working directory: {shell.workspace}\n\n{message_body}'
    assert not (office / 'runtime').exists()
    out, err = capsys.readouterr()
    assert (out, err) == (f'{message.id}\n', '')
    cli.service.alive.assert_not_called()


@pytest.mark.parametrize('empty_office', [False, True])
def test_only_the_acting_ship_is_considered_when_the_office_declaration_is_absent(shell, capsys, monkeypatch,
                                                                                 empty_office):
    earlier = declare(shell.project / 'ships' / 'first')
    other = declare(shell.project / 'ships' / 'second')
    (shell.project / 'ships/.default').write_text('first\n')
    if empty_office:
        (shell.project / 'office').mkdir()
    input_text(monkeypatch)
    assert cli.main(['-n', 'Take a look']) == 0
    assert shell.identity.call_args.args[0].folder == earlier
    order = drop(earlier)
    assert (order.sender, order.to) == ('ada', 'desk')
    out, err = capsys.readouterr()
    assert (out, err) == (f'{order.id}\n', '')
    assert not (other / 'runtime').exists()
    assert not (shell.project / 'office' / 'runtime').exists()

    monkeypatch.setenv('QD_SHIP', str(other))
    assert cli.main(['-n', 'A second request']) == 0
    order = drop(other)
    assert (order.sender, order.to) == ('ada', 'desk')
    out, err = capsys.readouterr()
    assert (out, err) == (f'{order.id}\n', '')


def test_separate_office_takes_precedence_over_the_acting_ships_desk(shell, capsys, monkeypatch):
    crew = declare(shell.project / 'ships' / 'crew')
    office = declare(shell.project / 'office', kind='office')
    input_text(monkeypatch)
    assert cli.main(['-n', 'Take a look']) == 0
    order = drop(crew)
    assert (order.sender, order.to) == ('ada@crew', 'desk@office')
    out, err = capsys.readouterr()
    assert (out, err) == (f'{order.id}\n', '')
    assert not (office / 'runtime').exists()


def test_inbox_command_can_use_the_invoked_qd_when_it_is_outside_path(shell, monkeypatch):
    monkeypatch.setenv('PATH', '')
    executable = str(shell.project / '.venv/bin/qd')
    office = shell.project / 'office'
    assert desk_request.command_entry(executable) == executable
    assert shlex.split(desk_request.shell_command(executable, office, 'inbox', '--wait')) == [
        executable, '--ship', str(office), 'inbox', '--wait']


def test_no_office_or_crew_points_to_init(shell, capsys, monkeypatch):
    input_text(monkeypatch)
    assert cli.main(['Start here']) == 2
    out, err = capsys.readouterr()
    assert not out and 'run qd init' in err


def test_missing_default_points_to_the_actual_shortcut_selection(shell, capsys, monkeypatch):
    declare(shell.project / 'ships' / 'first', human=False)
    declare(shell.project / 'ships' / 'second', human=False)
    declare(shell.project / 'office', kind='office')
    input_text(monkeypatch)
    assert cli.main(['Start here']) == 2
    out, err = capsys.readouterr()
    assert not out and 'no default ship among several' in err and 'set QD_SHIP' in err


@pytest.mark.parametrize('explicit', [False, True])
def test_initialized_office_posts_locally(shell, capsys, monkeypatch, explicit):
    office = declare(shell.project / 'office', kind='office')
    if explicit:
        monkeypatch.setenv('QD_SHIP', str(office))
    input_text(monkeypatch)
    assert cli.main(['-n', 'Start here']) == 0
    message = drop(office)
    assert (message.sender, message.to) == ('ada', 'desk')
    assert capsys.readouterr() == (f'{message.id}\n', '')


@pytest.mark.parametrize('offline', ['crew', 'office', 'both', 'office-only'])
@pytest.mark.parametrize('no_wait', [False, True])
def test_service_down_checks_both_ends_before_filing(shell, capsys, monkeypatch, offline, no_wait):
    office = declare(shell.project / 'office', kind='office')
    origin = office if offline == 'office-only' else declare(shell.project / 'ships' / 'crew', human=False)
    down = [origin, office] if offline == 'both' else [origin if offline == 'crew' else office]
    cli.service.alive.side_effect = lambda runtime: runtime.parent not in down
    input_text(monkeypatch)
    flags = ['-n'] if no_wait else []
    assert cli.main([*flags, 'Start here']) == (0 if no_wait else 2)
    out, err = capsys.readouterr()
    if no_wait:
        assert out == f'{drop(origin).id}\n' and not err
        cli.service.alive.assert_not_called()
    else:
        assert not out and not (origin / 'runtime').exists()
        for folder in down:
            assert f'the {folder.name} service; start it with {shlex.join(["qd", "--ship", str(folder), "up"])}' in err
        assert [call.args[0] for call in cli.service.alive.call_args_list] == [
            folder / 'runtime' for folder in dict.fromkeys([origin, office])]
    if origin != office:
        assert not (office / 'runtime').exists()


def test_two_desks_on_the_acting_ship_are_refused_by_name(shell, capsys, monkeypatch):
    crew = declare(shell.project / 'ships' / 'crew')
    declaration = crew / 'ship.toml'
    text = declaration.read_text()
    declaration.write_text(text + '\n[seats.second-desk]' + text.split('[seats.desk]', 1)[1])
    input_text(monkeypatch, 'Please review\n')
    assert cli.main([]) == 2
    out, err = capsys.readouterr()
    assert not out and 'more than one desk on crew (desk, second-desk)' in err
    assert 'qd -t <seat>' in err
    assert not (crew / 'runtime').exists()


@pytest.mark.parametrize(('role', 'human'), [('desk', False), ('member', True)])
def test_no_eligible_desk_names_the_acting_ship_and_the_remedies(shell, capsys, monkeypatch, role, human):
    crew = declare(shell.project / 'ships' / 'crew', role=role, human=human)
    peer = declare(shell.project / 'ships' / 'peer')
    (shell.project / 'ships/.default').write_text('crew\n')
    input_text(monkeypatch, 'Please review\n')
    assert cli.main([]) == 2
    out, err = capsys.readouterr()
    assert not out and 'no desk on crew' in err
    assert 'role = "desk"' in err and 'human = true' in err and 'qd -t <seat>' in err
    assert not (crew / 'runtime').exists() and not (peer / 'runtime').exists()


@pytest.mark.parametrize(('content', 'reason'), [
    (b'', 'declare kind = "office"'),
    (b'bad toml', 'not valid TOML'),
    (b'\xff', 'UnicodeDecodeError'),
    (PermissionError, 'PermissionError'),
    (None, 'needs one seat named desk'),
])
def test_invalid_office_declaration_names_its_file_and_refuses_before_filing(shell, capsys, monkeypatch,
                                                                           content, reason):
    crew = declare(shell.project / 'ships' / 'crew')
    office = declare(shell.project / 'office', kind='office', role='member')
    declaration = office / 'ship.toml'
    if content is PermissionError:
        read_text = type(declaration).read_text

        def read(path, *args, **kwargs):
            if path == declaration:
                raise PermissionError
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(type(declaration), 'read_text', read)
    elif content is not None:
        declaration.write_bytes(content)
    input_text(monkeypatch, 'Please review\n')
    assert cli.main([]) == 2
    out, err = capsys.readouterr()
    assert not out and str(declaration) in err and reason in err
    assert not (crew / 'runtime').exists() and not (office / 'runtime').exists()


@pytest.mark.parametrize('argv', [[], [' \t'], ['--json']])
def test_no_input_still_shows_help(shell, capsys, monkeypatch, argv):
    input_text(monkeypatch)
    assert cli.main(argv) == 0
    out, err = capsys.readouterr()
    assert out.splitlines()[0] == USAGE
    assert out.splitlines()[1].startswith('usage: qd ')
    assert not err


@pytest.mark.parametrize('source', ['closed pipe', 'empty file', 'whitespace'])
def test_empty_attachment_on_its_own_is_not_sent(shell, capsys, monkeypatch, tmp_path, source):
    office = declare(shell.project / 'office', kind='office')
    if source == 'closed pipe':
        reader, writer = os.pipe()
        os.close(writer)
    else:
        path = tmp_path / 'attachment'
        path.write_text(' \n' if source == 'whitespace' else '')
        reader = os.open(path, os.O_RDONLY)
    with os.fdopen(reader) as stream:
        monkeypatch.setattr(cli.sys, 'stdin', stream)
        assert cli.main([]) == 2
    out, err = capsys.readouterr()
    assert not out and err == 'qd: nothing to send\n'
    assert not list((office / 'runtime').glob('post/drop/*/*.json'))


@pytest.mark.parametrize(('argv', 'diagnostic'), [([], 'nothing to send'), (['-n', 'Look here'], 'stdin gave no byte')])
def test_initial_input_timeout_does_not_send(shell, capsys, monkeypatch, argv, diagnostic):
    office = declare(shell.project / 'office', kind='office')
    input_text(monkeypatch, timeout=True)
    assert cli.main(argv) == 2
    out, err = capsys.readouterr()
    assert not out and len(err.splitlines()) == 1
    assert diagnostic in err
    assert not list((office / 'runtime').glob('post/drop/*/*.json'))


@pytest.mark.parametrize('mode', ['launch environment', 'session recognition'])
def test_seat_never_reaches_input_or_routing(shell, capsys, monkeypatch, mode):
    folder = declare(shell.project / 'ships' / 'crew')
    if mode == 'launch environment':
        monkeypatch.setenv('QD_SEAT', 'worker')
    else:
        shell.identity.return_value = SimpleNamespace(refusal=Mock(return_value='Use qd post as the seat'))
    read = Mock(side_effect=AssertionError('read stdin'))
    route = Mock(side_effect=AssertionError('routed request'))
    monkeypatch.setattr(desk_request, 'read_stdin', read)
    monkeypatch.setattr(desk_request, 'destination', route)
    assert cli.main([]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines()[1].startswith('usage: qd ') and err == ''
    assert cli.main(['Look at this']) == 2
    out, err = capsys.readouterr()
    assert not out and 'qd post' in err
    assert cli.main(['-t', 'desk', 'hello']) == 2
    assert 'qd post' in capsys.readouterr().err
    read.assert_not_called()
    route.assert_not_called()
    if mode == 'launch environment':
        shell.identity.assert_not_called()
    else:
        assert shell.identity.call_args.args[0].folder == folder


@pytest.mark.parametrize(('argv', 'code'), [(['psot'], 2), (['Look', 'here'], 2), ([''], 2),
                                           (['why?'], 2), (['--help'], 0), (['--version'], 0)])
def test_other_argv_follows_the_existing_parser(shell, capsys, monkeypatch, argv, code):
    read = Mock(side_effect=AssertionError('read stdin'))
    monkeypatch.setattr(desk_request, 'read_stdin', read)
    with pytest.raises(SystemExit) as result:
        cli.main(argv)
    assert result.value.code == code
    out, err = capsys.readouterr()
    if code:
        assert 'invalid choice' in err and err.endswith(desk_request.QUOTE_HINT + '\n')
    if argv == ['--help']:
        assert out.splitlines()[0] == USAGE
        assert out.splitlines()[1].startswith('usage: qd ')
        help_text = ' '.join(out.split())
        assert 'first byte or EOF within 5 seconds' in help_text
        assert 'Stdin reading is limited to 10 minutes total' in help_text
        assert ('The UTF-8 serialized post, including its working directory and message fields, '
                'is limited to 1,048,576 bytes') in help_text
        assert 'timeout 10m qd "check the build" </dev/null' in out
        assert 'git diff >"$patch" && qd "review this" <"$patch"' in out
        assert 'qd "..." </dev/null' in out
        for code, label in enumerate(('Report received', 'Question received', 'Order not sent', 'Order filed but waiting')):
            assert f'  {code}  {label}' in out
    read.assert_not_called()
    shell.identity.assert_not_called()


@pytest.mark.parametrize(('argv', 'diagnostic', 'status'), [
    (['-n', '--unknown', 'hello'], 'unrecognized argument', 2),
    (['-t', 'a', '--to=b', 'hello'], 'only once', 2),
    (['--unknown', 'two words'], 'error:', 2),
    (['-n', '--help'], 'usage: qd', 0),
    (['-t', 'desk', '--version'], 'qd ', 0),
])
def test_leading_terminal_options_do_not_send(shell, capsys, monkeypatch, argv, diagnostic, status):
    read = Mock(side_effect=AssertionError('read stdin'))
    monkeypatch.setattr(desk_request, 'read_stdin', read)
    with pytest.raises(SystemExit) as result:
        cli.main(argv)
    assert result.value.code == status
    out, err = capsys.readouterr()
    assert diagnostic in (err if status else out)
    read.assert_not_called()


@pytest.mark.parametrize(('stdin', 'reason'), [
    ('"' * 256, 'post limit'),
    ('\udcff', 'must be UTF-8'),
    (ShipError('stdin must be UTF-8'), 'stdin must be UTF-8'),
    (ShipError('stdin did not reach EOF'), 'did not reach EOF'),
    (ShipError('cannot read stdin'), 'cannot read stdin'),
])
def test_failed_input_never_publishes_even_when_a_sentence_was_supplied(shell, capsys, monkeypatch, stdin, reason):
    office = declare(shell.project / 'office', kind='office')
    monkeypatch.setattr(post, 'DROP_MAX_BYTES', 500)
    fake = Mock(side_effect=stdin) if isinstance(stdin, Exception) else Mock(return_value=(stdin, False))
    monkeypatch.setattr(desk_request, 'read_stdin', fake)
    assert cli.main(['Review this']) == 2
    out, err = capsys.readouterr()
    assert not out and reason in err
    assert not list((office / 'runtime').glob('post/drop/*/*.json'))


@pytest.mark.parametrize('interrupted_at', ['wait', 'read'])
def test_interrupt_during_input_restores_blocking_mode_and_never_publishes(shell, capsys, monkeypatch, interrupted_at):
    office = declare(shell.project / 'office', kind='office')
    read_stdin = desk_request.read_stdin

    def interrupted_read(_):
        with monkeypatch.context() as patch:
            stream = Input(patch, [(4.5, b'partial input'), (20, KeyboardInterrupt())])
            if interrupted_at == 'wait':
                patch.setattr(desk_request.select, 'select', Mock(side_effect=KeyboardInterrupt))
            try:
                return read_stdin(stream, clock=lambda: stream.now)
            finally:
                assert stream.blocking

    monkeypatch.setattr(desk_request, 'read_stdin', interrupted_read)
    assert cli.main(['Review this']) == 130
    assert capsys.readouterr() == ('', 'qd: interrupted while reading stdin\n')
    assert not list((office / 'runtime').glob('post/drop/*/*.json'))


@pytest.mark.parametrize(('argv', 'body', 'json_output'), [
    (['Have a look'], 'Have a look', False),
    ([], '', False),
    (['-t', 'desk', 'hello'], 'hello', False),
    (['--to=desk', '--', '--json', 'please', 'review  this'], '--json please review  this', False),
    (['--', '-leading'], '-leading', False),
    (['--', 'status'], 'status', False),
    (['-t', 'desk', 'status'], 'status', False),
    (['-t', 'desk', 'text', '--help', '--unknown'], 'text --help --unknown', False),
    (['Please check', '--help'], 'Please check --help', False),
    (['-'], '-', False),
    (['--json', 'Have a look'], 'Have a look', True),
    (['--json'], '', True),
])
def test_waiting_request_prints_only_its_reply(shell, capsys, monkeypatch, argv, body, json_output):
    crew = declare(shell.project / 'ships' / 'crew')
    store = post.Store(crew / 'runtime')
    input_text(monkeypatch, 'attachment\n')
    captured = []

    def respond(interval):
        assert interval == desk_request.REPLY_INTERVAL and not captured
        order = load_drop(store)
        assert order.body == desk_request.body(body, 'attachment\n')
        reply = store.new('report', 'desk', 'ada', '  Finished.  \n\n', re=order.id)
        captured.extend([order, reply])

    on_reply_wait(monkeypatch, respond)
    assert cli.main(argv) == 0
    order, reply = captured
    out, err = capsys.readouterr()
    if json_output:
        assert json.loads(out) == dict(kind='report', id=reply.id, re=order.id, body=reply.body)
    else:
        assert out == '  Finished.  \n\n'
    assert err == f'sent to desk@crew ({order.id}); waiting for the reply\n'
    attempts = post.Attempts(crew / 'runtime')
    assert attempts.latest('ada') == {reply.id: 'watch'}
    assert attempts.undelivered(store, 'ada') == []


def test_one_word_and_global_verb_keep_the_command_parser(shell, capsys, monkeypatch):
    declare(shell.project / 'ships' / 'crew')
    run = Mock(return_value=cli.Reading({'verb': 'status'}, lambda data: data['verb']))
    monkeypatch.setattr(cli, 'cmd_status', run)
    input_text(monkeypatch, 'stdin for a request')
    for argv in (['status'], ['--json', 'status']):
        assert cli.main(argv) == 0
        out, err = capsys.readouterr()
        assert not err
        if '--json' in argv:
            assert json.loads(out)['verb'] == 'status'
        else:
            assert out.startswith('status')
    assert run.call_count == 2
    desk_request.read_stdin.assert_not_called()


def test_ship_flag_and_no_wait_json_preserve_directed_body_and_id(shell, capsys, monkeypatch):
    default = declare(shell.project / 'ships' / 'crew')
    scoped = declare(shell.project / 'ships' / 'scoped', role='member', human=False)
    (shell.project / 'ships/.default').write_text('crew\n')
    input_text(monkeypatch, 'file data\n')
    assert cli.main(['--json', '--ship', str(scoped), '--no-wait', '--to', 'desk', '--', ' -literal ', 'line']) == 0
    order = drop(scoped)
    assert order.body == desk_request.body(' -literal  line', 'file data\n')
    assert (order.to, order.sender) == ('desk', 'ada')
    out, err = capsys.readouterr()
    assert json.loads(out) == {'id': order.id} and not err
    assert not (default / 'runtime').exists()


@pytest.mark.parametrize(('address', 'diagnostic'), [
    ('missing', "no seat 'missing' to address"),
    ('desk@missing', "no ship 'missing' in this project"),
    ('other@crew', "no seat 'other' in ship crew"),
    ('desk@@crew', 'invalid post address'),
])
def test_directed_request_reuses_ordinary_post_validation(shell, capsys, monkeypatch, address, diagnostic):
    crew = declare(shell.project / 'ships' / 'crew')
    input_text(monkeypatch)
    assert cli.main(['-t', address, 'hello']) == 2
    assert cli.main(['post', 'order', '--to', address, 'hello']) == 2
    out, err = capsys.readouterr()
    assert not out and sum(diagnostic in line for line in err.splitlines()) == 2
    assert not list((crew / 'runtime').glob('post/drop/*/*.json'))


def test_down_seat_still_receives_the_request(shell, capsys, monkeypatch):
    crew = declare(shell.project / 'ships' / 'crew')
    store = post.Store(crew / 'runtime')
    input_text(monkeypatch)
    cli.seat_of.return_value.is_up = False

    def respond(interval):
        order = load_drop(store)
        store.new('report', 'desk', 'ada', 'I am back', re=order.id)

    on_reply_wait(monkeypatch, respond)
    assert cli.main(['-t', 'desk', 'hello']) == 0
    out, err = capsys.readouterr()
    assert out == 'I am back\n'
    assert err == f'sent to desk@crew ({load_drop(store).id}); waiting for the reply\n'


def test_reply_waiters_use_the_store_while_inbox_has_their_replies(shell, capsys, monkeypatch):
    crew = declare(shell.project / 'ships' / 'crew')
    runtime = crew / 'runtime'
    store = post.Store(runtime)
    input_text(monkeypatch)
    first = second = question = report = None

    def acknowledge_first():
        nonlocal first
        first = load_drop(store)
        assert first.id in capsys.readouterr().err
        store.new('ack', 'desk', 'ada', 'claimed', re=first.id)

    def start_second():
        assert cli.main(['-t', 'desk', 'second']) == 0
        out, err = capsys.readouterr()
        assert out == 'Second report\n' and not err

    def reply_to_both():
        nonlocal second, question, report
        second = load_drop(store)
        assert first.id != second.id
        assert second.id in capsys.readouterr().err
        child = store.new('order', 'desk', 'ada', 'Child order', re=first.id)
        store.new('report', 'desk', 'ada', 'Report on the child', re=child.id)
        store.new('question', 'desk', 'ship', 'To the silent sink?', re=first.id)
        store.new('report', 'ada', 'ada', 'Wrong sender', re=first.id)
        question = store.new('question', 'desk', 'ada', 'First question?', re=first.id)
        report = store.new('report', 'desk', 'ada', 'Second report', re=second.id)
        assert cli.main(['inbox', '--wait']) == 0
        seen = capsys.readouterr().out
        assert 'First question?' in seen and 'Second report' in seen
        store.new('report', 'desk', 'ada', 'Unrelated notice')

    steps = iter([acknowledge_first, start_second, reply_to_both])
    on_reply_wait(monkeypatch, lambda interval: next(steps)())
    assert cli.main(['First request']) == 1
    out, err = capsys.readouterr()
    assert out == 'First question?\n'
    assert err == f'question {question.id} from desk@crew; qd post answer --re {question.id} "your answer"\n'
    attempts = post.Attempts(runtime)
    for reply in question, report:
        assert [a.detail for a in attempts.for_message(reply.id)] == ['printed by qd inbox --wait',
                                                                    desk_request.PRINTED_REPLY]
    assert cli.main(['--json', 'inbox']) == 0
    reading = json.loads(capsys.readouterr().out)
    bodies = {m['body'] for m in reading['open'] + reading['received']}
    assert {'Unrelated notice', 'Wrong sender'} <= bodies
    assert {'First question?', 'Second report'}.isdisjoint(bodies)
    assert cli.main(['inbox', '--all']) == 0
    assert {'First question?', 'Second report'} <= set(capsys.readouterr().out.splitlines())
    assert cli.main(['thread', first.id]) == 0
    assert 'First question?' in capsys.readouterr().out


@pytest.mark.parametrize('route', ['default desk', 'direct', 'forwarded'])
def test_waiting_reply_crosses_the_fleet_to_the_origin_operator(shell, capsys, monkeypatch, route):
    crew_folder = declare(shell.project / 'ships' / 'crew', role='member', human=False)
    if route != 'default desk':
        other = declare(shell.project / 'ships' / 'other', role='member', human=False)
        (shell.project / 'ships/.default').write_text('crew\n')
        argv = ['-t', 'desk@other', 'hello', 'there']
    else:
        other = declare(shell.project / 'office', kind='office')
        argv = ['hello there']
    if route == 'forwarded':
        declaration = crew_folder / 'ship.toml'
        declaration.write_text(declaration.read_text() + '\n[forward]\nretired = "desk@other"\n')
        argv = ['-t', 'retired', 'hello', 'there']
    file = other / 'ship.toml'
    file.write_text(file.read_text().replace('"Ada"', '"Grace"'))
    crew, destination = shipmod.load(crew_folder), shipmod.load(other)
    store, foreign = post.Store(crew.runtime), post.Store(destination.runtime)
    input_text(monkeypatch, 'work in progress\n')
    observed = []

    def exchange(interval):
        assert not observed
        assert drop(crew_folder).to == ('retired' if route == 'forwarded' else f'desk@{other.name}')
        order = load_drop(store)
        assert order.body == desk_request.body('hello there', 'work in progress\n')
        assert order.to == f'desk@{other.name}'
        assert fleet_post.run(crew, store) == [] and fleet_post.run(destination, foreign) == []
        received, = foreign.for_seat('desk')
        assert (received.id, received.sender) == (order.id, 'ada@crew')
        reply = foreign.new('report', 'desk', received.sender, 'Done here', re=received.id)
        assert fleet_post.run(destination, foreign) == [] and fleet_post.run(crew, store) == []
        observed.extend([order.id, reply.id])

    on_reply_wait(monkeypatch, exchange)
    assert cli.main(argv) == 0
    order_id, reply_id = observed
    out, err = capsys.readouterr()
    assert out == 'Done here\n'
    assert err == f'sent to desk@{other.name} ({order_id}); waiting for the reply\n'
    for identifier in observed:
        assert (store.dir / f'{identifier}.json').read_bytes() == (foreign.dir / f'{identifier}.json').read_bytes()
    assert desk_request.printed_ids(crew.runtime, 'ada') == {reply_id}
    assert not desk_request.printed_ids(destination.runtime, 'grace')


@pytest.mark.parametrize('prefix', [[], ['-n']])
@pytest.mark.parametrize('address', ['review', 'review@crew'])
def test_terminal_address_of_a_declared_group_requires_explicit_post(shell, capsys, monkeypatch, prefix, address):
    crew = declare(shell.project / 'ships' / 'crew')
    declaration = crew / 'ship.toml'
    contents = declaration.read_text()
    declaration.write_text(contents + '\n[seats.member]' + contents.split('[seats.desk]', 1)[1]
                           + '\n[groups.review]\nlead = "desk"\nmembers = ["member"]\n')
    input_text(monkeypatch)
    assert cli.main([*prefix, '-t', address, 'hello']) == 2
    out, err = capsys.readouterr()
    assert not out and f'qd post order --to {address}' in err
    assert not list((crew / 'runtime').glob('post/drop/*/*.json'))


def test_report_filed_before_wait_closes_the_order_even_if_addressed_elsewhere(shell, capsys, monkeypatch):
    crew = declare(shell.project / 'ships' / 'crew')
    store = post.Store(crew / 'runtime')
    input_text(monkeypatch)
    original_publish = post.write_drop

    def publish(*args, **kwargs):
        path = original_publish(*args, **kwargs)
        order = load_drop(store)
        store.new('report', 'desk', 'ship', 'Already filed.\n', re=order.id)
        return path

    monkeypatch.setattr(post, 'write_drop', publish)
    on_reply_wait(monkeypatch, Mock(side_effect=AssertionError('blocked before checking the store')))
    assert cli.main(['Please do this']) == 0
    assert capsys.readouterr().out == 'Already filed.\n'


def test_terminal_reply_can_fall_back_from_an_unavailable_index(shell, capsys, monkeypatch):
    crew = declare(shell.project / 'ships' / 'crew')
    store = post.Store(crew / 'runtime')
    input_text(monkeypatch)
    for method in ('has_short', 'replies'):
        monkeypatch.setattr(post_index.MessageIndex, method, Mock(side_effect=post_index.IndexUnavailable))

    def respond(interval):
        order = load_drop(store)
        store.new('report', 'desk', 'ada', 'Done with the fallback', re=order.id)

    on_reply_wait(monkeypatch, respond)
    assert cli.main(['Please do this']) == 0
    assert capsys.readouterr().out == 'Done with the fallback\n'


def test_failed_wait_reports_the_filed_id_and_inbox(shell, capsys, monkeypatch):
    crew = declare(shell.project / 'ships' / 'crew')
    input_text(monkeypatch)
    monkeypatch.setattr(desk_request, 'wait_for_reply', Mock(side_effect=OSError('cannot read reply')))
    assert cli.main(['Please do this']) == 3
    out, err = capsys.readouterr()
    assert not out
    sent, failed = err.splitlines()
    identifier = drop(crew).id
    assert identifier in sent and identifier in failed
    assert 'cannot read reply' in failed and 'the reply will be in qd inbox' in failed


def test_interrupting_wait_leaves_the_sent_order_and_unprinted_reply(shell, capsys, monkeypatch):
    crew = declare(shell.project / 'ships' / 'crew')
    store = post.Store(crew / 'runtime')
    input_text(monkeypatch)

    def interrupt(interval):
        order = load_drop(store)
        store.new('report', 'desk', 'ada', 'Still in the inbox', re=order.id)
        raise KeyboardInterrupt

    on_reply_wait(monkeypatch, interrupt)
    assert cli.main(['--ship', str(crew), 'Please finish this']) == 130
    order = load_drop(store)
    out, err = capsys.readouterr()
    assert not out
    sent, stopped = err.splitlines()
    assert order.id in sent and order.id in stopped
    assert 'the reply will be in qd inbox' in stopped
    assert shlex.split(stopped.split('; use ', 1)[1]) == ['qd', '--ship', str(crew), 'inbox']
    assert post.Attempts(crew / 'runtime').all() == []
    assert cli.main(['inbox', '--wait']) == 0
    assert 'Still in the inbox' in capsys.readouterr().out


def test_closed_stdout_leaves_the_drop_published_without_waiting(shell, monkeypatch):
    crew = declare(shell.project / 'ships' / 'crew')
    input_text(monkeypatch)
    monkeypatch.setattr(cli.closed_pipe, 'silence', Mock())
    reader, writer = os.pipe()
    os.close(reader)
    with os.fdopen(writer, 'w') as broken, monkeypatch.context() as patch:
        patch.setattr(cli.sys, 'stdout', broken)
        assert cli.main(['Please do this']) == 141
    assert drop(crew).body == desk_request.body('Please do this', '')


def test_shell_signal_keeps_its_usual_status_after_filing(shell):
    crew = declare(shell.project / 'ships' / 'crew')
    caller = f'''
import os, signal, sys
from types import SimpleNamespace
sys.path.insert(0, {str(Path(cli.__file__).parents[1])!r})
from quarterdeck import main, cli, desk_request, post_sender
post_sender.seat_session = lambda ship: None
cli.service.alive = lambda runtime: True
cli.seat_of = lambda ship, seat: SimpleNamespace(is_up=True)
wait_for_reply = desk_request.wait_for_reply
desk_request.wait_for_reply = lambda *args: wait_for_reply(*args, pause=lambda _: os.kill(os.getpid(), signal.SIGTERM))
raise SystemExit(main())
'''
    proc = subprocess.run([sys.executable, '-c', caller, '--ship', str(crew), 'Keep this job'],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True)
    order = drop(crew)
    assert proc.returncode == -signal.SIGTERM
    assert proc.stdout == ''
    assert proc.stderr == f'sent to desk@crew ({order.id}); waiting for the reply\n'
    assert order.body == desk_request.body('Keep this job', '')


def test_shell_output_failure_preserves_the_filed_status(shell):
    crew = declare(shell.project / 'ships' / 'crew')
    caller = f'''
import sys
sys.path.insert(0, {str(Path(cli.__file__).parents[1])!r})
from quarterdeck import main, cli, desk_request, post, post_sender
post_sender.seat_session = lambda ship: None
cli.service.alive = lambda runtime: True

def reply(store, message, sender, recipient, stream):
    post.file_drops(store)
    return store.new('report', recipient, sender, 'This output cannot flush', re=message.id)

desk_request.wait_for_reply = reply
raise SystemExit(main())
'''
    with open('/dev/full', 'w') as full:
        proc = subprocess.run([sys.executable, '-c', caller, '--ship', str(crew), 'Keep this job'],
                              stdin=subprocess.DEVNULL, stdout=full, stderr=subprocess.PIPE, text=True)
    assert proc.returncode == 3
    order, reply = post.Store(crew / 'runtime').all()
    assert reply.re == order.id
    sent, failed = proc.stderr.splitlines()
    assert order.id in sent and order.id in failed
    assert 'the reply will be in qd inbox' in failed
    assert not desk_request.printed_ids(crew / 'runtime', 'ada')


@pytest.mark.parametrize('failure', [BrokenPipeError, OSError, KeyboardInterrupt])
def test_reply_is_unread_when_stdout_cannot_finish(shell, capsys, monkeypatch, failure):
    crew = declare(shell.project / 'ships' / 'crew')
    store = post.Store(crew / 'runtime')
    input_text(monkeypatch)

    def respond(interval):
        order = load_drop(store)
        store.new('report', 'desk', 'ada', 'An unread reply', re=order.id)

    class FailedFlush:
        closed = False

        def write(self, document):
            assert document == 'An unread reply\n'
            return len(document)

        def flush(self):
            raise failure('cannot finish stdout')

    on_reply_wait(monkeypatch, respond)
    with monkeypatch.context() as patch:
        patch.setattr(cli.sys, 'stdout', FailedFlush())
        patch.setattr(output.closed_pipe, 'silence', Mock())
        assert cli.main(['Do this task']) == {BrokenPipeError: 141, OSError: 3, KeyboardInterrupt: 130}[failure]
    assert post.Attempts(crew / 'runtime').all() == []
    capsys.readouterr()
    assert cli.main(['inbox', '--wait']) == 0
    assert 'An unread reply' in capsys.readouterr().out
