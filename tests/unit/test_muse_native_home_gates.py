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
