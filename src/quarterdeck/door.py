"""The Door: the one path anything is typed through (law: one-door).

It clears what it knows how to clear, refuses what it does not, types once into an empty
box, checks the box as the program shows it, submits, and confirms by the record. It
never types into a session the human owns. Only the service calls ``deliver``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .harness.base import Screen
from .seat import Seat

CONFIRM_SECONDS = 5.0
BOX_SECONDS = 2.0
SETTLE_SECONDS = 0.1


@dataclass(frozen=True)
class Reading:
    """Can the typing path deliver: open, or obstructed with the reason and what clears it."""

    open: bool
    reason: str = ""
    clears: str = ""
    working: bool = False

    def __str__(self) -> str:
        if self.open:
            return "open, queues" if self.working else "open"
        return f"obstructed: {self.reason}, {self.clears}"


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

    def screen(self) -> Screen | None:
        pane = self.seat.pane()
        if pane is None or pane.dead:
            return None
        return self.harness.screen(self.tmux.capture(pane.id))

    def reading(self, screen: Screen | None = None) -> Reading:
        """The door from the screen alone; nothing is pressed."""
        if self.seat.session.human:
            return Reading(False, "a human's session", "its watch delivers")  # law: one-door
        screen = screen or self.screen()
        if screen is None:
            return Reading(False, "no program in the pane", "qd up")
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
        """Dismiss a notice the adapter knows, once, and read again (law: one-door)."""
        screen = self.screen()
        if screen and screen.interruption and screen.interruption.dismissable:
            self.tmux.keys(self.seat.pane().id, screen.interruption.key)
            time.sleep(SETTLE_SECONDS)
            screen = self.screen()
        return screen

    def deliver(self, text: str, speed: str | None = None, confirm: bool = True) -> Delivery:
        """Type ``text`` once and confirm it, or withhold it and say why.

        A slash command is typed with ``confirm=False``: the control's own confirmation
        follows, since a local command leaves no input in the record.
        """
        screen = self.clear_known()  # law: one-door
        reading = self.reading(screen)
        if not reading.open:
            return Delivery("withheld", str(reading))
        pane = self.seat.pane()
        if speed == "interrupt" and screen.working:
            self.interrupt()
        self.tmux.paste(pane.id, text)
        shown = self.box_shows(self.harness.box(text))
        if shown is None or not self.harness.box(text).matches(shown.box):  # law: one-door
            self.empty_box()
            seen = shown.box if shown else None
            return Delivery("withheld", f"box showed {seen!r}, not the message; emptied")
        self.submit(pane.id)
        if speed == "now" and shown.working and self.harness.keys.send_now:
            self.tmux.keys(pane.id, *self.harness.keys.send_now)
        if not confirm:
            return Delivery("typed", f"submitted in {pane.id}")
        return self.confirm(text)  # law: one-door

    def box_shows(self, box, seconds: float = BOX_SECONDS) -> Screen | None:
        """The screen once the box shows the paste as the program folds it, or the last screen seen."""
        deadline = time.monotonic() + seconds
        while True:
            screen = self.screen()
            if screen is not None and box.matches(screen.box) or time.monotonic() > deadline:
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
        """Delivered when the record shows the text as an input or queued; blind when it never does."""
        wanted = " ".join(text.split())
        deadline = time.monotonic() + seconds
        while True:
            record = self.seat.record()
            if record is not None:
                state = self.harness.state(record)
                if state.last_input and " ".join(state.last_input.split()) == wanted:
                    return Delivery("delivered", f"in the record {record.name}")
                if any(" ".join(q.split()) == wanted for q in state.queued):
                    return Delivery("queued", "queued in the record; read at the turn's end")
            if time.monotonic() > deadline:
                break
            time.sleep(0.2)
        screen = self.screen()
        if screen and screen.queued:
            return Delivery("queued", "the screen shows it queued; the record will confirm")
        return Delivery("blind", f"typed and submitted, not seen in the record within {seconds:.0f}s")
