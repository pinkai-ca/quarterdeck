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


def test_the_command_hides_the_home_binds_the_workspace_writable_and_the_ship_read_only_in_depth_order(tmp_path: Path):
    home = tmp_path / "home"
    ship_folder = home / "dev" / "project" / "ships" / "x"
    runtime = ship_folder / "runtime"
    jail = sandbox.Jail(home=home, seat_home=runtime / "seats" / "w" / "home", seat_folder=runtime / "seats" / "w",
                        workspace=home / "dev" / "project", ship_folder=ship_folder, runtime=runtime,
                        read_only=(home / ".local" / "bin", home / ".opencode"), writable=(runtime / "post", runtime / "check"))
    args = sandbox.command(jail, ["pi", "--model", "m"])
    assert args[:13] == ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--size", str(sandbox.TMP_BYTES), "--tmpfs", "/tmp", "--tmpfs"]
    assert args[13] == str(home), "the operator's home is an empty tmpfs before anything under it is bound back"
    assert args[-9:] == ["--setenv", "HOME", str(runtime / "seats" / "w" / "home"), "--unshare-pid", "--die-with-parent", "--", "pi", "--model", "m"]
    text = " ".join(args)
    workspace, ship, seats, seat, post = (text.index(f"--bind {home / 'dev' / 'project'} "), text.index(f"--ro-bind {ship_folder} "),
                                         text.index(f"--tmpfs {runtime / 'seats'}"), text.index(f"--bind {runtime / 'seats' / 'w'} "),
                                         text.index(f"--bind-try {runtime / 'post'} "))
    assert workspace < ship < seats < seat, "a deeper mount comes later, so the ship folder is read-only inside the writable workspace and this seat is writable inside the hidden seats"
    assert ship < post, "the post store is writable inside the read-only ship folder"
    hidden = text.index(f"--perms 0700 --tmpfs {runtime / 'tmux'}")
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
    assert home / ".local" / "node" in jail.read_only and jail.writable == (ship.runtime / "post", ship.runtime / "check")


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
    over = [args[i + 1] for i, word in enumerate(args) if word in ("--tmpfs", "--bind", "--bind-try", "--ro-bind-try")
            and i + 1 < len(args) and ship.folder in Path(args[i + 1]).parents
            and (commands == Path(args[i + 1]) or Path(args[i + 1]) in commands.parents)]
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
