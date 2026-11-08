"""Bounded rendezvous and keyboard observations for the synthetic program only.

Controllers arm a phase before offering input; the program acknowledges that exact
phase and keeps reading keys until released. All files live in its throwaway home.
A missed rendezvous fails, rather than silently continuing after a timing delay.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def event(home: Path, kind: str, text: str) -> None:
    with (home / "synthetic-events.jsonl").open("a") as stream:
        stream.write(json.dumps({"event": kind, "text": text}) + "\n")


def events(home: Path) -> list[dict]:
    path = home / "synthetic-events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines(keepends=True) if line.endswith("\n")]


class Rendezvous:
    def __init__(self, home: Path, phase: str, contains: str, seconds: float = 30):
        self.folder = home / "rendezvous"
        self.request = self.folder / f"{phase}.json"
        self.arrived = self.folder / f"{phase}.arrived"
        self.contains, self.seconds = contains, seconds

    def __enter__(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        self.arrived.unlink(missing_ok=True)
        self.request.write_text(json.dumps({"contains": self.contains, "seconds": self.seconds}))
        return self

    def reached(self) -> dict | None:
        try:
            return json.loads(self.arrived.read_text())
        except (FileNotFoundError, ValueError):
            return None

    def release(self) -> None:
        self.request.unlink(missing_ok=True)

    def __exit__(self, *_):
        self.release()
        self.arrived.unlink(missing_ok=True)


def pause(home: Path, phase: str, text: str, wait) -> None:
    gate = Rendezvous(home, phase, "")
    try:
        request = json.loads(gate.request.read_text())
    except FileNotFoundError:
        return
    if request["contains"] not in text:
        return
    gate.arrived.write_text(json.dumps({"text": text, "pid": os.getpid(), "phase": phase}))
    deadline = time.monotonic() + request["seconds"]
    while gate.request.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"synthetic {phase} rendezvous was not released")
        wait(0.02)
