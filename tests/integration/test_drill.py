"""The all-hands drill on the fake harness: the whole control plane, end to end."""

from __future__ import annotations

import os

import pytest

from quarterdeck import drill


@pytest.fixture
def private_socket(monkeypatch):
    monkeypatch.delenv("QD_SHIP", raising=False)
    yield
    monkeypatch.delenv("QD_TMUX_SOCKET", raising=False)


def test_the_drill_passes_every_check(private_socket):
    passed, lines = drill.run(None)
    assert passed, "\n".join(lines)
    text = "\n".join(lines)
    assert "round 1: every order closed · 6 of 6" in text and "round 2: every order closed · 6 of 6" in text
    assert "service killed mid-delivery, recovered" in text
    assert "caught submitted open attempt before record acceptance" in text
    assert "in the record 1 time(s); submissions 1;" in text
    assert "desk never typed into · box ''" in text  # law: one-door
    assert "briefed with its open order" in text  # law: context-is-cache
    assert lines[-1] == f"all passed of {len(lines) - 1} checks"
    assert not os.environ.get("QD_SHIP")


def test_a_between_attempts_kill_cannot_pass_for_mid_delivery(tmp_path, monkeypatch):
    """Negative control: even a later delivery cannot replace the missing crash observation."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    ship = SimpleNamespace(runtime=tmp_path)
    captain = SimpleNamespace(name="synthetic", home=tmp_path, session=SimpleNamespace(human=False),
                              harness=SimpleNamespace(synthetic=True))
    exercise = drill.Drill(ship, Mock(), patience=0.1)
    monkeypatch.setattr(exercise, "seats", lambda: [captain])
    monkeypatch.setattr(exercise.store, "new", Mock())
    monkeypatch.setattr(drill, "wait_for", Mock(side_effect=[True, None]))
    kill = Mock(side_effect=AssertionError("no transport was caught; do not claim a crash"))
    monkeypatch.setattr(drill.os, "kill", kill)
    exercise.crash()
    assert len(exercise.checks) == 1 and not exercise.checks[0].passed
    assert "no submitted open attempt caught" in exercise.checks[0].detail
    kill.assert_not_called()
    assert not list((tmp_path / "rendezvous").iterdir())
