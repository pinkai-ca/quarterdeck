"""A named drill never touches the live ship: it runs on a copy with its own sessions and runtime."""

from __future__ import annotations

import shutil
from pathlib import Path

from quarterdeck import drill, ship as shipmod


def test_a_named_ship_is_drilled_on_a_copy_with_its_own_sessions(enterprise_ship: Path):
    (enterprise_ship / "runtime" / "post" / "store").mkdir(parents=True)
    (enterprise_ship / "runtime" / "post" / "store" / "live-message.json").write_text("{}")
    live = shipmod.load(enterprise_ship)
    copy, folder = drill.temporary_copy(live)
    try:
        assert copy.folder != live.folder and copy.folder.parent == folder
        assert [s.title for s in copy.sessions.values()] == ["qd-drill Ship's Bridge", "qd-drill Operator's Office", "qd-drill Crew"]
        assert copy.pools.keys() == live.pools.keys() and copy.seats.keys() == live.seats.keys(), "the real pools and programs"
        assert not (copy.folder / "runtime").exists() and not (copy.folder / ".git").exists(), "no live state, no repository"
        assert (enterprise_ship / "runtime" / "post" / "store" / "live-message.json").exists(), "the live runtime is untouched"
    finally:
        shutil.rmtree(folder, ignore_errors=True)
