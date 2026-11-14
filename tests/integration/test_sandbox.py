"""A jailed seat: the fake harness under bubblewrap writes the workspace, posts from inside, and cannot reach the home."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from quarterdeck import check, post, sandbox, seat as seatmod, ship as shipmod
from quarterdeck.door import Door
from quarterdeck.tmux import Tmux

from .waiting import wait_for



def namespaces_allowed() -> bool:
    """Whether bubblewrap can make a namespace here: not inside a jail, whose kernel view denies nested ones."""
    import subprocess

    if not sandbox.available():
        return False
    try:
        return subprocess.run(["bwrap", "--ro-bind", "/", "/", "true"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap is not installed, or cannot make a namespace here (inside a jail)")


def test_a_jailed_seat_writes_its_workspace_posts_from_inside_and_cannot_see_the_operators_home(drydock: Path, tmux: Tmux, tmp_path: Path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    text = (drydock / "ship.toml").read_text()
    text = shipmod.edit(text, "seats.worker-1", "sandbox", True)
    text = shipmod.edit(text, "seats.worker-1", "cwd", str(workspace))
    (drydock / "ship.toml").write_text(text + '\n[seats.worker-1.settings]\nstep_seconds = "0.05"\n')
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat("worker-1"), tmux)
    secret = Path.home() / f".qd-test-secret-{os.getpid()}"
    marker = Path.home() / f".qd-test-escape-{os.getpid()}"
    secret.write_text("the operator's own\n")
    try:
        proof = seat.up(resume=False)
        assert "jailed: the operator's home hidden, the workspace writable" in proof
        launched = seat.launched()
        assert launched.argv[0] == "bwrap" and "--unshare-pid" in launched.argv and launched.harness == "fake"
        assert str(Path.home()) in launched.argv[launched.argv.index("--tmpfs", 12) + 1:], "the home is an empty tmpfs"
        wait_for(lambda: Door(seat).reading().open)
        store = post.Store(ship.runtime)
        body = ("Prove the jail.\n"
                f"$ echo inside > {workspace}/ok.txt\n"
                f"$ cat {secret} && echo secret-readable || echo secret-hidden\n"
                f"$ touch {marker}; echo touched=$?\n"
                "$ qd post ack --re {this} claimed\n"
                "$ qd post order --to captain note from the jail\n"
                "$ qd post report --re {this} jail proven")
        message = store.new("order", "desk", "worker-1", body, subject="Jail")
        delivery = Door(seat).deliver(post.typed([message]))
        assert delivery.result == "delivered", delivery.detail
        def filed():
            post.file_drops(store)
            return next((m for m in store.all() if m.kind == "report" and m.re == message.id), None)
        report = wait_for(filed, 30)
        assert report.sender == "worker-1" and report.body == "jail proven", "qd post works from inside the jail"  # law: can-post
        assert any(m.kind == "ack" and m.sender == "worker-1" and m.re == message.id for m in store.all())
        assert any(m.kind == "order" and m.sender == "worker-1" and m.body == "note from the jail" for m in store.all())
        assert (workspace / "ok.txt").read_text().strip() == "inside", "the workspace is writable"
        assert not marker.exists(), "a write into the operator's home landed in the jail's empty one, not on the machine"
        screen = tmux.capture(seat.pane().id, history=200)
        assert "⎿ secret-hidden" in screen and "⎿ secret-readable" not in screen, "the operator's files are absent, not merely unwritable"
        record = seat.record()
        assert record is not None and record.is_relative_to(seat.home), "the program's record is in the seat home, readable from outside"
        turns = wait_for(lambda: seat.harness.turns(record) or None)
        assert any(t.name == "Bash" for t in turns[0].tools), "the turn is accounted like any other"
    finally:
        secret.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        seat.down()


def test_nested_synthetic_home_stays_hidden_while_allowed_mounts_work(tmp_path):
    import json
    import subprocess

    home = tmp_path/'operator-home'
    workspace = home/'project'
    folder = workspace/'ships'/'test'
    ship = shipmod.Ship(folder, 'synthetic', 'operator', workspace, {}, {}, {})
    own = ship.runtime/'seats'/'worker'
    program_home = own/'home'
    other = ship.runtime/'seats'/'other'
    for directory in (program_home, other):
        directory.mkdir(parents=True)
    (home/'operator-marker').write_text('synthetic operator marker')
    (other/'other-marker').write_text('synthetic other seat marker')
    jail = sandbox.plan(ship, 'worker', own, program_home, workspace, '/usr/bin/python3', '/usr/bin', home=home)
    probe = '''
import json, sys
from pathlib import Path
home, workspace, own, other, ship, post, drop = map(Path, sys.argv[1:])
def write(path):
    try:
        path.write_text('synthetic')
        return True
    except OSError:
        return False
print(json.dumps(dict(operator_read=(home/'operator-marker').exists(),
                      other_read=(other/'other-marker').exists(),
                      workspace=write(workspace/'written'), own=write(own/'written'),
                      post=write(post/'written'), drop=write(drop/'written'), ship=write(ship/'written'),
                      hidden_home=write(home/'escape-marker'))))
'''
    drop = ship.runtime/'post'/'drop'/'worker'
    result = subprocess.run(sandbox.command(jail, ['/usr/bin/python3', '-c', probe, *map(str, (
        home, workspace, own, other, folder, ship.runtime/'post', drop))]), capture_output=True, text=True, check=True)
    values = json.loads(result.stdout)
    assert not values['operator_read'] and not values['other_read']
    assert values['workspace'] and values['own'] and values['drop'] and not values['post'] and not values['ship']
    assert not (home/'escape-marker').exists(), 'the writable empty home must never reach the host home'
    assert (workspace/'written').exists() and (own/'written').exists() and (drop/'written').exists()
    assert not (ship.runtime/'post'/'written').exists()


def test_a_jailed_seat_cannot_forge_a_sender_and_its_own_drop_still_files(drydock: Path, tmp_path: Path):
    """The five forgery variants fail from inside real bwrap. The seat's own drop still files."""
    import json
    import subprocess

    workspace = tmp_path / "forge-work"
    workspace.mkdir()
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    (ship.runtime / "check").mkdir(parents=True, exist_ok=True)
    store = post.Store(ship.runtime)
    victim = store.new("order", "captain", "desk", "VICTIM")
    victim_bytes = (store.dir / f"{victim.id}.json").read_bytes()
    outside = tmp_path / "outside.txt"
    outside.write_text("UNTOUCHED")
    good = store.prepare("order", "worker-1", "captain", "from the jail")
    (workspace / "good.json").write_text(good.to_json())

    def joined(*parts: str) -> str:
        return "-".join(parts)

    foreign = joined("ord", "20261114", "0314", "captain", "ab12")
    mismatch_id = joined("ord", "20261114", "0314", "captain", "ab13")
    report_id = joined("rep", "20261114", "0314", "desk", "ab14")
    ack_id = joined("ack", "20261114", "0314", "desk", "ab15")
    planted = joined("ord", "20261114", "0314", "captain", "ab99")
    own = ship.runtime / "seats" / "worker-1"
    program_home = own / "home"
    program_home.mkdir(parents=True, exist_ok=True)
    jail = sandbox.plan(ship, "worker-1", own, program_home, workspace, "/usr/bin/python3", "/usr/bin", home=Path.home())
    drop = ship.runtime / "post" / "drop" / "worker-1"
    probe = r"""
import json, os, sys
from pathlib import Path
workspace, post_dir, drop, store, check, other, outside = map(Path, sys.argv[1:8])
good_name, victim, foreign, mismatch, report, ack, planted = sys.argv[8:15]
def write(path, text="forged"):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return True
    except OSError:
        return False
(workspace / "swap.json").write_text("REPLACED")
def replace(path):
    try:
        os.replace(workspace / "swap.json", path)
        return True
    except OSError:
        return False
copied = False
try:
    (drop / good_name).write_bytes((workspace / "good.json").read_bytes())
    copied = True
except OSError:
    copied = False
def message(mid, kind, sender, body, re=None):
    return json.dumps({"id": mid, "kind": kind, "from": sender, "to": "desk",
                       "at": "2026-11-14T03:14:00+00:00", "body": body,
                       "re": re, "subject": None, "due": None, "speed": None},
                      indent=2, ensure_ascii=False) + "\n"
values = dict(
    store_new=write(store / (planted + ".json"), message(planted, "order", "captain", "NEW")),
    store_edit=write(store / (victim + ".json"), "EDITED-IN-PLACE"),
    store_replace=replace(store / (victim + ".json")),
    other_drop=write(other / "planted.json"),
    admission=write(post_dir / "admission" / "planted"),
    attempts=write(post_dir / "attempts" / "planted.json"),
    inbox=write(post_dir / "inbox" / "desk.md"),
    check=write(check / "worker-1" / "worker-1.txt", "qd-check"),
    shared_check=write(check / "planted.txt"),
    good=copied,
    foreign=write(drop / (foreign + ".json"), message(foreign, "order", "captain", "FOREIGN")),
    mismatch=write(drop / (mismatch + ".json"), message(mismatch, "order", "worker-1", "MISMATCH")),
    report=write(drop / (report + ".json"), message(report, "report", "desk", "CLOSED", victim)),
    ack=write(drop / (ack + ".json"), message(ack, "ack", "desk", "claimed", victim)))
link = drop / "linked.json"
try:
    link.symlink_to(outside)
    values["link"] = link.is_symlink()
except OSError:
    values["link"] = False
print(json.dumps(values))
"""
    result = subprocess.run(sandbox.command(jail, [
        "/usr/bin/python3", "-c", probe, str(workspace), str(ship.runtime / "post"), str(drop), str(store.dir),
        str(ship.runtime / "check"), str(ship.runtime / "post" / "drop" / "desk"), str(outside),
        f"{good.id}.json", victim.id, foreign, mismatch_id, report_id, ack_id, planted]),
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    refused = ("store_new", "store_edit", "store_replace", "other_drop", "admission", "attempts", "inbox", "shared_check")
    assert not any(values[name] for name in refused), values
    assert values["check"] and values["good"] and values["foreign"] and values["mismatch"]
    assert values["report"] and values["ack"] and values["link"]
    good_bytes = (drop / f"{good.id}.json").read_bytes()
    fresh = post.file_drops(store)
    assert (store.dir / f"{good.id}.json").read_bytes() == good_bytes
    assert (store.dir / f"{victim.id}.json").read_bytes() == victim_bytes
    assert store.is_open(victim) and store.closing_report(victim) is None
    assert store.claimed("desk", post.now()) is None
    assert outside.read_text() == "UNTOUCHED" and (drop / "linked.json").is_symlink()
    assert jail.writable == (drop, ship.runtime / "check" / "worker-1")
    assert (ship.runtime / "check" / "worker-1" / "worker-1.txt").read_text() == "qd-check"
    assert not (ship.runtime / "check" / "planted.txt").exists()
    assert ("drop/worker-1", "from names another seat") in fresh
    assert ("drop/worker-1", "not a regular file") in fresh
    assert (drop / f"{foreign}.json").is_file()
    assert (drop / f"{report_id}.json").is_file()
    assert (drop / f"{ack_id}.json").is_file()


@pytest.mark.parametrize('cover', ['path-entry', 'install-prefix'])
def test_a_resolved_folder_that_contains_the_home_never_shows_it_again(cover):
    """A folder bound back read-only after the home's tmpfs is laid over it: one that contains the home would show
    the home again. Under the jail's own /tmp, a PATH entry or an install prefix that contains the home is not
    bound back; a program installed there still runs from its own folder."""
    import json
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(prefix='qdj-', dir='/tmp') as temporary:
        above = Path(temporary)/'tools'
        home = above/'nested'/'operator-home'
        workspace = Path(temporary)/'work'
        folder = workspace/'ships'/'test'
        ship = shipmod.Ship(folder, 'synthetic', 'operator', workspace, {}, {}, {})
        own = ship.runtime/'seats'/'worker'
        (own/'home').mkdir(parents=True)
        home.mkdir(parents=True)
        (home/'operator-marker').write_text('synthetic operator marker')
        probe = ('import json, sys\nfrom pathlib import Path\n'
                 'print(json.dumps(dict(ran=True, operator_read=Path(sys.argv[1]).exists())))\n')
        if cover == 'path-entry':
            path, program = f'/usr/bin{os.pathsep}{above}', '/usr/bin/python3'
            argv = [program, '-c', probe, str(home/'operator-marker')]
        else:
            (above/'bin').mkdir()
            program = str(above/'bin'/'synthetic-probe')
            Path(program).write_text('#!/usr/bin/python3\n' + probe)
            Path(program).chmod(0o755)
            path = f'/usr/bin{os.pathsep}{above/"bin"}'
            argv = [program, str(home/'operator-marker')]
        jail = sandbox.plan(ship, 'worker', own, own/'home', workspace, program, path, home=home)
        assert not any(sandbox.covers(f, home) for f in jail.read_only), jail.read_only
        if cover == 'install-prefix':
            assert above/'bin' in jail.read_only, 'the program is still bound back by its own folder'
        result = subprocess.run(sandbox.command(jail, argv), capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == dict(ran=True, operator_read=False)


@pytest.mark.parametrize('kind', ['read-only', 'writable'])
def test_an_ancestor_bound_into_the_jail_never_shows_or_reaches_the_home(kind):
    """Built past plan's refusal, as a jail made by hand would be: the home's tmpfs follows any mount that contains
    it, so the home stays hidden, and a write to the home's path lands in that tmpfs, never in the home."""
    import json
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(prefix='qdj-', dir='/tmp') as temporary:
        root = Path(temporary)
        above = root/'above'
        home = above/'operator-home'
        home.mkdir(parents=True)
        (home/'operator-marker').write_text('synthetic operator marker')
        seat = root/'ship'/'runtime'/'seats'/'worker'
        (seat/'home').mkdir(parents=True)
        (root/'ship'/'runtime'/'tmux').mkdir(mode=0o700)  # what plan makes before a launch
        (root/'ship'/'runtime'/'locks').mkdir(mode=0o700)
        (root/'work').mkdir()
        jail = sandbox.Jail(home=home, seat_home=seat/'home', seat_folder=seat, workspace=root/'work',
                            ship_folder=root/'ship', runtime=root/'ship'/'runtime',
                            read_only=(above,) if kind == 'read-only' else (),
                            writable=(above,) if kind == 'writable' else ())
        assert sandbox.covering(jail) is not None, 'plan refuses this jail; the mounts are tested past it'
        probe = (
            'import json, sys\nfrom pathlib import Path\nhome, above = map(Path, sys.argv[1:])\n'
            'def write(path):\n    try:\n        path.write_text("synthetic")\n        return True\n'
            '    except OSError:\n        return False\n'
            'print(json.dumps(dict(operator_read=(home/"operator-marker").exists(), home_write=write(home/"escape"),'
            ' beside=write(above/"beside"))))\n')
        result = subprocess.run(sandbox.command(jail, ['/usr/bin/python3', '-c', probe, str(home), str(above)]),
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        values = json.loads(result.stdout)
        assert values['operator_read'] is False
        assert values['beside'] is (kind == 'writable'), 'the ancestor itself is bound as declared'
        assert not (home/'escape').exists(), 'a write to the home path never reaches the home'
        assert (home/'operator-marker').read_text() == 'synthetic operator marker'
        assert sorted(p.name for p in home.iterdir()) == ['operator-marker']


@pytest.mark.parametrize('place', ['ships', 'office'])
def test_a_home_inside_a_fleet_folder_is_refused_and_stays_hidden(place):
    """Real bwrap: covering refuses a home inside ships/ or office/. Past that refusal the home's tmpfs
    sorts with the fleet by depth, so it lands on that folder and the home stays hidden."""
    import json
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(prefix='qdj-', dir='/tmp') as temporary:
        project = Path(temporary) / 'project'
        ship_folder = project / 'ships' / 'alpha'
        runtime = ship_folder / 'runtime'
        seat = runtime / 'seats' / 'worker'
        (seat / 'home').mkdir(parents=True)
        (runtime / 'tmux').mkdir(mode=0o700)
        (runtime / 'locks').mkdir(mode=0o700)
        (runtime / 'post').mkdir()
        (project / 'office').mkdir(parents=True)
        (project / 'work').mkdir()
        fleet = project / 'ships' if place == 'ships' else project / 'office'
        home = fleet / 'beta' / 'operator-home'
        home.mkdir(parents=True)
        (home / 'operator-marker').write_text('synthetic operator marker')
        ship = shipmod.Ship(ship_folder, 'synthetic', 'operator', project / 'work', {}, {}, {})
        with pytest.raises(shipmod.ShipError, match='the fleet folder') as caught:
            sandbox.plan(ship, 'worker', seat, seat / 'home', project / 'work',
                         '/usr/bin/python3', '/usr/bin', home=home)
        assert str(fleet) in str(caught.value)
        jail = sandbox.Jail(home=home, seat_home=seat / 'home', seat_folder=seat, workspace=project / 'work',
                            ship_folder=ship_folder, runtime=runtime,
                            writable=(runtime / 'post',))
        assert sandbox.covering(jail) == ('the fleet folder', fleet)
        probe = (
            'import json, sys\nfrom pathlib import Path\nhome = Path(sys.argv[1])\n'
            'def write(path):\n'
            '    try:\n'
            '        path.write_text("synthetic")\n'
            '        return True\n'
            '    except OSError:\n'
            '        return False\n'
            'marker = home / "operator-marker"\n'
            'print(json.dumps(dict(operator_read=marker.is_file() and marker.read_text()=="synthetic operator marker",'
            ' home_write=write(home / "escape"))))\n')
        result = subprocess.run(sandbox.command(jail, ['/usr/bin/python3', '-c', probe, str(home)]),
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        values = json.loads(result.stdout)
        assert values == dict(operator_read=False, home_write=True)
        assert (home / 'operator-marker').read_text() == 'synthetic operator marker'
        assert not (home / 'escape').exists()


def test_another_seats_check_folder_is_not_writable_from_the_jail(tmp_path: Path):
    """The jail can write its own proof folder and cannot plant a file in another seat's."""
    import json
    import subprocess

    home = tmp_path / "operator-home"
    workspace = home / "project"
    folder = workspace / "ships" / "test"
    ship = shipmod.Ship(folder, "synthetic", "operator", workspace, {}, {}, {})
    own = ship.runtime / "seats" / "worker"
    (own / "home").mkdir(parents=True)
    other = ship.runtime / "check" / "other"
    other.mkdir(parents=True)
    kept = "other-seat-proof-kept"
    (other / "other.txt").write_text(kept)
    jail = sandbox.plan(ship, "worker", own, own / "home", workspace, "/usr/bin/python3", "/usr/bin", home=home)
    drop = ship.runtime / "post" / "drop" / "worker"
    assert jail.writable == (drop, ship.runtime / "check" / "worker")
    proof_file = sandbox.check_proof_file(ship.runtime, "worker")
    assert proof_file == ship.runtime / "check" / "worker" / "worker.txt"
    line = "qd-check-synthetic"
    probe = (
        "import json, sys\nfrom pathlib import Path\nown, other, proof, line = sys.argv[1:]\n"
        "own, other, proof = Path(own), Path(other), Path(proof)\n"
        "def write(path, text='planted'):\n"
        "    try:\n"
        "        path.parent.mkdir(parents=True, exist_ok=True)\n"
        "        path.write_text(text)\n"
        "        return True\n"
        "    except OSError:\n"
        "        return False\n"
        "print(json.dumps(dict(own=write(own / 'scratch.txt'), other=write(other / 'other.txt'),"
        " sibling=write(other.parent / 'planted.txt'), proof=write(proof, line + '\\n'))))\n")
    result = subprocess.run(sandbox.command(jail, ["/usr/bin/python3", "-c", probe,
                                                   str(ship.runtime / "check" / "worker"), str(other),
                                                   str(proof_file), line]),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    assert values == {"own": True, "other": False, "sibling": False, "proof": True}
    assert (other / "other.txt").read_text() == kept
    assert kept not in result.stdout and kept not in result.stderr
    assert (ship.runtime / "check" / "worker" / "scratch.txt").read_text() == "planted"
    assert not (ship.runtime / "check" / "planted.txt").exists()
    # What qd check asks the jailed program to write, it reads back outside and finds matched.
    assert check.proof_verdict(proof_file, line) == (True, "matched")


def test_a_jailed_post_to_an_up_recipient_does_not_call_hidden_state_down(drydock: Path, tmux: Tmux, tmp_path: Path):
    """Real bwrap + qd post + fake recipient: hidden metadata is not a down seat."""
    from quarterdeck.service import Service

    workspace = tmp_path/'workspace'
    workspace.mkdir()
    path = drydock/'ship.toml'
    text = shipmod.edit(path.read_text(), 'seats.worker-1', 'sandbox', True)
    text = shipmod.edit(text, 'seats.worker-1', 'cwd', str(workspace))
    path.write_text(text+'\n[seats.worker-1.settings]\nstep_seconds="0.05"\n')
    ship = shipmod.load(drydock)
    captain = seatmod.Seat(ship, ship.seat('captain'), tmux)
    worker = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    svc = Service(ship, tmux)
    try:
        captain.up(resume=False)
        worker.up(resume=False)
        wait_for(lambda: Door(captain).reading().open and Door(worker).reading().open)
        assert captain.is_up and (captain.folder/'launch.json').is_file()
        message = svc.store.new('order','captain',worker.name,
            f'$ test ! -e {captain.folder}/launch.json && echo recipient-runtime-hidden\n'
            '$ qd post report --re {this} Synthetic jailed report', subject='Synthetic jailed post')
        sent = Door(worker).deliver(post.typed([message]))
        assert sent.result == 'delivered', sent
        def filed():
            post.file_drops(svc.store)
            return svc.store.closing_report(message)
        report = wait_for(filed, 30)
        assert report.sender == worker.name and report.to == captain.name
        assert captain.is_up, 'recipient is actually up outside the sender jail'
        proof = svc.deliver(captain,[report])
        print(proof)
        assert 'delivered:' in proof, proof
        assert svc.attempts.latest()[report.id] == 'delivered'
        assert captain.record() is not None
        screen = wait_for(lambda: (text if '· filed by the service ·' in (text := tmux.capture(worker.pane().id, history=200)) else None))
        assert '⎿ recipient-runtime-hidden' in screen
        assert 'captain is down' not in screen, screen  # law: record-first
        assert 'recipient state is not visible here' in screen, screen
    finally:
        worker.down()
        captain.down()
        svc.ledger.close()


PROBE = """for n in qd python python3 pip pip3; do
  p=$(command -v "$n" 2>/dev/null || true)
  r=""
  if [ -n "$p" ]; then r=$(readlink -f "$p"); fi
  printf '%s\\t%s\\t%s\\n' "$n" "$p" "$r"
done > "$1/resolved.tsv"
printf '%s\\n' "$PATH" > "$1/path.txt"
"""


def test_a_jailed_seat_sees_the_qd_only_folder_first_and_the_machines_interpreters_never_a_venv(drydock: Path, tmux: Tmux, tmp_path: Path):
    import shlex
    import shutil
    import sys
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "probe.sh").write_text(PROBE)
    text = (drydock / "ship.toml").read_text()
    text = shipmod.edit(text, "seats.worker-1", "sandbox", True)
    text = shipmod.edit(text, "seats.worker-1", "cwd", str(workspace))
    (drydock / "ship.toml").write_text(text + '\n[seats.worker-1.settings]\nstep_seconds = "0.05"\n')
    ship = shipmod.load(drydock)
    seat = seatmod.Seat(ship, ship.seat("worker-1"), tmux)
    try:
        seat.up(resume=False)
        assert seat.launched().argv[0] == "bwrap"
        words = shlex.split((seat.folder / "launch.sh").read_text().splitlines()[-1])
        launch_path = next(word for word in words if word.startswith("PATH="))[len("PATH="):]
        wait_for(lambda: Door(seat).reading().open)
        store = post.Store(ship.runtime)
        message = store.new("order", "desk", "worker-1", f"Resolve.\n$ sh {workspace}/probe.sh {workspace}\n"
                            "$ qd post report --re {this} resolved", subject="Probe")
        assert Door(seat).deliver(post.typed([message])).result == "delivered"
        def filed():
            post.file_drops(store)
            return next((m for m in store.all() if m.kind == "report" and m.re == message.id), None)
        wait_for(filed, 30)
        inside_path = (workspace / "path.txt").read_text().strip()
        assert inside_path == launch_path, "the jailed program runs with the launch's own PATH"
        assert inside_path.split(os.pathsep)[0] == str(ship.runtime / "bin")
        rows = {name: (found, real) for name, found, real in
                (line.split("\t") for line in (workspace / "resolved.tsv").read_text().splitlines())}
        assert rows["qd"][0] == str(ship.runtime / "bin" / "qd"), "qd is found in the qd-only folder"
        assert rows["qd"][1] == str((seatmod.qd_bin() / "qd").resolve()), "and resolves to the installed copy"
        venvs = [(ship.runtime / "venv").resolve()]
        if sys.prefix != sys.base_prefix:
            venvs.append(Path(sys.prefix).resolve())
        for name in ("python", "python3", "pip", "pip3"):
            found, real = rows[name]
            expected = shutil.which(name, path=launch_path)
            assert found == (expected or ""), (name, found, expected)
            assert not found.startswith(str(ship.runtime / "bin")), name
            for venv in venvs:
                assert not (found and Path(found).parent.resolve() == venv / "bin"), (name, found)
                assert not (real and Path(real).is_relative_to(venv / "bin")), (name, real)
    finally:
        seat.down()


def test_the_reseed_lock_cannot_be_opened_or_taken_from_inside_a_jail(drydock: Path, tmp_path: Path):
    """The service's lock folder is an empty private tmpfs inside the jail: the lock's path does not exist there, so no
    jailed seat can hold it (flock needs no write access) and stop the service's pass."""
    import json
    import subprocess

    from quarterdeck import credential

    workspace = tmp_path / "lock-work"
    workspace.mkdir()
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    own = ship.runtime / "seats" / "worker-1"
    (own / "home").mkdir(parents=True, exist_ok=True)
    with credential._reseeding(ship, 0.1):
        pass  # the service has made its lock file
    lock = ship.runtime / sandbox.LOCKS / "credential-reseed.lock"
    assert lock.is_file()
    jail = sandbox.plan(ship, "worker-1", own, own / "home", workspace, "/usr/bin/python3", "/usr/bin", home=Path.home())
    probe = r"""
import fcntl, json, os, sys
lock, folder = sys.argv[1:3]
seen = dict(folder=sorted(os.listdir(folder)) if os.path.isdir(folder) else None)
for flags, name in ((os.O_RDONLY, "read"), (os.O_RDWR | os.O_CREAT, "create")):
    try:
        descriptor = os.open(lock, flags, 0o600)
    except OSError as error:
        seen[name] = type(error).__name__
        continue
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        seen[name] = "locked"
    finally:
        os.close(descriptor)
print(json.dumps(seen))
"""
    result = subprocess.run(sandbox.command(jail, ["/usr/bin/python3", "-c", probe, str(lock), str(lock.parent)]),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert seen["folder"] == [] and seen["read"] == "FileNotFoundError", seen
    # A lock the jail makes lands in its own private tmpfs, never on the service's file.
    with credential._reseeding(ship, 0.5):
        pass
    assert sorted(os.listdir(lock.parent)) == ["credential-reseed.lock"]
