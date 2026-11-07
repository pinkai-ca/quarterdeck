"""What a seat costs the machine: its process tree, sampled on the service's tick.

Tokens say what a seat cost the provider; this says what it cost the shared box: CPU
seconds, peak memory, the most processes at once, and the bytes written to disk, read from
the processes' own counters under /proc. The service accumulates a turn's figures while it
runs and hands them to the Ledger when the turn is ingested.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .ledger import Machine

CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


@dataclass
class Process:
    pid: int
    parent: int
    cpu_seconds: float
    rss: int
    write_bytes: int


def processes() -> dict[int, Process]:
    """Every process the user can read, with its parent, CPU seconds, resident set and bytes written."""
    found = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = Path(entry.path, "stat").read_text()
            fields = stat.rsplit(")", 1)[1].split()
            parent, utime, stime, rss_pages = int(fields[1]), int(fields[11]), int(fields[12]), int(fields[21])
            write_bytes = 0
            for line in Path(entry.path, "io").read_text().splitlines():
                if line.startswith("write_bytes:"):
                    write_bytes = int(line.split()[1])
        except (OSError, IndexError, ValueError):
            continue
        found[pid] = Process(pid, parent, (utime + stime) / CLOCK_TICKS, rss_pages * PAGE, write_bytes)
    return found


def tree(root: int, table: dict[int, Process] | None = None) -> list[Process]:
    """The root process and every descendant."""
    table = table if table is not None else processes()
    children: dict[int, list[Process]] = {}
    for process in table.values():
        children.setdefault(process.parent, []).append(process)
    found, stack = [], [root]
    while stack:
        pid = stack.pop()
        if pid in table:
            found.append(table[pid])
        stack.extend(child.pid for child in children.get(pid, []))
    return found


@dataclass
class Meter:
    """Accumulates one process tree's machine use over a turn, from repeated samples; a new process gets a new meter."""

    root: int
    first: dict[int, Process] = field(default_factory=dict)
    last: dict[int, Process] = field(default_factory=dict)
    peak_rss: int = 0
    max_processes: int = 0

    def sample(self, table: dict[int, Process] | None = None) -> None:
        seen = tree(self.root, table)
        self.peak_rss = max(self.peak_rss, sum(p.rss for p in seen))
        self.max_processes = max(self.max_processes, len(seen))
        for process in seen:
            self.first.setdefault(process.pid, process)
            self.last[process.pid] = process

    def result(self) -> Machine:
        cpu = sum(self.last[pid].cpu_seconds - self.first[pid].cpu_seconds for pid in self.last)
        written = sum(self.last[pid].write_bytes - self.first[pid].write_bytes for pid in self.last)
        return Machine(round(cpu, 2), self.peak_rss, self.max_processes, written)
