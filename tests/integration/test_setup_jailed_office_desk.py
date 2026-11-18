"""Setup's office with a desk on a local model: the desk is jailed, so it is given a working folder of its own.

A desk on pi or OpenCode with an endpoint pool runs jailed, and the office setup writes declares no project
workspace: its programs would start in the office's own folder, which a jail shows read-only, and the jail refuses that.
So setup gives a jailed desk the office's own work/desk, made empty before its launch; a desk on a subscription program
is unchanged. When a start fails, setup says why in the refusal's own words, and what to do next.

Real setup, real tmux and real bubblewrap. Stood in: the endpoint's model listing, the programs (a stand-in that
answers its version and waits), the paid live check, and the installed copy (the checkout runs up in-process).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import cli, endpoint, harness, install, service, setup, ship as shipmod, setup_proposal
from quarterdeck import seat as seat_module
from quarterdeck.drill import Check
from quarterdeck.output import Reading
from quarterdeck.seat import Seat
from quarterdeck.tmux import Tmux

from .test_sandbox import namespaces_allowed

pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap is not installed, or cannot make a namespace here (inside a jail)")

QD = Path(sys.executable).parent / 'qd'
MODEL = 'qwen3.8-27b'
VERSIONS = {'pi': '0.79.0', 'opencode': '1.19.5'}


def stand_in(folder: Path, name: str) -> Path:
    """A program that answers its version and otherwise waits, as a seat's program does in its pane."""
    program = folder / name
    program.write_text(f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo {VERSIONS[name]}; exit 0; fi\nexec sleep 300\n')
    program.chmod(0o755)
    return program


@pytest.fixture
def newcomer(tmp_path, tmux: Tmux, monkeypatch):
    """A newcomer's project and machine: git identity, the fake endpoint's listing, stand-in programs on the PATH."""
    for name, value in {'GIT_AUTHOR_NAME': 'Setup Example', 'GIT_AUTHOR_EMAIL': 'setup@example.invalid',
                        'GIT_COMMITTER_NAME': 'Setup Example', 'GIT_COMMITTER_EMAIL': 'setup@example.invalid',
                        'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}.items():
        monkeypatch.setenv(name, value)
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'QD_SEAT', 'QD_SHIP'):
        monkeypatch.delenv(name, raising=False)
    project = tmp_path / 'project'
    (project / 'ships').mkdir(parents=True)
    workspace = tmp_path / 'my-app'
    workspace.mkdir()
    programs = tmp_path / 'programs'
    programs.mkdir()
    for name in VERSIONS:
        stand_in(programs, name)
    monkeypatch.setenv('PATH', os.pathsep.join([str(programs), str(QD.parent), os.environ.get('PATH', '')]))
    monkeypatch.setattr(seat_module, 'program_path', lambda: os.pathsep.join([str(programs), '/usr/bin', '/bin']))
    monkeypatch.setattr(endpoint, 'models', lambda url, timeout=0, **kwargs: [endpoint.Served(MODEL, 98304)])
    monkeypatch.setenv('QD_TMUX_SOCKET', tmux.socket)
    monkeypatch.setattr(install, 'prepare_up', lambda current: ([], False))
    monkeypatch.setattr(install, 'delegate_up', lambda *args, **kwargs: None)  # the checkout runs up itself
    template = setup.template

    def checkout_template(kind='default'):
        """The templates as shipped, run from this checkout as the drydock is: no installed copy is made here."""
        text, orders, _ = template(kind)
        text = shipmod.edit(text, 'ship', 'code', 'checkout')
        import tomllib
        return text, orders, tomllib.loads(text)
    monkeypatch.setattr(setup, 'template', checkout_template)
    calls = []

    class Operations(setup.Operations):
        def project_folder(self):
            return project

        def discover(self, defaults):
            return {name: (Check(name, name in VERSIONS, 'stand-in on PATH'), 'sign-in unknown')
                    for name in harness.NAMES if not harness.get(name).synthetic}

        def credential_present(self, home, filename):
            return False

        def readiness(self, current, selected):
            return [Check('readiness', True, 'synthetic')]

        def check(self, current, program):
            calls.append(('check', program))
            return [Check('check', True, 'synthetic: no paid live check here')]

        def status(self, current):
            return Reading({'service': 'ok', 'seats': [SimpleNamespace(name=name, activity=SimpleNamespace(state='idle'))
                                                       for name in current.seats]}, lambda _: ['synthetic status'])

        def access(self, current):
            return 'synthetic desk access'

    monkeypatch.setattr(setup, 'Operations', Operations)
    tmux.new_session('Synthetic bootstrap')
    tmux.kill_session('Synthetic bootstrap')
    started = []
    yield SimpleNamespace(project=project, workspace=workspace, tmux=tmux, calls=calls, started=started, tmp=tmp_path)
    for folder in (project / 'office', *sorted((project / 'ships').iterdir())):
        if (folder / 'runtime').is_dir():
            service.stop(shipmod.load(folder).runtime)


def jailed(argv: list[str]) -> bool:
    return any(Path(word).name == 'bwrap' for word in argv)


def bound(argv: list[str], folder: Path) -> bool:
    """Whether the jail binds the folder writable at its own path."""
    return any(argv[i:i + 3] == ['--bind', str(folder), str(folder)] for i in range(len(argv)))


def free_port() -> int:
    import socket
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def answers(newcomer, program: str) -> Path:
    port = free_port()
    path = newcomer.tmp / 'answers.json'
    path.write_text(json.dumps({
        'use_defaults': 'yes', 'name': 'Clonetest', 'workspace': str(newcomer.workspace), 'desk': program,
        # An unprivileged port: the jail's relay listens on it inside the jail; nothing answers it, the listing is
        # stood in.
        'provider': {program: 'local'}, 'endpoint': {program: f'http://127.0.0.1:{port}/v1'},
        'key_env': {program: 'none'},
        'model': {program: MODEL},
        'seats': {name: {'harness': program, 'model': MODEL, **({'effort': 'medium'} if program == 'pi' else {})}
                  for name in ('captain', 'engineering')},  # OpenCode takes no effort
        'accept': 'yes', 'check': 'yes', 'start': 'yes'}))
    return path


@pytest.mark.parametrize('program', ['pi', 'opencode'])
def test_setup_brings_a_jailed_office_desk_up_in_its_own_work_folder_and_the_crew_follows(newcomer, capsys, program):
    code = cli.main(['setup', '--answers', str(answers(newcomer, program))])
    out = capsys.readouterr()
    assert code == 0, out.err[-3000:] + out.out[-3000:]
    office = shipmod.load(newcomer.project / 'office')
    desk = office.seat('desk')
    assert desk.harness == program and office.jailed(desk)
    work = newcomer.project / 'office' / 'work' / 'desk'
    assert desk.cwd == work, 'the jailed desk works in its own folder under the office\'s work/'
    info = work.lstat()
    assert stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not any(work.iterdir())
    assert '/work/' in (newcomer.project / 'office' / '.gitignore').read_text().splitlines()
    assert shipmod._git(office.folder, 'status', '--porcelain') == '', 'the work folder is ignored, the office clean'
    launched = Seat(office, desk, newcomer.tmux).launched()
    assert launched is not None and jailed(launched.argv), 'the desk runs jailed (behind its endpoint relay)'
    assert bound(launched.argv, work), 'its own folder bound writable, at its own path'
    assert not bound(launched.argv, office.folder), 'the office itself is never bound writable'
    crew = shipmod.load(next(f for f in (newcomer.project / 'ships').iterdir() if (f / 'ship.toml').is_file()))
    for name, spec in crew.seats.items():
        seat = Seat(crew, spec, newcomer.tmux)
        assert seat.launched() is not None, f'{name} of the crew was started after the desk'
        assert jailed(seat.launched().argv) and bound(seat.launched().argv, newcomer.workspace)


def test_a_desk_on_a_subscription_program_keeps_the_offices_own_folder(tmp_path):
    """separate_office alone: a desk that is not jailed is given no working folder; the office is unchanged."""
    text, orders, _ = setup.template('default')
    office = setup.template('office')[0]
    proposal = setup_proposal.Proposal(shipmod._from_text(text, tmp_path / 'ships' / 'crew'), text, orders, [], None,
                                       'new')
    separated = setup_proposal.separate_office(proposal, tmp_path / 'office')
    assert separated.office.seat('desk').cwd is None and not separated.office.jailed(separated.office.seat('desk'))
    assert 'cwd' not in separated.office_text.split('[seats.desk]', 1)[1].split('[', 1)[0]
    assert office  # the template is read, not changed


def test_a_refused_start_names_its_own_words_and_the_next_step(tmp_path):
    """The installed copy's refusal travels up whole; the next step is one that can answer before a ship exists."""
    ship = SimpleNamespace(folder=tmp_path / 'office')
    said = install.refused_up(ship, 'something on stderr\nqd: desk: sandboxed with the ship folder as its working '
                                    'directory; a jailed seat reads the ship folder and cannot write it.\n')
    assert 'desk: sandboxed with the ship folder as its working directory' in said
    assert f'qd --ship {tmp_path / "office"} up' in said and 'qd status' not in said
    silent = install.refused_up(ship, '')
    assert 'without a reason' in silent and f'qd --ship {tmp_path / "office"} up' in silent


def test_setup_says_why_the_office_desk_did_not_start_and_what_to_do(newcomer, capsys, monkeypatch):
    def refuse(current):
        raise shipmod.ShipError('installed up refused: desk: synthetic refusal; no retry made; resolve that, then run '
                                f'qd --ship {current.folder} up')
    monkeypatch.setattr(setup.Operations, 'up_desk', lambda self, current: refuse(current))
    code = cli.main(['setup', '--answers', str(answers(newcomer, 'pi'))])
    out = capsys.readouterr()
    assert code == 2
    assert 'desk: synthetic refusal' in out.err and 'run qd init to start the desk' in out.err
    assert 'qd setup again for the crew' in out.err
    assert 'inspect qd status' not in out.err
    assert (newcomer.project / 'office' / 'ship.toml').is_file(), 'the office is kept for the next run'


def test_setup_run_again_after_a_refusal_keeps_the_desks_folder_private_and_its_own(newcomer, capsys, monkeypatch):
    """The flow a refusal advises: setup, refused at the desk's start; then qd init starts the desk. The desk's folder
    from the first run is kept with what is in it, and made private again however its mode was left."""
    real_up_desk = setup.Operations.up_desk
    monkeypatch.setattr(setup.Operations, 'up_desk', lambda self, current: (_ for _ in ()).throw(
        shipmod.ShipError('installed up refused: desk: synthetic refusal')))
    assert cli.main(['setup', '--answers', str(answers(newcomer, 'pi'))]) == 2
    work = newcomer.project / 'office' / 'work' / 'desk'
    assert work.is_dir() and stat.S_IMODE(work.stat().st_mode) == 0o700, 'made before the refused start'
    work.chmod(0o755)
    (work / 'left-by-the-desk.txt').write_text('synthetic')
    capsys.readouterr()
    monkeypatch.setattr(setup.Operations, 'up_desk', real_up_desk)
    code = cli.main(['init'])
    out = capsys.readouterr()
    assert code == 0, out.err[-3000:]
    assert stat.S_IMODE(work.lstat().st_mode) == 0o700 and not work.is_symlink()
    assert (work / 'left-by-the-desk.txt').read_text() == 'synthetic', 'its contents are the desk\'s, kept'
    office = shipmod.load(newcomer.project / 'office')
    launched = Seat(office, office.seat('desk'), newcomer.tmux).launched()
    assert launched is not None and jailed(launched.argv) and bound(launched.argv, work)


@pytest.mark.parametrize('planted', ['link', 'file', 'another user\'s'])
def test_a_desk_folder_that_is_not_the_operators_own_real_folder_is_refused(tmp_path, monkeypatch, planted):
    """A link, a file, or another user's folder is refused, and the other place is left as the fixture made it.

    mkdir keeps the caller's umask, so the fixture is made at umask 0 and then given its mode outright. The
    caller's umask is restored before the check. The refusal itself does not read the umask.
    """
    caller = os.umask(0)
    try:
        office_folder = tmp_path / 'office'
        office_folder.mkdir(mode=0o755)
        text = setup.template('office')[0]
        text = shipmod.edit(text, 'seats.desk', 'sandbox', True)
        text = shipmod.edit(text, 'seats.desk', 'cwd', setup.JAILED_DESK_WORK)
        office = shipmod._from_text(text, office_folder)
        work = office_folder / 'work' / 'desk'
        work.parent.mkdir(mode=0o755)
        elsewhere = tmp_path / 'elsewhere'
        elsewhere.mkdir(mode=0o755)
        elsewhere.chmod(0o755)
        if planted == 'link':
            work.symlink_to(elsewhere, target_is_directory=True)
        elif planted == 'file':
            work.write_text('not a folder')
            work.chmod(0o644)
        else:
            work.mkdir(mode=0o755)
            work.chmod(0o755)
            monkeypatch.setattr(os, 'getuid', lambda: os.stat(work).st_uid + 1)
    finally:
        os.umask(caller)
    with pytest.raises(shipmod.ShipError, match='is a link|not this user\'s own folder'):
        setup.make_jailed_work(office)
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755 and not any(elsewhere.iterdir()), 'nothing changed there'


def tree(folder: Path) -> dict[str, tuple]:
    """Every entry under ``folder`` as it stands, by kind, mode and content, but each ship's runtime/ and .git/: the
    runtime is the service's, never a declaration, and git refreshes its own index when it is read."""
    found = {}
    for path in sorted(folder.rglob('*')):
        parts = path.relative_to(folder).parts
        if any(part == '.git' for part in parts) or any(
                part == 'runtime' and (folder.joinpath(*parts[:i]) / 'ship.toml').is_file()
                for i, part in enumerate(parts)):
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            found[str(path.relative_to(folder))] = ('link', os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            found[str(path.relative_to(folder))] = ('folder', stat.S_IMODE(info.st_mode))
        else:
            found[str(path.relative_to(folder))] = ('file', stat.S_IMODE(info.st_mode), path.read_bytes())
    return found


def heads(project: Path) -> dict[str, str]:
    return {str(folder): shipmod._git(folder, 'rev-parse', 'HEAD')
            for folder in (project / 'office', *(project / 'ships').iterdir()) if (folder / 'ship.toml').is_file()}


def launches(project: Path, tmux: Tmux) -> dict[str, tuple | None]:
    found = {}
    for folder in (project / 'office', *(project / 'ships').iterdir()):
        if (folder / 'ship.toml').is_file():
            current = shipmod.load(folder)
            for name, spec in current.seats.items():
                launched = Seat(current, spec, tmux).launched()
                found[f'{current.name}/{name}'] = None if launched is None else (launched.pane, launched.pid)
    return found


@pytest.mark.parametrize('first', ['refused at the desk', 'complete'])
def test_setup_run_a_second_time_settles_as_the_first_run_left_things(newcomer, capsys, monkeypatch, first):
    """Setup run twice on one project, with the same answers. What the second run may change: each ship's runtime/
    (the service's, ignored by git); and, where the first stopped at the desk's refused start before any crew, the crew
    ship it then creates and the default ship's mark. Nothing else moves: the office's files, its work/desk with what
    the desk left there and its mode as left, every ship's commits, and every seat already up, kept in its own pane."""
    real_up_desk = setup.Operations.up_desk
    if first == 'refused at the desk':
        monkeypatch.setattr(setup.Operations, 'up_desk', lambda self, current: (_ for _ in ()).throw(
            shipmod.ShipError('installed up refused: desk: synthetic refusal')))
    path = answers(newcomer, 'pi')
    code = cli.main(['setup', '--answers', str(path)])
    out = capsys.readouterr()
    assert code == (2 if first == 'refused at the desk' else 0), out.err[-3000:] + out.out[-3000:]
    monkeypatch.setattr(setup.Operations, 'up_desk', real_up_desk)
    work = newcomer.project / 'office' / 'work' / 'desk'
    (work / 'left-by-the-desk.txt').write_text('synthetic')
    work.chmod(0o750)  # as the operator left it: only a launch makes it private again, not setup
    crew_before = [f for f in (newcomer.project / 'ships').iterdir() if (f / 'ship.toml').is_file()]
    assert len(crew_before) == (0 if first == 'refused at the desk' else 1)
    office_before, heads_before = tree(newcomer.project / 'office'), heads(newcomer.project)
    project_before, launched_before = tree(newcomer.project), launches(newcomer.project, newcomer.tmux)
    assert launched_before['office/desk'] is None if first == 'refused at the desk' else launched_before['office/desk']

    code = cli.main(['setup', '--answers', str(path)])
    out = capsys.readouterr()
    assert code == 0, out.err[-3000:] + out.out[-3000:]

    assert tree(newcomer.project / 'office') == office_before, 'nothing of the office moves'
    assert stat.S_IMODE(work.lstat().st_mode) == 0o750 and (work / 'left-by-the-desk.txt').read_text() == 'synthetic'
    assert shipmod._git(newcomer.project / 'office', 'status', '--porcelain') == ''
    after = heads(newcomer.project)
    assert {folder: head for folder, head in after.items() if folder in heads_before} == heads_before
    launched = launches(newcomer.project, newcomer.tmux)
    assert {seat: at for seat, at in launched.items() if seat in launched_before} == launched_before, \
        'a seat already up stays in its own pane; a desk the first run did not start is left for qd init'
    crew = [f for f in (newcomer.project / 'ships').iterdir() if (f / 'ship.toml').is_file()]
    assert len(crew) == 1
    project_after = tree(newcomer.project)
    moved = (project_after.keys() ^ project_before.keys()) | {
        name for name in project_before.keys() & project_after.keys() if project_before[name] != project_after[name]}
    if first == 'complete':
        assert moved == set(), sorted(moved)
        assert crew == crew_before
    else:
        allowed = {'ships/.default', f'ships/{crew[0].name}'}
        assert moved and all(name in allowed or name.startswith(f'ships/{crew[0].name}/') for name in moved), sorted(moved)
        assert all(launched[f'{shipmod.load(crew[0]).name}/{name}'] for name in shipmod.load(crew[0]).seats), \
            'the crew the first run never reached is created and brought up'
