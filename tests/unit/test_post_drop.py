"""A seat's message is a drop. The service files it, and sets the sender from the folder."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from quarterdeck import cli, post, ship as shipmod
from quarterdeck.ship import ShipError



@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    """A ship's runtime by its own name, so the post folder's no-follow walk applies as it does in a ship."""
    folder = tmp_path / "runtime"
    folder.mkdir()
    return folder

def test_an_open_post_is_filed_once_and_the_bytes_match(drydock, monkeypatch, capsys):
    monkeypatch.delenv("QD_SEAT", raising=False)
    code = cli.main(["--ship", str(drydock), "post", "order", "--to", "captain", "Count the stars café"])
    output = capsys.readouterr().out
    assert code == 0 and "· filed by the service ·" in output
    store = post.Store(shipmod.load(drydock).runtime)
    # The command returns before the store has the message. The drop has the exact bytes.
    ship_runtime = store.dir.parent.parent
    pending = list((ship_runtime / "post" / "drop" / "operator").glob("*.json"))
    assert len(pending) == 1
    raw = pending[0].read_bytes()
    assert "café".encode() in raw
    fresh = post.file_drops(store)
    assert fresh == []
    assert not pending[0].exists()
    assert (store.dir / pending[0].name).read_bytes() == raw
    message = store.all()[0]
    assert message.sender == "operator" and message.to == "captain"
    assert post.file_drops(store) == [] and len(store.all()) == 1


def test_a_restart_after_the_store_write_files_the_drop_once(runtime: Path):
    store = post.Store(runtime)
    message = store.prepare("order", "worker", "desk", "once")
    path = post.write_drop(runtime, message)
    text = path.read_text()
    store.publish_text(message, text)  # the service wrote the store and stopped before removing the drop
    assert path.is_file() and (store.dir / f"{message.id}.json").read_text() == text
    assert post.file_drops(store) == []
    assert not path.exists()
    assert (store.dir / f"{message.id}.json").read_text() == text
    assert len(list(store.dir.glob("*.json"))) == 1
    assert post.file_drops(store) == [] and len(store.all()) == 1


def test_a_drop_that_names_another_sender_is_held_and_kept(runtime: Path):
    store = post.Store(runtime)
    foreign = store.prepare("order", "captain", "desk", "FOREIGN")
    folder = post.drop_dir(runtime, "worker")
    folder.mkdir(parents=True)
    planted = folder / f"{foreign.id}.json"
    planted.write_text(foreign.to_json())
    fresh = post.file_drops(store)
    assert store.all() == [] and planted.is_file()
    assert ("drop/worker", "from names another seat") in fresh
    assert any("drop/worker" in line and "from names another seat" in line for line in post.drop_alarms(runtime))
    assert post.file_drops(store) == []  # the same hold is not logged again
    assert planted.is_file() and store.all() == []


def test_a_link_in_the_drop_is_held_and_not_followed(runtime: Path):
    store = post.Store(runtime)
    outside = runtime / "outside.txt"
    outside.write_text("UNTOUCHED")
    folder = post.drop_dir(runtime, "worker")
    folder.mkdir(parents=True)
    link = folder / "linked.json"
    link.symlink_to(outside)
    fresh = post.file_drops(store)
    assert ("drop/worker", "not a regular file") in fresh
    assert outside.read_text() == "UNTOUCHED" and link.is_symlink()
    assert store.all() == []


def test_a_different_file_at_an_existing_id_is_held(runtime: Path):
    store = post.Store(runtime)
    existing = store.new("order", "worker", "desk", "ORIGINAL")
    raw = (store.dir / f"{existing.id}.json").read_bytes()
    folder = post.drop_dir(runtime, "worker")
    folder.mkdir(parents=True)
    planted = folder / f"{existing.id}.json"
    planted.write_text(existing.to_json().replace("ORIGINAL", "REPLACED"))
    fresh = post.file_drops(store)
    assert ("drop/worker", "id already stored") in fresh
    assert (store.dir / f"{existing.id}.json").read_bytes() == raw
    assert planted.is_file() and store.get(existing.id).body == "ORIGINAL"


def test_a_report_or_ack_closes_or_claims_only_from_that_seats_drop(runtime: Path):
    store = post.Store(runtime)
    order = store.new("order", "captain", "desk", "Do the thing.")
    other = store.new("order", "captain", "desk", "Leave this open.")
    report = store.prepare("report", "desk", "captain", "done", re=order.id)
    ack = store.prepare("ack", "desk", "captain", "claimed", re=other.id)
    wrong = post.drop_dir(runtime, "worker")
    wrong.mkdir(parents=True)
    (wrong / f"{report.id}.json").write_text(report.to_json())
    (wrong / f"{ack.id}.json").write_text(ack.to_json())
    post.file_drops(store)
    assert store.is_open(order) and store.is_open(other)
    assert store.claimed("desk", post.now()) is None
    post.write_drop(runtime, report)
    post.write_drop(runtime, ack)
    post.file_drops(store)
    assert not store.is_open(order) and store.closing_report(order).sender == "desk"
    assert store.is_open(other) and store.claimed("desk", post.now()) == other.id


def test_a_drop_is_filed_only_when_the_id_the_from_and_the_folder_agree(runtime: Path):
    store = post.Store(runtime)
    message = store.prepare("order", "worker-1", "desk", "agreed")
    path = post.write_drop(runtime, message)
    assert post.id_sender(message.id) == "worker-1"
    text = path.read_text()
    assert post.file_drops(store) == []
    assert (store.dir / f"{message.id}.json").read_text() == text
    other = store.prepare("order", "worker-1", "desk", "renamed")
    forged_id = other.id.replace("-worker-1-", "-captain-", 1)
    planted = path.parent / f"{forged_id}.json"
    planted.write_text(other.to_json().replace(other.id, forged_id))
    fresh = post.file_drops(store)
    assert ("drop/worker-1", "from names another seat") in fresh
    assert planted.is_file() and all(item.id != forged_id for item in store.all())
    broken = path.parent / "ord-not-an-id.json"
    broken.write_text(other.to_json().replace(other.id, "ord-not-an-id"))
    assert ("drop/worker-1", "not a message") in post.file_drops(store)
    assert broken.is_file()


def test_a_pending_drop_id_is_not_allocated_again(runtime: Path):
    store = post.Store(runtime)
    first = store.prepare("order", "worker", "desk", "one")
    post.write_drop(runtime, first)
    second = store.prepare("order", "worker", "desk", "two")
    assert first.id != second.id and first.short != second.short


def test_publish_refuses_a_name_that_is_already_there(runtime: Path):
    store = post.Store(runtime)
    message = store.new("order", "worker", "desk", "kept")
    with pytest.raises(ShipError, match="immutable"):
        store.publish_text(message, message.to_json())
    link = store.dir / ("-".join(("ord", "20261114", "0000", "worker", "ffff")) + ".json")
    link.symlink_to(runtime / "missing")
    forged = post.Message(link.name[:-5], "order", "worker", "desk", message.at, "no")
    with pytest.raises(ShipError, match="post link held"):  # the post folder's walk names a link, never follows it
        store.publish_text(forged, forged.to_json())
    assert not (runtime / "missing").exists()
