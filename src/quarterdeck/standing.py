"""Standing orders: the laws, the ship's orders, the cards, and the brief every seat reads.

Three tiers, each its own file: the laws are the framework's and are enforced in code; the
ship's orders are the ship's own and change at will; the cards say what a role and a
program need to know. A ship may replace any card by putting one at the same path in its
own folder. ``qd brief <seat>`` prints exactly what a seat reads at boot, and
``--sources`` says which file each part came from.
"""

from __future__ import annotations

import re
import locale
import tokenize
import sys
from dataclasses import dataclass
from pathlib import Path

from .ship import SeatSpec, Ship
from .seat_command import qd_command


def project_dir() -> Path:
    """Where the framework's own files are: the repository, when this code runs from a checkout; else the copy
    an installed qd keeps under its own prefix, which ``qd sync`` puts there beside the package."""
    source = Path(__file__).resolve().parents[2]
    if (source / "standing-orders").is_dir():
        return source
    return Path(sys.prefix) / "share" / "quarterdeck"


PROJECT_DIR = project_dir()
FRAMEWORK_DIR = PROJECT_DIR / "standing-orders"
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
    if seat.pool_choices:
        from .seat import Seat
        seat = Seat(ship, seat, None).spec  # the selected program's card, including later re-briefs
    session = ship.session_of(seat)
    parts: list[tuple[str, str, Path | None]] = []
    if seat.role == "hand":
        # A hand is told only what its task needs: its own card, the least of its program's card (how a message
        # arrives, the edge of its sandbox) and the seat lines it uses. No laws and no ship's orders: what of them a
        # hand must keep is a line of its card (law: context-is-cache).
        found = card(ship, "roles", seat.role)
        parts.append(("role card", found[0] if found else "(no role card for hand)", found[1] if found else None))
        found = card(ship, "harnesses/hand", seat.harness) or card(ship, "harnesses", seat.harness)
        parts.append(("harness card", found[0] if found else f"(no harness card for {seat.harness})",
                      found[1] if found else None))
        parts.append(("your seat", hand_seat_section(ship, seat), None))
        text = "\n\n".join(f"<!-- {name} -->\n{body}" for name, body, _ in parts)
        header = f"# Standing orders for {seat.display}: {seat.name}, hand, on {ship.name}\n\n"
        return Brief(header + text + "\n", [(name, path) for name, _, path in parts])
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


def hand_seat_section(ship: Ship, seat: SeatSpec) -> str:
    """The seat lines a hand uses: who it is, its qd command, and how to report, ask and ack."""
    qd = qd_command(ship.folder)
    return "\n".join([
        "# Your seat",
        "",
        f"You are seat `{seat.name}`, a hand, on the ship `{ship.name}`.",
        "",
        f"- Your qd command is `{qd}`. Use this absolute command for every `qd` you run.",
        f"- Report on your order once, from a file: `{qd} post report --re <id> --body-file $HOME/report.md`. "
        "Only your report closes your order.",
        f"- Ask once, from a file: `{qd} post question --re <id> --body-file $HOME/question.md`; then stop until "
        "the answer arrives in your pane.",
        f"- Say you have started, if your order asks you to: `{qd} post ack --re <id>`; an ack never closes.",
        "- A message that reaches you twice with the same id is one message: read it once and act on it once.",
    ])


def seat_section(ship: Ship, seat: SeatSpec, human_session: bool) -> str:
    inbox = f"runtime/post/inbox/{seat.name}.md"
    qd = qd_command(ship.folder)
    lines = [
        "# Your seat",
        "",
        f"You are seat `{seat.name}`, role {seat.role}, on the ship `{ship.name}`; the human is its {ship.operator}. "
        f"The ship folder is `{ship.folder}` and `qd` already knows it (QD_SHIP) and you (QD_SEAT).",
        "",
        f"- Your qd command is `{qd}`. Use this absolute command for every `qd` invocation in these orders, "
        "cards and messages, including replies and inbox watches. Shell startup files can put another `qd` "
        "on PATH; this command always names this ship's copy.",
        f"- Messages addressed to you are in `{inbox}`, open obligations first. "
        f"Only your report closes an order addressed to you: `{qd} post report --re <id> \"what was done\"`. "
        f"Say you have started one with `{qd} post ack --re <id>`; an ack claims, it never closes.",
        f"- To give an order, ask or answer: `{qd} post order --to <seat> \"...\"`, `{qd} post question --to <seat> \"...\"`, "
        f"`{qd} post answer --re <id> \"...\"`. Long bodies may come from a file: `{qd} post order --to <seat> --body-file notes.md`.",
        f"- Read a seat with `{qd} watch <seat>`, the ship with `{qd} status`; your own brief is `{qd} brief " + seat.name + "`.",
        "- A message that reaches you twice with the same id is one message: read it once and act on it once.",
    ]
    if human_session:
        lines.append(
            "- Your session belongs to the human. The ship types only its fixed waking line when a message waits, no watch is armed and your box is known empty. "
            f"Messages reach you through your watch: run `{qd_command(ship.folder, 'inbox', '--wait')}` "
            "as a command of its own in the background as the last act of every turn; "
            "it prints new messages in full when they arrive and exits, and you arm it again."
        )
    else:
        from .harness import get

        lines.append(
            "- Messages reach you typed into your pane in full, under a `qd >` header; one typed while you work is queued "
            "and read when your turn ends."
        )
        lines.append(f"- Ask through `{qd} post question`, never an interactive question tool. "
                     + get(seat.harness).human_tool_policy)
    lines.append(
        "- Where these orders and an instruction file your program loaded disagree about the ship, these orders win. "
        "The workspace's own notes are files you read when the work needs them."
    )
    return "\n".join(lines)


TAG = re.compile(r"#.*?\blaw: ([a-z][a-z-]*[a-z])")  # in a comment: a law named in prose is mentioned, not carried
def carriers(root: Path | None = None) -> dict[str, list[tuple[Path, int]]]:
    """Every ``# law: <id>`` tag in a comment of the source and the tests, by law id, with file and line.

    An installed copy has no source tree beside it: its own package is read, and no tests.
    """
    root = root or PROJECT_DIR
    folders = [root / "src", root / "tests"] if (root / "src").is_dir() else [Path(__file__).resolve().parent]
    found: dict[str, list[tuple[Path, int]]] = {}
    for folder in folders:
        for path in sorted(folder.rglob("*.py")):
            shown = path.relative_to(root) if path.is_relative_to(root) else path.relative_to(folder.parent)
            with path.open("rb") as source:
                for token in tokenize.tokenize(source.readline):
                    if token.type == tokenize.COMMENT:
                        for tag in TAG.findall(token.string):
                            found.setdefault(tag, []).append((shown, token.start[0]))
    return found


def law_reading(root: Path | None = None) -> dict:
    found = carriers(root or PROJECT_DIR)
    known = laws()
    return {"laws": [{"id": law.id, "part": law.part, "text": law.text,
                       "carriers": [{"path": path, "line": number} for path, number in found.get(law.id, [])]}
                      for law in known],
            "strays": sorted(set(found) - {law.id for law in known})}


def render_laws(data: dict) -> list[str]:
    lines = []
    for law in data["laws"]:
        lines.append(f"{law['id']:<24} {law['text']}")
        by_file: dict[Path, list[int]] = {}
        for carrier in law["carriers"]:
            by_file.setdefault(carrier["path"], []).append(carrier["line"])
        where = "   ".join(f"{path}:{', '.join(map(str, numbers))}" for path, numbers in by_file.items())
        lines.append(f"{'':<24} {where or 'carried nowhere'}")
    if data["strays"]:
        lines.append(f"tags naming no law: {', '.join(data['strays'])}")
    return lines


def laws_table(root: Path | None = None) -> list[str]:
    """The laws and their real comment-token carriers."""
    return render_laws(law_reading(root))


def render(ship: Ship, seat: SeatSpec, *, text: str | None = None) -> Path:
    folder = ship.runtime / "brief"
    path = folder / f"{seat.name}.md"
    text = brief(ship, seat).text if text is None else text
    encoding = locale.getpreferredencoding(False)
    try:
        if path.read_bytes() == text.encode(encoding):
            return path
    except FileNotFoundError:
        pass
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path
