"""How a Muse home reaches the pool's credential: one link, never a copy.

The marker below is synthetic. The tests check that it is not written into
settings, the seed proof, the launch plan, or the command's output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quarterdeck.harness.muse import MuseHarness
from quarterdeck.ship import Pool, SeatSpec, ShipError

MARKER = "synthetic-pool-marker\n"
REFRESHED = "synthetic-refreshed-marker\n"


@pytest.fixture
def binary(monkeypatch):
    monkeypatch.setattr("quarterdeck.harness.muse_program.binary",
                        lambda **kwargs: Path("/synthetic/bin/muse-bin-1.4.20-R1.1"))


def layout(tmp_path, *, model="synthetic-catalog-model", provider="meta", settings=None):
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    source = pool_dir / "auth.json"
    source.write_text(MARKER)
    template = tmp_path / "template"
    template.mkdir()
    body = dict(provider=provider, model=model)
    if settings:
        body.update(settings)
    (template / "settings.json").write_text(json.dumps(body))
    spec = SeatSpec("synthetic-seat", "synthetic-session", "worker", "muse", ("synthetic-pool",),
                    cwd=tmp_path, model=model)
    pool = Pool("synthetic-pool", "meta", config_dir=pool_dir)
    return MuseHarness(), spec, pool, template, tmp_path / "home", source


def test_the_pool_file_is_linked_and_a_rewrite_lands_in_the_pool(tmp_path, binary, capsys):
    harness, spec, pool, template, home, source = layout(tmp_path)
    result = harness.build_home(home, spec, pool, template)
    attached, native = home / "auth.json", home / "config/muse/auth.json"
    assert attached.is_symlink() and attached.resolve() == source.resolve()
    assert native.is_symlink() and native.readlink() == attached
    assert attached.stat().st_ino == source.stat().st_ino
    attached.write_text(REFRESHED)
    assert source.read_text() == REFRESHED and attached.is_symlink()
    stored = (home / "config/muse/settings.json").read_text() + (home / "muse-settings-seed.json").read_text()
    assert MARKER.strip() not in stored and REFRESHED.strip() not in stored
    assert MARKER.strip() not in repr(result)
    brief = tmp_path / "brief.txt"
    brief.write_text("A fictional brief.")
    launch = harness.launch(spec, pool, home, brief, None, None)
    planned = " ".join(launch.argv) + "\n" + "\n".join(launch.env.values()) + "\n" + (launch.notice or "")
    assert MARKER.strip() not in planned and REFRESHED.strip() not in planned
    captured = capsys.readouterr()
    assert MARKER.strip() not in captured.out and MARKER.strip() not in captured.err


def test_an_echo_home_links_nothing(tmp_path, binary):
    harness, spec, pool, template, home, source = layout(tmp_path, provider="echo")
    assert harness.build_home(home, spec, pool, template) is None
    assert not (home / "auth.json").exists()
    assert not (home / "config/muse/auth.json").exists()
    assert source.read_text() == MARKER


@pytest.mark.parametrize("place", ["auth.json", "config/muse/auth.json"])
def test_a_detached_regular_file_is_kept_and_refused(tmp_path, binary, place):
    harness, spec, pool, template, home, source = layout(tmp_path)
    home.mkdir()
    (home / "config/muse").mkdir(parents=True)
    path = home / place
    path.write_text("synthetic-detached-marker\n")
    with pytest.raises(ShipError, match="detached Muse credential retained"):
        harness.build_home(home, spec, pool, template)
    assert path.is_file() and not path.is_symlink()
    assert path.read_text() == "synthetic-detached-marker\n"
    assert source.read_text() == MARKER


def test_a_native_link_with_another_target_is_kept_for_review(tmp_path, binary):
    harness, spec, pool, template, home, source = layout(tmp_path)
    other = tmp_path / "other-marker.json"
    other.write_text("synthetic-other-marker\n")
    home.mkdir()
    native_dir = home / "config/muse"
    native_dir.mkdir(parents=True)
    native = native_dir / "auth.json"
    native.symlink_to(other)
    with pytest.raises(ShipError, match="another target"):
        harness.build_home(home, spec, pool, template)
    assert native.is_symlink() and native.readlink() == other
    assert other.read_text() == "synthetic-other-marker\n"
    assert source.read_text() == MARKER
