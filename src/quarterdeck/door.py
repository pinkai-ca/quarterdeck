"""The Door: the one path anything is typed through (law: one-door).

It clears what it knows how to clear, refuses what it does not, types once into an empty
box, checks the box as the program shows it, submits, and confirms by the record. It
never types into a session the human owns. Only the service calls ``deliver``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .harness.base import Screen
from . import pending
from .quota import guard
from .seat import Seat

CONFIRM_SECONDS = 5.0
BOX_SECONDS = 2.0
SETTLE_SECONDS = 0.1
TRANSCRIPT_LINES = 2000  # scrollback read for an echo: a long message's echo climbs above the screen


@dataclass(frozen=True)
class Reading:
    """Can the typing path deliver: open, or obstructed with the reason and what clears it."""

    open: bool
    reason: str = ""
    clears: str = ""
    working: bool = False
    waiting: bool = False

    def __str__(self) -> str:
        if self.open:
            return "open, queues" if self.working else "open"
        if self.waiting:
            return self.reason
        return f"obstructed: {self.reason}, {self.clears}"


def echoed(echo: str, wanted: str) -> bool:
    """Whether a transcript echo is the wanted text; a program may cut a long echo short with an ellipsis."""
    flat = " ".join(echo.split())
    if flat.endswith("…"):
        return len(flat) > 1 and wanted.startswith(flat[:-1].rstrip())
    return flat == wanted


@dataclass(frozen=True)
class Delivery:
    result: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.result}: {self.detail}" if self.detail else self.result


class Door:
    def __init__(self, seat: Seat):
        self.seat = seat
        self.harness = seat.harness
        self.tmux = seat.tmux
        self.proof = None

    def screen(self) -> Screen | None:
        pane = self.seat.pane()
        if pane is None or pane.dead:
            return None
        return self.harness.screen(self.tmux.capture(pane.id))

    def echoes(self) -> tuple[str, ...]:
        """The inputs the transcript shows, read with scrollback, since a long message's echo reaches above the screen."""
        pane = self.seat.pane()
        if pane is None or pane.dead:
            return ()
        return self.harness.screen(self.tmux.capture(pane.id, history=TRANSCRIPT_LINES)).echoes

    def reading(self, screen: Screen | None = None) -> Reading:
        """Persisted quota evidence and screen obstruction; nothing is pressed or fetched."""
        if self.seat.session.human:
            return Reading(False, "a human's session", "its watch delivers")  # law: one-door
        screen = screen or self.screen()
        wall = guard(self.seat, screen)
        if hold := pending.held(self.seat, screen, self.proof):
            return Reading(False, hold[0], hold[1], waiting=hold[2])
        if screen is None:
            return Reading(False, "no program in the pane", "qd up")
        if wall:
            return Reading(False, wall, "quota reset or another pool")
        if screen.interruption is not None:
            return Reading(False, screen.interruption.name,
                           "dismissable" if screen.interruption.dismissable else "needs a human")
        if screen.unknown is not None:
            return Reading(False, f"unknown dialog: {screen.unknown}", "needs a human")
        if screen.box is None and not screen.text.strip():
            return Reading(False, "blank screen, the program is starting or drawing", "time")
        if screen.box is None:
            return Reading(False, "box not found on the screen", "needs a human")
        if screen.box.strip():
            return Reading(False, "text in the box", "needs a human")
        if screen.working and not self.harness.queues_while_working(self.seat.spec):
            return Reading(False, "working, program does not queue", "time")
        return Reading(True, working=screen.working)

    def clear_known(self) -> Screen | None:
        """Dismiss a notice the adapter knows, once, and read again (law: one-door).

        Never while text sits in the box: a key pressed then could submit a person's draft.
        """
        screen = self.screen()
        if self.seat.session.human:
            return screen  # no key of ours is ever pressed in a human's session
        if not pending.held(self.seat, screen, self.proof) and screen and screen.interruption and screen.interruption.dismissable and not (screen.box or "").strip():
            self.tmux.keys(self.seat.pane().id, screen.interruption.key)
            time.sleep(SETTLE_SECONDS)
            screen = self.screen()
        return screen

    def deliver(self, text: str, speed: str | None = None, confirm: bool = True, proof=None) -> Delivery:
        """Type ``text`` once and confirm it, or withhold it and say why.

        A slash command is typed with ``confirm=False``: the control's own confirmation
        follows, since a local command leaves no input in the record.
        """
        self.proof = proof
        if any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in text):
            return Delivery("withheld", "message contains unsupported terminal control characters; revise it")
        screen = self.clear_known()  # law: one-door
        if wall := guard(self.seat, screen):
            return Delivery("withheld", wall)
        if hold := pending.held(self.seat, screen, proof):
            return Delivery("withheld", hold[0])
        if speed == "interrupt" and screen is not None and screen.working and not self.seat.session.human:
            self.interrupt()
            screen = self.settled()
        reading = self.reading(screen)
        if not reading.open:
            return Delivery("withheld", str(reading))
        pane = self.seat.pane()
        if proof and not proof.same_launch(self.seat):
            return Delivery("blind", "launch changed before paste; no keys sent")
        if proof:
            proof.save(phase="pasting")
        self.tmux.paste(pane.id, text)
        shown = self.box_shows(self.harness.box(text))
        if shown is None or not self.matches(self.harness.box(text), shown):  # law: one-door
            seen = shown.box if shown else None
            return Delivery("blind", f"box showed {seen!r}, not the message; not submitted or cleared; needs a human")
        if proof and not proof.same_launch(self.seat):
            return Delivery("blind", "launch changed after paste; not submitted or cleared")
        if proof:
            proof.save(phase="submitting", shown=shown.box)
        self.submit(pane.id)
        if proof:
            proof.save(phase="submitted")
        if speed == "now" and shown.working and self.harness.keys.send_now:
            self.tmux.keys(pane.id, *self.harness.keys.send_now)
        if not confirm:
            return Delivery("typed", f"submitted in {pane.id}")
        return self.confirm(text)  # law: one-door

    def settled(self, seconds: float = BOX_SECONDS) -> Screen | None:
        """The screen once the program has stopped working, or the last screen seen."""
        deadline = time.monotonic() + seconds
        while True:
            screen = self.screen()
            if screen is None or not screen.working or time.monotonic() > deadline:
                return screen
            time.sleep(SETTLE_SECONDS)

    def matches(self, box, screen: Screen) -> bool:
        pane = self.seat.pane()
        # The ordinary Codex composer has two prefix cells and one right margin.
        # The other adapters use a two-cell prompt/continuation prefix.
        columns = self.harness.box_columns(pane.width) if pane else None
        return box.matches(screen.box, columns)

    def box_shows(self, box, seconds: float = BOX_SECONDS) -> Screen | None:
        """The screen once the box shows the paste as the program folds it, or the last screen seen."""
        deadline = time.monotonic() + seconds
        while True:
            screen = self.screen()
            if screen is not None and self.matches(box, screen) or time.monotonic() > deadline:
                return screen
            time.sleep(SETTLE_SECONDS)

    def submit(self, pane_id: str) -> None:
        """Enter, or a raw carriage return when the pane is in copy mode, which swallows keys."""
        pane = self.tmux.pane(pane_id)
        if pane and pane.in_mode:
            self.tmux.paste(pane_id, "\r", bracketed=False)
        else:
            self.tmux.keys(pane_id, self.harness.keys.submit)

    def empty_box(self) -> None:
        pane = self.seat.pane()
        if pane and self.harness.keys.empty:
            self.tmux.keys(pane.id, *self.harness.keys.empty)

    def interrupt(self) -> str:
        pane = self.seat.pane()
        key = self.harness.keys.interrupt
        if pane is None:
            return f"{self.seat.name} is not up"
        if key is None:
            return f"{self.harness.name} has no interrupt key"
        self.tmux.keys(pane.id, key)
        return f"{self.seat.name} interrupted with {key} in {pane.id}"

    def confirm(self, text: str, seconds: float = CONFIRM_SECONDS) -> Delivery:
        """Delivered when the record shows the text as an input or queued; blind when it never does.

        A program whose record the framework cannot read is confirmed by its transcript: the
        text echoed above the box, as the adapter reads it.
        """
        if self.proof:
            deadline = time.monotonic() + seconds
            while True:
                if result := self.proof.confirmed(self.seat):
                    return Delivery(result, "exact payload in the record after paste")
                if time.monotonic() >= deadline:
                    return Delivery("pending", "submitted once; waiting for record confirmation (600s intervention bound)")
                time.sleep(0.2)
        wanted = text.strip()
        if not self.harness.record_confirms:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if any(echoed(e, " ".join(text.split())) for e in self.echoes()):
                    return Delivery("delivered", "echoed in the transcript; this program's record is not read")
                time.sleep(0.2)
            return Delivery("blind", f"typed and submitted, not echoed in the transcript within {seconds:.0f}s")
        deadline = time.monotonic() + seconds
        while True:
            record = self.seat.record()
            if record is not None:
                try:
                    state = self.harness.state(record)
                except OSError:
                    state = None
                if state and state.last_input and state.last_input.strip() == wanted:
                    return Delivery("delivered", f"in the record {record.name}")
                if state and any(c.strip() == wanted for c in state.consumed):
                    return Delivery("delivered", "queued and taken into the running turn")
                if state and any(q.strip() == wanted for q in state.queued):
                    return Delivery("queued", "queued in the record; read at the turn's end")
            if time.monotonic() > deadline:
                break
            time.sleep(0.2)
        screen = self.screen()
        if screen and (screen.queued or any(echoed(e, wanted) for e in self.echoes())):
            return Delivery("queued", "the screen shows it queued; the record will confirm")
        if screen and screen.working and screen.box == "":
            return Delivery("queued", "the box emptied and the program went to work; the record will confirm")
        return Delivery("blind", f"typed and submitted, not seen in the record within {seconds:.0f}s")
