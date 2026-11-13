"""Ordinary test-tool environment isolation, with synthetic caller values."""
import json

from tests.testrunner import environment


def test_runner_removes_parent_tmux_connection_and_pane(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUX", "/synthetic/parent.sock,123,0")
    monkeypatch.setenv("TMUX_PANE", "%123")
    monkeypatch.setenv("EXAMPLE_TEST_ENVIRONMENT", "preserved")
    child, record = environment.build(tmp_path / "scratch", "synthetic-run")
    assert "TMUX" not in child
    assert "TMUX_PANE" not in child
    assert {"TMUX", "TMUX_PANE"} <= set(record["removed_names"])
    assert "/synthetic/parent.sock" not in json.dumps(record)
    assert "%123" not in json.dumps(record)
    assert child["EXAMPLE_TEST_ENVIRONMENT"] == "preserved"
