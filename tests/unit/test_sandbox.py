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
