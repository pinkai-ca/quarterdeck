"""A power cut at any step of a durable write leaves a reader the old file or the complete new one.

The cut is simulated in-process. ``Disk`` records the operations that order
durability (a file's fsync, a rename, a folder's fsync) and raises ``PowerCut``
before the k-th of them, for every k. What survives is then derived from the
POSIX contract, as delayed allocation exercises it: data not yet fsynced may come
back empty or partial, and a rename (or an in-place rewrite) not yet made durable
by its folder's fsync may come back undone. Every such view is checked against
the old state and the complete new one, and a view outside them is shown with
what the program's own reader then sees.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from quarterdeck import pending, post
from quarterdeck.harness.types import Join
from quarterdeck.seat import Launched, Seat
from quarterdeck.ship import Pool, SeatSpec, Session, Ship


class PowerCut(BaseException):
    """Not an Exception: no writer may recover from losing power."""


class Disk:
    def __init__(self, monkeypatch, cut_at: int | None = None):
        self.ops: list[str] = []
        self.cut_at = cut_at
        self.synced: dict[int, bytes] = {}
        self.renamed: dict[str, bool] = {}
        real_fsync, real_replace, real_rename = os.fsync, os.replace, os.rename

        def step(name):
            if self.cut_at is not None and len(self.ops) == self.cut_at:
                raise PowerCut(f"cut before {name}")
            self.ops.append(name)

        def fsync(fd):
            info = os.fstat(fd)
            folder = stat.S_ISDIR(info.st_mode)
            step("folder-fsync" if folder else "file-fsync")
            real_fsync(fd)
            if folder:
                for name in self.renamed:
                    parent = os.stat(Path(name).parent)
                    if (parent.st_dev, parent.st_ino) == (info.st_dev, info.st_ino):
                        self.renamed[name] = True
            else:
                with open(f"/proc/self/fd/{fd}", "rb") as stream:
                    self.synced[info.st_ino] = stream.read()

        def renaming(real):
            def move(source, target, *args, **kwargs):
                step("rename")
                result = real(source, target, *args, **kwargs)
                self.renamed[os.fspath(target)] = False
                return result
            return move

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "replace", renaming(real_replace))
        monkeypatch.setattr(os, "rename", renaming(real_rename))

    def views(self, path: Path, old: bytes | None) -> set[bytes | None]:
        """What the name can hold after the power returns (None: absent)."""
        try:
            current = path.read_bytes()
            inode = path.stat().st_ino
        except FileNotFoundError:
            return {old}
        if current == old:
            return {old}
        data = {current} if self.synced.get(inode) == current else {b"", current[:len(current) // 2], current}
        return data if self.renamed.get(os.fspath(path)) else data | {old}


def seat_at(base: Path) -> Seat:
    pools = {name: Pool(name, "fake") for name in ("pool-a", "pool-b")}
    spec = SeatSpec("worker", "crew", "worker", "fake", tuple(pools), cwd=base)
    ship = Ship(base, "example", "admiral", base, pools, {"crew": Session("crew", "crew")}, {"worker": spec})
    seat = Seat(ship, spec, SimpleNamespace(pane=lambda ident: None))
    seat.folder.mkdir(parents=True, exist_ok=True)
    seat.home.mkdir(parents=True, exist_ok=True)
    return seat


def launched(pane: str, pid: int) -> Launched:
    return Launched(pane, pid, post.now().isoformat(), "pool-a", None, ["fake"], "synthetic", "fake",
                    price={"synthetic": "Ω rate"})


def message(body: str = "synthetic order: Ω durable body") -> post.Message:
    return post.Message("ord-20300101-2200-lead-0d0a", "order", "lead", "worker", post.now(), body)


def journal_data(runtime: Path, phase: str) -> dict:
    return dict(seat="worker", identity={"pane": "%1", "pid": 1001, "launch": None, "harness": "fake"},
                at=post.now().isoformat(), since=post.now().isoformat(), payload="synthetic Ω payload",
                phase=phase, record=None, shown=None, attempts=[f"{message().id}-1.json"])


def outcome(read):
    try:
        return repr(read())[:160]
    except Exception as error:
        return f"refused: {type(error).__name__}: {error}"[:160]


# Each scenario prepares its old state, then names its targets, the writer to cut and the reader.

def post_new(base, monkeypatch):
    store = post.Store(base)
    return [store.dir / f"{message().id}.json"], lambda: store.write(message()), \
        lambda: [m.body for m in post.Store(base).all()]


def ack_new_beside_posts(base, monkeypatch):
    store = post.Store(base)
    store.write(message())
    ack = post.Message("ack-20300101-2201-worker-0d0b", "ack", "worker", "lead", post.now(), "started", re=message().id)
    return [store.dir / f"{ack.id}.json"], lambda: store.write(ack), \
        lambda: [m.id for m in post.Store(base).all()]


def attempt_open(base, monkeypatch):
    attempts = post.Attempts(base)
    return [attempts.dir / f"{message().id}-1.json"], \
        lambda: attempts.open(message(), "worker", "%1", 1001, None), \
        lambda: [(a.message, a.result) for a in post.Attempts(base).all()]


def attempt_close(base, monkeypatch):
    attempts = post.Attempts(base)
    opened = attempts.open(message(), "worker", "%1", 1001, None)
    return [opened.path], lambda: attempts.close(opened, "delivered", "confirmed by record"), \
        lambda: [(a.message, a.result) for a in post.Attempts(base).all()]


def journal_new(base, monkeypatch):
    post.Attempts(base).open(message(), "worker", "%1", 1001, None)
    path = base / "post" / "pending" / "worker" / f"{message().id}-1.json"
    proof = pending.Proof(path, {})
    return [path], lambda: proof.save(**journal_data(base, "prepared")), \
        lambda: (lambda found, errors: ([p.data["phase"] for p in found], errors))(*pending._journals(base, "worker"))


def journal_update(base, monkeypatch):
    post.Attempts(base).open(message(), "worker", "%1", 1001, None)
    path = base / "post" / "pending" / "worker" / f"{message().id}-1.json"
    proof = pending.Proof(path, {})
    proof.save(**journal_data(base, "prepared"))
    return [path], lambda: proof.save(phase="submitted", shown="synthetic Ω payload"), \
        lambda: (lambda found, errors: ([p.data["phase"] for p in found], errors))(*pending._journals(base, "worker"))


def launch_replace(base, monkeypatch):
    seat = seat_at(base)
    launched("%1", 1001).write(seat.folder / "launch.json")
    path = seat.folder / "launch.json"
    return [path, seat.folder / "launch-price.json"], lambda: launched("%2", 1002).write(path), \
        lambda: (lambda found: found and (found.pane, found.price))(seat_at(base).launched())


def record_pointer(base, monkeypatch):
    seat = seat_at(base)
    launched("%1", 1001).write(seat.folder / "launch.json")
    record = seat.home / "synthetic-record.jsonl"
    record.write_text("{}\n")
    monkeypatch.setattr(seat.harness, "join", lambda *args: Join(record))
    return [seat.folder / "record"], seat.join, lambda: seat_at(base).join()


def pool_selection(base, monkeypatch):
    seat = seat_at(base)
    seat.move_to("pool-a")
    return [seat.folder / "pool"], lambda: seat.move_to("pool-b"), lambda: (seat_at(base).folder / "pool").read_text()


SCENARIOS = [post_new, ack_new_beside_posts, attempt_open, attempt_close, journal_new, journal_update,
             launch_replace, record_pointer, pool_selection]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.__name__)
def test_a_power_cut_at_any_step_leaves_the_old_or_the_complete_new_file(tmp_path, monkeypatch, scenario):
    def run(name, cut_at):
        base = tmp_path / name
        base.mkdir()
        with monkeypatch.context() as patch:
            targets, write, read = scenario(base, patch)
            old = {path: path.read_bytes() if path.exists() else None for path in targets}
            disk = Disk(patch, cut_at)
            try:
                write()
            except PowerCut:
                pass
            seen = {path: disk.views(path, old[path]) for path in targets}
            exposed = []
            for path, views in seen.items():
                new = path.read_bytes() if path.exists() else None
                for view in views - {old[path], new}:
                    path.write_bytes(view)
                    exposed.append(f"{path.name} shows {view[:40]!r}; reader: {outcome(read)}")
            return disk.ops, exposed

    steps, _ = run("uncut", None)
    failures = []
    for cut in range(len(steps) + 1):
        _, exposed = run(f"cut-{cut}", cut)
        label = f"cut before {steps[cut]}" if cut < len(steps) else f"after {steps[-1] if steps else 'the write'}"
        failures += [f"{label}: {line}" for line in exposed]
    assert not failures, "\n".join(failures)


def test_the_shared_writer_syncs_the_file_then_renames_then_syncs_the_folder(tmp_path, monkeypatch):
    from quarterdeck import durable

    path = tmp_path / "launch.json"
    path.write_text("old\n")
    before = {entry.name for entry in os.scandir(tmp_path)}
    disk = Disk(monkeypatch)
    durable.replace_file(path, "new Ω\n")
    assert disk.ops == ["file-fsync", "rename", "folder-fsync"]
    assert path.read_text() == "new Ω\n" and {entry.name for entry in os.scandir(tmp_path)} == before
    monkeypatch.undo()  # each cut below gets the real os beneath one Disk, never a Disk beneath another
    for cut in range(3):
        with monkeypatch.context() as patch, pytest.raises(PowerCut):
            Disk(patch, cut)
            durable.replace_file(path, f"cut {cut}\n")
        live = "new Ω\n" if cut < 2 else "cut 2\n"  # the last cut falls after the rename
        assert path.read_text() == live and {entry.name for entry in os.scandir(tmp_path)} == before


def test_the_shared_writer_removes_its_temporary_on_an_ordinary_failure(tmp_path, monkeypatch):
    from quarterdeck import durable

    path = tmp_path / "pool"
    path.write_text("pool-a\n")

    def refuse(*args):
        raise OSError("synthetic full disk")

    monkeypatch.setattr(os, "fsync", refuse)
    with pytest.raises(OSError, match="synthetic full disk"):
        durable.replace_file(path, "pool-b\n")
    assert path.read_text() == "pool-a\n" and [entry.name for entry in os.scandir(tmp_path)] == ["pool"]


@pytest.mark.parametrize("pointer", [b"", b"\n", b"  \n", b"\xff\xfe\n"], ids=["empty", "newline", "blank", "undecodable"])
def test_an_emptied_or_unreadable_record_pointer_joins_no_record(tmp_path, pointer):
    """What the crash left for una: a pointer emptied after its launch; nothing is guessed from it."""
    seat = seat_at(tmp_path)
    launched("%1", 1001).write(seat.folder / "launch.json")
    (seat.folder / "record").write_bytes(pointer)
    joined = seat.join()
    assert joined.record is None
    assert joined.reason.startswith("record pointer empty" if pointer.strip() == b"" else "record pointer unreadable")
    assert seat.record() is None
