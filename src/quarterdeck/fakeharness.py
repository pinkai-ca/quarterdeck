"""qd-fakeharness: a small terminal program that behaves like each real agent program.

It shows a box that folds a long paste, a dialog, a program that queues while working and
one that does not; it takes a message, works through steps, writes the record in the
shape of the program it imitates, and replies. Lines of the message beginning ``$ `` are
run as tool calls, so a drill can ask it to do something and check that it did.

    qd-fakeharness --as claude|codex [--model M] [--effort E] [--brief FILE] [--resume ID] [--first TEXT]

Its home is ``QD_FAKE_HOME``; its own keys arrive as ``QD_FAKE_<KEY>``: QUEUES (1 or 0),
DIALOG (daybreak, unknown), STEPS, STEP_SECONDS.
"""

from __future__ import annotations

import argparse
import os
import select
import subprocess
import sys
import termios
import tty
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .fakerecord import ClaudeShape, CodexShape

RULE = "─" * 40
PASTE_START, PASTE_END = "\x1b[200~", "\x1b[201~"
DIALOGS = {
    "daybreak": ("Daybreak", "What is new in this build", "Press Esc to dismiss", "\x1b"),
    "unknown": ("Something else", "A notice nobody has seen before", "Press 1 to continue", "1"),
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Program:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.home = Path(os.environ["QD_FAKE_HOME"])
        self.queues = os.environ.get("QD_FAKE_QUEUES", "1") != "0"
        self.steps = int(os.environ.get("QD_FAKE_STEPS", "2"))
        self.step_seconds = float(os.environ.get("QD_FAKE_STEP_SECONDS", "0.3"))
        self.dialog = DIALOGS.get(os.environ.get("QD_FAKE_DIALOG", ""))
        self.model, self.effort = args.model, args.effort
        shape = ClaudeShape if args.shape == "claude" else CodexShape
        self.record = shape(self.home, Path.cwd(), args.resume or str(uuid.uuid4()), self.model)
        self.box = ""
        self.queue: list[str] = []
        self.working = False
        self.interrupted = False
        self.box_lines = 0
        self.pastes = 0

    # -- drawing -----------------------------------------------------------------------

    def say(self, line: str) -> None:
        """A transcript line: erase the box, print the line, draw the box again."""
        self.erase_box()
        sys.stdout.write(line + "\r\n")
        self.draw_box()

    def erase_box(self) -> None:
        if self.box_lines:
            sys.stdout.write(f"\x1b[{self.box_lines}A\r\x1b[J")
        self.box_lines = 0

    def draw_box(self) -> None:
        shown = self.shown_box()
        status = f"fake {self.args.shape} · model {self.model} · effort {self.effort} · {self.status()}"
        lines = [RULE, *[("> " if i == 0 else "  ") + l for i, l in enumerate(shown.split("\n"))], RULE, status]
        sys.stdout.write("\r\n".join(lines) + "\r")
        sys.stdout.flush()
        width = max(os.get_terminal_size().columns, 1) if sys.stdout.isatty() else 80
        self.box_lines = sum(max(1, (len(l) + width - 1) // width) for l in lines) - 1

    def shown_box(self) -> str:
        if "\n" in self.box.strip():
            return f"[Pasted text #{self.pastes} +{self.box.strip().count(chr(10))} lines]"
        return self.box

    def status(self) -> str:
        state = "working" if self.working else "idle"
        if self.queue:
            state += f" · queued {len(self.queue)}"
        return state

    def show_dialog(self) -> None:
        title, body, hint, _ = self.dialog
        width = max(len(body), len(hint)) + 2
        for line in (f"┌ {title} " + "─" * (width - len(title) - 2) + "┐",
                     f"│ {body.ljust(width - 1)}│", f"│ {hint.ljust(width - 1)}│", "└" + "─" * width + "┘"):
            self.say(line)

    # -- input -------------------------------------------------------------------------

    def read(self, timeout: float) -> str:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return ""
        data = os.read(sys.stdin.fileno(), 65536)
        while data.endswith(b"\x1b") or (b"\x1b[200~" in data and b"\x1b[201~" not in data):
            more, _, _ = select.select([sys.stdin], [], [], 0.05)
            if not more:
                break
            data += os.read(sys.stdin.fileno(), 65536)
        return data.decode("utf-8", "replace")

    def handle(self, data: str) -> None:
        if self.dialog:
            if data.startswith(self.dialog[3]):
                self.dialog = None
                self.erase_box()
                sys.stdout.write("\x1b[4A\r\x1b[J")  # the dialog's four lines go with it
                self.draw_box()
                self.say("· notice dismissed")
            return
        if data == "\x1b":
            self.escape()
        elif data == "\x03":
            raise SystemExit(0)
        elif data == "\x18\x13" or data == "\x18":
            self.send_now(data)
        elif data == "\x15":
            self.box = ""
            self.draw()
        elif data.startswith(PASTE_START):
            self.paste(data)
        else:
            self.type(data)

    def paste(self, data: str) -> None:
        text = data[len(PASTE_START):]
        text = text[: text.index(PASTE_END)] if PASTE_END in text else text
        if self.working and not self.queues:
            return
        self.pastes += 1
        self.box += text.replace("\r\n", "\n").replace("\r", "\n")
        self.draw()

    def type(self, data: str) -> None:
        if self.working and not self.queues:
            return
        for char in data:
            if char in "\r\n":
                self.submit()
            elif char == "\x7f":
                self.box = self.box[:-1]
            elif char >= " ":
                self.box += char
        self.draw()

    def draw(self) -> None:
        self.erase_box()
        self.draw_box()

    def escape(self) -> None:
        if self.working:
            self.interrupted = True
            self.say("· interrupted")
        else:
            self.box = ""
            self.draw()

    def send_now(self, data: str) -> None:
        if data == "\x18":
            data += self.read(0.3)
        if data == "\x18\x13" and self.working and self.box.strip():
            self.queue.insert(0, self.box)
            self.box = ""
            self.say("· sent now")

    def submit(self) -> None:
        text = self.box.strip()
        self.box = ""
        if not text:
            return
        if text.startswith("/"):
            self.command(text)
        elif self.working:
            self.queue.append(text)
            self.record.queued(text)
            self.say(f"queued: {text.splitlines()[0]}")
        else:
            self.queue.append(text)

    def command(self, text: str) -> None:
        name, _, value = text[1:].partition(" ")
        if name in ("model", "effort") and value:
            setattr(self, name, value)
            self.record.command(text, f"{name} set to {value}")
            self.say(f"· {name} set to {value}")
        else:
            self.say(f"· unknown command {text}")

    # -- turns -------------------------------------------------------------------------

    def turn(self, text: str) -> None:
        self.working, self.interrupted = True, False
        first, extra = text.splitlines()[0], text.count("\n")
        self.say(f"> {first}" + (f"  … (+{extra} lines)" if extra else ""))
        self.record.begin(text, self.model, self.effort)
        commands = [line[2:] for line in text.splitlines() if line.startswith("$ ")]
        for command in commands or [None] * self.steps:
            if self.interrupted:
                break
            self.step(command)
        reply = next((l[7:] for l in text.splitlines() if l.startswith("reply: ")), f"done: {first}")
        if self.interrupted:
            self.record.abort()
        else:
            self.say(f"⏺ {reply}")
            self.record.end(reply)
        self.working = False
        self.draw()

    def step(self, command: str | None) -> None:
        if command is None:
            self.record.tool("Read", "a file", "contents", False)
            self.wait(self.step_seconds)
            return
        self.say(f"⏺ Bash({command})")
        done = subprocess.run(command, shell=True, capture_output=True, text=True)
        output = (done.stdout + done.stderr).strip()
        self.say(f"  ⎿ {output.splitlines()[0] if output else '(no output)'}")
        self.record.tool("Bash", command, output, done.returncode != 0)
        self.wait(self.step_seconds)

    def wait(self, seconds: float) -> None:
        """Keep reading the keyboard while a step runs, as a real program does."""
        deadline = datetime.now().timestamp() + seconds
        while (remaining := deadline - datetime.now().timestamp()) > 0:
            data = self.read(remaining)
            if data:
                self.handle(data)
            if self.interrupted:
                return

    # -- main loop ---------------------------------------------------------------------

    def run(self) -> int:
        if self.args.brief:
            self.say(f"briefed: {Path(self.args.brief).stat().st_size} bytes from {self.args.brief}")
        if self.args.resume:
            self.say(f"resumed {self.args.resume} · {self.record.turns_before()} turns")
        if self.dialog:
            self.show_dialog()
        if self.args.first:
            self.queue.append(self.args.first)
        self.draw()
        while True:
            if self.queue and not self.dialog:
                self.turn(self.queue.pop(0))
                continue
            data = self.read(1.0)
            if data:
                self.handle(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qd-fakeharness", description=__doc__.split("\n\n")[0])
    parser.add_argument("--as", dest="shape", choices=("claude", "codex"), default="claude")
    parser.add_argument("--model", default="fake-1")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--brief")
    parser.add_argument("--resume")
    parser.add_argument("--first")
    args = parser.parse_args(argv)
    if "QD_FAKE_HOME" not in os.environ:
        print("qd-fakeharness: QD_FAKE_HOME is not set; it never runs in a real home", file=sys.stderr)
        return 2
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setraw(fd)
    sys.stdout.write("\x1b[?2004h")
    try:
        return Program(args).run()
    except SystemExit as stop:
        return int(stop.code or 0)
    finally:
        sys.stdout.write("\x1b[?2004l\r\n")
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


if __name__ == "__main__":
    sys.exit(main())
