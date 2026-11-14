"""The jail: which folders a program resolves through, and the bubblewrap command around it."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from quarterdeck import sandbox, ship as shipmod
from quarterdeck.ship import ShipError


def install(home: Path) -> str:
    """A home with pi installed the way npm does it, a link in ``~/.local/bin`` to a node prefix; the PATH that finds it."""
    node = home / ".local" / "node"
    package = node / "lib" / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist"
    package.mkdir(parents=True)
    (package / "cli.js").write_text("#!/usr/bin/env node\n")
    (package / "cli.js").chmod(0o755)
    (node / "bin").mkdir()
    (node / "bin" / "pi").symlink_to(Path("..") / "lib" / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "cli.js")
    (node / "bin" / "node").write_text("#!/bin/sh\n")
    (node / "bin" / "node").chmod(0o755)
    (home / ".local" / "bin").mkdir()
    (home / ".local" / "bin" / "pi").symlink_to(node / "bin" / "pi")
    (home / ".local" / "bin" / "node").symlink_to(node / "bin" / "node")
    (home / ".local" / "share" / "keyrings").mkdir(parents=True)
    (home / ".opencode" / "bin").mkdir(parents=True)
    (home / ".opencode" / "bin" / "opencode").write_text("binary")
    (home / ".opencode" / "bin" / "opencode").chmod(0o755)
    (home / ".local" / "bin" / "opencode").symlink_to(home / ".opencode" / "bin" / "opencode")
    return os.pathsep.join([str(home / ".local" / "bin"), "/usr/bin"])


def test_program_folders_follow_the_links_to_the_install_prefix_and_never_bind_the_homes_own_share(tmp_path: Path):
    home = tmp_path / "home"
    path = install(home)
    folders = sandbox.program_folders(("pi", "node"), path, home)
    assert home / ".local" / "bin" in folders and home / ".local" / "node" in folders and home / ".local" / "node" / "bin" in folders
    assert home / ".local" / "node" / "lib" in folders, "the root above node_modules"
    assert home / ".local" not in folders and home / ".local" / "share" not in folders, "~/.local/share is the operator's"
    assert folders == sorted(folders, key=lambda f: (len(f.parts), str(f))), "parents first"
    assert all(home in f.parents for f in folders), "folders outside the home are visible through the read-only root"
    assert home / ".opencode" in sandbox.program_folders(("opencode",), path, home), "a single binary's install prefix"
    assert sandbox.program_folders(("nothing-here",), path, home) == [home / ".local" / "bin"], "a program not found binds nothing; the PATH entry is bound even so"


def test_no_folder_bound_back_is_the_home_or_contains_it():
    """Every bind back follows the home's tmpfs, so a folder at or above the home would show it again."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix='qdj-', dir='/tmp') as temporary:
        above = Path(temporary)/'tools'
        home = above/'operator-home'
        (home/'bin').mkdir(parents=True)
        (above/'bin').mkdir()
        path = os.pathsep.join(map(str, (above, home, home/'bin', above/'bin', '/usr/bin')))
        hidden = sandbox.hidden_folders(('nothing-here',), path, home)
        assert above not in hidden and home not in hidden, 'a PATH entry at or above the home stays hidden'
        assert home/'bin' in hidden and above/'bin' in hidden, 'folders beside or below the home are still bound back'
        assert sandbox.program_folders(('nothing-here',), path, home) == [home/'bin']
        assert sandbox.covers(home, home) and sandbox.covers(above, home) and not sandbox.covers(home/'bin', home)


def test_the_command_hides_the_home_binds_the_workspace_writable_and_the_ship_read_only_in_depth_order(tmp_path: Path):
    home = tmp_path / "home"
    ship_folder = home / "dev" / "project" / "ships" / "x"
    runtime = ship_folder / "runtime"
    jail = sandbox.Jail(home=home, seat_home=runtime / "seats" / "w" / "home", seat_folder=runtime / "seats" / "w",
                        workspace=home / "dev" / "project", ship_folder=ship_folder, runtime=runtime,
                        read_only=(home / ".local" / "bin", home / ".opencode"),
                        writable=(runtime / "post" / "drop" / "w", runtime / "check" / "w"),
                        post_view=runtime / "views" / "w" / "post")
    args = sandbox.command(jail, ["pi", "--model", "m"])
    assert args[:12] == ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--size", str(sandbox.TMP_BYTES), "--tmpfs", "/tmp"]
    session = sandbox.session_hidden()
    assert args[12:12 + len(session)] == session, "the desktop session's /run and the shared scratch are hidden first"
    after = 12 + len(session)
    assert args[after:after + 4] == ["--size", str(sandbox.HOME_BYTES), "--tmpfs", str(home)], "the operator's home is an empty, bounded tmpfs before anything under it is bound back"
    assert args[-10:] == ["--setenv", "HOME", str(runtime / "seats" / "w" / "home"), "--unshare-pid", "--unshare-net", "--die-with-parent", "--", "pi", "--model", "m"]
    text = " ".join(args)
    workspace, ship, seats, seat, post = (text.index(f"--bind {home / 'dev' / 'project'} "), text.index(f"--ro-bind {ship_folder} "),
                                         text.index(f"--tmpfs {runtime} "), text.index(f"--bind {runtime / 'seats' / 'w'} "),
                                         text.index(f"--bind-try {runtime / 'post' / 'drop' / 'w'} "))
    assert workspace < ship < seats < seat, "a deeper mount comes later, so the ship folder is read-only inside the writable workspace and this seat is writable inside the hidden runtime"
    assert text.index(f"--ro-bind {runtime / 'views' / 'w' / 'post'} {runtime / 'post'} ") < post, \
        "the seat's own view is shown read-only in the post's place, the seat's drop writable over it"
    assert f"--ro-bind-try {runtime / 'post'} " not in text and f"--ro-bind {runtime / 'post'} " not in text, "never the post whole"
    assert text.index(f"--remount-ro {runtime}") > post, "the runtime's fence is made read-only after everything inside it"
    assert ship < post, "only the seat's drop folder is writable inside the read-only ship folder"
    binds = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--bind-try"]
    assert str(runtime / "post" / "drop" / "w") in binds and str(runtime / "post") not in binds
    assert str(runtime / "check" / "w") in binds and str(runtime / "check") not in binds
    hidden = text.index(f"--perms 0700 --size {sandbox.HIDDEN_BYTES} --tmpfs {runtime / 'tmux'}")
    assert ship < hidden, "the ship's own tmux server is hidden inside the read-only ship folder, by an empty private tmpfs"
    assert f"--ro-bind-try {home / '.local' / 'bin'} {home / '.local' / 'bin'}" in text and f"--ro-bind-try {home / '.opencode'} " in text
    assert text.index(f"--ro-bind-try {home / '.opencode'} ") > args.index(str(home)), "bound back after the home is hidden"


def test_plan_refuses_a_jail_that_jails_nothing_or_one_without_bubblewrap(tmp_path: Path, drydock: Path, monkeypatch):
    ship = shipmod.load(drydock)
    home = tmp_path / "home"
    path = install(home)
    monkeypatch.setattr(sandbox, "available", lambda: False)
    with pytest.raises(ShipError, match="bwrap is not on the PATH; install bubblewrap, or set sandbox = false"):
        sandbox.plan(ship, "w", ship.runtime / "seats" / "w", ship.runtime / "seats" / "w" / "home", home / "work", "pi", path, home=home)
    monkeypatch.setattr(sandbox, "available", lambda: True)
    (home / "work").mkdir()
    with pytest.raises(ShipError, match="jails nothing"):
        sandbox.plan(ship, "w", ship.runtime / "seats" / "w", ship.runtime / "seats" / "w" / "home", home, "pi", path, home=home)
    with pytest.raises(ShipError, match="ship folder as its working directory"):
        sandbox.plan(ship, "w", ship.runtime / "seats" / "w", ship.runtime / "seats" / "w" / "home", ship.folder, "pi", path, home=home)
    jail = sandbox.plan(ship, "w", ship.runtime / "seats" / "w", ship.runtime / "seats" / "w" / "home", home / "work", "pi", path, home=home)
    assert jail.workspace == (home / "work").resolve() and jail.ship_folder == ship.folder and jail.seat_home == ship.runtime / "seats" / "w" / "home"
    assert home / ".local" / "node" in jail.read_only and jail.writable == (ship.runtime / "post" / "drop" / "w", ship.runtime / "check" / "w")
    assert (ship.runtime / "check" / "w").is_dir()


def test_each_jail_binds_only_its_own_check_folder(tmp_path: Path, drydock: Path, monkeypatch):
    ship = shipmod.load(drydock)
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    monkeypatch.setattr(sandbox, "available", lambda: True)
    path = "/usr/bin"

    def planned(name: str):
        return sandbox.plan(ship, name, ship.runtime / "seats" / name, ship.runtime / "seats" / name / "home",
                            home / "work", "pi", path, home=home)

    one, two = planned("one"), planned("two")
    assert one.writable == (ship.runtime / "post" / "drop" / "one", ship.runtime / "check" / "one")
    assert two.writable == (ship.runtime / "post" / "drop" / "two", ship.runtime / "check" / "two")
    assert ship.runtime / "check" not in one.writable and ship.runtime / "check" not in two.writable
    text = " ".join(sandbox.command(one, ["true"]))
    assert f"--bind-try {ship.runtime / 'check' / 'one'} {ship.runtime / 'check' / 'one'}" in text
    assert f"--bind-try {ship.runtime / 'check'} " not in text
    assert f"--bind-try {ship.runtime / 'check' / 'two'} " not in text
    with pytest.raises(ShipError, match="one directory under the check folder"):
        sandbox.check_proof_folder(ship.runtime, "one/two")
    with pytest.raises(ShipError, match="one directory under the check folder"):
        sandbox.check_proof_folder(ship.runtime, "..")


def test_a_link_at_the_check_folder_is_refused_before_a_bind(tmp_path: Path, drydock: Path, monkeypatch):
    ship = shipmod.load(drydock)
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    monkeypatch.setattr(sandbox, "available", lambda: True)
    started = []

    def popen(argv, *args, **kwargs):
        started.append(argv)
        raise AssertionError("bwrap started")

    def command(*args, **kwargs):
        raise AssertionError("bwrap started")

    monkeypatch.setattr(sandbox.subprocess, "Popen", popen)
    monkeypatch.setattr(sandbox, "command", command)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = "outside-marker-must-stay-unprinted"
    (outside / "kept.txt").write_text(marker)
    link = ship.runtime / "check" / "worker"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ShipError, match=str(link)) as caught:
        sandbox.plan(ship, "worker", ship.runtime / "seats" / "worker",
                     ship.runtime / "seats" / "worker" / "home", home / "work", "pi", "/usr/bin", home=home)
    assert marker not in str(caught.value) and str(outside) not in str(caught.value)
    assert started == []
    assert link.is_symlink() and (outside / "kept.txt").read_text() == marker
    assert [path.name for path in outside.iterdir()] == ["kept.txt"]


def test_a_link_at_the_check_directory_is_not_followed(tmp_path: Path, drydock: Path, monkeypatch):
    ship = shipmod.load(drydock)
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    monkeypatch.setattr(sandbox, "available", lambda: True)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = "outside-marker-must-stay-unprinted"
    (outside / "kept.txt").write_text(marker)
    ship.runtime.mkdir(parents=True, exist_ok=True)
    link = ship.runtime / "check"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ShipError, match=str(link)) as caught:
        sandbox.plan(ship, "worker", ship.runtime / "seats" / "worker",
                     ship.runtime / "seats" / "worker" / "home", home / "work", "pi", "/usr/bin", home=home)
    assert marker not in str(caught.value) and str(outside) not in str(caught.value)
    assert link.is_symlink() and (outside / "kept.txt").read_text() == marker
    assert [path.name for path in outside.iterdir()] == ["kept.txt"]


@pytest.mark.parametrize('location', ['parent', 'home', 'root', 'alias', 'parent-through-link'])
def test_ancestor_workspace_cannot_rebind_the_hidden_home(tmp_path, drydock, monkeypatch, location):
    home = tmp_path/'operator'/'home'
    home.mkdir(parents=True)
    alias = tmp_path/'alias'
    alias.symlink_to(home.parent, target_is_directory=True)
    workspace = {'parent': home.parent, 'home': home, 'root': Path('/'),
                 'alias': alias, 'parent-through-link': alias/'home'/'..'}[location]
    ship = shipmod.load(drydock)
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    with pytest.raises(ShipError, match='contains the operator home'):
        sandbox.plan(ship, 'worker', ship.runtime/'seats'/'worker', ship.runtime/'seats'/'worker'/'home',
                     workspace, '/usr/bin/python3', '/usr/bin', home=home)


@pytest.mark.parametrize('place', ['ships', 'office', 'ships-root', 'office-root'])
def test_covering_refuses_a_home_that_a_fleet_folder_is_or_contains(tmp_path, monkeypatch, place):
    """ships/ and office/ are mounts. One that is the home, or contains it, is refused."""
    project = tmp_path / 'project'
    ship_folder = project / 'ships' / 'alpha'
    work = project / 'work'
    work.mkdir(parents=True)
    fleet = project / 'ships' if place.startswith('ships') else project / 'office'
    home = fleet if place.endswith('root') else fleet / 'beta' / 'operator-home'
    home.mkdir(parents=True)
    ship = shipmod.Ship(ship_folder, 'synthetic', 'operator', work, {}, {}, {})
    own = ship.runtime / 'seats' / 'worker'
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    with pytest.raises(ShipError, match='the fleet folder') as caught:
        sandbox.plan(ship, 'worker', own, own / 'home', work, '/usr/bin/python3', '/usr/bin', home=home)
    assert str(fleet.resolve()) in str(caught.value)
    jail = sandbox.Jail(home=home.resolve(), seat_home=own / 'home', seat_folder=own, workspace=work,
                        ship_folder=ship_folder, runtime=ship.runtime)
    assert sandbox.covering(jail) == ('the fleet folder', fleet)


@pytest.mark.parametrize('inside', ['ship folder', 'below the ship folder', 'post store', 'seat folder'])
def test_a_home_at_or_inside_the_ship_folder_is_refused(tmp_path, inside, monkeypatch):
    """The ship folder and the stores inside it are bound after the home's tmpfs: one that holds the home shows it."""
    folder = tmp_path/'ship'
    ship = shipmod.Ship(folder, 'synthetic', 'operator', tmp_path/'work', {}, {}, {})
    own = ship.runtime/'seats'/'worker'
    home = {'ship folder': folder, 'below the ship folder': folder/'h'/'operator-home',
            'post store': ship.runtime/'post'/'operator-home', 'seat folder': own/'operator-home'}[inside]
    home.mkdir(parents=True)
    (tmp_path/'work').mkdir()
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    with pytest.raises(ShipError, match='is that home or contains it'):
        sandbox.plan(ship, 'worker', own, own/'home', tmp_path/'work', '/usr/bin/python3', '/usr/bin', home=home)


def test_the_home_tmpfs_lies_in_depth_order_after_any_folder_that_contains_it(tmp_path: Path):
    above = tmp_path/'above'
    home = above/'operator-home'
    jail = sandbox.Jail(home=home, seat_home=tmp_path/'s'/'home', seat_folder=tmp_path/'s', workspace=tmp_path/'w',
                        ship_folder=tmp_path/'ship', runtime=tmp_path/'ship'/'runtime',
                        read_only=(above, home/'bin'), writable=(tmp_path/'ship'/'runtime'/'post', above))
    text = ' '.join(sandbox.command(jail, ['true']))
    hidden = text.index(f'--tmpfs {home} ')
    assert text.index(f'--ro-bind-try {above} ') < hidden and text.index(f'--bind-try {above} ') < hidden
    assert hidden < text.index(f'--ro-bind-try {home/"bin"} '), 'a folder inside the home is still bound back over it'
    assert sandbox.covering(jail) == ('the bound-back folder', above)
    assert sandbox.covering(replace_mounts(jail, read_only=(home/'bin',))) == ('the writable folder', above)
    assert sandbox.covering(replace_mounts(jail, read_only=(home/'bin',), writable=())) is None
    assert sandbox.covering(replace_mounts(jail, read_only=(), writable=(), seat_folder=home)) == ("the seat's own folder", home)


def replace_mounts(jail, **fields):
    from dataclasses import replace
    return replace(jail, **fields)


def test_a_jailed_seat_commits_as_the_workspace_would_outside(tmp_path: Path):
    """The operator's home, and with it the global git configuration, is gone inside the jail; the identity travels in."""
    import subprocess

    workspace = tmp_path / "work"
    workspace.mkdir()
    subprocess.run(["git", "-C", str(workspace), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "A Shipwright"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "shipwright@example.org"], check=True)
    identity = sandbox.git_identity(workspace)
    assert identity == (("GIT_AUTHOR_NAME", "A Shipwright"), ("GIT_COMMITTER_NAME", "A Shipwright"),
                        ("GIT_AUTHOR_EMAIL", "shipwright@example.org"), ("GIT_COMMITTER_EMAIL", "shipwright@example.org"))
    home = tmp_path / "home"
    jail = sandbox.Jail(home=home, seat_home=home / "s" / "home", seat_folder=home / "s", workspace=workspace, ship_folder=tmp_path / "ship",
                        runtime=tmp_path / "ship" / "runtime", environment=identity)
    args = sandbox.command(jail, ["pi"])
    assert args[args.index("GIT_AUTHOR_NAME") - 1] == "--setenv" and args[args.index("GIT_AUTHOR_NAME") + 1] == "A Shipwright"
    assert args.index("GIT_COMMITTER_EMAIL") < args.index("--unshare-pid"), "set before the program runs"
    assert sandbox.git_identity(tmp_path / "nowhere") == (), "no repository, no identity, and no refusal"


def test_a_jailed_programs_scratch_is_the_jails_own_tmp(tmp_path: Path):
    """pi 0.74.2 spooled a tool's output to the launch's TMPDIR, a filesystem the jail mounts read-only, and died."""
    home = tmp_path / "home"
    jail = sandbox.Jail(home=home, seat_home=home / "s" / "home", seat_folder=home / "s", workspace=tmp_path / "work",
                        ship_folder=tmp_path / "ship", runtime=tmp_path / "ship" / "runtime")
    args = sandbox.command(jail, ["pi"])
    for variable in ("TMPDIR", "TMP", "TEMP"):
        assert args[args.index(variable) - 1] == "--setenv" and args[args.index(variable) + 1] == "/tmp"
    assert args.index("TMPDIR") < args.index("--unshare-pid")


# -- a jailed seat's PATH: the qd-only command folder first, the machine's interpreters after, no venv -------------


FRAMEWORK_NAMES = ("python", "python3", "pip", "pip3")


def venv_bins(ship) -> set[Path]:
    import sys
    found = {(ship.runtime / "venv" / "bin").resolve()}
    if sys.prefix != sys.base_prefix:
        found.add((Path(sys.prefix) / "bin").resolve())  # the framework's own venv, which runs these tests
    return found


def test_a_jailed_launch_runs_with_the_same_qd_only_path_and_sets_no_path_of_its_own(drydock: Path, tmp_path: Path):
    from quarterdeck import seat as seatmod
    ship = shipmod.load(drydock)
    path = seatmod.prepare_program_path(ship, synthetic=True)
    entries = path.split(os.pathsep)
    commands = ship.runtime / "bin"
    assert entries[0] == str(commands), "the ship's qd-only command folder comes first"
    assert sorted(p.name for p in commands.iterdir()) in (["qd"], ["qd", "qd-fakeharness"])
    assert (commands / "qd").resolve() == (seatmod.qd_bin() / "qd").resolve(), "qd is the installed copy"
    assert not any((commands / name).exists() for name in FRAMEWORK_NAMES), "no interpreter or pip beside qd"
    assert not {Path(entry).resolve() for entry in entries if entry} & venv_bins(ship), "no venv bin on the PATH"
    # The jail adds no PATH of its own: the launch's PATH, the same as unjailed, reaches the program inside.
    jail = sandbox.Jail(home=tmp_path / "home", seat_home=ship.runtime / "seats" / "worker-1" / "home",
                        seat_folder=ship.runtime / "seats" / "worker-1", workspace=tmp_path / "work",
                        ship_folder=ship.folder, runtime=ship.runtime, read_only=(), writable=())
    args = sandbox.command(jail, ["fake"])
    assert not [args[i + 1] for i, word in enumerate(args) if word == "--setenv" and args[i + 1] == "PATH"]
    words = seatmod.launch_words(path, args, socket=None, tmux_tmpdir="/synthetic/tmux", markers=[])
    assert f"PATH={path}" in words and words.index(f"PATH={path}") < words.index("bwrap")
    # The ship folder is bound back read-only; nothing deeper is mounted over the command folder or its parents.
    assert f"--ro-bind {ship.folder} {ship.folder}" in " ".join(args)
    # Inside the runtime's fence the command folder is bound back read-only at its own path; nothing writable or
    # hiding is laid over it or its parents beyond that fence.
    assert f"--ro-bind-try {commands} {commands}" in " ".join(args)
    over = [args[i + 1] for i, word in enumerate(args) if word in ("--tmpfs", "--bind", "--bind-try", "--ro-bind-try")
            and i + 1 < len(args) and ship.folder in Path(args[i + 1]).parents
            and (commands == Path(args[i + 1]) or Path(args[i + 1]) in commands.parents)
            and Path(args[i + 1]) != ship.runtime and not (word == "--ro-bind-try" and Path(args[i + 1]) == commands)]
    assert over == [], over


def test_the_machines_python_and_pip_are_found_through_the_machine_path_never_a_venv(drydock: Path):
    import shutil
    from quarterdeck import seat as seatmod
    ship = shipmod.load(drydock)
    path = seatmod.prepare_program_path(ship, synthetic=True)
    bins = venv_bins(ship) | {(ship.runtime / "bin").resolve()}
    machine = os.pathsep.join(entry for entry in os.environ.get("PATH", "").split(os.pathsep)
                              if entry and Path(entry).resolve() not in bins)
    for name in FRAMEWORK_NAMES:
        found = shutil.which(name, path=path)
        assert found == shutil.which(name, path=machine), name
        if found:
            assert Path(found).parent.resolve() not in bins and not any(
                Path(found).resolve().is_relative_to(b.parent) for b in bins if b.name == "bin"), (name, found)


@pytest.mark.parametrize('place', ['ships/templates/example', 'ships/alpha/runtime/work', 'ships/beta', 'office', 'office/work',
                                   'ships/alpha/homes', 'ships/alpha/homes/claude', 'ships/alpha/.git', 'ships/alpha/roles',
                                   'ships/alpha/elsewhere'])
def test_plan_refuses_a_workspace_inside_a_fleet_folder_by_path(tmp_path, monkeypatch, place):
    """The jail fences ships/ and office/ empty after the workspace: a workspace inside one would be absent."""
    project = tmp_path / 'project'
    ship = shipmod.Ship(project / 'ships' / 'alpha', 'synthetic', 'operator', project, {}, {}, {})
    workspace = project / place
    workspace.mkdir(parents=True)
    own = ship.runtime / 'seats' / 'worker'
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    with pytest.raises(ShipError, match='inside the fleet folder') as caught:
        sandbox.plan(ship, 'worker', own, own / 'home', workspace, '/usr/bin/python3', '/usr/bin', home=tmp_path / 'home')
    root = project / place.split('/')[0]
    assert f'only in {ship.folder.resolve() / "work"}' in str(caught.value)
    assert f'at {workspace.resolve()}, inside the fleet folder {root}' in str(caught.value)
    assert not (ship.runtime / 'post').exists(), 'refused before anything of the launch is made'


@pytest.mark.parametrize('office', [False, True])
def test_plan_makes_each_missing_fleet_folder_empty_so_the_jail_fences_it(tmp_path, monkeypatch, office):
    project = tmp_path / 'project'
    folder = project / 'office' if office else project / 'ships' / 'alpha'
    folder.mkdir(parents=True)
    ship = shipmod.Ship(folder, 'synthetic', 'operator', tmp_path / 'work', {}, {}, {}, kind='office' if office else 'crew')
    (tmp_path / 'work').mkdir()
    own = ship.runtime / 'seats' / 'worker'
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    missing = project / ('ships' if office else 'office')
    assert not missing.exists()
    jail = sandbox.plan(ship, 'worker', own, own / 'home', tmp_path / 'work', '/usr/bin/python3', '/usr/bin', home=tmp_path / 'home')
    assert missing.is_dir() and not missing.is_symlink() and not any(missing.iterdir())
    args = sandbox.command(jail, ['synthetic-program'])
    assert ['--tmpfs', str(missing)] in [args[i:i + 2] for i in range(len(args))]
    assert ['--remount-ro', str(missing)] in [args[i:i + 2] for i in range(len(args))]


@pytest.mark.parametrize('form', ['unwritable parent', 'dangling link', 'link to a folder', 'file'])
def test_plan_refuses_a_fleet_folder_it_cannot_fence_by_path(tmp_path, monkeypatch, form):
    project = tmp_path / 'project'
    ship = shipmod.Ship(project / 'ships' / 'alpha', 'synthetic', 'operator', tmp_path / 'work', {}, {}, {})
    ship.folder.mkdir(parents=True)
    (tmp_path / 'work').mkdir()
    office = project / 'office'
    elsewhere = tmp_path / 'elsewhere'
    if form == 'dangling link':
        office.symlink_to(elsewhere)
    elif form == 'link to a folder':
        elsewhere.mkdir()
        office.symlink_to(elsewhere)
    elif form == 'file':
        office.write_text('')
    own = ship.runtime / 'seats' / 'worker'
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    if form == 'unwritable parent':
        real = Path.mkdir

        def refuse(self, *args, **kwargs):
            if self == office:
                raise PermissionError(13, 'Permission denied')
            return real(self, *args, **kwargs)
        monkeypatch.setattr(Path, 'mkdir', refuse)
    with pytest.raises(ShipError, match=f'the fleet folder {office}') as caught:
        sandbox.plan(ship, 'worker', own, own / 'home', tmp_path / 'work', '/usr/bin/python3', '/usr/bin', home=tmp_path / 'home')
    message = str(caught.value)
    assert ('cannot be made (Permission denied)' in message) if form == 'unwritable parent' else ('not a real folder' in message)
    assert not elsewhere.exists() or not any(elsewhere.iterdir()), 'nothing made through a link'


def test_a_workspace_in_the_ships_own_folder_is_allowed_and_mounted_writable_after_that_folder(tmp_path, monkeypatch):
    """The forum's reviewer works in the forum's own work folder: writable, while the rest of the ship stays read-only."""
    project = tmp_path / 'project'
    ship = shipmod.Ship(project / 'ships' / 'alpha', 'synthetic', 'operator', project, {}, {}, {})
    workspace = ship.folder / 'work'
    workspace.mkdir(parents=True)
    own = ship.runtime / 'seats' / 'worker'
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    jail = sandbox.plan(ship, 'worker', own, own / 'home', workspace, '/usr/bin/python3', '/usr/bin', home=tmp_path / 'home')
    args = sandbox.command(jail, ['synthetic-program'])
    windows = [args[i:i + 3] for i in range(len(args))]
    ship_bind = windows.index(['--ro-bind', str(ship.folder), str(ship.folder)])
    work_bind = windows.index(['--bind', str(workspace.resolve()), str(workspace.resolve())])
    fence = [args[i:i + 2] for i in range(len(args))].index(['--tmpfs', str(project / 'ships')])
    assert fence < ship_bind < work_bind, 'the workspace is bound writable after its ship\'s read-only folder'


def test_a_jailed_seat_on_a_provider_pool_is_refused_and_an_endpoint_pool_still_plans(tmp_path: Path, drydock: Path, monkeypatch):
    """A jail has no network but its relayed endpoint, so a program that must reach its provider is refused at plan."""
    from quarterdeck.ship import Pool

    ship = shipmod.load(drydock)
    home = tmp_path / "home"
    path = install(home)
    (home / "work").mkdir()
    monkeypatch.setattr(sandbox, "available", lambda: True)
    seat = ship.runtime / "seats" / "w"
    plan = lambda **kw: sandbox.plan(ship, "w", seat, seat / "home", home / "work", "pi", path, home=home, **kw)
    for pool in (Pool("claude", "anthropic", config_dir=tmp_path / "claude"), Pool("keyed", "anthropic", api_key="KEY")):
        with pytest.raises(ShipError, match="a jailed seat has no network; only an endpoint pool's model is relayed in"):
            plan(pool=pool)
    local = Pool("local", "local", endpoint="http://127.0.0.1:8000/v1")
    jail = plan(pool=local, endpoint=local.endpoint)
    assert jail.endpoint == local.endpoint
    assert plan(pool=Pool("fake", "synthetic"), synthetic=True).endpoint is None, "a synthetic program needs no network"


@pytest.mark.parametrize('through_a_link', [False, True])
def test_plan_refuses_to_bind_the_operators_ssh_folder(tmp_path: Path, drydock: Path, monkeypatch, through_a_link):
    """Its connection-sharing sockets give a server's session to anything that reaches them: no jail binds ~/.ssh."""
    ship = shipmod.load(drydock)
    home = tmp_path / "home"
    path = install(home)
    (home / "work").mkdir()
    (home / ".ssh" / "bin").mkdir(parents=True)
    entry = home / ".ssh" / "bin"
    if through_a_link:
        (home / "keys").symlink_to(home / ".ssh")
        entry = home / "keys" / "bin"
    monkeypatch.setattr(sandbox, "available", lambda: True)
    seat = ship.runtime / "seats" / "w"
    with pytest.raises(ShipError, match=r"is the operator's ~/.ssh or lies inside it"):
        sandbox.plan(ship, "w", seat, seat / "home", home / "work", "pi", f"{entry}{os.pathsep}{path}", home=home)
