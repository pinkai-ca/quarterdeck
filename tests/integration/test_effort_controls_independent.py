"""Shipless controls through the real CLI parser and both output renderers.

These tests execute no subprocesses and do not measure native capabilities.
"""

import json
import subprocess

import pytest

from quarterdeck import cli, harness, ship
from quarterdeck.harness import base


EXPECTED_KINDS = {
    "fake": "unrestricted",
    "claude": "unknown",
    "codex": "unknown",
    "grok": "unknown",
    "antigravity": "unknown",
    "pi": "levels",
    "opencode": "none",
}
PI_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh"]


@pytest.fixture
def shipless(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # Even an inherited ship setting must not hand off or read the ship.
    monkeypatch.setenv("QD_SHIP", str(tmp_path / "absent-ship-example"))

    def forbidden(*args, **kwargs):
        pytest.fail("shipless controls tried to load a ship or call a program")

    monkeypatch.setattr(cli, "handoff", forbidden)
    monkeypatch.setattr(cli, "load_ship", forbidden)
    monkeypatch.setattr(ship, "load", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(base, "program_version", forbidden)
    return tmp_path


@pytest.mark.parametrize("name", tuple(EXPECTED_KINDS))
@pytest.mark.parametrize("json_output", (False, True))
def test_shipless_capabilities_plain_and_json(shipless, capsys, name, json_output):
    argv = ["controls", "--harness", name]
    if json_output:
        argv.append("--json")
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    if json_output:
        data = json.loads(captured.out)
        assert data["harness"] == name
        facts = data["effort"]
        assert facts["kind"] == EXPECTED_KINDS[name]
        assert facts["version"] is None and facts["measured"] is False
        assert facts["levels"] == (PI_LEVELS if name == "pi" else [])
        assert data["defaults"]["model_default"] is (name not in ("pi", "opencode"))
        assert isinstance(data["controls"], list)
    else:
        levels = ", ".join(PI_LEVELS) if name == "pi" else EXPECTED_KINDS[name]
        assert f"{name}: effort {levels}" in captured.out
        assert "installed version/support unknown; no program was called" in captured.out
        assert "measured versions:" in captured.out
        if name in ("grok", "antigravity", "fake"):
            assert "program's default (not checked)" in captured.out
    assert list(shipless.iterdir()) == []


def test_shipless_matrix_covers_every_declared_adapter():
    assert set(EXPECTED_KINDS) == set(harness.NAMES)
