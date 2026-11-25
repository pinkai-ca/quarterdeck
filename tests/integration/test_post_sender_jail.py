"""What qd sees of a seat from inside its jail, when a command there lost QD_SEAT (post_sender).

Real bubblewrap and the fake program, no model, on a synthetic ship. A probe inside the jail records verdicts
only: the names of the processes its ancestry reaches, whether the launch records are visible, and whether HOME is
the seat's home. Then the same jailed shell posts with QD_SEAT unset, and nothing goes out in the operator's name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quarterdeck import post, seat as seatmod, ship as shipmod
from quarterdeck.door import Door
from quarterdeck.tmux import Tmux

from .test_sandbox import namespaces_allowed
from .waiting import wait_for

pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap is not installed, or cannot make a namespace here (inside a jail)")

PROBE = r"""
import json, os, sys
from pathlib import Path
out, runtime, seat = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
names, pid, seen = [], os.getpid(), set()
while pid > 0 and pid not in seen:
    seen.add(pid)
    try:
        names.append(Path(f'/proc/{pid}/comm').read_text().strip())
        pid = int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        break
home = os.path.realpath(os.environ.get('HOME', ''))
out.write_text(json.dumps(dict(
    ancestry=names, pid1=Path('/proc/1/comm').read_text().strip(),
    trusted_visible=(runtime / 'trusted').exists(),
    own_launch_visible=(runtime / 'trusted' / seat / 'launch.json').exists(),
    home_is_seat_home=home == os.path.realpath(runtime / 'seats' / seat / 'home'),
    qd_seat_set='QD_SEAT' in os.environ)))
"""


def test_a_jailed_shell_without_qd_seat_posts_nothing_in_the_operators_name(drydock: Path, tmux: Tmux, tmp_path: Path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "probe.py").write_text(PROBE)
    text = (drydock / "ship.toml").read_text()
    text = shipmod.edit(text, "seats.worker-1", "sandbox", True)
    text = shipmod.edit(text, "seats.worker-1", "cwd", str(workspace))
    (drydock / "ship.toml").write_text(text + '\n[seats.worker-1.settings]\nstep_seconds = "0.05"\n')
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat("worker-1"), tmux)
    store = post.Store(ship.runtime)
    try:
        seat.up(resume=False)
        wait_for(lambda: Door(seat).reading().open)
        message = store.new("order", "desk", "worker-1", (
            "Probe.\n"
            f"$ env -u QD_SEAT python3 -I {workspace}/probe.py {workspace}/seen.json {ship.runtime} worker-1\n"
            f"$ env -u QD_SEAT qd post order --to captain Synthetic-in-the-operator-name > {workspace}/post.txt 2>&1;"
            f" echo exit=$? >> {workspace}/post.txt\n"
            "$ qd post report --re {this} probed"), subject="Probe")
        assert Door(seat).deliver(post.typed([message])).result == "delivered"

        def filed():
            post.file_drops(store)
            return next((m for m in store.all() if m.kind == "report" and m.re == message.id), None)
        wait_for(filed, 60)
        seen = json.loads((workspace / "seen.json").read_text())
        said = (workspace / "post.txt").read_text()
    finally:
        seat.down()
    print(json.dumps(seen))
    print(said)
    # What qd sees from inside: its own process ids, ending at the jail's own first process; no launch record.
    assert seen["pid1"] == "bwrap" and seen["ancestry"][-1] == "bwrap"
    assert not seen["own_launch_visible"]  # the trusted folders are an empty private tmpfs inside
    assert seen["home_is_seat_home"] and not seen["qd_seat_set"]
    # So the pane is out of reach there: the jail itself places the shell in a seat's session.
    assert "exit=0" not in said
    assert "session of seat worker-1 of ship drydock" in said, said
    assert not any(m.sender == "operator" for m in store.all())
    assert not (ship.runtime / "post" / "drop" / "operator").exists()
