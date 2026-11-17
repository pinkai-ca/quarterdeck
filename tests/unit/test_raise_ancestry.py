"""Ordinary Linux process metadata models for the approving process ancestry.

Independent consent gates and their recorded program scenarios stay on Claude.
"""
import json

import pytest

from quarterdeck import raise_ship
from quarterdeck.ship import ShipError


def stat(proc, pid, parent, start):
    folder = proc / str(pid)
    folder.mkdir(parents=True, exist_ok=True)
    fields = ["S", str(parent), *["0"] * 17, str(start)]
    (folder / "stat").write_text(f"{pid} (example (process) name) " + " ".join(fields) + "\n")


@pytest.fixture
def modeled_ancestry(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    stat(proc, 60, 40, 600)
    stat(proc, 40, 1, 400)
    stat(proc, 1, 0, 100)
    reader = raise_ship.process_ancestry
    monkeypatch.setattr(raise_ship, "process_ancestry", lambda _: reader(60, proc=proc))
    project = tmp_path / "project"
    project.mkdir()
    return project, proc, reader


def launch(project, location, *, pid=40, start="400", seat="example"):
    path = project / location / "runtime" / "trusted" / seat / "launch.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": pid} if start is ABSENT else {"pid": pid, "start": start}))
    return path


ABSENT = object()
STARTLESS = [ABSENT, None, "", "unknown", "4OO", 400]


def test_the_walk_preserves_each_parent_pid_and_start_time(modeled_ancestry):
    _, proc, reader = modeled_ancestry
    assert reader(60, proc=proc) == ((60, "600", 40), (40, "400", 1), (1, "100", 0))


def test_an_ancestry_under_no_recorded_program_is_accepted(modeled_ancestry):
    project, _, _ = modeled_ancestry
    assert raise_ship.require_person_process(project) == []


@pytest.mark.parametrize("location", ["office", "ships/example", "ships/drydock"])
def test_a_modeled_recorded_pid_and_start_is_refused(modeled_ancestry, location):
    project, _, _ = modeled_ancestry
    path = launch(project, location)
    before = path.read_bytes()
    with pytest.raises(ShipError, match="recorded seat program"):
        raise_ship.require_person_process(project)
    assert path.read_bytes() == before


def test_a_reused_pid_with_a_different_start_is_a_different_process(modeled_ancestry):
    project, _, _ = modeled_ancestry
    launch(project, "office", start="399")
    assert raise_ship.require_person_process(project) == []


def test_an_unreadable_parent_refuses_the_approval(modeled_ancestry):
    project, proc, _ = modeled_ancestry
    (proc / "40" / "stat").unlink()
    with pytest.raises(ShipError, match="cannot read.*ancestry"):
        raise_ship.require_person_process(project)


def test_malformed_process_metadata_is_unknown(modeled_ancestry):
    project, proc, _ = modeled_ancestry
    (proc / "40" / "stat").write_text("40 (example) S unreadable\n")
    with pytest.raises(ShipError, match="cannot read.*ancestry"):
        raise_ship.require_person_process(project)


def test_a_changing_ancestry_is_unknown(modeled_ancestry, monkeypatch):
    project, _, _ = modeled_ancestry
    readings = iter([((60, "600", 1), (1, "100", 0)), ((60, "600", 0),)])
    monkeypatch.setattr(raise_ship, "process_ancestry", lambda _: next(readings))
    with pytest.raises(ShipError, match="ancestry changed"):
        raise_ship.require_person_process(project)


def test_an_unreadable_launch_identity_refuses_a_new_yes(modeled_ancestry):
    project, _, _ = modeled_ancestry
    path = launch(project, "office")
    path.write_text("unreadable")
    with pytest.raises(ShipError, match="cannot read.*recorded seat programs"):
        raise_ship.require_person_process(project)


@pytest.mark.parametrize("start", STARTLESS)
def test_a_record_without_a_start_whose_pid_is_no_ancestor_is_ignored_and_named(modeled_ancestry, start):
    project, _, _ = modeled_ancestry
    path = launch(project, "ships/example", pid=77, start=start)
    before = path.read_bytes()
    assert raise_ship.require_person_process(project) == [
        f"a recorded seat program without a readable start time was ignored: {path} "
        "(its pid 77 is not in this process ancestry)"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("start", STARTLESS)
@pytest.mark.parametrize("pid", [60, 40, 1])
def test_a_record_without_a_start_whose_pid_is_an_ancestor_refuses_the_yes(modeled_ancestry, start, pid):
    project, _, _ = modeled_ancestry
    path = launch(project, "office", pid=pid, start=start)
    before = path.read_bytes()
    with pytest.raises(ShipError, match=rf"without a readable start time \({path}\) has pid {pid}, "
                                        "which is in this process ancestry"):
        raise_ship.require_person_process(project)
    assert path.read_bytes() == before


def test_a_full_record_still_decides_by_pid_and_start_beside_a_startless_one(modeled_ancestry):
    project, _, _ = modeled_ancestry
    launch(project, "ships/example", pid=77, start=None)
    launch(project, "office", pid=40, start="400", seat="other")
    with pytest.raises(ShipError, match="recorded seat program owns this process ancestry"):
        raise_ship.require_person_process(project)


@pytest.mark.parametrize("pid", [None, "40", 0, -1, 4.0, True])
def test_a_record_without_a_readable_pid_still_refuses_every_yes(modeled_ancestry, pid):
    project, _, _ = modeled_ancestry
    launch(project, "office", pid=pid, start=None)
    with pytest.raises(ShipError, match="cannot read.*recorded seat programs"):
        raise_ship.require_person_process(project)
