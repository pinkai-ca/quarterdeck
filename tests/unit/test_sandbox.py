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
    import sys
    assert args[:5] == [sys.executable, "-I", "-m", "quarterdeck.jail_environment", "--"], "started from nothing"
    args = args[5:]
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


def test_a_jailed_launch_runs_with_the_same_qd_only_path_and_sets_that_path_alone(drydock: Path, tmp_path: Path):
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
    # The jail starts from --clearenv, so it sets PATH itself: the launch's own PATH, the same as unjailed, and no
    # other. A jail that is given none sets none.
    jail = sandbox.Jail(home=tmp_path / "home", seat_home=ship.runtime / "seats" / "worker-1" / "home",
                        seat_folder=ship.runtime / "seats" / "worker-1", workspace=tmp_path / "work",
                        ship_folder=ship.folder, runtime=ship.runtime, read_only=(), writable=(), path=path)
    args = sandbox.command(jail, ["fake"])
    assert [args[i + 2] for i, word in enumerate(args) if word == "--setenv" and args[i + 1] == "PATH"] == [path]
    from dataclasses import replace
    bare = sandbox.command(replace(jail, path=''), ["fake"])
    assert not [i for i, word in enumerate(bare) if word == "--setenv" and bare[i + 1] == "PATH"]
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


def test_plan_refuses_an_ssh_folder_that_leads_outside_the_home(tmp_path: Path, drydock: Path, monkeypatch):
    """The home's hiding covers ~/.ssh only where it lies: a link out of the home is refused."""
    ship = shipmod.load(drydock)
    home = tmp_path / "home"
    path = install(home)
    (home / "work").mkdir()
    outside = tmp_path / "elsewhere" / "ssh"
    outside.mkdir(parents=True)
    (home / ".ssh").symlink_to(outside)
    monkeypatch.setattr(sandbox, "available", lambda: True)
    seat = ship.runtime / "seats" / "w"
    with pytest.raises(ShipError, match=r"~/.ssh leads outside the home"):
        sandbox.plan(ship, "w", seat, seat / "home", home / "work", "pi", path, home=home)


def test_plan_refuses_a_bound_folder_that_holds_the_ssh_folders_target(tmp_path: Path, drydock: Path, monkeypatch):
    """A folder the jail would bind that contains where ~/.ssh really lies is refused, whatever leads there."""
    ship = shipmod.load(drydock)
    home = tmp_path / "home"
    path = install(home)
    (home / "work").mkdir()
    (home / "tools" / "keys").mkdir(parents=True)
    (home / "tools" / "bin").mkdir()
    (home / ".ssh").symlink_to(home / "tools" / "keys")  # inside the home, but within a folder the jail binds
    monkeypatch.setattr(sandbox, "available", lambda: True)
    seat = ship.runtime / "seats" / "w"
    entry = os.pathsep.join([str(home / "tools"), path])
    with pytest.raises(ShipError, match=r"holds the operator's ~/.ssh"):
        sandbox.plan(ship, "w", seat, seat / "home", home / "work", "pi", entry, home=home)



def own_cwd_ship(tmp_path: Path, hand_a_cwd=None, hand_b_cwd=None):
    """A stand-in framework checkout holding the ship, with two hands, each declared at work/hands/<name>."""
    project = tmp_path / 'project'
    (project / '.git').mkdir(parents=True, exist_ok=True)
    (project / 'pyproject.toml').write_text('')
    (project / 'src' / 'quarterdeck').mkdir(parents=True, exist_ok=True)
    (project / 'src' / 'quarterdeck' / 'sandbox.py').write_text('')
    hands = project / 'work' / 'hands'
    for name in ('hand-a', 'hand-b'):
        (hands / name).mkdir(parents=True, exist_ok=True)
    crew = tmp_path / 'crew'
    crew.mkdir(exist_ok=True)

    def seat(name, cwd):
        return shipmod.SeatSpec(name, 'crew', 'hand', 'pi', ('local',), cwd=cwd, sandbox=True)
    seats = {'captain': shipmod.SeatSpec('captain', 'crew', 'captain', 'fake', ('fake',)),
             'hand-a': seat('hand-a', hand_a_cwd or hands / 'hand-a'), 'hand-b': seat('hand-b', hand_b_cwd or hands / 'hand-b')}
    return shipmod.Ship(project / 'ships' / 'alpha', 'alpha', 'operator', crew, {}, {}, seats), hands


def plan_seat(ship, name: str, home: Path):
    own = ship.runtime / 'seats' / name
    workspace = ship.cwd_of(ship.seat(name)) if name in ship.seats else ship.workspace
    return sandbox.plan(ship, name, own, own / 'home', workspace, '/usr/bin/python3', '/usr/bin', home=home)


CWD_CASES = ['another hand', 'the hands folder', 'a link to another hand', 'the checkout',
             'a folder holding the checkout', 'another seat at the default workspace']


@pytest.mark.parametrize('case', CWD_CASES)
def test_plan_refuses_a_jailed_cwd_that_is_not_the_seats_own_naming_both_by_real_path(tmp_path, monkeypatch, case):
    """A jail binds its cwd writable: a declared cwd that is another seat's, holds one, or is (or holds) the
    framework's checkout is refused, compared by real path."""
    project, hands, link = tmp_path / 'project', tmp_path / 'project' / 'work' / 'hands', tmp_path / 'link'
    hand_a_cwd = {'another hand': hands / 'hand-b', 'the hands folder': hands, 'a link to another hand': link,
                'the checkout': project, 'a folder holding the checkout': tmp_path,
                'another seat at the default workspace': tmp_path / 'crew'}[case]
    ship, hands = own_cwd_ship(tmp_path, hand_a_cwd=hand_a_cwd)
    link.symlink_to(hands / 'hand-b')
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    home = Path('/nonexistent-operator-home') if case == 'a folder holding the checkout' else tmp_path / 'operator-home'
    with pytest.raises(ShipError) as caught:
        plan_seat(ship, 'hand-a', home)
    message = str(caught.value)
    real = Path(os.path.realpath(hand_a_cwd))
    joint = ', inside ' if case == 'the hands folder' else ', which '
    assert message.startswith(f'hand-a: sandboxed with its working directory at {real}{joint}'), message
    if case in ('the checkout', 'a folder holding the checkout'):
        assert f"the framework's checkout {project.resolve()}" in message
    elif case == 'another seat at the default workspace':
        assert f"is also seat captain's working directory ({real})" in message
    elif case == 'the hands folder':
        assert f"inside the framework's checkout {project.resolve()}" in message
    else:
        assert f"is also seat hand-b's working directory ({(hands / 'hand-b').resolve()})" in message
    assert not (ship.runtime / 'post').exists(), 'refused before anything of the launch is made'


def test_a_jailed_cwd_declared_through_a_link_to_this_seats_own_folder_is_refused(tmp_path, monkeypatch):
    """Another seat declared through a link to this seat's own folder shares it, by real path."""
    link = tmp_path / 'to-hand-a'
    ship, hands = own_cwd_ship(tmp_path, hand_b_cwd=link)
    link.symlink_to(hands / 'hand-a')
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    with pytest.raises(ShipError, match="hand-a: .*which is also seat hand-b's working directory"):
        plan_seat(ship, 'hand-a', tmp_path / 'operator-home')


def test_a_jailed_hand_at_its_own_work_hands_folder_plans(tmp_path, monkeypatch):
    ship, hands = own_cwd_ship(tmp_path)
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    jail = plan_seat(ship, 'hand-a', tmp_path / 'operator-home')
    assert jail.workspace == (hands / 'hand-a').resolve()
    args = sandbox.command(jail, ['synthetic-program'])
    assert ['--bind', str((hands / 'hand-a').resolve()), str((hands / 'hand-a').resolve())] in [args[i:i + 3] for i in range(len(args))]


@pytest.mark.parametrize('name', ['captain', 'check-pi-1a2b'])
def test_a_jailed_seat_at_the_ships_common_workspace_or_a_check_seat_still_plans(tmp_path, monkeypatch, name):
    """The rule binds a declared cwd: a seat at the ship's workspace, and qd check's undeclared seat there, plan."""
    ship, _ = own_cwd_ship(tmp_path)
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    assert plan_seat(ship, name, tmp_path / 'operator-home').workspace == (tmp_path / 'crew').resolve()


def test_a_project_holding_the_fleet_that_is_not_a_checkout_is_no_checkout(tmp_path):
    ship, _ = own_cwd_ship(tmp_path)
    assert sandbox.framework_checkouts(ship)[0] == (tmp_path / 'project').resolve()
    (tmp_path / 'project' / 'pyproject.toml').unlink()
    assert (tmp_path / 'project').resolve() not in sandbox.framework_checkouts(ship)


def test_qd_checks_temporary_seat_at_a_declared_seats_folder_plans_and_is_never_counted(tmp_path, monkeypatch):
    """qd check's temporary seat stands in for a declared one, in its folder: it is not bound, nor counted as another."""
    from dataclasses import replace
    ship, hands = own_cwd_ship(tmp_path)
    check = shipmod.SeatSpec('check-wake-pi-1a2b', 'qd-check', 'worker', 'pi', ('local',), cwd=hands / 'hand-a',
                             sandbox=True, temporary=True)
    ship = replace(ship, seats={**ship.seats, check.name: check})
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    assert plan_seat(ship, check.name, tmp_path / 'operator-home').workspace == (hands / 'hand-a').resolve()
    assert plan_seat(ship, 'hand-a', tmp_path / 'operator-home').workspace == (hands / 'hand-a').resolve()
    with pytest.raises(ShipError, match="is also seat hand-a's"):
        plan_seat(ship := replace(ship, seats={**ship.seats, check.name: replace(check, temporary=False)}),
                  check.name, tmp_path / 'operator-home')


INSIDE_CHECKOUT = ['src', 'src/quarterdeck', 'tests', '.git', '.git/hooks', 'work', 'work/hands',
                   'work/store-incremental', 'work/store-incremental/tests', 'work/queue']


@pytest.mark.parametrize('rel', INSIDE_CHECKOUT)
def test_a_jailed_cwd_inside_the_checkout_but_not_under_work_hands_is_refused(tmp_path, monkeypatch, rel):
    """Inside a framework checkout a declared jailed cwd lies at work/hands/<name> or below, or nowhere: the code, its
    history and hooks, a worker's worktree and its tests, and the captain's queue are refused by real path."""
    cwd = tmp_path / 'project' / rel
    cwd.mkdir(parents=True, exist_ok=True)
    ship, _ = own_cwd_ship(tmp_path, hand_a_cwd=cwd)
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    with pytest.raises(ShipError) as caught:
        plan_seat(ship, 'hand-a', tmp_path / 'operator-home')
    checkout = (tmp_path / 'project').resolve()
    assert str(caught.value).startswith(f"hand-a: sandboxed with its working directory at {cwd.resolve()}, inside the "
                                        f"framework's checkout {checkout}; a jailed seat works there only in a folder "
                                        f"of its own under {checkout / 'work' / 'hands'}"), caught.value
    assert not (ship.runtime / 'post').exists(), 'refused before anything of the launch is made'


def test_a_jailed_cwd_declared_through_a_link_into_the_checkouts_code_is_refused_by_its_real_path(tmp_path, monkeypatch):
    link = tmp_path / 'looks-like-a-hand'
    ship, _ = own_cwd_ship(tmp_path, hand_a_cwd=link)
    link.symlink_to(tmp_path / 'project' / 'src' / 'quarterdeck')
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    real = (tmp_path / 'project' / 'src' / 'quarterdeck').resolve()
    with pytest.raises(ShipError, match=f"hand-a: sandboxed with its working directory at {real}, inside the framework's"):
        plan_seat(ship, 'hand-a', tmp_path / 'operator-home')


def test_a_jailed_cwd_inside_another_hands_folder_is_refused_naming_it(tmp_path, monkeypatch):
    sub = tmp_path / 'project' / 'work' / 'hands' / 'hand-b' / 'sub'
    sub.mkdir(parents=True)
    ship, hands = own_cwd_ship(tmp_path, hand_a_cwd=sub)
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    with pytest.raises(ShipError) as caught:
        plan_seat(ship, 'hand-a', tmp_path / 'operator-home')
    assert str(caught.value).startswith(f"hand-a: sandboxed with its working directory at {sub.resolve()}, inside seat "
                                        f"hand-b's declared working directory ({(hands / 'hand-b').resolve()})"), caught.value


def test_a_jailed_cwd_inside_another_seats_declared_cwd_outside_any_checkout_is_refused(tmp_path, monkeypatch):
    """The rule follows a declared cwd wherever it lies: a film folder another seat declares is that seat's."""
    from dataclasses import replace
    film = tmp_path / 'film'
    (film / 'cut').mkdir(parents=True)
    ship, _ = own_cwd_ship(tmp_path, hand_a_cwd=film / 'cut')
    media = shipmod.SeatSpec('media', 'office', 'media', 'fake', ('fake',), cwd=film)
    ship = replace(ship, seats={**ship.seats, 'media': media})
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    with pytest.raises(ShipError, match=r"hand-a: .*, inside seat media's declared working directory"):
        plan_seat(ship, 'hand-a', tmp_path / 'operator-home')


def test_a_jailed_hand_below_its_own_folder_and_through_a_link_to_it_plans(tmp_path, monkeypatch):
    deeper = tmp_path / 'project' / 'work' / 'hands' / 'hand-a' / 'scratch' / 'deeper'
    deeper.mkdir(parents=True)
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    ship, _ = own_cwd_ship(tmp_path, hand_a_cwd=deeper)
    assert plan_seat(ship, 'hand-a', tmp_path / 'operator-home').workspace == deeper.resolve()
    link = tmp_path / 'to-hand-a'
    link.symlink_to(tmp_path / 'project' / 'work' / 'hands' / 'hand-a')
    ship, hands = own_cwd_ship(tmp_path, hand_a_cwd=link)
    assert plan_seat(ship, 'hand-a', tmp_path / 'operator-home').workspace == (hands / 'hand-a').resolve()


WIDE = ('wide-a', 'wide-b', 'wide-c', 'wide-d', 'wide-e', 'wide-f')


def wide_ship(tmp_path: Path, **hands):
    """The stand-in checkout as the common workspace (tmp_path/project), with six seats declared at the folder holding
    it (tmp_path), as six seats of a ship may declare ~/dev above the project."""
    from dataclasses import replace
    ship, folder = own_cwd_ship(tmp_path, **hands)
    wide = {name: shipmod.SeatSpec(name, 'crew', 'synthetic', 'fake', ('fake',), cwd=tmp_path) for name in WIDE}
    return replace(ship, workspace=tmp_path / 'project', seats={**ship.seats, **wide}), folder


def test_a_jailed_cwd_under_a_seats_folder_above_the_common_workspace_but_outside_it_is_refused_naming_both(
        tmp_path, monkeypatch):
    """A seat declared at a folder holding the common workspace is passed over only for a cwd inside the common
    workspace; tmp_path/crew/sub lies inside its folder and outside the common workspace, so it is refused."""
    (tmp_path / 'crew' / 'sub').mkdir(parents=True)
    ship, _ = wide_ship(tmp_path, hand_a_cwd=tmp_path / 'crew' / 'sub')
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    with pytest.raises(ShipError) as caught:
        plan_seat(ship, 'hand-a', tmp_path / 'operator-home')
    real, theirs = (tmp_path / 'crew' / 'sub').resolve(), tmp_path.resolve()
    assert str(caught.value).startswith(
        f"hand-a: sandboxed with its working directory at {real}, inside seat wide-a's declared working directory "
        f"({theirs})"), caught.value
    assert not (ship.runtime / 'post').exists(), 'refused before anything of the launch is made'


def test_a_hand_inside_the_common_workspace_still_plans_under_a_seat_declared_above_it(tmp_path, monkeypatch):
    """Inside the common workspace, the checkout rule rules: a hand at work/hands/<name> plans though seats are
    declared at the folder holding the project."""
    ship, hands = wide_ship(tmp_path)
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    for name in ('hand-a', 'hand-b'):
        assert plan_seat(ship, name, tmp_path / 'operator-home').workspace == (hands / name).resolve()


# The ship's own declaration shape: its seats' cwds and jails as they stand in its ship file, rebased from the
# operator's home to tmp_path/home, with every seat renamed synthetic. Six seats at ~/dev, above the project; two at
# folders of their own; four jailed hands on local endpoints at the project's work/hands/<name>; the rest at the common
# workspace, the project. No pool carries anything but its kind.
SHIP_SHAPE = """
[ship]
name = "synthetic"
workspace = "{home}/dev/quarterdeck"

[pools.subscription]
provider = "anthropic"
config_dir = "{home}/.claude"

[pools.local-a]
provider = "local"
endpoint = "http://127.0.0.1:8000/v1"

[pools.local-b]
provider = "local"
endpoint = "http://127.0.0.1:8001/v1"

[sessions.crew]
title = "Crew"

[sessions.office]
title = "Office"
human = true
{seats}"""

SHAPE_SEATS = (
    [(f'above-{n}', 'office' if n < 3 else 'crew', 'claude', 'subscription', '{home}/dev', {1: False, 5: False}.get(n))
     for n in range(6)]
    + [('own-a', 'office', 'claude', 'subscription', '{home}/career', None),
       ('own-b', 'office', 'claude', 'subscription', '{home}/dev/vision', None)]
    + [(f'hand-{n}', 'crew', harness, pool, '{home}/dev/quarterdeck/work/hands/hand-' + str(n), None)
       for n, (harness, pool) in enumerate([('pi', 'local-a'), ('opencode', 'local-a'), ('opencode', 'local-b'),
                                            ('pi', 'local-b')])]
    + [(f'common-{n}', 'crew', 'claude', 'subscription', None, None) for n in range(15)])


def shape_ship(tmp_path: Path) -> shipmod.Ship:
    home = tmp_path / 'home'
    project = home / 'dev' / 'quarterdeck'
    (project / '.git').mkdir(parents=True)
    (project / 'pyproject.toml').write_text('')
    (project / 'src' / 'quarterdeck').mkdir(parents=True)
    (project / 'src' / 'quarterdeck' / 'sandbox.py').write_text('')
    seats = []
    for name, session, harness, pool, cwd, jail in SHAPE_SEATS:
        lines = [f'[seats.{name}]', f'session = "{session}"', f'role = "{"hand" if harness != "claude" else "synthetic"}"',
                 f'harness = "{harness}"', f'pool = "{pool}"', 'model = "synthetic-model"']
        if cwd is not None:
            folder = Path(cwd.format(home=home))
            folder.mkdir(parents=True, exist_ok=True)
            lines.append(f'cwd = "{folder}"')
        if jail is not None:
            lines.append(f'sandbox = {"true" if jail else "false"}')
        seats.append('\n'.join(lines))
    text = SHIP_SHAPE.format(home=home, seats='\n\n' + '\n\n'.join(seats) + '\n')
    (project / 'ships' / 'synthetic').mkdir(parents=True)
    return shipmod._from_text(text, project / 'ships' / 'synthetic')


def test_the_ships_own_declaration_shape_plans_every_hand_and_refuses_one_moved_outside_the_project(
        tmp_path, monkeypatch):
    ship = shape_ship(tmp_path)
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    hands = [name for name, spec in ship.seats.items() if ship.jailed(spec)]
    assert hands == ['hand-0', 'hand-1', 'hand-2', 'hand-3'], 'only the hands are jailed, as on the ship'
    work = tmp_path / 'home' / 'dev' / 'quarterdeck' / 'work' / 'hands'
    for name in hands:
        assert plan_seat(ship, name, tmp_path / 'operator-home').workspace == (work / name).resolve()
    from dataclasses import replace
    moved = tmp_path / 'home' / 'dev' / 'other'
    moved.mkdir()
    ship = replace(ship, seats={**ship.seats, 'hand-0': replace(ship.seat('hand-0'), cwd=moved)})
    with pytest.raises(ShipError, match=f"hand-0: sandboxed with its working directory at {moved.resolve()}, inside "
                                        f"seat above-0's declared working directory"):
        plan_seat(ship, 'hand-0', tmp_path / 'operator-home')


def office_with_desk(tmp_path: Path, desk_cwd=None):
    """A stand-in checkout holding the office, whose jailed desk declares a cwd (by default the office's work/desk)."""
    own_cwd_ship(tmp_path)  # the checkout itself
    office = tmp_path / 'project' / 'office'
    (office / 'work' / 'desk').mkdir(parents=True, exist_ok=True)
    desk = shipmod.SeatSpec('desk', 'office', 'desk', 'pi', ('local',), cwd=desk_cwd or office / 'work' / 'desk',
                            sandbox=True)
    return shipmod.Ship(office, 'office', 'operator', tmp_path / 'crew', {}, {}, {'desk': desk})


def test_a_jailed_office_desk_at_the_offices_own_work_desk_plans(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    office = office_with_desk(tmp_path)
    jail = plan_seat(office, 'desk', tmp_path / 'operator-home')
    assert jail.workspace == (tmp_path / 'project' / 'office' / 'work' / 'desk').resolve()


@pytest.mark.parametrize('case', ['the office work folder itself', 'a link from outside to the office work folder',
                                  'a link in the office work folder into the code', 'the office a link in the checkout'])
def test_a_jailed_office_cwd_that_is_not_its_own_folder_under_office_work_is_refused(tmp_path, monkeypatch, case):
    """Only office/work/<name> or below, by real path, in an office that is a real folder of the checkout."""
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    project = tmp_path / 'project'
    if case == 'the office work folder itself':
        office = office_with_desk(tmp_path, desk_cwd=project / 'office' / 'work')
        real = (project / 'office' / 'work').resolve()
    elif case == 'a link from outside to the office work folder':
        office = office_with_desk(tmp_path, desk_cwd=tmp_path / 'to-office-work')
        (tmp_path / 'to-office-work').symlink_to(project / 'office' / 'work')
        real = (project / 'office' / 'work').resolve()
    elif case == 'a link in the office work folder into the code':
        office = office_with_desk(tmp_path, desk_cwd=project / 'office' / 'work' / 'code')
        (project / 'office' / 'work' / 'code').symlink_to(project / 'src' / 'quarterdeck')
        real = (project / 'src' / 'quarterdeck').resolve()
    else:
        own_cwd_ship(tmp_path)
        (project / 'elsewhere' / 'work' / 'desk').mkdir(parents=True)
        (project / 'office').symlink_to(project / 'elsewhere')
        office = office_with_desk(tmp_path)
        real = (project / 'elsewhere' / 'work' / 'desk').resolve()
    with pytest.raises(ShipError) as caught:
        plan_seat(office, 'desk', tmp_path / 'operator-home')
    assert str(caught.value).startswith(f"desk: sandboxed with its working directory at {real}, inside the framework's "
                                        f"checkout {project.resolve()}"), caught.value


def test_a_crew_seat_at_the_offices_work_folder_is_still_refused(tmp_path, monkeypatch):
    """The office's work folder is the office's own: a crew ship's jailed seat declared there stays refused."""
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    office_with_desk(tmp_path)
    (tmp_path / 'project' / 'office' / 'work' / 'other').mkdir()
    ship, _ = own_cwd_ship(tmp_path, hand_a_cwd=tmp_path / 'project' / 'office' / 'work' / 'other')
    with pytest.raises(ShipError, match='hand-a: sandboxed with its working directory at '):
        plan_seat(ship, 'hand-a', tmp_path / 'operator-home')


def test_an_office_given_through_a_link_of_another_name_is_ruled_by_its_real_path(tmp_path, monkeypatch):
    """The checkout is found from the office's real folder, not its name: through a link called myoffice, a desk cwd
    in the checkout's code is refused, and the office's own work/desk still plans."""
    from dataclasses import replace
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    office = office_with_desk(tmp_path)
    project = tmp_path / 'project'
    (tmp_path / 'myoffice').symlink_to(project / 'office')
    linked = replace(office, folder=tmp_path / 'myoffice')
    assert sandbox.fleet_roots(linked.folder) == (project.resolve() / 'ships', project.resolve() / 'office')
    assert project.resolve() in sandbox.framework_checkouts(linked)
    code = replace(linked, seats={'desk': replace(linked.seats['desk'], cwd=project / 'src' / 'quarterdeck')})
    with pytest.raises(ShipError) as caught:
        plan_seat(code, 'desk', tmp_path / 'operator-home')
    assert str(caught.value).startswith(f"desk: sandboxed with its working directory at "
                                        f"{(project / 'src' / 'quarterdeck').resolve()}, inside the framework's "
                                        f"checkout {project.resolve()}"), caught.value
    jail = plan_seat(linked, 'desk', tmp_path / 'operator-home')
    assert jail.workspace == (project / 'office' / 'work' / 'desk').resolve()


def ship_below_home(tmp_path: Path):
    """A throwaway home in the test's own folder, with the ship folder and the workspace below it: a ~/.ssh that
    leads into either stays inside the home, so only the check of what the jail binds can refuse it."""
    home = tmp_path / "home"
    folder = home / "dev" / "ship"
    folder.mkdir(parents=True)
    (home / "work").mkdir()
    ship = shipmod.Ship(folder, 'synthetic', 'operator', home / "work", {}, {}, {})
    return ship, home, ship.runtime / "seats" / "worker"


@pytest.mark.parametrize("inside", ["ship folder", "seat folder"])
def test_plan_refuses_an_ssh_folder_whose_target_lies_inside_the_ship_or_seat_folder(tmp_path: Path, monkeypatch, inside):
    """The ship folder and the seat's own folder are bound too: a ~/.ssh whose real path lies inside either would be
    shown inside the jail, keys and all, so it is refused with the same reason as any other bound folder."""
    ship, home, own = ship_below_home(tmp_path)
    target = {"ship folder": ship.folder / "keys", "seat folder": own / "keys"}[inside]
    target.mkdir(parents=True)
    (home / ".ssh").symlink_to(target)
    monkeypatch.setattr(sandbox, "available", lambda: True)
    with pytest.raises(ShipError, match=r"holds the operator's ~/.ssh"):
        sandbox.plan(ship, "worker", own, own / "home", home / "work", "/usr/bin/python3", "/usr/bin", home=home)


@pytest.mark.parametrize("endpoint", [None, "http://127.0.0.1:8000/v1"])
def test_every_bind_the_command_makes_is_in_the_checked_set(tmp_path: Path, monkeypatch, endpoint):
    """The ~/.ssh check reads shown(), from the one list the command binds from. Compared with the command's own bind
    arguments, every source it binds (but the root's own, which the home's tmpfs and the outside-home rule cover) is
    checked: a bind added to the command and left out of the check fails here."""
    import dataclasses
    ship, home, own = ship_below_home(tmp_path)
    monkeypatch.setattr(sandbox, "available", lambda: True)
    jail = sandbox.plan(ship, "worker", own, own / "home", home / "work", "/usr/bin/python3", "/usr/bin", home=home)
    if endpoint is not None:
        jail = dataclasses.replace(jail, endpoint=endpoint)
    args = sandbox.command(jail, ["true"])
    bound = {args[i + 1] for i, word in enumerate(args[:-1])
             if word in ("--bind", "--ro-bind", "--bind-try", "--ro-bind-try")} - {"/"}
    checked = {str(path) for path in sandbox.shown(jail)}
    missing = bound - checked
    assert not missing, f"bound by the command but never checked for ~/.ssh: {sorted(missing)}"
    assert str(jail.ship_folder) in checked and str(jail.seat_folder) in checked
    if endpoint is not None:
        assert any("/etc/hosts" == args[i + 2] for i, word in enumerate(args[:-2]) if word == "--ro-bind")
