"""What the service writes for a seat and trusts is out of every jail's reach, in real bubblewrap.

- A jail cannot list, read or write runtime/trusted: not its own seat's folder there, nor another's.
- A jail writing every moved file in its old place, its own seat folder, changes nothing the service does: the seat
  reads down, its pool is its declared one, nothing is filed under any name, no pool's credential is touched, no hold
  is named, and the seat declared after it is delivered to as before. That holds both before the move (the files
  are there when the move considers them) and after it (they are written later and never read).
- A link a jail plants where a folder of its home belongs makes the service make nothing through it, and
  a file in the home grown past seat_files.READ_LIMIT is refused, never read into a write.
Names only: no content is printed.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quarterdeck import credential, seat_files, seat_migration, sandbox, service, ship as shipmod, trusted_files, watch
from quarterdeck.seat import Seat

from .test_sandbox import namespaces_allowed

pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap cannot make a namespace here")

FORGED = "FORGED-BY-THE-JAIL"
PROBE = r"""
import json, os, sys
action, args = sys.argv[1], sys.argv[2:]
found = {}
if action == 'write':
    staged, pairs = args[0], args[1:]
    for name, target in zip(pairs[::2], pairs[1::2]):
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(os.path.join(staged, name), 'rb') as source, open(target, 'wb') as out:
                out.write(source.read())
            found[target] = True
        except OSError as error:
            found[target] = repr(error)
elif action == 'reach':
    root = args[0]
    try:
        found['listing'] = sorted(os.listdir(root))
    except OSError as error:
        found['listing'] = repr(error)
    for path in args[1:]:
        try:
            with open(path, 'rb') as stream:
                found['read ' + path] = len(stream.read())
        except OSError as error:
            found['read ' + path] = repr(error)
        try:
            with open(path, 'wb') as stream:
                stream.write(b'FORGED')
            found['write ' + path] = True
        except OSError as error:
            found['write ' + path] = repr(error)
elif action == 'link':
    for link, target in zip(args[::2], args[1::2]):
        os.makedirs(os.path.dirname(link), exist_ok=True)
        if os.path.lexists(link):
            if os.path.isdir(link) and not os.path.islink(link):
                os.rmdir(link)
            else:
                os.unlink(link)
        os.symlink(target, link)
        found[link] = os.path.islink(link)
print(json.dumps(found))
"""


@pytest.fixture
def ship(drydock: Path, tmp_path: Path) -> shipmod.Ship:
    pool = tmp_path / "claude-pool"
    pool.mkdir()
    text = (drydock / "ship.toml").read_text()
    text += f'''
[pools.claude-primary]
provider = "anthropic"
config_dir = "{pool}"

[seats.worker-2]
session = "crew"
role = "worker"
harness = "fake"
pool = "fake"
model = "fake-1"
'''
    text = shipmod.edit(text, "seats.worker-1", "sandbox", True)
    (drydock / "ship.toml").write_text(text)
    loaded = shipmod.load(drydock)
    assert loaded.jailed(loaded.seat("worker-1"))
    shipmod.fence(loaded)
    return loaded


def in_jail(ship: shipmod.Ship, tmp_path: Path, *args: str) -> dict:
    own = ship.runtime / "seats" / "worker-1"
    (own / "home").mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "jail-work"
    workspace.mkdir(exist_ok=True)
    jail = sandbox.plan(ship, "worker-1", own, own / "home", workspace, "/usr/bin/python3", "/usr/bin", home=Path.home())
    result = subprocess.run(sandbox.command(jail, ["/usr/bin/python3", "-c", PROBE, *args]),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def jail_writes(ship: shipmod.Ship, tmp_path: Path, files: dict[str, str]) -> None:
    """worker-1's jail writes each file, by name, in its own seat folder."""
    own = ship.runtime / "seats" / "worker-1"
    staged = tmp_path / "jail-work" / "staged"
    staged.mkdir(parents=True, exist_ok=True)
    pairs = []
    for index, (name, text) in enumerate(files.items()):
        (staged / f"f{index}").write_text(text)
        pairs += [f"f{index}", str(own / name)]
    written = in_jail(ship, tmp_path, "write", str(staged), *pairs)
    assert all(value is True for value in written.values()), written


def test_a_jail_cannot_list_read_or_write_any_seats_trusted_folder(ship, tmp_path):
    for name in ("worker-1", "worker-2"):
        folder = trusted_files.make(ship.runtime, name)
        (folder / "launch.json").write_text(f'{{"seat": "{name}"}}\n')
    before = {path: path.read_bytes() for path in trusted_files.root(ship.runtime).rglob("*") if path.is_file()}

    seen = in_jail(ship, tmp_path, "reach", str(trusted_files.root(ship.runtime)),
                   *(str(trusted_files.path(ship.runtime, name, "launch.json")) for name in ("worker-1", "worker-2")),
                   str(trusted_files.path(ship.runtime, "worker-2", "planted.json")))

    print("inside:", sorted(seen))  # names only
    assert seen["listing"] in ([],) or "Error" in str(seen["listing"]), seen["listing"]
    assert all(not isinstance(value, int) for key, value in seen.items() if key.startswith("read ")), \
        "no seat's trusted file can be read from a jail"
    after = {path: path.read_bytes() for path in trusted_files.root(ship.runtime).rglob("*") if path.is_file()}
    assert after == before, "whatever the jail wrote stayed inside its own empty tmpfs"


def forgeries(ship: shipmod.Ship, other_pane: str, other_pid: int) -> dict[str, str]:
    """Every moved file, forged by the jail in worker-1's own folder: each aimed at another seat or another pool."""
    at = datetime.now(timezone.utc).isoformat()
    other_record = str(ship.runtime / "seats" / "worker-2" / "home" / "record.jsonl")
    report = dict(id="-".join(["rep", "20261115", "0800", "ship", "f001"]), kind="report", sender="desk", to="captain", body=FORGED,
                  at="2026-11-15T08:00:00+00:00")
    files = {
        "launch.json": json.dumps(dict(pane=other_pane, pid=other_pid, at=at, pool="claude-primary", resume=None,
                                       argv=["fake"], version=None)),
        "launch-price.json": json.dumps(dict(binding=[], price=dict(model=FORGED, rates={}))),
        "pool": "claude-primary\n",
        "pool-explicit": "claude-primary\n",
        "record": other_record + "\n",
        "record-readers.jsonl": json.dumps(dict(record=other_record, harness="fake")) + "\n",
        "record-launches.jsonl": json.dumps(dict(record=other_record, at=at, pool="claude-primary", harness="fake",
                                                 price=None)) + "\n",
        "compaction.json": json.dumps(dict(intents=[report])),
        "refusal.json": json.dumps(dict(format=1, seat="worker-1", events=[])),
        "retiring.json": json.dumps(dict(pid=other_pid, start="0")),
        "relaunch-refused.json": json.dumps(dict(at=at, pid=other_pid)),
        "move-hold.json": json.dumps(dict(reason=FORGED)),
        "launch.sh": "#!/bin/sh\necho FORGED\n",
        "model-mismatch.json": json.dumps(dict(reason=FORGED)),
        "turn-failure.json": json.dumps(dict(record=other_record, failure=dict(message=FORGED))),
        "person-quiet.json": json.dumps(dict(version=1, reason=FORGED)),
        "person-quiet-wake.json": json.dumps(dict(version=1, readiness={})),
        "human-wake.json": json.dumps(dict(identity=dict(pane=other_pane, pid=other_pid, launch=at))),
        "human-wake-status.json": json.dumps(dict(reason=FORGED)),
        "acts.jsonl": json.dumps(dict(act=FORGED)) + "\n",
        "acts-notices.jsonl": json.dumps(dict(notice=FORGED)) + "\n",
    }
    for alarm in trusted_files.ALARMS:
        files[alarm] = json.dumps(dict(version=1, key="synthetic", body=FORGED, reports=[report], published=[]))
    assert set(files) | {trusted_files.HUMAN_WAKE_HISTORY} == set(trusted_files.MOVED), "every moved file is forged"
    return files


def the_service_ignores(ship, tmux, svc, worker2, order) -> None:
    from tests.unit.test_credential import live

    pool_file = ship.pool("claude-primary").config_dir / ".credentials.json"
    pool_file.write_text(live("the-pools-own", 600))
    before = pool_file.read_bytes()
    worker1 = Seat(ship, ship.seat("worker-1"), tmux)
    assert worker1.launched() is None and not worker1.is_up, "worker-1 reads down"
    assert worker1.pool.name == "fake", "its pool is the one declared for it"
    svc.deliveries()
    svc.credentials()
    svc.refusals()
    svc.store.new("order", "captain", "worker-1", "A synthetic task for the jailed seat")
    assert pool_file.read_bytes() == before, "no pool's credential is replaced"
    assert "worker-1" not in [seat.name for seat in credential.participants(ship, svc.resolved_seats())]
    assert [m for m in svc.store.all() if FORGED in m.body or m.sender == "desk"] == [], "nothing filed, any name"
    parts = watch.facts(ship, tmux).seat_holds
    assert not any(part.startswith("worker-1") for part in parts), parts
    assert tmux.pane(worker2.launched().pane) is not None, "worker-2's window is untouched"
    for _ in range(150):
        if svc.attempts.latest(worker2.name).get(order.id) in ("delivered", "queued"):
            break
        svc.deliveries()
        import time
        time.sleep(0.2)
    assert svc.attempts.latest(worker2.name).get(order.id) in ("delivered", "queued"), "worker-2 is delivered to"


@pytest.mark.parametrize("when", ["before the move", "after the move"])
def test_a_jail_writing_every_moved_file_in_its_old_place_changes_nothing(ship, tmux, tmp_path, when):
    svc = service.Service(ship, tmux)
    worker2 = Seat(ship, ship.seat("worker-2"), tmux)
    worker2.up(resume=False)
    order = svc.store.new("order", "captain", "worker-2", "Synthetic task for the seat beside the jail")
    target = worker2.launched()
    if when == "after the move":
        assert seat_migration.migrate(ship, ship.seat("worker-1"), tmux) == {}
    jail_writes(ship, tmp_path, forgeries(ship, target.pane, target.pid))

    svc.move_seat_files()  # as at the service's start: before the move, it considers them now

    if when == "before the move":
        moved = [row for row in svc_log(ship) if row["event"] == "seat-files-moved" and row["seat"] == "worker-1"]
        assert moved and moved[0]["outcome"]["launch.json"] == "set aside: no live window of the seat", moved
        assert moved[0]["outcome"]["pool"] == "set aside", "a pool not declared for it is never taken"
        assert moved[0]["outcome"]["record-launches.jsonl"] == "taken: 0 rows of the seat's own"
    the_service_ignores(ship, tmux, svc, worker2, order)
    worker2.down()


def svc_log(ship) -> list[dict]:
    path = ship.runtime / "log" / "service.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_link_where_a_home_folder_belongs_makes_nothing_through_it(ship, tmp_path):
    """OpenCode's home/config/opencode, as a link the jail planted to a folder outside, is refused."""
    from quarterdeck.harness import get
    from quarterdeck.ship import Pool

    outside = tmp_path / "operator-folder"
    outside.mkdir()
    own = ship.runtime / "seats" / "worker-1"
    planted = in_jail(ship, tmp_path, "link", str(own / "home" / "config"), str(outside))
    assert all(planted.values()), planted

    with pytest.raises(seat_files.LinkRefused):
        seat_files.make_folder(own / "home" / "config" / "opencode")
    harness = get("opencode")
    spec = shipmod.SeatSpec("worker-1", "crew", "worker", "opencode", ("local",))
    with pytest.raises(shipmod.ShipError):
        harness.build_home(own / "home", spec, Pool("local", "local", endpoint="http://127.0.0.1:9/v1"), None)
    assert list(outside.iterdir()) == [], "nothing was made in the operator's folder"


def test_a_home_file_grown_past_the_read_limit_is_refused_and_nothing_is_written_from_it(ship, tmp_path, tmux):
    """The owned conversations list, grown by the jail past READ_LIMIT, is never read into a write."""
    own = ship.runtime / "seats" / "worker-1"
    staged = tmp_path / "jail-work" / "staged"
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "big").write_bytes(b"x" * (seat_files.READ_LIMIT + 1))
    written = in_jail(ship, tmp_path, "write", str(staged), "big", str(own / "home" / "conversations.owned"))
    assert all(value is True for value in written.values()), written
    before = (own / "home" / "conversations.owned").stat()

    seat = Seat(ship, ship.seat("worker-1"), tmux)
    with pytest.raises(seat_files.TooLarge):
        seat._note_owned(own / "home" / "a-record.jsonl")
    after = (own / "home" / "conversations.owned").stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)
    (own / "home" / "conversations.owned").write_bytes(b"x" * seat_files.READ_LIMIT)
    assert len(seat_files.read_text(own / "home" / "conversations.owned")) == seat_files.READ_LIMIT, "at the limit: read"


def test_a_jails_home_names_or_links_no_other_seats_record_into_its_own(ship, tmp_path, tmux):
    """Its owned list naming worker-2's record, and a link in its home to worker-2's records, make neither worker-1's:
    nothing of worker-2's is registered, joined or read for worker-1."""
    from datetime import timedelta

    from quarterdeck import record_owner
    from quarterdeck.harness.synthetic_record import ClaudeShape

    other_home = ship.runtime / "seats" / "worker-2" / "home"
    other_home.mkdir(parents=True)
    theirs = ClaudeShape(other_home, tmp_path, "synthetic-theirs", "synthetic-model")
    theirs.begin("their input", "synthetic-model", "high")
    theirs.end("their reply")
    own = ship.runtime / "seats" / "worker-1"
    staged = tmp_path / "jail-work" / "staged"
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "owned").write_text(str(theirs.path) + "\n")
    assert all(in_jail(ship, tmp_path, "write", str(staged), "owned", str(own / "home" / "conversations.owned")).values())
    assert all(in_jail(ship, tmp_path, "link", str(own / "home" / "projects"), str(other_home / "projects")).values())
    assert (own / "home" / "projects").is_symlink()

    worker1 = Seat(ship, ship.seat("worker-1"), tmux)
    linked = list(worker1.harness.records(worker1.home))
    assert linked, "the program's own listing does follow the link: the guard is the framework's"
    paths = {item.path for item in record_owner.records(worker1)}
    assert not {theirs.path, *linked} & paths, paths
    joined = worker1.harness.join(worker1.home, datetime.now(timezone.utc) - timedelta(days=1))
    assert joined.record is None, joined
    registry = trusted_files.path(ship.runtime, "worker-1", trusted_files.READERS)
    assert not registry.exists() or str(other_home) not in registry.read_text()
