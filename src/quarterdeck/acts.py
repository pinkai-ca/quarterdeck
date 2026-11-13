"""Systems' two acts on a screen the door does not know: qd press and qd type (law: one-door).

The door itself never guesses. Where it holds a seat at a screen it does not know, a systems seat may answer
it, visibly, through this same path, in the service: press a named choice of a dialog as the screen shows it,
or type one line into a stalled seat's empty box. Only a caller whose seat the ship file declares with the role
``systems`` may act, and never into a seat in a session marked human, whoever asks. Each act is written to
``runtime/seats/<seat>/acts.jsonl`` before a key is sent, with the screen as shown (bounded), the choice or
line, the caller and the time, then once more with its outcome; nothing is sent when that record cannot be
written. ``qd watch`` shows the last act.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import durable
from .ship import Ship
from .tmux import TmuxError

SCREEN_LINES = 60  # the screen as shown, bounded: its last lines, as many characters at most
SCREEN_CHARS = 6000
LINE_CHARS = 2000
CONFIRM_SECONDS = 3.0  # the record's word on a typed line, within the caller's wait for the service's answer
# A choice's own label, right before its text on the screen, names the key that chooses it: "[0] Skip", "1. Yes",
# "(2) No", "3) Later". Only these forms, so no key is guessed from free text.
LABEL = re.compile(r"(?:\[([0-9A-Za-z])\]|\(([0-9A-Za-z])\)|(?<![0-9A-Za-z])([0-9])[.)])\s+$")
OWN_LABEL = re.compile(r"^\s*(?:\[([0-9A-Za-z])\]|\(([0-9A-Za-z])\)|([0-9])[.)])\s+\S")


# Security's guard (adopted by the captain): the qd path itself refuses, by the screen's measured form and the
# choice's own text, any act that grants a permission, trusts a path or folder, widens a sandbox, allows a command
# always, or answers an account holder's or a terms question; such a screen is the desk's, never a systems seat's.
# A screen it cannot classify is refused too. The words are matched broadly: a refusal sends a screen to the desk,
# a miss would press it.
KINDS = (
    ("grants a permission", re.compile(
        r"\b(allow|allowed|approve|approval|grant|permit|permission|permissions|authori[sz]e|proceed|auto[- ]?mode|"
        r"accept edits|bypass)\b", re.I)),
    ("trusts a path or folder", re.compile(r"\b(trust|trusted|trusts|trusting)\b", re.I)),
    ("widens a sandbox", re.compile(
        r"sandbox|full[- ]access|unrestricted|danger|yolo|network access|outside (the|this) (workspace|project|folder)|"
        r"workspace[- ]write|read[- ]only|--dangerously", re.I)),
    ("allows a command always", re.compile(
        r"\balways\b|don'?t ask again|do not ask again|never ask|for (the rest of )?(this|the) session|"
        r"auto[- ]?(approve|accept)|remember (this|my) (choice|decision|answer)", re.I)),
    ("answers an account holder's or terms question", re.compile(
        r"\b(terms|privacy|policy|policies|agree|agreement|consent|license|licence|training|account|sign[- ]?in|"
        r"log[- ]?in|login|logout|authenticate|subscription|subscribe|billing|payment|upgrade|survey|feedback|"
        r"rating|opt[- ]?(in|out)|telemetry|share (my |your |usage )?data|improve (the |our )?(model|product)s?)\b", re.I)),
)
REGION_LINES = 15  # the dialog: the lines above its choice (or above the box) and everything below


def _kind(text: str) -> tuple[str, str] | None:
    for kind, pattern in KINDS:
        if found := pattern.search(text):
            return kind, found.group(0)
    return None


def guard(screen, choice: str | None = None, line: str | None = None) -> str | None:
    """Why this act is the desk's and not a systems seat's, or None: by the screen's measured form, then the
    choice's own text or the typed line, then the dialog's words. A screen that cannot be classified is refused."""
    from .harness.types import ControlInterruption, _cells
    known = screen.interruption
    if known is not None:
        if isinstance(known, ControlInterruption):
            return f"its screen is the measured {known.name!r}, which only its own control request answers"
        if known.click is not None:
            return f"its screen is the measured {known.name!r}, an account holder's question whose answer the orders fix"
        if found := _kind(known.name):
            return f"its screen is the measured {known.name!r}, which {found[0]}"
    own = choice if choice is not None else line
    if own is not None and (found := _kind(own)):
        return f"the {'choice' if choice is not None else 'line'} {own!r} {found[0]} ({found[1]!r})"
    rows = screen.text.rstrip("\n").split("\n")
    at = next((y for y, row in enumerate(rows) if choice and choice in row), None) if choice else None
    if at is None:
        at = next((y for y in range(len(rows) - 1, -1, -1) if rows[y].strip()), 0)
    region = rows[max(0, at - REGION_LINES):]
    if not any(row.strip() for row in region) or any(_cells(row) is None for row in region):
        return "its screen cannot be classified (blank, or characters the reader cannot measure)"
    if found := _kind("\n".join(region)):
        return f"its screen reads as a question that {found[0]} (it says {found[1]!r})"
    return None


def path(seat) -> Path:
    return seat.folder / "acts.jsonl"


def authority(ship: Ship, caller: str | None, seat) -> str | None:
    """Why ``caller`` may not act on ``seat``, or None: only a systems seat, never in a human's session."""
    if seat.session.human:
        return f"refused: {seat.name} sits in a human's session; nothing is pressed or typed there, whoever asks"
    if not caller:
        return "refused: only a systems seat acts on a screen, and QD_SEAT names no seat here"
    spec = ship.seats.get(caller)
    if spec is None:
        return f"refused: {caller} is not a seat of {ship.name}; only a systems seat acts on a screen"
    if spec.role != "systems":
        return f"refused: {caller} is a {spec.role} seat; only a systems seat acts on a screen"
    return None


def bounded(text: str) -> str:
    lines = text.rstrip("\n").split("\n")[-SCREEN_LINES:]
    shown = "\n".join(lines)
    return shown[-SCREEN_CHARS:]


def write(seat, entry: dict) -> None:
    """One line appended and made durable; raises OSError when it cannot be, so nothing is sent."""
    target = path(seat)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with target.open("a", encoding="utf-8") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())
    durable.sync_folder(target.parent)


def last(seat) -> dict | None:
    """The seat's last act, with its outcome when one was written; None when there is none or it is unreadable."""
    try:
        rows = path(seat).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    acts: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        try:
            entry = json.loads(row)
            key = entry["act"]
        except (ValueError, KeyError, TypeError):
            continue
        if key not in acts:
            order.append(key)
            acts[key] = dict(entry)
        else:
            acts[key].update(outcome=entry.get("outcome"), detail=entry.get("detail"), done=entry.get("at"))
    return acts[order[-1]] if order else None


def describe(act: dict) -> str:
    what = f"pressed {act.get('choice')!r}" if act.get("verb") == "press" else f"typed {act.get('line')!r}"
    text = f"last act: {what} by {act.get('caller')} at {act.get('at')} · {act.get('outcome')}"
    return text + (f" ({act['detail']})" if act.get("detail") else "")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _frame(door):
    """The pane and the screen of the seat's own launch, read once."""
    found = door.seat.read_pane()
    if found is None:
        return None, None
    return found[0], door.harness.screen(found[1])


def _settled(door, screen):
    """The same frame read again a moment later, or None: an act is made only on a screen that stands still."""
    from .door import SETTLE_SECONDS
    time.sleep(SETTLE_SECONDS)
    pane, again = _frame(door)
    if again is None or again.text != screen.text:
        return None, None
    return pane, again


def label_key(text: str, choice: str) -> str | None:
    """The key the choice's own label names, read from its row on the screen, or from the choice as named."""
    own = OWN_LABEL.match(choice)
    if own:
        return next(g for g in own.groups() if g)
    row = next((r for r in text.split("\n") if choice in r), "")
    before = LABEL.search(row[:row.index(choice)]) if choice in row else None
    return next(g for g in before.groups() if g) if before else None


def press(service, seat, choice: str, caller: str | None) -> str:
    """qd press: a named choice of the dialog on the screen, recorded before it is pressed."""
    from .door import ANSWER_SECONDS, SETTLE_SECONDS, Door, choice_cell
    if reason := authority(service.ship, caller, seat):
        return reason
    if not choice or not choice.strip() or "\n" in choice:
        return "refused: name one choice as the screen shows it, on one line"
    launched = seat.launched()
    door = Door(seat)
    pane, screen = _frame(door)
    if launched is None or pane is None or screen is None:
        return f"refused: {seat.name} is not up; nothing is pressed"
    count = screen.text.count(choice)
    if count == 0:
        return f"refused: no visible choice on {seat.name}'s screen is named {choice!r}; nothing is pressed"
    cell = choice_cell(screen.text, choice)
    if count > 1:
        return f"refused: {choice!r} shows {count} times on {seat.name}'s screen, not once; nothing is pressed"
    if cell is None:
        return f"refused: the place of {choice!r} on {seat.name}'s screen cannot be measured; nothing is pressed"
    if screen.box is not None and screen.box.strip():
        return f"refused: {seat.name}'s box holds text; a key pressed now could submit it; nothing is pressed"
    if reason := guard(screen, choice=choice):
        return f"refused: {reason}; that is the desk's to answer, never a systems seat's; nothing is pressed"
    key = label_key(screen.text, choice) if screen.box is None else None  # a key into a drawn box would be typed
    how = None
    if key is not None:
        how = ("key", key)
    else:
        modes = door.tmux.mouse_modes(pane.id, pane.pid) if hasattr(door.tmux, "mouse_modes") else None
        if modes and modes.get("sgr") and (modes.get("button") or modes.get("all") or modes.get("standard")):
            how = ("click", cell)
    if how is None:
        return (f"refused: {choice!r} names no key of its own and {seat.name}'s program takes no mouse; "
                "nothing is pressed")
    pane, screen = _settled(door, screen)
    if screen is None:
        return f"refused: {seat.name}'s screen changed while it was read; nothing is pressed"
    act = dict(act=uuid.uuid4().hex[:12], verb="press", at=_now(), caller=caller, seat=seat.name, choice=choice,
               by=f"key {how[1]}" if how[0] == "key" else f"click at {how[1][0]},{how[1][1]}",
               launch=dict(pane=launched.pane, pid=launched.pid, at=launched.at), screen=bounded(screen.text),
               outcome="pressing")
    try:
        write(seat, act)
    except OSError as error:
        return f"refused: the act could not be recorded ({error}); nothing is pressed"  # law: one-door
    try:
        if how[0] == "key":
            door.tmux.keys(pane.id, how[1])
        else:
            door.tmux.click(pane.id, *how[1])
    except TmuxError as error:
        return _finish(seat, act, "not pressed", str(error))
    gone = False
    deadline = time.monotonic() + ANSWER_SECONDS
    while time.monotonic() < deadline and not gone:
        time.sleep(SETTLE_SECONDS)
        _, after = _frame(door)
        gone = after is None or choice not in after.text
    outcome = "the choice is gone from the screen" if gone else "the choice still shows"
    return _finish(seat, act, "pressed", outcome)


def type_line(service, seat, line: str, caller: str | None) -> str:
    """qd type: one line into a stalled seat's empty box, submitted once, recorded before it is typed."""
    from .door import BOX_SECONDS, Door
    if reason := authority(service.ship, caller, seat):
        return reason
    if not line or not line.strip() or len(line) > LINE_CHARS:
        return f"refused: give one line of text, at most {LINE_CHARS} characters"
    if any(ord(c) < 32 or ord(c) == 127 for c in line):
        return "refused: one line only, with no newline or terminal control character"
    launched = seat.launched()
    door = Door(seat)
    pane, screen = _frame(door)
    if launched is None or pane is None or screen is None:
        return f"refused: {seat.name} is not up; nothing is typed"
    if screen.box is None:
        return f"refused: {seat.name}'s box is not on the screen; nothing is typed"
    if screen.box.strip():
        shown = screen.box if len(screen.box) <= 80 else screen.box[:77] + "..."
        return f"refused: {seat.name}'s box is not empty, it shows {shown!r}; nothing is typed"
    if reason := guard(screen, line=line):
        return f"refused: {reason}; that is the desk's to answer, never a systems seat's; nothing is typed"
    pane, screen = _settled(door, screen)
    if screen is None or screen.box is None or screen.box.strip():
        return f"refused: {seat.name}'s screen changed while it was read; nothing is typed"
    act = dict(act=uuid.uuid4().hex[:12], verb="type", at=_now(), caller=caller, seat=seat.name, line=line,
               launch=dict(pane=launched.pane, pid=launched.pid, at=launched.at), screen=bounded(screen.text),
               outcome="typing")
    try:
        write(seat, act)
    except OSError as error:
        return f"refused: the act could not be recorded ({error}); nothing is typed"
    door.since = datetime.now(timezone.utc)
    try:
        door.tmux.paste(pane.id, line)
        box = door.harness.box(line)
        shown = door.box_shows(box, BOX_SECONDS)
        if shown is None or not door.matches(box, shown):
            seen = shown.box if shown is not None else None
            return _finish(seat, act, "not submitted", f"the box showed {seen!r}, not the line; it stays for a look")
        if seat.launched() != launched:
            return _finish(seat, act, "not submitted", "the launch changed after the paste")
        door.submit(pane.id)
    except TmuxError as error:
        return _finish(seat, act, "not submitted", str(error))
    delivery = door.confirm(line, seconds=CONFIRM_SECONDS)
    detail = ("the record shows it" if delivery.result == "delivered"
              else "submitted once; the record does not show it (a program's own prompt may not record it)")
    return _finish(seat, act, "typed", detail)


def _finish(seat, act: dict, outcome: str, detail: str) -> str:
    try:
        write(seat, dict(act=act["act"], verb=act["verb"], at=_now(), outcome=outcome, detail=detail))
        kept = ""
    except OSError as error:
        kept = f"; its outcome could not be recorded ({error})"
    what = f"pressed {act['choice']!r} by {act['by']}" if act["verb"] == "press" else f"typed {act['line']!r}"
    if outcome.startswith("not "):
        what = f"{act['verb']} {act.get('choice', act.get('line'))!r}: {outcome}"
    return f"{seat.name}: {what} · {detail} · recorded in {path(seat).relative_to(seat.ship.runtime)}{kept}"
