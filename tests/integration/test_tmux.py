"""The tmux module on a private server: sessions, windows, panes, paste, keys, status."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from quarterdeck.tmux import HISTORY_LINES, Tmux


def wait_for(predicate, seconds: float = 5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def screen_showing(tmux: Tmux, pane_id: str, text: str) -> str | None:
    screen = tmux.capture(pane_id)
    return screen if text in screen else None


def dead_pane(tmux: Tmux, pane_id: str):
    pane = tmux.pane(pane_id)
    return pane if pane and pane.dead else None


def test_session_opens_with_the_ship_history_and_environment(tmux: Tmux, tmp_path: Path):
    assert not tmux.running() and tmux.sessions() == []
    tmux.new_session("Ship's Bridge", env={"QD_SHIP": str(tmp_path)})
    assert tmux.has_session("Ship's Bridge")
    assert tmux.run("show-option", "-v", "-t", "=Ship's Bridge:", "history-limit").strip() == str(HISTORY_LINES)
    pane = tmux.new_window("Ship's Bridge", "Pike", tmp_path, ["cat"], env={"SEAT": "pike"})
    assert pane.id.startswith("%") and pane.session == "Ship's Bridge" and pane.window == "Pike"
    assert [p.window for p in tmux.panes("Ship's Bridge")] == ["Pike"], "the placeholder window is gone"
    assert tmux.session_environment("Ship's Bridge", "QD_SHIP") == str(tmp_path)
    assert tmux.run("show-option", "-v", "-w", "-t", pane.id, "remain-on-exit").strip() == "failed"


def test_paste_reaches_the_program_and_the_screen_reads_back(tmux: Tmux, tmp_path: Path):
    tmux.new_session("Crew")
    pane = tmux.new_window("Crew", "worker-1", tmp_path, ["cat"])
    tmux.paste(pane.id, "qd > order from desk · ord-7f3a · a subject\nline two\n")
    tmux.keys(pane.id, "Enter")
    screen = wait_for(lambda: screen_showing(tmux, pane.id, "line two"))
    assert "qd > order from desk · ord-7f3a · a subject" in screen
    assert tmux.pane(pane.id) is not None and tmux.pane("%999") is None


@pytest.mark.parametrize("exit_code", [3, 7])
def test_a_failed_program_leaves_its_pane_with_the_exit_status(tmux: Tmux, tmp_path: Path, exit_code):
    tmux.new_session("Crew")
    pane = tmux.new_window("Crew", "worker-1", tmp_path, ["sh", "-c", f"exit {exit_code}"])
    dead = wait_for(lambda: dead_pane(tmux, pane.id))
    assert dead.exit_status == exit_code
    assert dead.id == pane.id and dead.pid == pane.pid and pane.pid > 0
    assert dead.window == "worker-1" and dead.session == "Crew"
    tmux.kill_window(pane.id)
    assert tmux.pane(pane.id) is None


def test_status_line_is_refreshed_by_tmux_itself(tmux: Tmux, tmp_path: Path):
    tmux.new_session("Bridge")
    tmux.status_line("Bridge", "echo service ok", interval=1)
    assert tmux.run("show-option", "-v", "-t", "=Bridge:", "status-right").strip() == "#(echo service ok)"
    assert tmux.run("show-option", "-v", "-t", "=Bridge:", "status-interval").strip() == "1"


def test_prepared_pane_preserves_environment_cwd_pid_and_other_window_options(tmux: Tmux, tmp_path: Path):
    tmux.new_session("Crew")
    global_before = tmux.run("show-option", "-gwv", "remain-on-exit")
    anchor = tmux.new_window("Crew", "anchor", tmp_path, ["cat"])
    tmux.run("set-option", "-w", "-t", anchor.id, "remain-on-exit", "off")
    pane = tmux.new_window("Crew", "worker", tmp_path,
                           ["sh", "-c", 'printf "%s\\n" "$$" "$SEAT_VALUE" "$PWD"; exec cat'],
                           env={"SEAT_VALUE": "synthetic value"})
    shown = wait_for(lambda: screen_showing(tmux, pane.id, "synthetic value"))
    assert str(pane.pid) in shown.splitlines()
    joined = tmux.run("capture-pane", "-pJ", "-t", pane.id)
    assert str(tmp_path) in joined.splitlines(), "cwd survives even when its screen line wraps"
    assert tmux.pane(pane.id).pid == pane.pid
    assert tmux.run("show-option", "-wv", "-t", pane.id, "remain-on-exit").strip() == "failed"
    assert tmux.run("show-option", "-wv", "-t", anchor.id, "remain-on-exit").strip() == "off"
    assert tmux.run("show-option", "-gwv", "remain-on-exit") == global_before
    tmux.paste(pane.id, "still interactive")
    tmux.keys(pane.id, "Enter")
    wait_for(lambda: screen_showing(tmux, pane.id, "still interactive"))
    assert all(p.window != "qd" for p in tmux.panes("Crew"))


def test_an_immediately_successful_program_is_not_retained(tmux: Tmux, tmp_path: Path):
    tmux.new_session("Crew")
    anchor = tmux.new_window("Crew", "anchor", tmp_path, ["cat"])
    pane = tmux.new_window("Crew", "success", tmp_path, ["sh", "-c", "exit 0"])
    assert pane.pid > 0 and pane.id != anchor.id
    wait_for(lambda: tmux.pane(pane.id) is None)
    assert tmux.pane(anchor.id) is not None
