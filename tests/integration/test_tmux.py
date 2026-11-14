"""The tmux module on a private server: sessions, windows, panes, paste, keys, status."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarterdeck.tmux import HISTORY_LINES, Tmux

from .waiting import wait_for


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
    current, captured = tmux.read_pane(pane.id)
    assert current.pid == pane.pid and current.width == tmux.pane(pane.id).width
    assert "line two" in captured and tmux.read_pane("%999") is None
    assert tmux.clients_attached(["Crew"]) is False


@pytest.mark.parametrize("exit_code", [3, 7])
def test_a_failed_program_leaves_its_pane_with_the_exit_status(tmux: Tmux, tmp_path: Path, exit_code):
    tmux.new_session("Crew")
    pane = tmux.new_window("Crew", "worker-1", tmp_path, ["sh", "-c", f"exit {exit_code}"])
    dead = wait_for(lambda: found if (found := dead_pane(tmux, pane.id))
                    and found.exit_status is not None else None)
    assert dead.exit_status == exit_code
    import time
    assert dead.dead_time is not None and abs(time.time() - dead.dead_time) < 60, "tmux's own death time, read with the pane"
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


def test_temporary_session_cleanup_uses_identity_when_a_name_is_reused(tmux: Tmux):
    tmux.new_session("Anchor")
    old = tmux.new_session("Temporary")
    tmux.kill_session("Temporary")
    current = tmux.new_session("Temporary")
    assert old != current
    tmux.kill_session_id(old)
    assert tmux.has_session("Temporary"), "old ownership cannot kill a replacement session"
    tmux.kill_session_id(current)
    assert not tmux.has_session("Temporary")


def test_failed_session_setup_cleans_only_the_session_it_created(tmux: Tmux, monkeypatch):
    from quarterdeck.tmux import TmuxError
    import pytest

    tmux.new_session("Existing")
    run = tmux._invoke

    def fail_option(*args, **kwargs):
        if args[0] == "set-option":
            raise TmuxError("synthetic configuration failure")
        return run(*args, **kwargs)

    monkeypatch.setattr(tmux, "_invoke", fail_option)
    with pytest.raises(TmuxError, match="synthetic configuration failure"):
        tmux.new_session("Temporary")
    assert tmux.sessions() == ["Existing"]


def test_reused_ids_on_another_server_do_not_transfer_ownership(tmux: Tmux):
    other = tmux.private_peer()
    try:
        old = tmux.new_session("Temporary")
        current = other.new_session("Temporary")
        assert old.id == current.id and old.token != current.token
        other.kill_session_id(old)
        assert other.has_session("Temporary")
        other.kill_session_id(current)
        assert not other.has_session("Temporary")
    finally:
        assert other.close(), other.held


def test_cleanup_checks_ownership_in_the_server_that_performs_the_kill(tmux: Tmux, monkeypatch):
    other = tmux.private_peer()
    try:
        old = tmux.new_session("Temporary")
        current = other.new_session("Temporary")
        assert old.id == current.id and old.token != current.token
        run = tmux._invoke
        def switch_server(*args, **kwargs):
            # The earlier existence checks saw the owned session. The action reaches a replacement server.
            if args[0] == "if-shell":
                return other._invoke(*args, **kwargs)
            return run(*args, **kwargs)
        monkeypatch.setattr(tmux, "_invoke", switch_server)
        tmux.kill_session_id(old)
        assert other.has_session("Temporary")
        other.kill_session_id(current)
        assert not other.has_session("Temporary")
    finally:
        assert other.close(), other.held


def test_control_frame_reads_the_cursor_cell_in_the_same_bracket(tmux: Tmux, tmp_path: Path):
    """The cursor's cell, 0-based (column, row), comes with the frame and its visibility, from one queue."""
    tmux.new_session("Crew")
    script = "printf 'top\\n  prompt'; printf '\\033[3;7H'; sleep 30"
    pane = tmux.new_window("Crew", "worker-1", tmp_path, ["sh", "-c", script])
    frame = wait_for(lambda: found if (found := tmux.read_control_frame(pane.id))
                     and found.cursor == (6, 2) else None)
    assert frame.pane.pid == pane.pid and frame.cursor_visible is True
    assert "prompt" in frame.text
