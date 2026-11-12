"""Claude-owned real-bwrap gates for the Muse fixed-path mask, with a synthetic program in Muse's place.

The password-database home is a synthetic folder under the test's own tmp, so the two fixed targets
are synthetic; /tmp is the one host target, and only names this test invents are looked for there.
No native Muse, provider or credential is involved.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from quarterdeck import post, ship as shipmod
from quarterdeck.harness import muse_mask

from .test_sandbox import namespaces_allowed

pytestmark = pytest.mark.skipif(not namespaces_allowed(),
                                reason="bubblewrap is not installed, or cannot make a namespace here (inside a jail)")

PROBE = r'''
set -eu
printf authority > "$1/session-name-authority-synthetic"
printf config > "$2/settings-synthetic"
printf temporary > "/tmp/$3"
readlink /proc/self/ns/net > "$4/net-namespace"
tail -n +3 /proc/net/dev | cut -d: -f1 | tr -d ' ' > "$4/interfaces"
exit "$5"
'''


@pytest.fixture
def masked(tmp_path, monkeypatch):
    passwd = tmp_path / "synthetic-passwd-home"
    (passwd / ".local/share/muse").mkdir(parents=True)
    (passwd / ".config/muse").mkdir(parents=True)
    monkeypatch.setattr(muse_mask.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(passwd)))
    home, out = tmp_path / "synthetic-seat-home", tmp_path / "synthetic-out"
    out.mkdir()
    muse_mask.prepare(home)
    return SimpleNamespace(passwd=passwd, home=home, out=out, cwd=tmp_path, name=f"qd-m1-synthetic-{os.getpid()}-wxyz")


def run(masked, status=0, offline=False):
    argv = ("/bin/sh", "-c", PROBE, "probe", str(muse_mask.authority_target()), str(muse_mask.config_target()),
            masked.name, str(masked.out), str(status))
    return subprocess.run(muse_mask.command(masked.home, masked.cwd, argv, offline=offline),
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("offline", [False, True], ids=["production", "offline"])
def test_fixed_path_and_tmp_writes_land_in_the_seat_and_the_targets_stay_untouched(masked, offline):
    done = run(masked, offline=offline)
    assert done.returncode == 0, done.stderr
    assert (masked.home / "mask/passwd-muse/session-name-authority-synthetic").read_text() == "authority"
    assert (masked.home / "mask/passwd-config/settings-synthetic").read_text() == "config"
    assert (masked.home / "mask/tmp" / masked.name).read_text() == "temporary"
    assert list((masked.passwd / ".local/share/muse").iterdir()) == []
    assert list((masked.passwd / ".config/muse").iterdir()) == []
    assert not (Path("/tmp") / masked.name).exists()


@pytest.mark.parametrize("offline", [False, True], ids=["production", "offline"])
def test_only_the_offline_switch_gives_the_program_its_own_empty_network(masked, offline):
    assert run(masked, offline=offline).returncode == 0
    inner = (masked.out / "net-namespace").read_text().strip()
    interfaces = (masked.out / "interfaces").read_text().split()
    assert (inner != os.readlink("/proc/self/ns/net")) is offline
    if offline:
        assert interfaces == ["lo"]


def test_the_programs_failure_status_is_kept_and_nothing_stays_mounted(masked):
    assert run(masked, status=3).returncode == 3
    mounts = Path("/proc/self/mountinfo").read_text()
    assert str(masked.home) not in mounts and str(masked.passwd) not in mounts


def test_qd_post_from_inside_the_mask_reaches_the_ship(masked, drydock):
    """Proves the mask and seat environment let a seat post; Muse's own tool path is not exercised here."""
    store = post.Store(shipmod.load(drydock).runtime)
    order = store.new("order", "captain", "worker-1", "A fictional order.")
    qd = str(Path(sys.executable).parent / "qd")
    environment = {key: value for key, value in os.environ.items() if key not in ("QD_SHIP", "QD_SEAT")}
    environment.update(QD_SHIP=str(drydock), QD_SEAT="worker-1", TMPDIR=str(masked.home / "mask/tmp"))
    done = subprocess.run(muse_mask.command(masked.home, masked.cwd,
                                            (qd, "post", "report", "--re", order.id, "synthetic masked report")),
                          env=environment, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    report = store.closing_report(order)
    assert report is not None and report.sender == "worker-1" and report.body == "synthetic masked report"  # law: can-post
