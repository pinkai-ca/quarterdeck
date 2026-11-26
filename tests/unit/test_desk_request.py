"""The operator's terminal shortcut, using synthetic input and seats."""

from __future__ import annotations

import errno
import shlex
import stat
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quarterdeck import cli, desk_request, post, post_sender, standing
from quarterdeck.ship import ShipError


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
    return SimpleNamespace(project=project, workspace=workspace, identity=identity)


def input_text(monkeypatch, stdin='', timeout=False):
    monkeypatch.setattr(desk_request, 'read_stdin', Mock(return_value=(stdin, timeout)))


def drop(folder):
    file, = (folder / 'runtime/post/drop/ada').glob('*.json')
    return post.Message.from_json(file.read_text())


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
    assert cli.main(argv) == 0
    message = drop(crew)
    assert (message.kind, message.sender, message.to, message.subject) == ('order', 'ada@crew', 'desk@office', None)
    assert message.body == f'Working directory: {shell.workspace}\n\n{message_body}'
    assert not (office / 'runtime').exists()
    out, err = capsys.readouterr()
    assert err == '' and out == f'Posted {message.id} to desk@office · see the reply with qd inbox --wait\n'
    cli.service.alive.assert_called_once_with(crew / 'runtime')


@pytest.mark.parametrize('empty_office', [False, True])
def test_only_the_acting_ship_is_considered_when_the_office_declaration_is_absent(shell, capsys, monkeypatch,
                                                                                 empty_office):
    earlier = declare(shell.project / 'ships' / 'first')
    other = declare(shell.project / 'ships' / 'second')
    (shell.project / 'ships/.default').write_text('first\n')
    if empty_office:
        (shell.project / 'office').mkdir()
    input_text(monkeypatch)
    assert cli.main(['Take a look']) == 0
    assert shell.identity.call_args.args[0].folder == earlier
    order = drop(earlier)
    assert (order.sender, order.to) == ('ada', 'desk')
    out, err = capsys.readouterr()
    assert not err and out == f'Posted {order.id} to desk@first · see the reply with qd inbox --wait\n'
    assert not (other / 'runtime').exists()
    assert not (shell.project / 'office' / 'runtime').exists()

    monkeypatch.setenv('QD_SHIP', str(other))
    assert cli.main(['A second request']) == 0
    order = drop(other)
    assert (order.sender, order.to) == ('ada', 'desk')
    out, err = capsys.readouterr()
    assert not err and out == f'Posted {order.id} to desk@second · see the reply with qd inbox --wait\n'


def test_separate_office_takes_precedence_over_the_acting_ships_desk(shell, capsys, monkeypatch):
    crew = declare(shell.project / 'ships' / 'crew')
    office = declare(shell.project / 'office', kind='office')
    input_text(monkeypatch)
    assert cli.main(['Take a look']) == 0
    order = drop(crew)
    assert (order.sender, order.to) == ('ada@crew', 'desk@office')
    out, err = capsys.readouterr()
    assert not err and out == f'Posted {order.id} to desk@office · see the reply with qd inbox --wait\n'
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
def test_initialized_office_posts_locally_and_scopes_only_when_needed(shell, capsys, monkeypatch, explicit):
    office = declare(shell.project / 'office', kind='office')
    if explicit:
        monkeypatch.setenv('QD_SHIP', str(office))
    input_text(monkeypatch)
    assert cli.main(['Start here']) == 0
    message = drop(office)
    assert (message.sender, message.to) == ('ada', 'desk')
    out, err = capsys.readouterr()
    proof, command = out.strip().split(' · see the reply with ', 1)
    assert not err and proof == f'Posted {message.id} to desk@office'
    flags = [] if explicit else ['--ship', str(office)]
    assert shlex.split(command) == ['qd', *flags, 'inbox', '--wait']


@pytest.mark.parametrize('scoped', [False, True])
def test_origin_down_files_the_order_and_points_to_the_correct_start_command(shell, capsys, monkeypatch, scoped):
    office = declare(shell.project / 'office', kind='office')
    origin = office if scoped else declare(shell.project / 'ships' / 'crew', human=False)
    cli.service.alive.return_value = False
    input_text(monkeypatch)
    assert cli.main(['Start here']) == 0
    message = drop(origin)
    out, err = capsys.readouterr()
    assert not err and len(out.splitlines()) == 1
    proof, delivery, start, inbox = out.strip().split(' · ')
    assert proof == f'Filed {message.id} for desk@office'
    assert delivery == f'delivery waits for the {origin.name} service'
    flags = ['--ship', str(office)] if scoped else []
    assert shlex.split(start.removeprefix('start it with ')) == ['qd', *flags, 'up']
    assert shlex.split(inbox.removeprefix('see the reply with ')) == ['qd', *flags, 'inbox', '--wait']
    cli.service.alive.assert_called_once_with(origin / 'runtime')
    if not scoped:
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
    assert 'qd post order --to <seat>' in err
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
    assert 'role = "desk"' in err and 'human = true' in err and 'qd post order --to <seat>' in err
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


@pytest.mark.parametrize(('argv', 'stdin', 'timeout'), [([], '', False), ([], ' \n\t', False), ([], '', True),
                                                        ([' \t'], '', False)])
def test_no_input_still_shows_help(shell, capsys, monkeypatch, argv, stdin, timeout):
    input_text(monkeypatch, stdin, timeout)
    assert cli.main(argv) == 0
    out, err = capsys.readouterr()
    assert out.splitlines()[0] == 'Send a request to the desk with qd "a sentence" or echo text | qd'
    assert out.splitlines()[1].startswith('usage: qd ')
    assert ('stdin gave no byte or EOF' in err) == timeout
    assert len(err.splitlines()) == int(timeout)


def test_initial_input_timeout_can_still_send_the_sentence(shell, capsys, monkeypatch):
    office = declare(shell.project / 'office', kind='office')
    input_text(monkeypatch, timeout=True)
    assert cli.main(['Look here']) == 0
    assert drop(office).body.endswith('\n\nLook here')
    out, err = capsys.readouterr()
    assert len(out.splitlines()) == len(err.splitlines()) == 1
    assert 'stdin gave no byte or EOF within 5 seconds' in err


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
    read.assert_not_called()
    route.assert_not_called()
    if mode == 'launch environment':
        shell.identity.assert_not_called()
    else:
        assert shell.identity.call_args.args[0].folder == folder


@pytest.mark.parametrize(('argv', 'code'), [(['psot'], 2), (['Look', 'here'], 2), ([''], 2),
                                           (['--json'], 0), (['--help'], 0), (['--version'], 0)])
def test_other_argv_follows_the_existing_parser(shell, capsys, monkeypatch, argv, code):
    read = Mock(side_effect=AssertionError('read stdin'))
    monkeypatch.setattr(desk_request, 'read_stdin', read)
    if argv == ['--json']:
        assert cli.main(argv) == code
    else:
        with pytest.raises(SystemExit) as result:
            cli.main(argv)
        assert result.value.code == code
    out, err = capsys.readouterr()
    if argv == ['psot']:
        assert 'invalid choice' in err and 'psot' in err
    if argv == ['--help']:
        assert out.splitlines()[0] == 'Send a request to the desk with qd "a sentence" or echo text | qd'
        assert out.splitlines()[1].startswith('usage: qd ')
        help_text = ' '.join(out.split())
        assert 'up to 5 seconds for its first byte or EOF' in help_text
        assert 'Stdin reading is limited to 10 minutes total' in help_text
        assert ('The UTF-8 serialized post, including its working directory and message fields, '
                'is limited to 1,048,576 bytes') in help_text
    read.assert_not_called()
    shell.identity.assert_not_called()


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
