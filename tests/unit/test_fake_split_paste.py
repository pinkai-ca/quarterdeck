"""A bracketed paste that reaches the synthetic program in pieces is still taken as one message.

A busy machine can deliver a paste across several reads, with a pause between them longer than any
fixed wait. A terminal program reads the paste as one whatever the reads; so does the simulator, which
stands in for one in the drill and the tests.
"""

import os
from pathlib import Path
import pty
import select
import subprocess
import time
from types import SimpleNamespace

import pytest

from quarterdeck.harness.fake import FakeHarness

START, END = b"\x1b[200~", b"\x1b[201~"
MESSAGE = ("qd > order from tester · ord-0000 · a synthetic subject\n"
           "first line of a synthetic body\n"
           "second line of a synthetic body")
PAUSE = 1.0  # far past the simulator's 0.05 s wait for a lone ESC, its only timed wait on the way


class Program:
    """The synthetic program in a pty of the test's own: no tmux, no ship."""

    def __init__(self, tmp_path: Path, step_seconds: str = "0.01"):
        self.harness = FakeHarness()
        self.home = tmp_path / "home"
        self.shown = b""
        spec = SimpleNamespace(name="synthetic-seat", settings={"as": "claude", "step_seconds": step_seconds},
                               model="synthetic-model", effort="medium", model_mode=None, effort_mode=None)
        self.harness.build_home(self.home, spec, None, None)
        prepared = self.harness.launch(spec, None, self.home, self.home / "brief.md", None, None)
        (self.home / "brief.md").write_text("Synthetic brief.\n")
        env = {key: value for key, value in os.environ.items() if not key.startswith("QD_")}
        env.update(prepared.env)
        env.update(PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"), PYTHONNOUSERSITE="1")
        self.master, slave = pty.openpty()
        self.child = subprocess.Popen(prepared.argv, stdin=slave, stdout=slave, stderr=slave, env=env,
                                      cwd=self.home)
        os.close(slave)
        self.wait(lambda: self.harness.synthetic_ready(self.home), "the program never came ready")

    def drain(self) -> None:
        while select.select([self.master], [], [], 0)[0]:
            try:
                self.shown += os.read(self.master, 65536)
            except OSError:
                return

    def wait(self, condition, what: str, seconds: float = 10.0):
        deadline = time.monotonic() + seconds
        while not (found := condition()):
            assert time.monotonic() < deadline, what
            self.drain()
            time.sleep(0.01)
        return found

    def write(self, *pieces: bytes) -> None:
        """Each piece written alone, with the pause between them."""
        for index, piece in enumerate(pieces):
            if index:
                time.sleep(PAUSE)
            os.write(self.master, piece)

    def inputs(self) -> list[str]:
        records = self.harness.records(self.home)
        return [entry.text for record in records for entry in self.harness.inputs(record)]

    def close(self) -> None:
        try:
            os.write(self.master, b"\x03")
            self.child.wait(timeout=5)
        finally:
            if self.child.poll() is None:
                self.child.kill()
                self.child.wait()
            os.close(self.master)


@pytest.fixture
def program(tmp_path):
    started = Program(tmp_path)
    try:
        yield started
    finally:
        started.close()


def paste(text: str) -> bytes:
    return START + text.encode() + END


def taken(program: Program) -> list[str]:
    return program.wait(lambda: [text for text in program.inputs() if "synthetic" in text],
                        "the message was never taken")


def settled(program: Program) -> list[str]:
    """What the record holds once nothing more arrives for a while."""
    found = taken(program)
    time.sleep(PAUSE)
    program.drain()
    return [text for text in program.inputs() if "synthetic" in text] or found


def test_a_whole_paste_is_taken_once_as_it_was(program):
    program.write(paste(MESSAGE) + b"\r")
    assert settled(program) == [MESSAGE]


CUTS = {
    'before its first newline': lambda data: data.index(b"\n") - 5,
    'at its first newline': lambda data: data.index(b"\n"),
    'inside its end marker': lambda data: len(data) - 3,
    'between its end marker and its body': lambda data: len(data) - len(END),
    'right after its ESC': lambda data: 1,
    'after its ESC and bracket': lambda data: 2,
    'inside its start marker': lambda data: 3,
    'inside a two-byte character': lambda data: data.index("·".encode()) + 1,
}


@pytest.mark.parametrize("cut", CUTS.values(), ids=CUTS)
def test_a_paste_split_by_a_long_pause_is_taken_once_with_its_exact_text(program, cut):
    data = paste(MESSAGE)
    at = cut(data)
    assert 0 < at < len(data)
    program.write(data[:at], data[at:] + b"\r")
    assert settled(program) == [MESSAGE]


def test_a_paste_in_three_pieces_and_its_enter_alone_is_one_message(program):
    data = paste(MESSAGE)
    first, second = data.index(b"\n"), data.rindex(b"\n")
    program.write(data[:first], data[first:second], data[second:], b"\r")
    assert settled(program) == [MESSAGE]


def test_a_lone_esc_with_nothing_after_it_is_still_the_key(tmp_path):
    """The door's interrupt is one ESC that nothing follows: it must act alone, so a lone ESC cannot wait
    for a later byte. Only the rest of a paste start marker after it is taken back into the paste."""
    program = Program(tmp_path, step_seconds="30")
    try:
        program.write(paste("a synthetic turn that works for a while") + b"\r")
        program.wait(lambda: b"working" in program.shown, "the turn never started")
        program.write(b"\x1b")
        program.wait(lambda: "interrupted" in program.shown.decode("utf-8", "replace"),
                     "a lone ESC was never acted on")
    finally:
        program.close()
