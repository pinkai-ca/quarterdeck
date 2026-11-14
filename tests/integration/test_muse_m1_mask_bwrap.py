"""A real process under Muse's planned environment, in place of the old mask.

The adapter no longer wraps the program. This runs /bin/sh with only the
environment a launch would pass, and checks that its writes land in the seat
home. The synthetic password-database folders stand in for the operator paths
the mask used to cover. No native Muse and no credential is involved.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from quarterdeck.harness import muse_home


def test_planned_environment_writes_land_in_the_seat_and_the_operator_paths_stay_empty(tmp_path, monkeypatch):
    home = tmp_path / "synthetic-seat-home"
    passwd = tmp_path / "synthetic-passwd-home"
    (passwd / ".local/share/muse").mkdir(parents=True)
    (passwd / ".config/muse").mkdir(parents=True)
    muse_home.prepare(home)
    monkeypatch.setenv("HOME", str(passwd))
    name = f"qd-muse-synthetic-{os.getpid()}"
    script = (
        "set -eu\n"
        "printf seat > \"$HOME/landed\"\n"
        "printf data > \"$XDG_DATA_HOME/landed\"\n"
        "printf config > \"$XDG_CONFIG_HOME/landed\"\n"
        f"printf temporary > \"$TMPDIR/{name}\"\n"
    )
    done = subprocess.run(("/bin/sh", "-c", script), env=muse_home.environment(home),
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert (home / "user-home/landed").read_text() == "seat"
    assert (home / "data/landed").read_text() == "data"
    assert (home / "config/landed").read_text() == "config"
    assert (home / "tmp" / name).read_text() == "temporary"
    assert list((passwd / ".local/share/muse").iterdir()) == []
    assert list((passwd / ".config/muse").iterdir()) == []
    assert not (Path("/tmp") / name).exists()
    assert "seat" not in (done.stdout + done.stderr)
