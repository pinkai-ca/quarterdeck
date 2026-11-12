"""A service pass writes what changed and nothing else; what it does write stays atomic and durable.

Every pass reconciles every pending journal, and a settled journal used to be
rewritten each time, fsyncs included. These tests read the disk itself (calls to
the durable writer, inode and mtime) rather than the objects in memory. Equal
bytes mean only that there is nothing to write: never that an earlier failed
write is now durable or repaired, and nothing reports it so.
"""

from __future__ import annotations

from datetime import datetime
import errno
import json
import locale
import os
from pathlib import Path
import shutil
import stat
import traceback
from types import SimpleNamespace

import pytest

from quarterdeck import durable, install, pending, post, standing
from quarterdeck.service import Service
from quarterdeck.ship import load

from .test_durable_writes import Disk, PowerCut, journal_data, message
from .test_pending_delivery import setup, send, input_row  # noqa: F401
from .test_status_presentation import live  # noqa: F401

OLD_NS = 1_000_000_000_000_000_000  # a mark no write made today can carry


class Writes:
    """Every call to the shared durable writer, which still does the writing."""

    def __init__(self, monkeypatch):
        self.paths: list[Path] = []
        real = durable.replace_file

        def spy(path, text, **kwargs):
            self.paths.append(Path(path))
            return real(path, text, **kwargs)

        monkeypatch.setattr(durable, "replace_file", spy)

    def under(self, folder: Path) -> list[Path]:
        return [path for path in self.paths if folder in path.parents]


def aged(paths) -> dict[Path, tuple[int, int]]:
    """Give each file an old mtime, so any later write shows, and return its marks."""
    for path in paths:
        os.utime(path, ns=(OLD_NS, OLD_NS))
    return marks(paths)


def marks(paths) -> dict[Path, tuple[int, int]]:
    return {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in paths}


def canonical(data: dict) -> bytes:
    """What a journal write has always put on disk: indented JSON, one newline, the locale's encoding."""
    return (json.dumps(data, indent=2) + "\n").encode(locale.getpreferredencoding(False))


def valid_journal(base: Path, phase: str) -> dict:
    """The shared synthetic journal, with the launch stamp the journal reader requires."""
    data = journal_data(base, phase)
    return {**data, "identity": {**data["identity"], "launch": post.now().isoformat()}}


def log_lines(svc: Service) -> list[str]:
    return svc.log_path.read_text().splitlines() if svc.log_path.exists() else []


# -- pending journals across whole service passes ----------------------------------------------


@pytest.mark.parametrize("count", [1, 7])
def test_a_pass_over_settled_journals_writes_none_yet_still_settles_their_receipts(setup, count):
    svc, seat, live, record = setup
    launch = seat.launched()
    made = []
    for index in range(count):
        item = svc.store.new("order", "desk", seat.name, f"synthetic settled item {index}")
        attempt = svc.attempts.open(item, seat.name, launch.pane, launch.pid, launch.at)
        proof = pending.Proof.begin(seat, attempt, post.typed([item]))
        proof.save(phase="delivered")  # the verdict is durable; its receipt may lag behind it
        if index % 3 == 0:
            svc.attempts.close(attempt, "delivered", "exact payload found in owned record after paste")
        elif index % 3 == 2:
            svc.attempts.close(attempt, "pending", "synthetic receipt left behind by a crash")
        made.append((item, attempt, proof))
    lagging = {attempt.path for index, (_, attempt, _) in enumerate(made) if index % 3}
    journals = [proof.path for _, _, proof in made]
    pending_dir = svc.runtime / "post" / "pending"
    attempts_dir = svc.runtime / "post" / "attempts"
    before = aged(journals)
    with pytest.MonkeyPatch.context() as patch:
        writes = Writes(patch)
        svc.reconcile()
        assert writes.under(pending_dir) == [], "a journal already in its phase is not written again"
        assert set(writes.under(attempts_dir)) == lagging, "every lagging receipt is still settled, once"
        assert marks(journals) == before
        assert svc.attempts.latest() == {item.id: "delivered" for item, _, _ in made}
        settled = {path: path.read_bytes() for path in attempts_dir.glob("*.json")}
        writes.paths.clear()
        restarted = Service(svc.ship, svc.tmux)
        try:
            for service in (svc, restarted, restarted):
                service.reconcile()
        finally:
            restarted.ledger.close()
        assert writes.under(pending_dir) == writes.under(attempts_dir) == []
        assert marks(journals) == before
        assert {path: path.read_bytes() for path in attempts_dir.glob("*.json")} == settled
    assert [json.loads(path.read_text())["phase"] for path in journals] == ["delivered"] * count
    assert live["pasted"] == live["submitted"] == 0  # law: one-door


def test_a_journal_that_really_changes_is_written_once_and_then_left_alone(setup):
    svc, seat, live, record = setup
    item, proof = send(svc, seat)
    assert proof.data["phase"] == "submitted"
    (attempt,) = [a for a in svc.attempts.all() if a.message == item.id]
    input_row(record, proof.data["payload"], post.now())
    pending_dir = svc.runtime / "post" / "pending"
    with pytest.MonkeyPatch.context() as patch:
        writes = Writes(patch)
        svc.reconcile()
        assert writes.under(pending_dir) == [proof.path]
        assert writes.under(svc.runtime / "post" / "attempts") == [attempt.path]
        assert json.loads(proof.path.read_text())["phase"] == "delivered"
        assert svc.attempts.latest()[item.id] == "delivered"
        before = aged([proof.path, attempt.path])
        writes.paths.clear()
        restarted = Service(svc.ship, svc.tmux)
        try:
            svc.reconcile()
            restarted.reconcile()
        finally:
            restarted.ledger.close()
        assert writes.under(pending_dir) == writes.under(svc.runtime / "post" / "attempts") == []
        assert marks([proof.path, attempt.path]) == before
    assert live["pasted"] == live["submitted"] == 1


def test_a_failed_folder_sync_is_raised_with_its_path_and_never_reported_repaired(setup, monkeypatch):
    """The rename survived but its folder sync failed: the next pass finds equal bytes and claims nothing."""
    svc, seat, live, record = setup
    item, proof = send(svc, seat)
    input_row(record, proof.data["payload"], post.now())
    folder = proof.path.parent
    real_fsync, armed = os.fsync, [True]

    def fsync(descriptor):
        info, target = os.fstat(descriptor), os.stat(folder)
        if armed[0] and stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            armed[0] = False
            raise OSError(errno.EIO, "synthetic folder sync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError) as raised:
        svc.reconcile()
    assert raised.value.errno == errno.EIO and not armed[0]
    trace = "".join(traceback.format_exception(raised.value))  # what the service's pass trace records
    assert str(proof.path) in trace and "synthetic folder sync failure" in trace
    assert json.loads(proof.path.read_text())["phase"] == "delivered"  # renamed into place before the failure
    assert svc.attempts.latest()[item.id] == "pending"  # the pass stopped before its receipt
    before, logged = aged([proof.path]), log_lines(svc)

    fresh, errors = pending._journals(svc.runtime, seat.name)
    assert not errors and len(fresh) == 1 and set(vars(fresh[0])) == {"path", "data"}, "no carried repair state"
    with pytest.MonkeyPatch.context() as patch:
        writes = Writes(patch)
        fresh[0].finish("delivered")
        svc.reconcile()  # the next pass builds its own journals again
        assert writes.under(folder) == []
    assert marks([proof.path]) == before
    assert svc.attempts.latest()[item.id] == "delivered", "the receipt is still settled on the next pass"
    later = log_lines(svc)[len(logged):]
    assert not [line for line in later if proof.path.name in line
                or any(word in line.lower() for word in ("repair", "resolv", "durab"))]
    assert live["pasted"] == live["submitted"] == 1


# -- one journal, read against the disk ------------------------------------------------------------


@pytest.fixture
def journal(tmp_path):
    post.Attempts(tmp_path).open(message(), "worker", "%1", 1001, None)
    path = tmp_path / "post" / "pending" / "worker" / f"{message().id}-1.json"
    pending.Proof(path, {}).save(**valid_journal(tmp_path, "submitted"))
    return path


def test_a_missing_journal_is_written_even_when_memory_already_matches(tmp_path, monkeypatch):
    data = valid_journal(tmp_path, "delivered")
    path = tmp_path / "post" / "pending" / "worker" / "absent-1.json"
    proof = pending.Proof(path, dict(data))
    writes = Writes(monkeypatch)
    proof.save(phase="delivered")
    assert writes.paths == [path] and path.read_bytes() == canonical(data)
    proof.save(phase="delivered")
    assert writes.paths == [path]


def test_the_disk_decides_not_the_object_in_memory(journal, monkeypatch):
    on_disk = json.loads(journal.read_text())
    behind = pending.Proof(journal, {**on_disk, "phase": "prepared"})  # another pass already moved it on
    writes = Writes(monkeypatch)
    behind.save(phase="submitted")
    assert writes.paths == [] and behind.data == on_disk, "equal to the disk: nothing to write"

    stale = pending.Proof(journal, dict(on_disk))
    journal.write_bytes(canonical({**on_disk, "shown": "synthetic edit made by another writer"}))
    stale.save()
    assert writes.paths == [journal] and journal.read_bytes() == canonical(on_disk), "memory differs from disk: written"


def test_a_differently_formatted_journal_is_normalised_once(journal, monkeypatch):
    data = json.loads(journal.read_text())
    journal.write_text(json.dumps(data))  # the same data in another form
    proof = pending._journals(journal.parents[3], "worker")[0][0]
    writes = Writes(monkeypatch)
    proof.save()
    assert writes.paths == [journal] and journal.read_bytes() == canonical(data)
    pending._journals(journal.parents[3], "worker")[0][0].save()
    assert writes.paths == [journal]


def test_a_failed_write_publishes_nothing_and_the_real_change_is_still_owed(journal, monkeypatch):
    old = journal.read_bytes()
    proof = pending.Proof(journal, json.loads(old))
    real_fsync, armed = os.fsync, [True]

    def fsync(descriptor):
        if armed[0] and not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            armed[0] = False
            raise OSError(errno.ENOSPC, "synthetic full disk")
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError) as raised:
        proof.save(phase="delivered")
    assert raised.value.errno == errno.ENOSPC and str(journal) in "".join(traceback.format_exception(raised.value))
    assert proof.data["phase"] == "submitted", "a failed write is not published in memory"
    assert journal.read_bytes() == old and sorted(p.name for p in journal.parent.iterdir()) == [journal.name]
    writes = Writes(monkeypatch)
    pending._journals(journal.parents[3], "worker")[0][0].finish("delivered")
    assert writes.paths == [journal] and json.loads(journal.read_text())["phase"] == "delivered"


def test_an_unreadable_journal_path_raises_with_its_path_and_publishes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "post" / "pending" / "worker" / "folder-1.json"
    path.mkdir(parents=True)
    proof = pending.Proof(path, valid_journal(tmp_path, "submitted"))
    writes = Writes(monkeypatch)
    with pytest.raises(OSError) as raised:
        proof.save(phase="delivered")
    assert str(path) in "".join(traceback.format_exception(raised.value))
    assert writes.paths == [] and proof.data["phase"] == "submitted" and path.is_dir()


def test_a_changed_journal_is_still_synced_renamed_and_its_folder_synced_and_an_equal_one_touches_nothing(journal, monkeypatch):
    proof = pending._journals(journal.parents[3], "worker")[0][0]
    with monkeypatch.context() as patch:
        disk = Disk(patch)
        proof.finish("delivered")
        assert disk.ops == ["file-fsync", "rename", "folder-fsync"]
    with monkeypatch.context() as patch:
        disk = Disk(patch)
        pending._journals(journal.parents[3], "worker")[0][0].finish("delivered")
        assert disk.ops == []


def test_a_reconstructed_journal_that_changes_survives_a_power_cut_at_every_step(tmp_path, monkeypatch):
    def run(name, cut):
        base = tmp_path / name
        base.mkdir()
        post.Attempts(base).open(message(), "worker", "%1", 1001, None)
        path = base / "post" / "pending" / "worker" / f"{message().id}-1.json"
        pending.Proof(path, {}).save(**valid_journal(base, "submitted"))
        old = path.read_bytes()
        with monkeypatch.context() as patch:
            disk = Disk(patch, cut)
            try:
                found, errors = pending._journals(base, "worker")
                assert not errors and len(found) == 1
                found[0].finish("delivered")
            except PowerCut:
                pass
            views = disk.views(path, old)
        return disk.ops, views - {old, path.read_bytes()}, path

    steps, exposed, path = run("uncut", None)
    assert steps == ["file-fsync", "rename", "folder-fsync"] and not exposed
    assert json.loads(path.read_text())["phase"] == "delivered"
    for cut in range(len(steps) + 1):
        _, exposed, _ = run(f"cut-{cut}", cut)
        assert not exposed, f"cut before step {cut} exposed {exposed!r}"


# -- attempt receipts ----------------------------------------------------------------------------


def attempt_item() -> post.Message:
    return post.Message("ord-20250101-0000-synthetic-wxyz", "order", "captain", "worker", post.now(), "synthetic")


def test_closing_a_receipt_writes_only_a_real_change_and_terminal_receipts_stay_terminal(tmp_path, monkeypatch):
    attempts = post.Attempts(tmp_path)
    writes = Writes(monkeypatch)
    opened = attempts.open(attempt_item(), "worker", "%1", 1001, None)
    assert writes.paths == [opened.path], "the first receipt is written"
    closed = attempts.close(opened, "queued", "synthetic queue")
    assert writes.paths == [opened.path] * 2
    before = aged([opened.path])

    assert attempts.close(opened, "queued", "synthetic queue") == closed  # a caller holding the older object
    assert attempts.close(closed, "queued", "synthetic queue") == closed
    assert len(writes.paths) == 2 and marks([opened.path]) == before

    detail = attempts.close(closed, "queued", "synthetic queue, detail changed")
    delivered = attempts.close(detail, "delivered", "synthetic confirmation")
    assert len(writes.paths) == 4 and post.Attempts(tmp_path).all() == [delivered]

    abandoned = attempts.close(delivered, "abandoned", "synthetic seat removed")
    assert len(writes.paths) == 5
    for result in ("delivered", "abandoned"):
        assert attempts.close(delivered, result, "a late door verdict") == abandoned
    assert len(writes.paths) == 5 and post.Attempts(tmp_path).all() == [abandoned]


def test_a_changed_receipt_is_still_durable_and_an_equal_one_touches_nothing(tmp_path, monkeypatch):
    attempts = post.Attempts(tmp_path)
    opened = attempts.open(attempt_item(), "worker", "%1", 1001, None)
    with monkeypatch.context() as patch:
        disk = Disk(patch)
        closed = attempts.close(opened, "delivered", "synthetic confirmation")
        assert disk.ops == ["file-fsync", "rename", "folder-fsync"]
    with monkeypatch.context() as patch:
        disk = Disk(patch)
        assert attempts.close(opened, "delivered", "synthetic confirmation") == closed
        assert disk.ops == []


# -- briefs --------------------------------------------------------------------------------------


@pytest.fixture
def briefed(drydock, tmp_path, monkeypatch):
    framework = tmp_path / "framework"
    shutil.copytree(standing.FRAMEWORK_DIR, framework)
    monkeypatch.setattr(standing, "FRAMEWORK_DIR", framework)
    ship = load(drydock)
    paths = {name: standing.render(ship, spec) for name, spec in ship.seats.items()}
    svc = Service(ship, SimpleNamespace(pane=lambda ident: None, has_session=lambda title: False))
    yield ship, svc, paths, framework
    svc.ledger.close()


def test_a_brief_is_written_when_missing_or_changed_and_not_when_equal(briefed):
    ship, svc, paths, framework = briefed
    captain = ship.seats["captain"]
    text = standing.brief(ship, captain).text
    assert paths["captain"].read_bytes() == text.encode(locale.getpreferredencoding(False))
    before = aged([paths["captain"]])
    standing.render(ship, captain)
    standing.render(ship, captain, text=text)
    assert marks([paths["captain"]]) == before

    standing.render(ship, captain, text=text + "\nsynthetic change\n")
    assert paths["captain"].read_text() == text + "\nsynthetic change\n"
    assert marks([paths["captain"]]) != before

    shutil.rmtree(paths["captain"].parent)
    standing.render(ship, captain)
    assert paths["captain"].read_text() == text


def test_ordinary_rebrief_rewrites_only_the_brief_whose_text_changed(briefed):
    ship, svc, paths, framework = briefed
    before = aged(paths.values())
    laws = framework / "laws.md"
    os.utime(laws, ns=(laws.stat().st_atime_ns, laws.stat().st_mtime_ns + 10_000_000_000))
    svc.rebrief()  # the sources' stamp moved, their text did not
    assert marks(paths.values()) == before

    card = framework / "roles" / "captain.md"
    card.write_text(card.read_text() + "\nSynthetic later instruction.\n")
    svc.rebrief()
    after = marks(paths.values())
    assert after[paths["captain"]] != before[paths["captain"]]
    assert "Synthetic later instruction." in paths["captain"].read_text()
    assert {p: m for p, m in after.items() if p != paths["captain"]} == \
        {p: m for p, m in before.items() if p != paths["captain"]}


def test_an_upgrade_held_by_an_unresolved_seat_retries_without_rewriting_healthy_briefs(briefed):
    ship, svc, paths, framework = briefed
    data = dict(kind="rebrief", commit="synthetic-commit", token="synthetic-token", at="2025-01-01T00:00:00+00:00",
                **install.brief_snapshot(ship))
    card = framework / "roles" / "captain.md"
    card.write_text(card.read_text() + "\nSynthetic upgraded instruction.\n")
    unknown = ship.runtime / "seats" / "worker-1" / "pool"
    unknown.mkdir(parents=True)  # an unreadable saved selection: the seat cannot be resolved

    assert svc.upgrade_rebrief(data) is None
    assert "Synthetic upgraded instruction." in paths["captain"].read_text()
    before = aged(paths.values())
    for _ in range(3):
        assert svc.upgrade_rebrief(data) is None, "the unresolved seat still holds the operation"
    assert marks(paths.values()) == before

    unknown.rmdir()
    answer = json.loads(svc.upgrade_rebrief(data))
    assert answer["status"] == "complete" and "changed briefs: captain ·" in answer["proof"]
    assert marks(paths.values()) == before


# -- writes that carry real new facts --------------------------------------------------------------


def test_heartbeat_and_status_still_advance_on_an_unchanged_ship(live):
    ship, svc, lock = live
    from quarterdeck import presentation, servicectl

    beat, status = servicectl.heartbeat_path(ship.runtime), ship.runtime / presentation.NAME
    old_beat, old_status = beat.read_text(), json.loads(status.read_text())
    svc.heartbeat()
    svc.publish_status()
    new_beat, new_status = beat.read_text(), json.loads(status.read_text())
    assert new_beat.split()[0] == old_beat.split()[0]
    assert datetime.fromisoformat(new_beat.split()[1]) > datetime.fromisoformat(old_beat.split()[1])
    assert new_status["wall"] > old_status["wall"] and new_status["monotonic"] > old_status["monotonic"]
    assert new_status["line"] == old_status["line"], "the same display is still a newer reading"
