"""Claude selects the ship's executable throughout a launch. All programs here are synthetic."""

from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from quarterdeck import check, doctor, drill, key_launch, record_owner
from quarterdeck.harness import get
from quarterdeck.seat import Seat
from quarterdeck.ship import Pool, ProgramPinError, Session, load, program_adapter
from quarterdeck.tmux import Pane


def executable(path, version, mode):
    path.write_text(f'#!/bin/sh\n[ "$DISABLE_AUTOUPDATER" = 1 ] || exit 1\nprintf "{version}\\n"\n')
    path.chmod(mode)
    return sha256(path.read_bytes()).hexdigest()


def declare(path, program, digest):
    path.write_text(path.read_text() + f'\n[programs.claude]\npath = "{program}"\nsha256 = "{digest}"\n')


@pytest.fixture
def machines(tmp_path, monkeypatch):
    folder = tmp_path / 'ship'
    folder.mkdir()
    declaration = folder / 'ship.toml'
    declaration.write_text('''[ship]
name = "synthetic"
code = "checkout"
workspace = "."
[pools.test]
provider = "anthropic"
[sessions.crew]
title = "Synthetic"
[seats.example]
session = "crew"
role = "worker"
harness = "claude"
pool = "test"
[seats.example.settings.env]
DISABLE_AUTOUPDATER = "0"
''')
    machine = tmp_path / 'bin' / 'claude'
    machine.parent.mkdir()
    executable(machine, '9.9.9', 0o700)
    monkeypatch.setenv('PATH', f'{machine.parent}{os.pathsep}{os.environ["PATH"]}')
    unpinned = load(folder)
    program = folder / 'runtime' / 'programs' / 'claude-release'
    program.parent.mkdir(parents=True, mode=0o700)
    digest = executable(program, '1.2.3', 0o500)
    declare(declaration, program.relative_to(folder), digest)
    return unpinned, load(folder), program, digest


def test_restart_adopts_pin_and_keeps_saved_conversation(machines, monkeypatch):
    unpinned, pinned, program, _ = machines
    spec = pinned.seats['example']
    panes = {}
    launches = []
    tmux = SimpleNamespace(pane=panes.get)

    def open_window(self, argv, env, path):
        assert env['DISABLE_AUTOUPDATER'] == '1'
        assert json.loads((self.home / 'settings.json').read_text())['env']['DISABLE_AUTOUPDATER'] == '1'
        pane = Pane(f'%{len(launches) + 1}', 'Synthetic', 'example', os.getpid(), 100, 25, False, False, None)
        panes[pane.id] = pane
        launches.append(argv)
        return pane

    monkeypatch.setattr(Seat, '_open_window', open_window)
    monkeypatch.setattr(Seat, '_path', lambda self: os.environ['PATH'])
    monkeypatch.setattr(Seat, '_survives', lambda *args: None)
    machine = Seat(unpinned, spec, tmux)
    machine.up(resume=False)
    assert machine.launched().version == '9.9.9'
    assert launches[0][0] == 'claude'

    changed = Seat(pinned, spec, tmux)
    previous = changed.launched()
    assert changed.pane().id == previous.pane
    record = changed.home / 'projects' / 'synthetic' / 'conversation.jsonl'
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({'type': 'user', 'sessionId': 'conversation', 'timestamp': previous.at,
                                  'message': {'content': 'synthetic question'}}) + '\n')

    def old_process_stops():
        panes.pop(previous.pane)
        return 'synthetic process stopped'

    monkeypatch.setattr(changed, '_relaunch_down', old_process_stops)
    assert 'resumed conversa' in changed.restart()
    launch = changed.launched()
    assert launch.version == '1.2.3' and launch.resume == 'conversation'
    assert launches[1][0] == str(program) and launches[1][-2:] == ['--resume', 'conversation']
    assert changed.pane().id == launch.pane and record_owner.launch_reader(launch) == 'claude'
    assert record_owner.launch_reader(replace(launch, argv=['codex'], harness='codex')) == 'codex'
    assert record_owner.launch_reader(SimpleNamespace(argv=[str(program.with_name('other-program'))])) is None
    panes[launch.pane] = replace(changed.pane(), pid=launch.pid + 1)
    assert changed.pane() is None


def test_check_and_doctor_fix_choose_the_pin_without_a_machine_binary(machines, monkeypatch):
    _, ship, program, digest = machines
    monkeypatch.setattr(doctor, 'program_path', lambda: '')
    launches = []

    @contextmanager
    def temporary(ship, tmux, spec, orders, purpose, *, human=False, **options):
        session = Session(spec.session, spec.session, human=human)
        seat = Seat(replace(ship, sessions={**ship.sessions, spec.session: session}), spec, tmux)

        def up(*, resume):
            argv = seat.harness.launch(seat.adapter_spec(), seat.pool, seat.home, Path('brief'), None, None).argv
            launches.append((purpose, human, argv[0]))
            seat.launched = lambda: SimpleNamespace(argv=argv)
            return 'synthetic up'

        seat.up = up
        yield seat

    def probe(ship, seat, orders):
        seat.up(resume=False)
        return [drill.Check('synthetic task', True)]

    def wake(seat, orders):
        return probe(seat.ship, seat, orders)

    monkeypatch.setattr(check, 'temporary_seat', temporary)
    monkeypatch.setattr(check, 'probe', probe)
    monkeypatch.setattr(check, 'probe_wake', wake)
    monkeypatch.setattr(doctor, 'temporary_seat', temporary)
    monkeypatch.setattr(doctor, 'wait_for', lambda *args: None)
    assert all(row.passed for row in check.check(ship, 'claude', None))
    assert doctor.fix(ship, [], None)[0].passed
    assert launches == [('qd check', False, str(program)), ('qd check human wake', True, str(program)),
                        ('qd doctor', False, str(program))]
    row, version = doctor.program_observation('claude', ship.runtime, ship=ship)
    assert version == '1.2.3' and row.passed and 'proven by qd check' in row.detail
    assert (ship.runtime / 'proven' / f'claude-1.2.3-{digest}').is_file()


def test_key_wrapper_keeps_the_pin_as_its_exec_program(machines, key_file, monkeypatch):
    _, ship, program, _ = machines
    key_file.write(paid='synthetic-key')
    pool = Pool('test', 'anthropic', api_key='keys:paid', ship_folder=ship.folder)
    seat = Seat(replace(ship, pools={'test': pool}), ship.seats['example'], SimpleNamespace(pane=lambda ident: None))
    monkeypatch.setattr(seat, '_path', lambda: os.environ['PATH'])
    monkeypatch.setattr(seat, '_survives', lambda *args: None)
    monkeypatch.setattr(seat, '_open_window', lambda *args: Pane('%1', 'Synthetic', '', os.getpid(), 100, 25, False, False, None))
    seat.up(resume=False)
    argv = seat.launched().argv
    assert argv[3] == key_launch.MODULE and key_launch.inner(argv)[0] == str(program)


def test_changed_pin_refuses_version_and_launch_without_falling_back(machines):
    _, ship, program, _ = machines
    adapter = program_adapter(ship, get('claude'))
    assert adapter.version() == '1.2.3'
    program.chmod(0o700)
    executable(program, '4.5.6', 0o500)
    with pytest.raises(ProgramPinError, match='SHA-256 does not match'):
        adapter.version()
    with pytest.raises(ProgramPinError, match='SHA-256 does not match'):
        adapter.launch(ship.seats['example'], ship.pools['test'], Path('home'), Path('brief'), None, None)
    assert 'SHA-256 does not match' in check.check(ship, 'claude', None)[0].detail
    assert 'SHA-256 does not match' in doctor.fix(ship, [], None)[0].detail


def test_named_drill_stages_only_the_declared_runtime_executable(machines):
    _, ship, program, _ = machines
    (ship.runtime / 'unrelated-state').write_text('state belongs to the original ship')
    copied, folder = drill.temporary_copy(ship)
    try:
        staged = program_adapter(copied, get('claude')).selected_program()
        assert staged.parent.parent == copied.runtime and not staged.samefile(program)
        assert not (copied.runtime / 'unrelated-state').exists()
        assert program_adapter(copied, get('claude')).version() == '1.2.3'
    finally:
        shutil.rmtree(folder)
