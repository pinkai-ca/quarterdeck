"""The ship file: reading, the line editor, the commit on set, the fence."""

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path

import pytest

from quarterdeck import ship as shipmod
from quarterdeck.ship import ShipError, edit, literal

from tests.conftest import FIXTURES, git

DESIGN_EXAMPLE = (FIXTURES / "ships" / "enterprise.toml").read_text()


def test_loads_the_design_example(enterprise_ship: Path):
    ship = shipmod.load(enterprise_ship)
    assert ship.name == "enterprise" and ship.operator == "Admiral"
    assert ship.workspace == Path("~/dev/quarterdeck").expanduser()
    assert ship.pools["codex"].config_dir == Path("~/.codex").expanduser()
    assert ship.pools["anthropic-api"].kind == "key"
    assert ship.pools["anthropic-api"].budget == "200 USD/month"
    assert ship.pools["claude-primary"].kind == "subscription"
    assert ship.sessions["office"].human and not ship.sessions["bridge"].human
    desk = ship.seat("desk")
    assert desk.pools == ("claude-secondary", "claude-primary")
    assert desk.cwd == Path("~/dev").expanduser()
    assert desk.remote_control and not ship.seat("pike").remote_control
    assert ship.seat("pike").pools == ("claude-secondary",)
    worker = ship.seat("worker-1")
    assert not worker.up and worker.fresh == "after-order" and worker.persona is None
    assert worker.display == "worker-1" and desk.display == "Maeve"
    assert worker.settings == {"model_auto_compact_token_limit": 200000}


def test_the_drydock_loads_and_runs_anywhere(drydock: Path):
    ship = shipmod.load(drydock)
    assert ship.workspace == drydock
    assert ship.pools["fake"].kind == "subscription" and ship.pools["fake"].config_dir is None
    assert {s.session for s in ship.seats.values()} == {"bridge", "office", "crew"}
    assert ship.seat("captain").persona == "Picard" and ship.seat("desk").persona == "Maeve"


def test_the_default_ship_is_the_smallest_crew_on_real_programs():
    folder = FIXTURES.parent.parent / "ships" / "default"
    ship = shipmod.load(folder)
    assert ship.workspace == folder.parent.parent, "the crew works on the project folder the ship sits in"
    assert [(s.name, s.role, s.harness, s.persona) for s in ship.seats.values()] == [
        ("desk", "desk", "claude", "Maeve"), ("captain", "captain", "codex", "Picard"), ("engineering", "engineering", "codex", "Scott")]
    assert {p.provider for p in ship.pools.values()} == {"claude", "codex"}
    assert ship.session_of(ship.seat("desk")).human and not ship.session_of(ship.seat("captain")).human
    assert "the desk lands it" in (folder / "orders.md").read_text()


def test_refusals_name_what_lifts_them(tmp_path: Path):
    (tmp_path / "ship.toml").write_text(
        '[sessions.bridge]\ntitle = "B"\n[seats.a]\nsession = "nowhere"\nharness = "fake"\n'
    )
    with pytest.raises(ShipError, match="session 'nowhere' is not declared"):
        shipmod.load(tmp_path)
    with pytest.raises(ShipError, match="holds no ship.toml"):
        shipmod.load(tmp_path / "missing")
    with pytest.raises(ShipError, match="no seat 'nobody'"):
        shipmod.load(FIXTURES.parent.parent / "ships" / "drydock").seat("nobody")


def test_find_takes_the_argument_then_the_environment(drydock: Path, monkeypatch):
    monkeypatch.delenv("QD_SHIP", raising=False)
    with pytest.raises(ShipError, match="--ship"):
        shipmod.find(None)
    monkeypatch.setenv("QD_SHIP", str(drydock))
    assert shipmod.find(None) == drydock
    assert shipmod.find(str(drydock)) == drydock


# -- the line editor -------------------------------------------------------------------


def changed_lines(before: str, after: str) -> list[str]:
    return [l for l in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0, lineterm="")
            if l[:1] in "+-" and not l.startswith(("+++", "---"))]


def test_edit_changes_one_line_and_keeps_every_other():
    after = edit(DESIGN_EXAMPLE, "seats.pike", "model", "claude-fable-5-1")
    assert changed_lines(DESIGN_EXAMPLE, after) == [
        '-model = "claude-opus-5-5"',
        '+model = "claude-fable-5-1"',
    ]
    # the second seat with the same key is untouched: the edit is scoped to its table
    assert after.count('model = "gpt-6-astra"') == 2


def test_edit_keeps_the_comment_on_the_edited_line():
    after = edit(DESIGN_EXAMPLE, "seats.desk", "pool", ["claude-primary"])
    assert 'pool = ["claude-primary"]   # starts on the first; moves on at a wall and stays' in after
    after = edit(DESIGN_EXAMPLE, "seats.worker-1", "up", True)
    assert "up = true                           # declared, brought up on demand" in after


def test_edit_adds_a_missing_key_at_the_end_of_its_table():
    after = edit(DESIGN_EXAMPLE, "seats.scott", "cwd", "~/dev/other")
    lines = after.splitlines()
    index = lines.index('cwd = "~/dev/other"')
    assert lines[index - 1] == 'effort = "xhigh"'
    assert lines[index + 1] == "" and lines[index + 2] == "[seats.desk]"


def test_edit_adds_a_missing_table_at_the_end():
    after = edit(DESIGN_EXAMPLE, "seats.scott.settings", "model_reasoning_summary", "concise")
    assert after.endswith('[seats.scott.settings]\nmodel_reasoning_summary = "concise"\n')
    assert after.startswith(DESIGN_EXAMPLE)


def test_edit_round_trips_through_the_reader(enterprise_ship: Path):
    file = enterprise_ship / "ship.toml"
    text = file.read_text()
    text = edit(text, "seats.pike", "effort", "high")
    text = edit(text, "seats.desk", "pool", ["claude-primary", "claude-secondary"])
    text = edit(text, "seats.worker-1.settings", "model_auto_compact_token_limit", 100000)
    file.write_text(text)
    ship = shipmod.load(enterprise_ship)
    assert ship.seat("pike").effort == "high"
    assert ship.seat("desk").pools == ("claude-primary", "claude-secondary")
    assert ship.seat("worker-1").settings["model_auto_compact_token_limit"] == 100000


def test_literals():
    assert literal("a") == '"a"' and literal(True) == "true" and literal(3) == "3"
    assert literal(["a", "b"]) == '["a", "b"]' and literal(Path("~/x")) == '"~/x"'


# -- set, commit, fence ----------------------------------------------------------------


def test_set_value_writes_the_file_and_commits_in_a_repository(enterprise_ship: Path):
    ship = shipmod.load(enterprise_ship)
    proof = shipmod.set_value(ship, "seats.pike", "model", "claude-fable-5-1")
    assert proof.startswith('ship.toml: seats.pike.model = "claude-fable-5-1" · committed ')
    assert shipmod.load(enterprise_ship).seat("pike").model == "claude-fable-5-1"
    assert git(enterprise_ship, "log", "--format=%s", "-1") == 'qd set seats.pike.model = "claude-fable-5-1"'
    assert git(enterprise_ship, "status", "--porcelain") == ""
    assert shipmod.set_value(ship, "seats.pike", "model", "claude-fable-5-1").endswith("already")


def test_set_value_refuses_a_declaration_it_could_not_load(enterprise_ship: Path):
    ship = shipmod.load(enterprise_ship)
    before = ship.file.read_text()
    with pytest.raises(ShipError, match="session 'nowhere' is not declared"):
        shipmod.set_value(ship, "seats.pike", "session", "nowhere")
    assert ship.file.read_text() == before and git(enterprise_ship, "status", "--porcelain") == ""


def test_set_value_in_a_folder_that_is_not_a_repository_says_so(drydock: Path):
    ship = shipmod.load(drydock)
    proof = shipmod.set_value(ship, "seats.captain", "effort", "max")
    assert "not a repository" in proof
    assert shipmod.load(drydock).seat("captain").effort == "max"


def test_fence_makes_the_runtime_a_nested_repository(enterprise_ship: Path):
    ship = shipmod.load(enterprise_ship)
    assert shipmod.fence(ship) == "runtime/ fenced: nested repository created"
    assert (ship.runtime / ".git").is_dir()
    assert "runtime/" in (enterprise_ship / ".gitignore").read_text().splitlines()
    assert git(enterprise_ship, "status", "--porcelain") == ""
    assert shipmod.fence(ship) == "runtime/ fenced"
    # git run in the ship folder leaves the state alone  # law: one-folder
    (ship.runtime / "post").mkdir()
    (ship.runtime / "post" / "message.json").write_text("{}")
    git(enterprise_ship, "clean", "-fdx")
    git(enterprise_ship, "reset", "--hard", "--quiet")
    assert (ship.runtime / "post" / "message.json").exists()


def test_fence_refuses_a_tracked_runtime(enterprise_ship: Path):
    (enterprise_ship / "runtime").mkdir()
    (enterprise_ship / "runtime" / "state").write_text("x")
    git(enterprise_ship, "add", "runtime")
    git(enterprise_ship, "commit", "--quiet", "-m", "track by mistake")
    with pytest.raises(ShipError, match="rm -r --cached runtime"):
        shipmod.fence(shipmod.load(enterprise_ship))


def test_fence_in_a_folder_that_is_not_a_repository(drydock: Path):
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    assert (ship.runtime / ".git").is_dir()
    assert subprocess.run(["git", "-C", str(ship.runtime), "rev-parse", "HEAD"], capture_output=True).returncode
