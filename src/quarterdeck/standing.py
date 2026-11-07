"""Standing orders: the laws, the ship's orders, the cards, and the brief every seat reads.

Three tiers, each its own file: the laws are the framework's and are enforced in code; the
ship's orders are the ship's own and change at will; the cards say what a role and a
program need to know. A ship may replace any card by putting one at the same path in its
own folder. ``qd brief <seat>`` prints exactly what a seat reads at boot, and
``--sources`` says which file each part came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .ship import SeatSpec, Ship

FRAMEWORK_DIR = Path(__file__).resolve().parents[2] / "standing-orders"
LAW_LINE = re.compile(r"^- `([a-z-]+)` (.+)$")
BRIEF_WORDS = 3000


@dataclass(frozen=True)
class Law:
    id: str
    part: str
    text: str


def laws(path: Path | None = None) -> list[Law]:
    """The laws of ``laws.md``: grouped by the part that enforces them."""
    found, part = [], ""
    for line in (path or FRAMEWORK_DIR / "laws.md").read_text().splitlines():
        if line.startswith("## "):
            part = line[3:].strip()
        elif match := LAW_LINE.match(line):
            found.append(Law(match.group(1), part, match.group(2).strip()))
    return found


def card(ship: Ship, kind: str, name: str) -> tuple[str, Path] | None:
    """A role or harness card: the ship's own at the same path, else the framework's."""
    for folder in (ship.folder, FRAMEWORK_DIR):
        path = folder / kind / f"{name}.md"
        if path.is_file():
            return path.read_text().strip(), path
    return None


@dataclass(frozen=True)
class Brief:
    text: str
    sources: list[tuple[str, Path | None]]

    @property
    def words(self) -> int:
        return len(self.text.split())


def brief(ship: Ship, seat: SeatSpec) -> Brief:
    """What one seat reads at boot: laws, the ship's orders, its cards, and its own seat."""
    session = ship.session_of(seat)
    parts: list[tuple[str, str, Path | None]] = []
    laws_path = FRAMEWORK_DIR / "laws.md"
    parts.append(("laws", laws_path.read_text().strip(), laws_path))
    orders = ship.folder / "orders.md"
    parts.append(("ship's orders", orders.read_text().strip() if orders.is_file() else
                  f"# Ship's orders: {ship.name}\n\n(none written yet: {orders})", orders if orders.is_file() else None))
    for kind, label, name in (("roles", "role card", seat.role), ("harnesses", "harness card", seat.harness)):
        found = card(ship, kind, name)
        parts.append((label, found[0] if found else f"(no {label} for {name})", found[1] if found else None))
    parts.append(("your seat", seat_section(ship, seat, session.human), None))
    text = "\n\n".join(f"<!-- {name} -->\n{body}" for name, body, _ in parts)
    header = f"# Standing orders for {seat.display}: {seat.name}, {seat.role}, on {ship.name}\n\n"
    return Brief(header + text + "\n", [(name, path) for name, _, path in parts])


def seat_section(ship: Ship, seat: SeatSpec, human_session: bool) -> str:
    inbox = f"runtime/post/inbox/{seat.name}.md"
    lines = [
        "# Your seat",
        "",
        f"You are seat `{seat.name}`, role {seat.role}, on the ship `{ship.name}`; the human is its {ship.operator}. "
        f"The ship folder is `{ship.folder}` and `qd` already knows it (QD_SHIP) and you (QD_SEAT).",
        "",
        f"- Messages addressed to you are in `{inbox}`, open obligations first. "
        "Only your report closes an order addressed to you: `qd post report --re <id> \"what was done\"`. "
        "Say you have started one with `qd post ack --re <id>`; an ack claims, it never closes.",
        "- To give an order, ask or answer: `qd post order --to <seat> \"...\"`, `qd post question --to <seat> \"...\"`, "
        "`qd post answer --re <id> \"...\"`. Long bodies may come from a file: `qd post order --to <seat> --body-file notes.md`.",
        "- Read a seat with `qd watch <seat>`, the ship with `qd status`; your own brief is `qd brief " + seat.name + "`.",
    ]
    if human_session:
        lines.append(
            "- Your session belongs to the human, so nothing is ever typed into your pane by the ship. "
            "Messages reach you through your watch: run `qd inbox --wait` in the background as the last act of every turn; "
            "it prints new messages in full when they arrive and exits, and you arm it again."
        )
    else:
        lines.append(
            "- Messages reach you typed into your pane in full, under a `qd >` header; one typed while you work is queued "
            "and read when your turn ends."
        )
    lines.append(
        "- Where these orders and an instruction file your program loaded disagree about the ship, these orders win. "
        "The workspace's own notes are files you read when the work needs them."
    )
    return "\n".join(lines)


TAG = re.compile(r"law: ([a-z][a-z-]*[a-z])")
PROJECT_DIR = FRAMEWORK_DIR.parent


def carriers(root: Path | None = None) -> dict[str, list[tuple[Path, int]]]:
    """Every ``law: <id>`` tag in the source and the tests, by law id, with file and line."""
    root = root or PROJECT_DIR
    found: dict[str, list[tuple[Path, int]]] = {}
    for folder in ("src", "tests"):
        for path in sorted((root / folder).rglob("*.py")):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                for tag in TAG.findall(line):
                    found.setdefault(tag, []).append((path.relative_to(root), number))
    return found


def laws_table(root: Path | None = None) -> list[str]:
    """``qd laws``: each law with the lines that carry it, in the code and in the tests."""
    root = root or PROJECT_DIR
    found = carriers(root)
    lines = []
    for law in laws():
        lines.append(f"{law.id:<24} {law.text}")
        by_file: dict[Path, list[int]] = {}
        for path, number in found.get(law.id, []):
            by_file.setdefault(path, []).append(number)
        where = "   ".join(f"{path}:{', '.join(map(str, numbers))}" for path, numbers in by_file.items())
        lines.append(f"{'':<24} {where or 'carried nowhere'}")
    strays = sorted(set(found) - {law.id for law in laws()})
    if strays:
        lines.append(f"tags naming no law: {', '.join(strays)}")
    return lines


def render(ship: Ship, seat: SeatSpec) -> Path:
    folder = ship.runtime / "brief"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{seat.name}.md"
    path.write_text(brief(ship, seat).text)
    return path
