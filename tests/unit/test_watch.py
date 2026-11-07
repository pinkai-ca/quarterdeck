"""The Watch's pure parts: the status line from facts, the disk reading, the age format."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from quarterdeck import watch
from quarterdeck.watch import Facts


def test_the_status_line_says_everything_that_waits():  # law: nothing-waits-silently
    quiet = Facts(service=True, waiting=0, oldest=0, blind=0)
    assert quiet.line() == "service ok · 0 waiting"
    loud = Facts(service=False, waiting=2, oldest=14, blind=1, needs_human=["laan"], unarmed=["desk"],
                 walls=["claude-primary 7d"], disks=[("root", 94)], login=[("claude-secondary", "11-13 22:24")],
                 queued=1, oldest_queued=31)
    assert loud.line() == (
        "SERVICE DOWN · 2 waiting, oldest 14m · 1 queued behind a turn, oldest 31m · 1 blind · laan needs a human"
        " · desk: no watch armed · wall: claude-primary 7d · disk: root 94% · claude-secondary · login needed by 11-13 22:24"
    )


def test_disks_names_only_the_nearly_full_filesystems(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(watch, "DISK_WARN", 2.0)
    assert watch.disks([tmp_path, Path("/")]) == []
    monkeypatch.setattr(watch, "DISK_WARN", 0.0)
    found = watch.disks([tmp_path, tmp_path / "again", Path("/nonexistent")])
    assert len(found) == 1 and found[0][1] >= 0, "one filesystem is named once"


def test_ages_read_as_a_person_says_them():
    now = datetime.now(timezone.utc)
    assert watch._ago(now - timedelta(seconds=42), now) == "42s"
    assert watch._ago(now - timedelta(minutes=14, seconds=3), now) == "14m"
    assert watch._ago(None, now) == ""
    assert str(watch.Activity("working", "in Bash 3m")) == "working, in Bash 3m" and str(watch.Activity("idle")) == "idle"
