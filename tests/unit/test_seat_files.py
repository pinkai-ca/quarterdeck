"""Writes into a seat's folder never follow a link the seat's jail could have planted (seat_files)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from quarterdeck import durable, seat_files

DECOY = "decoy\n"


@pytest.fixture
def seat(tmp_path):
    folder = tmp_path / "runtime" / "seats" / "worker"
    (folder / "home").mkdir(parents=True)
    decoy = tmp_path / "operator" / "decoy.txt"
    decoy.parent.mkdir()
    decoy.write_text(DECOY)
    return folder, decoy


def test_a_link_at_the_name_is_replaced_by_the_written_file(seat):
    folder, decoy = seat
    (folder / "launch.sh").symlink_to(decoy)
    seat_files.write_text(folder / "launch.sh", "#!/bin/sh\n")
    assert decoy.read_text() == DECOY
    assert not (folder / "launch.sh").is_symlink() and (folder / "launch.sh").read_text() == "#!/bin/sh\n"


def test_durable_writes_in_a_seat_folder_take_the_same_walk(seat):
    folder, decoy = seat
    for name, write in (("a.json", durable.replace_file), ("b.json", durable._replace_law_record)):
        (folder / "home" / name).symlink_to(decoy)
        write(folder / "home" / name, "{}\n")
        assert decoy.read_text() == DECOY and not (folder / "home" / name).is_symlink()


def test_a_link_on_the_way_is_refused_and_nothing_is_written(seat, tmp_path):
    folder, decoy = seat
    elsewhere = tmp_path / "operator" / "dir"
    elsewhere.mkdir()
    (folder / "home" / "agent").symlink_to(elsewhere)
    with pytest.raises(seat_files.LinkRefused):
        durable.replace_file(folder / "home" / "agent" / "settings.json", "{}\n")
    with pytest.raises(seat_files.LinkRefused):
        seat_files.symlink(folder / "home" / "agent" / "auth.json", decoy)
    assert list(elsewhere.iterdir()) == []


def test_a_symlink_is_renamed_over_whatever_stood_there(seat):
    folder, decoy = seat
    source = folder.parent.parent / "pool.json"
    source.write_text("pool\n")
    (folder / "home" / "cred.json").symlink_to(decoy)
    seat_files.symlink(folder / "home" / "cred.json", source)
    assert os.readlink(folder / "home" / "cred.json") == str(source) and decoy.read_text() == DECOY
    (folder / "home" / "cred.json").unlink()
    (folder / "home" / "cred.json").write_text("refresh\n")
    seat_files.symlink(folder / "home" / "cred.json", source)
    assert (folder / "home" / "cred.json").is_symlink()


def test_an_append_through_a_link_is_refused(seat):
    folder, decoy = seat
    (folder / "acts.jsonl").symlink_to(decoy)
    with pytest.raises(seat_files.LinkRefused):
        with seat_files.open_append(folder / "acts.jsonl") as stream:
            stream.write("x\n")
    assert decoy.read_text() == DECOY
    with seat_files.open_append(folder / "acts-other.jsonl", "a+") as stream:
        stream.write("one\n")
    assert (folder / "acts-other.jsonl").read_text() == "one\n"


def test_a_read_that_feeds_a_write_reads_a_link_as_nothing(seat):
    folder, decoy = seat
    (folder / "home" / ".claude.json").symlink_to(decoy)
    assert seat_files.read_text(folder / "home" / ".claude.json") is None
    assert seat_files.read_with_times(folder / "home" / ".claude.json") is None
    (folder / "home" / "plain.json").write_text("{}")
    assert seat_files.read_text(folder / "home" / "plain.json") == "{}"
    assert seat_files.read_text(folder / "home" / "missing" / "x.json") is None


def test_a_copy_keeps_the_sources_bytes_times_and_the_mode_asked(seat, tmp_path):
    folder, decoy = seat
    source = tmp_path / "pool.json"
    source.write_text("token\n")
    os.utime(source, ns=(1_000_000_000, 2_000_000_000))
    (folder / "home" / "cred.json").symlink_to(decoy)
    seat_files.copy(source, folder / "home" / "cred.json", mode=0o600)
    held = (folder / "home" / "cred.json").lstat()
    assert (folder / "home" / "cred.json").read_text() == "token\n" and held.st_mtime_ns == 2_000_000_000
    assert held.st_mode & 0o777 == 0o600 and decoy.read_text() == DECOY


def test_unlink_removes_a_link_itself_never_its_target(seat):
    folder, decoy = seat
    (folder / "home" / "x").symlink_to(decoy)
    seat_files.unlink(folder / "home" / "x")
    assert not os.path.lexists(folder / "home" / "x") and decoy.read_text() == DECOY
    seat_files.unlink(folder / "home" / "gone" / "y")  # nothing there: nothing to do


def test_a_path_outside_any_seat_folder_keeps_the_ordinary_write(tmp_path):
    target = tmp_path / "elsewhere" / "file.txt"
    target.parent.mkdir()
    assert not seat_files.under_seat(target)
    seat_files.write_text(target, "plain\n")
    assert target.read_text() == "plain\n"
