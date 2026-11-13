"""Ordinary startup and record compatibility of the synthetic terminal program."""

import os
from pathlib import Path
import pty
import select
import subprocess
import time
from types import SimpleNamespace

import pytest

from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.synthetic_record import ClaudeShape, CodexShape, PiShape


def launch(harness, home, shape="claude"):
    spec = SimpleNamespace(settings={"as": shape}, model="synthetic-model", effort="medium")
    harness.build_home(home, spec, None, None)
    return harness.launch(spec, None, home, home / "brief.md", None, None)


def test_each_launch_requires_its_own_startup_acknowledgment(tmp_path):
    harness = FakeHarness()
    home = tmp_path / "home"
    first = launch(harness, home)
    ready = home / "synthetic-ready"
    assert not harness.synthetic_ready(home)
    ready.write_text(first.env["QD_FAKE_STARTUP_TOKEN"])
    assert harness.synthetic_ready(home)
    second = launch(harness, home)
    assert second.env["QD_FAKE_STARTUP_TOKEN"] != first.env["QD_FAKE_STARTUP_TOKEN"]
    assert not harness.synthetic_ready(home)
    ready.write_text(second.env["QD_FAKE_STARTUP_TOKEN"])
    assert harness.synthetic_ready(home)
    assert not FakeHarness().synthetic_ready(home)


@pytest.mark.parametrize("shape", ["claude", "codex", "pi"])
def test_program_acknowledges_its_launch_after_the_first_prompt(tmp_path, shape):
    harness = FakeHarness()
    home = tmp_path / "home"
    prepared = launch(harness, home, shape)
    brief = home / "brief.md"
    brief.write_text("Synthetic startup brief.\n")
    env = {key: value for key, value in os.environ.items() if not key.startswith("QD_")}
    env.update(prepared.env)
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"), PYTHONNOUSERSITE="1")
    master, slave = pty.openpty()
    child = subprocess.Popen(prepared.argv, stdin=slave, stdout=slave, stderr=slave, env=env)
    os.close(slave)
    shown = b""
    try:
        deadline = time.monotonic() + 5
        while b"\xc2\xb7 idle" not in shown or not harness.synthetic_ready(home):
            remaining = deadline - time.monotonic()
            assert remaining > 0, "fake startup did not acknowledge its prompt"
            if select.select([master], [], [], min(remaining, 0.01))[0]:
                shown += os.read(master, 65536)
        assert b"briefed:" in shown
        assert ("fake " + shape).encode() in shown
        assert b"model synthetic-model" in shown
        os.write(master, b"\x03")
        assert child.wait(timeout=2) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(master)


@pytest.mark.parametrize("shape", [ClaudeShape, CodexShape, PiShape])
def test_lazy_readers_preserve_completed_turns_on_resume(tmp_path, shape):
    home = tmp_path / "home"
    writer = shape(home, tmp_path, "synthetic-session-zzzz", "synthetic-model")
    writer.begin("Synthetic first request.", "synthetic-model", "medium")
    writer.end("Synthetic first reply.")
    resumed = shape(home, tmp_path, "synthetic-session-zzzz", "synthetic-model")
    assert resumed.path == writer.path
    assert resumed.turns_before() == 1
    resumed.begin("Synthetic second request.", "synthetic-model", "medium")
    resumed.end("Synthetic second reply.")
    assert shape(home, tmp_path, "synthetic-session-zzzz", "synthetic-model").turns_before() == 2
