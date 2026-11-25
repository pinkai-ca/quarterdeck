"""A seat's post never goes out in the operator's name (post_sender, law: identity).

A seat's shell that lost QD_SEAT is still the seat's: qd post finds the seat by its process ancestry, joined to a
launch record by process id and start time together, and refuses before anything is filed. A shell under no
recorded pane signs as the operator, as before. Synthetic ships and synthetic or throwaway processes only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from quarterdeck import cli, post_sender, servicectl, ship as shipmod, trusted_files


def record(ship_folder: Path, seat: str, pid: int, start: str | None, pane: str = '%9') -> None:
    folder = trusted_files.make(ship_folder / 'runtime', seat)
    data = {'pane': pane, 'pid': pid, 'at': '2026-11-25T12:00:00+00:00', 'pool': 'fake', 'resume': None,
            'argv': ['synthetic'], 'version': None}
    if start is not None:
        data['start'] = start
    (folder / trusted_files.LAUNCH).write_text(json.dumps(data))


def fake_proc(tmp_path: Path, chain: list[tuple[int, int, str]], state: dict[int, str] | None = None) -> Path:
    """A /proc of (pid, parent, start) entries: each stat line in the kernel's form, comm with a space and a ')'."""
    proc = tmp_path / 'proc'
    for pid, parent, start in chain:
        (proc / str(pid)).mkdir(parents=True)
        fields = [(state or {}).get(pid, 'S'), str(parent)] + ['0'] * 17 + [start, '0']
        (proc / str(pid) / 'stat').write_text(f"{pid} (a b) c) {' '.join(fields)}\n")
    return proc


CHAIN = [(300, 200, '7000'), (200, 100, '5000'), (100, 1, '4000')]


def drops(folder: Path) -> list[Path]:
    return sorted((folder / 'runtime' / 'post').rglob('*.json'))


@pytest.fixture
def operator(monkeypatch):
    monkeypatch.delenv('QD_SEAT', raising=False)


def test_an_ancestor_recorded_as_a_seats_pane_process_names_that_seat(drydock, tmp_path):
    record(drydock, 'worker-1', 200, '5000', pane='%42')
    found = post_sender.seat_session(shipmod.load(drydock), pid=300, proc=fake_proc(tmp_path, CHAIN))
    assert found == post_sender.SeatSession(drydock, 'worker-1', '%42', 200)


def test_a_reused_process_id_with_another_start_names_nobody(drydock, tmp_path):
    record(drydock, 'worker-1', 200, '4999')  # the recorded pane process ended; its id now belongs to another
    assert post_sender.seat_session(shipmod.load(drydock), pid=300, proc=fake_proc(tmp_path, CHAIN)) is None


def test_a_record_without_its_start_time_joins_no_process(drydock, tmp_path):
    record(drydock, 'worker-1', 200, None)
    assert post_sender.seat_session(shipmod.load(drydock), pid=300, proc=fake_proc(tmp_path, CHAIN)) is None


def test_an_exited_ancestor_ends_the_walk(drydock, tmp_path):
    record(drydock, 'worker-1', 100, '4000')
    proc = fake_proc(tmp_path, CHAIN, state={200: 'Z'})
    assert post_sender.seat_session(shipmod.load(drydock), pid=300, proc=proc) is None


def test_a_loop_in_the_ancestry_ends(drydock, tmp_path):
    proc = fake_proc(tmp_path, [(300, 200, '7000'), (200, 300, '5000')])
    assert post_sender.ancestry(300, proc) == [(300, '7000'), (200, '5000')]
    assert post_sender.seat_session(shipmod.load(drydock), pid=300, proc=proc) is None


def test_a_seat_of_a_peer_ship_in_the_same_project_is_found(drydock, tmp_path):
    alpha, beta = drydock.parent / 'alpha', drydock.parent / 'beta'  # drydock itself is a template, in no fleet
    shutil.copytree(drydock, alpha)
    shutil.copytree(drydock, beta)
    record(beta, 'captain', 100, '4000', pane='%7')
    found = post_sender.seat_session(shipmod.load(alpha), pid=300, proc=fake_proc(tmp_path, CHAIN))
    assert found == post_sender.SeatSession(beta, 'captain', '%7', 100)
    words = found.refusal(alpha, None)
    assert 'seat captain of ship beta' in words and '<seat>@alpha' in words


def test_a_ship_outside_the_project_is_not_looked_at(drydock, tmp_path):
    elsewhere = tmp_path / 'other' / 'ships' / 'gamma'
    shutil.copytree(drydock, elsewhere)
    record(elsewhere, 'captain', 200, '5000')
    assert post_sender.seat_session(shipmod.load(drydock), pid=300, proc=fake_proc(tmp_path, CHAIN)) is None


def test_the_nearest_recorded_ancestor_names_the_session(drydock, tmp_path):
    record(drydock, 'captain', 100, '4000', pane='%1')
    record(drydock, 'worker-1', 200, '5000', pane='%2')
    found = post_sender.seat_session(shipmod.load(drydock), pid=300, proc=fake_proc(tmp_path, CHAIN))
    assert found.seat == 'worker-1'


def test_a_program_marker_naming_a_recorded_pane_process_finds_a_process_outside_its_tree(drydock, tmp_path):
    record(drydock, 'worker-1', 200, '5000', pane='%3')
    proc = fake_proc(tmp_path, [(400, 1, '9000'), (200, 100, '5000'), (100, 1, '4000')])  # 400 was reparented
    found = post_sender.seat_session(shipmod.load(drydock), pid=400, proc=proc, environ={'CLAUDE_PID': '200'})
    assert found == post_sender.SeatSession(drydock, 'worker-1', '%3', 200)


@pytest.mark.parametrize('value', ['200', '201', '', 'x200', '２００'])
def test_a_marker_joins_only_a_live_process_with_the_recorded_start(drydock, tmp_path, value):
    record(drydock, 'worker-1', 200, '4999')  # 200 is live, but started after the record's process
    proc = fake_proc(tmp_path, [(400, 1, '9000'), (200, 100, '5000')])
    assert post_sender.seat_session(shipmod.load(drydock), pid=400, proc=proc, environ={'CLAUDE_PID': value}) is None


def test_a_marker_naming_an_unrecorded_process_signs_as_before(drydock, tmp_path):
    record(drydock, 'worker-1', 200, '5000')
    proc = fake_proc(tmp_path, [(400, 1, '9000'), (500, 1, '5000')])
    assert post_sender.seat_session(shipmod.load(drydock), pid=400, proc=proc, environ={'CLAUDE_PID': '500'}) is None


def throwaway_post(drydock: Path, *argv: str, seat: str | None = None) -> subprocess.CompletedProcess:
    """qd post from an ordinary child process of this test, its environment without QD_SEAT unless given."""
    env = {name: value for name, value in os.environ.items() if name not in ('QD_SEAT', 'QD_SHIP')}
    if seat:
        env['QD_SEAT'] = seat
    code = 'import sys; from quarterdeck import cli; sys.exit(cli.main(sys.argv[1:]))'
    return subprocess.run([sys.executable, '-c', code, '--ship', str(drydock), 'post', *argv],
                          env=env, capture_output=True, text=True, timeout=120)


def test_a_process_below_a_recorded_pane_without_qd_seat_is_refused_before_filing(drydock):
    """This test's own process stands as the pane process; the child below it is an ordinary process."""
    record(drydock, 'worker-1', os.getpid(), servicectl.process_start(os.getpid()), pane='%5')
    before = drops(drydock)
    result = throwaway_post(drydock, 'order', '--to', 'captain', 'Synthetic: in the operator name.')
    assert result.returncode != 0
    assert 'session of seat worker-1 of ship drydock (pane %5' in result.stderr
    assert 'never as the operator' in result.stderr and 'QD_SEAT=worker-1' in result.stderr
    assert drops(drydock) == before
    result = throwaway_post(drydock, 'report', '--from', 'captain', '--to', 'captain', 'Synthetic: as another.')
    assert result.returncode != 0 and 'never as --from captain' in result.stderr
    assert drops(drydock) == before


def test_a_seat_that_names_itself_posts_as_before(drydock):
    record(drydock, 'worker-1', os.getpid(), servicectl.process_start(os.getpid()))
    result = throwaway_post(drydock, 'order', '--to', 'captain', 'Synthetic: as myself.', seat='worker-1')
    assert result.returncode == 0, result.stderr
    assert [path.parent.name for path in drops(drydock)] == ['worker-1']


def test_a_shell_under_no_recorded_pane_signs_as_the_operator(drydock):
    record(drydock, 'worker-1', os.getpid(), '1')  # a stale record of this process id: not this process
    result = throwaway_post(drydock, 'order', '--to', 'captain', 'Synthetic: from the person.')
    assert result.returncode == 0, result.stderr
    assert [path.parent.name for path in drops(drydock)] == ['operator']
    result = throwaway_post(drydock, 'order', '--from', 'captain', '--to', 'worker-1', 'Synthetic: by --from.')
    assert result.returncode == 0, result.stderr
    assert sorted(path.parent.name for path in drops(drydock)) == ['captain', 'operator']


def test_resend_signs_nothing_and_is_not_asked(drydock, capsys, operator, monkeypatch):
    calls = []
    monkeypatch.setattr(post_sender, 'seat_session', lambda *a, **k: calls.append(a))
    cli.main(['--ship', str(drydock), 'post', 'resend', 'ord-nothing'])
    assert calls == []


# The second signal, a program's marker beside a seat's home, tested apart from the first: a synthetic program.
SYNTHETIC = {'synthetic': post_sender.Program(markers=('SYNTHETIC_MARK',), homes=('SYNTHETIC_HOME',))}


def seat_home(ship_folder: Path, seat: str) -> Path:
    home = ship_folder / 'runtime' / 'seats' / seat / 'home'
    home.mkdir(parents=True, exist_ok=True)
    return home


def by_home(ship_folder: Path, **environ: str):
    return post_sender.by_home(shipmod.load(ship_folder), environ=environ, programs=SYNTHETIC)


def test_a_marker_beside_a_declared_seats_home_names_that_seat(drydock):
    home = seat_home(drydock, 'worker-1')
    assert by_home(drydock, SYNTHETIC_MARK='1', SYNTHETIC_HOME=str(home)) == post_sender.SeatSession(
        drydock, 'worker-1', home='SYNTHETIC_HOME')
    assert by_home(drydock, SYNTHETIC_MARK='1', SYNTHETIC_HOME=str(home.parent)) is not None  # the seat folder itself


def test_a_marker_beside_a_personal_home_signs_as_the_operator(drydock, tmp_path):
    personal = tmp_path / 'person' / 'home'
    personal.mkdir(parents=True)
    assert by_home(drydock, SYNTHETIC_MARK='1', SYNTHETIC_HOME=str(personal)) is None


def test_a_seat_home_without_the_marker_establishes_nothing(drydock):
    home = seat_home(drydock, 'worker-1')
    assert by_home(drydock, SYNTHETIC_HOME=str(home)) is None
    assert by_home(drydock, SYNTHETIC_MARK='', SYNTHETIC_HOME=str(home)) is None


@pytest.mark.parametrize('place', ['seats/worker-1x/home', 'seats-x/worker-1/home', 'seats/ghost/home', 'seats',
                                   'trusted/worker-1'])
def test_a_misleading_prefix_or_an_undeclared_seat_is_no_evidence(drydock, place):
    path = drydock / 'runtime' / place
    path.mkdir(parents=True, exist_ok=True)
    assert by_home(drydock, SYNTHETIC_MARK='1', SYNTHETIC_HOME=str(path)) is None


def test_a_relative_home_is_no_evidence(drydock, monkeypatch):
    seat_home(drydock, 'worker-1')
    monkeypatch.chdir(drydock)
    assert by_home(drydock, SYNTHETIC_MARK='1', SYNTHETIC_HOME='runtime/seats/worker-1/home') is None


def test_a_link_out_of_the_runtime_is_no_evidence(drydock, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    seats = drydock / 'runtime' / 'seats'
    seats.mkdir(parents=True)
    (seats / 'worker-1').symlink_to(outside)
    assert by_home(drydock, SYNTHETIC_MARK='1', SYNTHETIC_HOME=str(seats / 'worker-1')) is None


def test_a_seat_folder_kept_outside_its_runtime_is_no_evidence(drydock, tmp_path):
    outside = tmp_path / 'outside-seats'
    (outside / 'worker-1' / 'home').mkdir(parents=True)
    (drydock / 'runtime').mkdir(exist_ok=True)
    (drydock / 'runtime' / 'seats').symlink_to(outside)
    home = drydock / 'runtime' / 'seats' / 'worker-1' / 'home'
    assert by_home(drydock, SYNTHETIC_MARK='1', SYNTHETIC_HOME=str(home)) is None
    assert by_home(drydock, SYNTHETIC_MARK='1', SYNTHETIC_HOME=str(outside / 'worker-1' / 'home')) is None


def test_a_link_that_resolves_into_a_seats_home_is_that_home(drydock, tmp_path):
    link = tmp_path / 'short'
    link.symlink_to(seat_home(drydock, 'worker-1'))
    assert by_home(drydock, SYNTHETIC_MARK='1', SYNTHETIC_HOME=str(link)).seat == 'worker-1'


def test_a_peer_ships_seat_home_is_found_and_another_projects_is_not(drydock, tmp_path):
    alpha, beta = drydock.parent / 'alpha', drydock.parent / 'beta'
    shutil.copytree(drydock, alpha)
    shutil.copytree(drydock, beta)
    gamma = tmp_path / 'other' / 'ships' / 'gamma'
    shutil.copytree(drydock, gamma)
    assert by_home(alpha, SYNTHETIC_MARK='1', SYNTHETIC_HOME=str(seat_home(beta, 'captain'))) == \
        post_sender.SeatSession(beta, 'captain', home='SYNTHETIC_HOME')
    assert by_home(alpha, SYNTHETIC_MARK='1', SYNTHETIC_HOME=str(seat_home(gamma, 'captain'))) is None


def test_the_two_signals_are_independent(drydock, tmp_path):
    record(drydock, 'worker-1', 200, '5000')
    proc = fake_proc(tmp_path, CHAIN)
    ship = shipmod.load(drydock)
    assert post_sender.by_home(ship, environ={}, programs=SYNTHETIC) is None  # a pane alone is not the second
    home = {'SYNTHETIC_MARK': '1', 'SYNTHETIC_HOME': str(seat_home(drydock, 'captain'))}
    assert post_sender.by_pane(ship, pid=400, proc=fake_proc(tmp_path / 'x', [(400, 1, '1')]), environ=home) is None
    assert post_sender.by_pane(ship, pid=300, proc=proc, environ={}).seat == 'worker-1'
    found = post_sender.seat_session(ship, pid=300, proc=proc, environ=home, programs=SYNTHETIC)
    assert found.seat == 'worker-1'  # the pane, where both are found


def test_qd_post_refuses_on_the_second_signal_without_printing_the_home(drydock, capsys, operator, monkeypatch):
    home = seat_home(drydock, 'worker-1')
    monkeypatch.setattr(post_sender, 'PROGRAMS', SYNTHETIC)
    monkeypatch.setenv('SYNTHETIC_MARK', '1')
    monkeypatch.setenv('SYNTHETIC_HOME', str(home))
    before = drops(drydock)
    assert cli.main(['--ship', str(drydock), 'post', 'order', '--to', 'captain', 'Synthetic: by home.']) != 0
    err = capsys.readouterr().err
    assert 'session of seat worker-1 of ship drydock' in err and 'SYNTHETIC_HOME names that seat' in err
    assert str(home) not in err
    assert drops(drydock) == before
    monkeypatch.delenv('SYNTHETIC_MARK')
    assert cli.main(['--ship', str(drydock), 'post', 'order', '--to', 'captain', 'Synthetic: by the person.']) == 0
    assert [path.parent.name for path in drops(drydock)] == ['operator']


MEASURED = {  # work/captures/seat-session-markers.md: one marker name each program adds, as sanctioned
    'claude': 'CLAUDECODE', 'codex': 'CODEX_THREAD_ID', 'opencode': 'OPENCODE', 'pi': 'PI_CODING_AGENT'}


def test_the_table_holds_the_measured_programs_alone_and_each_adapters_home():
    from quarterdeck.harness.claude import ClaudeHarness
    from quarterdeck.harness.codex import CodexHarness
    from quarterdeck.harness.opencode import OpenCodeHarness
    from quarterdeck.harness.pi import PiHarness

    from quarterdeck.harness import reader_stamp, session_markers

    assert post_sender.PROGRAMS is session_markers.PROGRAMS
    assert not any(name in modules for modules in (*reader_stamp.MODULES.values(), reader_stamp.ATTRIBUTION)
                   for name in ('harness/session_markers.py', 'post_sender.py'))  # no stamp delta
    assert set(post_sender.PROGRAMS) == set(MEASURED)  # Grok Build: its pane alone, until it is measured
    for name, adapter in (('claude', ClaudeHarness), ('codex', CodexHarness), ('opencode', OpenCodeHarness),
                          ('pi', PiHarness)):
        assert adapter.home_variable in post_sender.PROGRAMS[name].homes
        assert MEASURED[name] in post_sender.PROGRAMS[name].markers
    assert post_sender.PROCESS_MARKERS == ('CLAUDE_PID',)


@pytest.mark.parametrize('program', sorted(MEASURED))
def test_each_measured_marker_beside_its_seat_home_names_the_seat_and_beside_a_personal_home_does_not(
        drydock, tmp_path, program):
    ship = shipmod.load(drydock)
    home = str(seat_home(drydock, 'worker-1'))
    personal = tmp_path / 'person'
    personal.mkdir()
    for variable in post_sender.PROGRAMS[program].homes:
        environ = {MEASURED[program]: '1', 'HOME': str(personal), variable: home}
        assert post_sender.by_home(ship, environ=environ).seat == 'worker-1'
        assert post_sender.by_home(ship, environ={MEASURED[program]: '1', 'HOME': str(personal)}) is None
    others = {name for other, entry in post_sender.PROGRAMS.items() if other != program for name in entry.homes}
    assert post_sender.by_home(ship, environ={**{name: home for name in others}, 'HOME': str(personal)}) is None


# The third signal, the jail itself, where the pane is hidden and no program marker is set.
def jail_proc(tmp_path: Path, first: str) -> Path:
    proc = tmp_path / 'jail-proc'
    (proc / '1').mkdir(parents=True)
    (proc / '1' / 'comm').write_text(first + '\n')
    return proc


def test_a_jails_own_first_process_and_a_seat_home_name_the_seat(drydock, tmp_path):
    home = str(seat_home(drydock, 'worker-1'))
    found = post_sender.by_jail(shipmod.load(drydock), proc=jail_proc(tmp_path, 'bwrap'), environ={'HOME': home})
    assert found == post_sender.SeatSession(drydock, 'worker-1', home='HOME', jail=True)
    assert "it runs in that seat's jail" in found.refusal(drydock, None) and home not in found.refusal(drydock, None)


@pytest.mark.parametrize('first', ['systemd', 'init', 'bwrapper', ''])
def test_a_seat_home_outside_a_jail_is_not_the_jail_signal(drydock, tmp_path, first):
    home = str(seat_home(drydock, 'worker-1'))
    assert post_sender.by_jail(shipmod.load(drydock), proc=jail_proc(tmp_path, first), environ={'HOME': home}) is None


def test_a_jail_with_a_home_that_is_no_seats_is_not_the_jail_signal(drydock, tmp_path):
    personal = tmp_path / 'person'
    personal.mkdir()
    ship = shipmod.load(drydock)
    proc = jail_proc(tmp_path, 'bwrap')
    assert post_sender.by_jail(ship, proc=proc, environ={'HOME': str(personal)}) is None
    assert post_sender.by_jail(ship, proc=proc, environ={}) is None
    assert post_sender.by_jail(ship, proc=tmp_path / 'no-proc', environ={'HOME': str(seat_home(drydock, 'x'))}) is None


def test_the_refusals_command_runs_as_written_where_the_project_path_has_spaces(tmp_path):
    """bash runs the command as the refusal gives it, with a harmless stand-in for qd that prints what it got."""
    folder = tmp_path / 'my projects' / 'ships' / "o'brien"
    stand_in = folder / 'runtime' / 'bin' / 'qd'
    stand_in.parent.mkdir(parents=True)
    stand_in.write_text('#!/bin/sh\nprintf "%s|%s|%s\\n" "$QD_SHIP" "$QD_SEAT" "$*"\n')
    stand_in.chmod(0o755)
    words = post_sender.SeatSession(folder, 'worker-1', '%1', 5).refusal(folder, None)
    command = words.split('Post as worker-1: ', 1)[1]
    assert command.endswith(' post ...')
    ran = subprocess.run(['bash', '-c', command[:-len(' ...')] + ' report --re x done'], capture_output=True,
                         text=True, timeout=30, env={'PATH': os.environ.get('PATH', '')})
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == f'{folder}|worker-1|post report --re x done\n'
