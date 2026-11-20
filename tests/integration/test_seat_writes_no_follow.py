"""A jail's links in its own seat folder steer no write of the service or of qd, in real bubblewrap.

A jail for worker-1 plants, in its own seat folder, launch.sh and a file the harness writes in the home as links to
decoy files outside. The next launch and a reseed leave every decoy byte for byte as it was, and the launch still
works: each planted link is replaced by a plain file, never followed. launch.sh itself is written in the seat's
trusted folder, which no jail sees, so the link planted at its old name is never written at all.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from quarterdeck import credential, sandbox, seat as seatmod, ship as shipmod, trusted_files
from quarterdeck.harness import home as harness_home
from quarterdeck.ship import Pool

from .test_sandbox import namespaces_allowed

pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap cannot make a namespace here")

DECOY = "an operator's file the jail must never reach\n"
PROBE = r"""
import json, os, sys
pairs = sys.argv[1:]
planted = {}
for link, target in zip(pairs[::2], pairs[1::2]):
    try:
        os.makedirs(os.path.dirname(link), exist_ok=True)
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(target, link)
        planted[link] = os.path.islink(link)
    except OSError as error:
        planted[link] = repr(error)
print(json.dumps(planted))
"""


@pytest.fixture
def ship(drydock: Path) -> shipmod.Ship:
    loaded = shipmod.load(drydock)
    shipmod.fence(loaded)
    return loaded


def plant(ship: shipmod.Ship, tmp_path: Path, links: dict[Path, Path]) -> None:
    """worker-1's jail makes each link in its own seat folder, pointing at a decoy outside it."""
    own = ship.runtime / "seats" / "worker-1"
    (own / "home").mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "jail-work"
    workspace.mkdir(exist_ok=True)
    pairs = []
    for link, target in links.items():
        assert link.is_relative_to(own) and not target.is_relative_to(own)
        pairs += [str(link), str(target)]
    jail = sandbox.plan(ship, "worker-1", own, own / "home", workspace, "/usr/bin/python3", "/usr/bin", home=Path.home())
    result = subprocess.run(sandbox.command(jail, ["/usr/bin/python3", "-c", PROBE, *pairs]),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    planted = json.loads(result.stdout.strip().splitlines()[-1])
    assert all(value is True for value in planted.values()), planted
    assert all(link.is_symlink() for link in links)


def decoys(tmp_path: Path, *names: str) -> dict[str, Path]:
    folder = tmp_path / "operator"
    folder.mkdir(exist_ok=True)
    made = {}
    for name in names:
        made[name] = folder / name
        made[name].write_text(DECOY)
    return made


def test_a_planted_launch_script_and_home_file_are_replaced_never_followed_and_the_launch_works(ship, tmux, tmp_path):
    decoy = decoys(tmp_path, "launch-target", "settings-target", "trusted-target")
    own = ship.runtime / "seats" / "worker-1"
    plant(ship, tmp_path, {own / trusted_files.LAUNCH_SCRIPT: decoy["launch-target"],
                           own / "home" / "settings.json": decoy["settings-target"]})
    # launch.sh now lives in the seat's trusted folder, which no jail sees (trusted_files); were a link ever made
    # there, by anyone, the write replaces it too.
    trusted = trusted_files.make(ship.runtime, "worker-1")
    (trusted / trusted_files.LAUNCH_SCRIPT).symlink_to(decoy["trusted-target"])

    worker = seatmod.Seat(ship, ship.seat("worker-1"), tmux)
    worker.up(resume=False)

    assert all(path.read_text() == DECOY for path in decoy.values()), "no decoy was written through"
    old = own / trusted_files.LAUNCH_SCRIPT
    assert not old.exists() and not old.is_symlink(), "nothing writes it there any more"
    set_aside = list((ship.runtime / "evidence" / "seat-move").glob("worker-1-*/launch.sh"))
    assert len(set_aside) == 1 and set_aside[0].is_symlink(), "the move set the jail's link aside, as a link, unfollowed"
    script = trusted / trusted_files.LAUNCH_SCRIPT
    assert not script.is_symlink() and script.read_text().startswith("#!/bin/sh")
    assert not (own / "home" / "settings.json").is_symlink()
    assert worker.is_up, "the launch still works"
    worker.down()


def test_a_planted_credential_link_meets_the_homes_copy_and_a_reseed_without_being_followed(ship, tmux, tmp_path):
    """The home's credential, as the copy a launch makes and as a reseed relinks or brings it up, replaces a planted
    link at its name: the decoy is never written."""
    from tests.unit.test_credential import live

    decoy = decoys(tmp_path, "credential-target", "copy-target", "exchange-target")
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    (pool_dir / ".credentials.json").write_text(live("the-pools-own", 3600))
    pool = Pool("claude-secondary", "anthropic", config_dir=pool_dir)
    own = ship.runtime / "seats" / "worker-1"
    home = own / "home"
    plant(ship, tmp_path, {home / ".credentials.json": decoy["credential-target"],
                           home / ".credentials.json.qd-copy": decoy["copy-target"],
                           home / ".credentials.json.qd-tmp": decoy["exchange-target"]})

    harness_home.credential(pool, home / ".credentials.json", "ANTHROPIC_API_KEY", copy=True)  # as a launch builds it
    assert not (home / ".credentials.json").is_symlink()
    assert json.loads((home / ".credentials.json").read_text())["claudeAiOauth"]["refreshToken"] == "the-pools-own"

    worker = seatmod.Seat(ship, ship.seat("worker-1"), tmux)
    credential.reseed(ship, [worker])  # the service's reseed over this seat
    harness_home.credential(pool, home / ".credentials.json", "ANTHROPIC_API_KEY")  # and a relink, as a launch makes
    assert (home / ".credentials.json").is_symlink()
    assert (home / ".credentials.json").resolve() == (pool_dir / ".credentials.json").resolve()

    assert all(path.read_text() == DECOY for path in decoy.values()), "no decoy was written through"
    assert json.loads((pool_dir / ".credentials.json").read_text())["claudeAiOauth"]["refreshToken"] == "the-pools-own"
