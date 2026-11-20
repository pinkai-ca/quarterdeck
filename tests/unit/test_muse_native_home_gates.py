"""The fixed-path mask's proofs, on the native HOME/XDG home.

No bubblewrap wrapper remains. A launch's command is the versioned binary, and
its HOME, XDG roots and TMPDIR are directories the adapter creates under the
seat home. Nothing here executes Muse or reads a credential.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from quarterdeck.harness.muse import MuseHarness
from quarterdeck.harness import muse_home
from quarterdeck.seat import launch_words
from quarterdeck.ship import Pool, SeatSpec, ShipError


def binary(monkeypatch):
    monkeypatch.setattr("quarterdeck.harness.muse_program.binary",
                        lambda **kwargs: Path("/synthetic/bin/muse-bin-1.4.20-R1.1"))


def echo_launch(tmp_path, monkeypatch, *, offline: bool = False):
    binary(monkeypatch)
    template = tmp_path / "template"
    template.mkdir()
    (template / "settings.json").write_text(json.dumps(dict(provider="echo", model="synthetic-catalog-model")))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = SeatSpec("synthetic-seat", "synthetic-session", "worker", "muse", ("synthetic-pool",),
                    cwd=workspace, model=None)
    pool = Pool("synthetic-pool", "meta", config_dir=tmp_path / "unused-pool")
    home = tmp_path / "home"
    brief = tmp_path / "brief.txt"
    brief.write_text("A fictional brief.")
    harness = MuseHarness()
    harness.build_home(home, spec, pool, template)
    return harness, harness.launch(spec, pool, home, brief, None, "A fictional open message.", offline=offline), home


def test_launch_is_the_versioned_binary_under_the_seat_home_and_never_a_mask(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "operator-home"))
    monkeypatch.setenv("META_API_KEY", "synthetic-operator-key")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "operator-config"))
    harness, launch, home = echo_launch(tmp_path, monkeypatch)
    assert launch.argv[0] == "/synthetic/bin/muse-bin-1.4.20-R1.1"
    assert "/usr/bin/bwrap" not in launch.argv
    assert not any(flag.startswith(("--bind", "--dev-bind", "--ro-bind", "--unshare", "--tmpfs"))
                   for flag in launch.argv)
    assert harness.owns_launch(launch.argv)
    assert not harness.owns_launch(("/usr/bin/bwrap", "--dev-bind", "/", "/", *launch.argv))
    env = launch.env
    assert env["HOME"] == str(home / "user-home")
    assert env["XDG_CONFIG_HOME"] == str(home / "config")
    assert env["XDG_DATA_HOME"] == str(home / "data")
    assert env["XDG_CACHE_HOME"] == str(home / "cache")
    assert env["XDG_STATE_HOME"] == str(home / "state")
    assert env["TMPDIR"] == str(home / "tmp")
    assert set(env) == {"HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                        "XDG_STATE_HOME", "MUSE_NO_AUTO_UPDATE", "TMPDIR"}
    assert "META_API_KEY" not in env and "synthetic-operator-key" not in env.values()
    assert not (tmp_path / "operator-home").exists()
    words = launch_words("/synthetic/path", list(launch.argv), socket=None, tmux_tmpdir=tmp_path / "tmux",
                         markers=harness.launch_unset)
    for name in ("META_API_KEY", "MUSE_AUTH_PATH", "MUSE_LOGIN"):
        assert words[words.index(name) - 1] == "-u"
        assert f"{name}=synthetic-operator-key" not in words
    assert "QD_SHIP" not in harness.launch_unset and "QD_SEAT" not in harness.launch_unset


def test_offline_refuses_instead_of_planning_a_private_network(tmp_path, monkeypatch):
    with pytest.raises(ShipError, match="no adapter-owned offline wrapper"):
        echo_launch(tmp_path, monkeypatch, offline=True)


def test_prepare_creates_owned_mode_0700_directories_and_refuses_a_linked_one(tmp_path):
    home = tmp_path / "home"
    muse_home.prepare(home)
    for name in ("user-home", "config", "config/muse", "data", "cache", "state", "tmp"):
        info = (home / name).lstat()
        assert stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
        assert info.st_uid == os.getuid()
    folder = home / "tmp"
    folder.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    folder.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ShipError, match="not an owned real directory"):
        muse_home.prepare(home)
    assert not (elsewhere / "config").exists()


FOLDERS = ("user-home", "config", "config/muse", "data", "cache", "state", "tmp")


def echo_parts(tmp_path, monkeypatch):
    binary(monkeypatch)
    template = tmp_path / "template"
    template.mkdir()
    (template / "settings.json").write_text(json.dumps(dict(provider="echo", model="synthetic-catalog-model")))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = SeatSpec("synthetic-seat", "synthetic-session", "worker", "muse", ("synthetic-pool",),
                    cwd=workspace, model=None)
    pool = Pool("synthetic-pool", "meta", config_dir=tmp_path / "unused-pool")
    brief = tmp_path / "brief.txt"
    brief.write_text("A fictional brief.")
    return MuseHarness(), spec, pool, template, tmp_path / "home", brief


def modes(home):
    return {name: stat.S_IMODE((home / name).lstat().st_mode) for name in ("", *FOLDERS)}


def widen(home, mode):
    for name in ("", *FOLDERS):
        os.chmod(home / name, mode)


@pytest.mark.parametrize("mode", [0o755, 0o777])
def test_a_widened_home_is_tightened_at_build_and_at_launch_and_the_launch_goes_ahead(tmp_path, monkeypatch, mode):
    harness, spec, pool, template, home, brief = echo_parts(tmp_path, monkeypatch)
    harness.build_home(home, spec, pool, template)
    widen(home, mode)
    harness.build_home(home, spec, pool, template)
    assert set(modes(home).values()) == {0o700}
    widen(home, mode)
    launch = harness.launch(spec, pool, home, brief, None, "A fictional open message.")
    assert launch.argv[0] == "/synthetic/bin/muse-bin-1.4.20-R1.1"
    assert set(modes(home).values()) == {0o700}
    assert all((home / name).lstat().st_uid == os.getuid() for name in ("", *FOLDERS))


def swap_in_link(home, name, tmp_path):
    """Replace one folder with a link to a widened directory holding a file."""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "muse").mkdir(parents=True)
    (elsewhere / "kept.txt").write_text("synthetic content")
    os.chmod(elsewhere / "kept.txt", 0o664)
    os.chmod(elsewhere / "muse", 0o775)
    os.chmod(elsewhere, 0o755)
    folder = home / name
    for child in sorted(folder.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        child.unlink() if not child.is_dir() or child.is_symlink() else child.rmdir()
    folder.rmdir()
    folder.symlink_to(elsewhere, target_is_directory=True)
    return elsewhere


def behind(elsewhere):
    return {str(item.relative_to(elsewhere)): (stat.S_IMODE(item.lstat().st_mode),
                                               item.read_bytes() if item.is_file() else None)
            for item in (elsewhere, *elsewhere.rglob("*"))}


@pytest.mark.parametrize("name", FOLDERS)
@pytest.mark.parametrize("step", ["build", "launch"])
def test_a_link_swapped_in_at_a_folder_is_refused_and_nothing_behind_it_changes(tmp_path, monkeypatch, name, step):
    harness, spec, pool, template, home, brief = echo_parts(tmp_path, monkeypatch)
    harness.build_home(home, spec, pool, template)
    widen(home, 0o755)
    elsewhere = swap_in_link(home, name, tmp_path)
    before, home_before = behind(elsewhere), stat.S_IMODE(home.lstat().st_mode)
    with pytest.raises(ShipError, match="not an owned real directory"):
        if step == "build":
            harness.build_home(home, spec, pool, template)
        else:
            harness.launch(spec, pool, home, brief, None, "A fictional open message.")
    assert behind(elsewhere) == before
    assert (home / name).is_symlink()
    # Every folder is proved before any is mended: the refusal changes no mode at all.
    assert stat.S_IMODE(home.lstat().st_mode) == home_before == 0o755


@pytest.mark.parametrize("name", FOLDERS)
def test_a_link_swapped_in_after_the_path_proof_is_refused_by_the_descriptor(tmp_path, monkeypatch, name):
    home = tmp_path / "home"
    muse_home.prepare(home)
    elsewhere = swap_in_link(home, name, tmp_path)
    before = behind(elsewhere)
    # The swap lands between directory()'s proof and the open: only the descriptor sees it.
    monkeypatch.setattr(muse_home, "directory", lambda path, create=False: None)
    with pytest.raises(ShipError, match="not an owned real directory"):
        muse_home.owner_only(home)
    assert behind(elsewhere) == before


def test_a_linked_home_is_refused_by_the_descriptor(tmp_path, monkeypatch):
    real = tmp_path / "real"
    muse_home.prepare(real)
    os.chmod(real, 0o755)
    home = tmp_path / "home"
    home.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(muse_home, "directory", lambda path, create=False: None)
    with pytest.raises(ShipError, match="not an owned real directory"):
        muse_home.owner_only(home)
    assert stat.S_IMODE(real.lstat().st_mode) == 0o755


def test_a_mode_that_will_not_hold_refuses(tmp_path, monkeypatch):
    home = tmp_path / "home"
    muse_home.prepare(home)
    os.chmod(home / "data", 0o755)
    monkeypatch.setattr(muse_home.os, "fchmod", lambda descriptor, mode: None)
    with pytest.raises(ShipError, match="still not mode 0700"):
        muse_home.owner_only(home)
