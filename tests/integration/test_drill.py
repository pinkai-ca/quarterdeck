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
    assert "desk never typed into · box ''" in text  # law: one-door
    assert "briefed with its open order" in text  # law: context-is-cache
    assert lines[-1] == f"all passed of {len(lines) - 1} checks"
    assert not os.environ.get("QD_SHIP")
