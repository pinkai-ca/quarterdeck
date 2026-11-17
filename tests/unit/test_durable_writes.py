"""Whole-file writers keep old or complete new data across process interruption.

The same writer scenarios remain. Both sync APIs fail except for the initial
attempt's named file and folder barriers; cuts before
and after each rename leave a named target old or equal to the complete staged
bytes. Power-loss persistence is deliberately not promised by the new default.
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


class ProcessCrash(BaseException):
    """A process interrupted at a publication boundary."""


class Disk:
    def __init__(self, monkeypatch, cut_at: int | None = None, *, law_record=None):
        self.ops: list[str] = []
        self.intended = {}
        self.syncs = []
        real_sync = os.fsync
        real_replace, real_rename = os.replace, os.rename
        def step(name):
            if cut_at is not None and len(self.ops) == cut_at:
                raise ProcessCrash(f'cut before {name}')
            self.ops.append(name)
        def forbidden_sync(_):
            pytest.fail('ordinary writer forced a storage sync')
        def sync(descriptor):
            if law_record is None:
                forbidden_sync(descriptor)
            actual = Path(os.readlink(f'/proc/self/fd/{descriptor}'))
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                assert actual == law_record.parent
                kind = 'folder'
            else:
                assert actual.parent == law_record.parent and actual.name.startswith('.' + law_record.name + '.')
                kind = 'file'
            self.syncs.append(kind)
            real_sync(descriptor)
        def renaming(real):
            def move(source, target, *args, **kwargs):
                # A rename by name within a folder's handle (as the seat folder's no-follow writer makes it) is
                # resolved through that handle; the record of what each name was to hold is by full path, as before.
                def full(name, folder):
                    return Path(os.readlink(f'/proc/self/fd/{folder}')) / name if folder is not None else Path(name)
                self.intended[os.fspath(full(target, kwargs.get('dst_dir_fd')))] = \
                    full(source, kwargs.get('src_dir_fd')).read_bytes()
                step('before-rename')
                result = real(source, target, *args, **kwargs)
                step('after-rename')
                return result
            return move
        monkeypatch.setattr(os, 'fsync', sync)
        monkeypatch.setattr(os, 'fdatasync', forbidden_sync)
        monkeypatch.setattr(os, 'replace', renaming(real_replace))
        monkeypatch.setattr(os, 'rename', renaming(real_rename))

    def views(self, path: Path, old: bytes | None) -> set[bytes | None]:
        return {path.read_bytes() if path.exists() else None}

    def allowed(self, path, old):
        return {old, self.intended.get(os.fspath(path), old)}


def seat_at(base: Path) -> Seat:
    pools = {name: Pool(name, "fake") for name in ("pool-a", "pool-b")}
    spec = SeatSpec("worker", "crew", "worker", "fake", tuple(pools), cwd=base)
    ship = Ship(base, "example", "operator", base, pools, {"crew": Session("crew", "crew")}, {"worker": spec})
    seat = Seat(ship, spec, SimpleNamespace(pane=lambda ident: None))
    seat.folder.mkdir(parents=True, exist_ok=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    seat.home.mkdir(parents=True, exist_ok=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
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
    launched("%1", 1001).write(seat.trusted / "launch.json")
    path = seat.trusted / "launch.json"
    return [path, seat.trusted / "launch-price.json"], lambda: launched("%2", 1002).write(path), \
        lambda: (lambda found: found and (found.pane, found.price))(seat_at(base).launched())


def record_pointer(base, monkeypatch):
    seat = seat_at(base)
    launched("%1", 1001).write(seat.trusted / "launch.json")
    record = seat.home / "synthetic-record.jsonl"
    record.write_text("{}\n")
    monkeypatch.setattr(seat.harness, "join", lambda *args: Join(record))
    return [seat.trusted / "record"], seat.join, lambda: seat_at(base).join()


def pool_selection(base, monkeypatch):
    seat = seat_at(base)
    seat.move_to("pool-a")
    return [seat.trusted / "pool"], lambda: seat.move_to("pool-b"), lambda: (seat_at(base).trusted / "pool").read_text()


SCENARIOS = [post_new, ack_new_beside_posts, attempt_open, attempt_close, journal_new, journal_update,
             launch_replace, record_pointer, pool_selection]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.__name__)
def test_process_interruption_keeps_old_or_complete_new_files_with_only_the_initial_attempt_synced(tmp_path, monkeypatch, scenario):
    def run(name, cut_at):
        base = tmp_path / name
        base.mkdir()
        with monkeypatch.context() as patch:
            targets, write, read = scenario(base, patch)
            old = {path: path.read_bytes() if path.exists() else None for path in targets}
            disk = Disk(patch, cut_at, law_record=targets[0] if scenario is attempt_open else None)
            try:
                write()
            except ProcessCrash:
                pass
            if cut_at is None:
                assert disk.syncs == (['file', 'folder'] if scenario is attempt_open else [])
            seen = {path: disk.views(path, old[path]) for path in targets}
            exposed = []
            for path, views in seen.items():
                new = path.read_bytes() if path.exists() else None
                for view in views - disk.allowed(path, old[path]):
                    path.write_bytes(view)
                    exposed.append(f"{path.name} shows {view[:40]!r}; reader: {outcome(read)}")
            return disk.ops, exposed

    steps, _ = run("uncut", None)
    assert steps and len(steps) % 2 == 0  # every writer must publish by rename
    failures = []
    for cut in range(len(steps) + 1):
        _, exposed = run(f"cut-{cut}", cut)
        label = f"cut before {steps[cut]}" if cut < len(steps) else f"after {steps[-1] if steps else 'the write'}"
        failures += [f"{label}: {line}" for line in exposed]
    assert not failures, "\n".join(failures)


def test_the_shared_writer_renames_whole_without_any_sync(tmp_path, monkeypatch):
    from quarterdeck import durable
    path = tmp_path / 'launch.json'
    path.write_text('old\n')
    before = {entry.name for entry in os.scandir(tmp_path)}
    with monkeypatch.context() as patch:
        disk = Disk(patch)
        durable.replace_file(path, 'new Ω\n')
        assert disk.ops == ['before-rename', 'after-rename']
    assert path.read_text() == 'new Ω\n' and {entry.name for entry in os.scandir(tmp_path)} == before
    for cut in range(2):
        with monkeypatch.context() as patch, pytest.raises(ProcessCrash):
            Disk(patch, cut)
            durable.replace_file(path, f'cut {cut}\n')
        live = 'new Ω\n' if cut == 0 else 'cut 1\n'
        assert path.read_text() == live and {entry.name for entry in os.scandir(tmp_path)} == before


def test_the_shared_writer_removes_its_temporary_on_an_ordinary_failure(tmp_path, monkeypatch):
    from quarterdeck import durable

    path = tmp_path / "pool"
    path.write_text("pool-a\n")

    def refuse(*args):
        raise OSError("synthetic full disk")

    def forbidden_sync(_):
        pytest.fail('ordinary failed write forced a sync')
    monkeypatch.setattr(os, 'fsync', forbidden_sync)
    monkeypatch.setattr(os, 'fdatasync', forbidden_sync)
    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="synthetic full disk"):
        durable.replace_file(path, "pool-b\n")
    assert path.read_text() == "pool-a\n" and [entry.name for entry in os.scandir(tmp_path)] == ["pool"]


@pytest.mark.parametrize("pointer", [b"", b"\n", b"  \n", b"\xff\xfe\n"], ids=["empty", "newline", "blank", "undecodable"])
def test_an_emptied_or_unreadable_record_pointer_joins_no_record(tmp_path, pointer):
    """What the crash left for signer: a pointer emptied after its launch; nothing is guessed from it."""
    seat = seat_at(tmp_path)
    launched("%1", 1001).write(seat.trusted / "launch.json")
    (seat.trusted / "record").write_bytes(pointer)
    joined = seat.join()
    assert joined.record is None
    assert joined.reason.startswith("record pointer empty" if pointer.strip() == b"" else "record pointer unreadable")
    assert seat.record() is None
