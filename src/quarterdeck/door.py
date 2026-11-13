"""The Door: the one path anything is typed through (law: one-door).

It clears what it knows how to clear, refuses what it does not, types once into an empty
box, checks the box as the program shows it, submits, and confirms by the record.
Into a session the human owns it types only when a message waits there, no watch is armed
and the box is empty. Only the service calls ``deliver``.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from . import ledger as ledgermod, pending, input_receipt, quota
from .harness.base import Screen
from .harness.types import _cells
from .quota import reset_text, stamp
from .seat import Seat

CONFIRM_SECONDS = 5.0
BOX_SECONDS = 2.0
SETTLE_SECONDS = 0.1
ANSWER_SECONDS = 5.0  # measured: Grok's training question was gone within one second of the press
TRANSCRIPT_LINES = 2000  # scrollback read for an echo: a long message's echo climbs above the screen
UNCAPTURED = object()


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
        self.before_submit = None
        self.human_wake = False
        self.since = datetime.now(timezone.utc)

    def screen(self) -> Screen | None:
        if hasattr(self.seat, 'read_pane'):
            found = self.seat.read_pane()
            return self.harness.screen(found[1]) if found else None
        pane = self.seat.pane()
        if pane is None or pane.dead:
            return None
        return self.harness.screen(self.tmux.capture(pane.id))

    def echoes(self) -> tuple[str, ...]:
        """The inputs the transcript shows, read with scrollback, since a long message's echo reaches above the screen."""
        if hasattr(self.seat, 'read_pane'):
            found = self.seat.read_pane(history=TRANSCRIPT_LINES)
            return self.harness.screen(found[1]).echoes if found else ()
        pane = self.seat.pane()
        if pane is None or pane.dead:
            return ()
        return self.harness.screen(self.tmux.capture(pane.id, history=TRANSCRIPT_LINES)).echoes

    def wall(self, screen: Screen | None) -> str | None:
        """A quota wall on this seat's pool: the one the Ledger knows, or the banner on the screen.

        A reading, so nothing is written: the service persists what the record and the screen show
        on its own tick, and a banner the book already knows, even an expired one, is not news.
        """
        launched = self.seat.launched()
        pool = launched.pool if launched else self.seat.pool.name
        shown = self.harness.quota_wall(screen.quota, datetime.now(timezone.utc)) if screen and screen.quota else None
        book = ledgermod.Ledger.reading(self.seat.ship.runtime / "ledger.sqlite")
        if book is not None:
            with book:
                try:
                    known = book.quota.blocked(self.seat.name, pool) or next(
                        (w for w in book.walls(self.seat.ship, scope=quota.model_scope(self.seat, pool, running=True)) if w.split(" ")[0] == pool), None)
                    if known:
                        return known
                    if shown is not None and book.quota.recognized(self.seat.name, shown):
                        return None  # an old banner the record has already placed in time
                except sqlite3.OperationalError:
                    pass  # a book written by an older service: the screen still speaks
        if shown is not None:
            return f"{pool} {shown.reason} · {reset_text(stamp(shown.resets_at), from_screen=True)}"
        return None

    def reading(self, screen: Screen | None = None) -> Reading:
        """Can the typing path deliver, from the screen and the Ledger's walls; nothing is pressed, written or fetched."""
        from .turn_failure import obstruction
        if reason := obstruction(self.seat):
            return Reading(False, reason, "needs a human")  # law: nothing-waits-silently
        if self.seat.session.human:
            from .recovery import held
            if reason := held(self.seat):
                return Reading(False, f"a human owns this session; {reason}", "needs a human")
            if not self.human_wake:
                return Reading(False, "a human's session", "its watch delivers")  # law: one-door
            from .human_wake import eligible
            if reason := eligible(self.seat, screen, self.proof):
                return Reading(False, reason, "needs a human")
        screen = screen or self.screen()
        return self._reading(screen)

    def _reading(self, screen: Screen | None) -> Reading:
        """Read this captured frame, including absence, without taking another capture."""
        from .turn_failure import obstruction
        if reason := obstruction(self.seat):
            return Reading(False, reason, "needs a human")  # law: record-first
        wall = self.wall(screen)
        if hold := pending.held(self.seat, screen, self.proof, wall):
            return Reading(False, hold[0], hold[1], waiting=hold[2])
        if screen is None:
            return Reading(False, "no program in the pane", "qd up")
        if wall:
            return Reading(False, wall, "quota reset or another pool")
        if screen.interruption is not None:
            if screen.interruption.click is not None:
                return Reading(False, *self._question(screen))
            return Reading(False, screen.interruption.name,
                           "dismissable" if screen.interruption.dismissable else "needs a human")
        if screen.fresh is not None and self.answered_and_cleared(screen.fresh):
            screen = replace(screen, box="", unknown=None, fresh=None)  # the measured form, in its own launch
        if screen.unknown is not None:
            return Reading(False, f"unknown dialog: {screen.unknown}", "needs a human")
        if screen.starting:
            return Reading(False, "starting, waiting for the input box", "time")
        if screen.box is None and not screen.text.strip():
            return Reading(False, "blank screen, the program is starting or drawing", "time")
        if screen.box is None:
            return Reading(False, "box not found on the screen", "needs a human")
        if screen.box.strip():
            return Reading(False, "text in the box", "needs a human")
        if screen.working and not self.harness.queues_while_working(self.seat.spec):
            return Reading(False, "working, program does not queue", "time")
        if not self.harness.startup_ready(getattr(self.seat, 'home', None)):
            return Reading(False, "starting, waiting for the program's startup choice", "time")
        return Reading(True, working=screen.working)

    def human_obstruction(self) -> tuple[Reading, str | None]:
        """Alarm evidence only: confirm an unknown frame twice, never grant Door authority."""
        if self.seat.session.human:
            reading = self.reading()
            return reading, reading.reason if reading.clears == 'needs a human' else None
        launched = self.seat.launched()
        screen = self.screen()
        reading = self._reading(screen)
        reason = reading.reason if not reading.open and reading.clears == 'needs a human' else None
        if reason and screen is not None and screen.interruption is None:
            unknown = (f'unknown dialog: {screen.unknown}' if screen.unknown is not None
                       else 'box not found on the screen' if screen.box is None and screen.text.strip() else None)
            if reason == unknown:
                confirmed = self.screen()
                if (launched is None or self.seat.launched() != launched or confirmed is None
                        or confirmed.text != screen.text):
                    reason = None
        return reading, reason

    def initial_obstruction(self, screen: Screen | None) -> Reading | None:
        """Absence or an unknown initial frame holds without a replacement capture."""
        if screen is None or screen.unknown is not None or (screen.box is None and screen.interruption is None):
            return self._reading(screen)
        return None

    def answered_and_cleared(self, question: str) -> bool:
        """This launch's record shows ``question`` answered and cleared, on a version whose composer after it is measured."""
        launched = self.seat.launched()
        return (launched is not None and launched.version in self.harness.fresh_versions
                and any(item.get("question") == question and item.get("outcome") == "cleared"
                        for item in launched.answered or ()))

    def _question(self, screen: Screen) -> tuple[str, str]:
        """A question the framework answers: once per launch, on a screen that reads not working with its box read;
        after that a screen still showing it needs a human."""
        question = screen.interruption
        launched = self.seat.launched()
        done = next((item for item in (launched.answered or []) if item.get("question") == question.name), None) if launched else None
        if done is not None:
            return (f"{question.name}: {done.get('choice')} was pressed at {done.get('at')} and the question did not clear; "
                    "nothing more is pressed", "needs a human")
        if screen.unknown is not None:
            return f"{question.name}; {screen.unknown}; nothing is pressed", "needs a human"
        if screen.working:
            return f"{question.name}; answered {question.click} once the turn under it ends", "time"
        return f"{question.name}; the framework answers it {question.click}", "dismissable"

    def answer(self, screen: Screen) -> Screen | None:
        """Press the one choice the orders fix for this question, by its text, once in this launch (law: one-door).

        Only on an empty box, only where the program has asked for SGR mouse reports, and only on the frame
        read just before the press. The press is recorded in the launch before it is sent, then its outcome.
        """
        question = screen.interruption
        launched = self.seat.launched()
        if launched is None or any(item.get("question") == question.name for item in launched.answered or ()):
            return screen  # once per launch: never pressed again
        found = self.seat.read_pane()
        if found is None:
            return screen
        pane, raw = found
        current = self.harness.screen(raw)
        if (current.interruption is not question or current.text != screen.text or current.working
                or current.box is None or current.box.strip()):
            return current  # changed under us, or a turn still running (unmeasured there): nothing is pressed
        cell = choice_cell(current.text, question.click)
        modes = self.tmux.mouse_modes(pane.id, pane.pid)
        if cell is None or not modes or not modes["sgr"] or not (modes["button"] or modes["all"] or modes["standard"]):
            return current
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = dict(question=question.name, choice=question.click, at=at, outcome="pressed")
        recorded = self.seat.record_answer(launched, entry)
        if recorded is None:
            return current
        self.tmux.click(pane.id, *cell)
        after, cleared = current, False
        deadline = time.monotonic() + ANSWER_SECONDS
        while time.monotonic() < deadline and not cleared:
            time.sleep(SETTLE_SECONDS)
            found = self.seat.read_pane()
            if found is None:
                break
            after, cleared = self.harness.screen(found[1]), self.harness.question_cleared(found[1], question)
        self.seat.record_answer(recorded, dict(entry, outcome="cleared" if cleared else "did not clear"))
        return after

    def clear_known(self, screen=UNCAPTURED) -> Screen | None:
        """Dismiss a notice the adapter knows, once, and read again (law: one-door).

        Never while text sits in the box: a key pressed then could submit a person's draft.
        """
        if screen is UNCAPTURED:
            screen = self.screen()
        if self.seat.session.human:
            return screen  # no key of ours is ever pressed in a human's session; the person answers there
        if (not pending.held(self.seat, screen, self.proof) and screen and screen.interruption
                and screen.interruption.click is not None and screen.box is not None and not screen.box.strip()):
            return self.answer(screen)
        if not pending.held(self.seat, screen, self.proof) and screen and screen.interruption and screen.interruption.dismissable and not (screen.box or "").strip():
            self.tmux.keys(self.seat.pane().id, screen.interruption.key)
            time.sleep(SETTLE_SECONDS)
            screen = self.screen()
        return screen

    def deliver(self, text: str, speed: str | None = None, confirm: bool = True, proof=None, verify=None, *, human_wake: bool = False) -> Delivery:
        """Type ``text`` once and confirm it, or withhold it and say why.

        A slash command is typed with ``confirm=False``: the control's own confirmation
        follows, since a local command leaves no input in the record.
        """
        self.proof = proof
        self.human_wake = human_wake
        if self.seat.session.human:
            from .human_wake import authorized
            if not human_wake or not authorized(self.seat, proof, text):
                return Delivery("withheld", "a human's session; only the reserved fixed waking line may be typed")
            if speed is not None or not confirm or verify is not None:
                return Delivery("withheld", "human wake cannot send a control or alter receipt semantics")
        elif human_wake:
            return Delivery("withheld", "human wake requires a human-owned session")
        self.before_submit = None
        self.since = datetime.now(timezone.utc)
        from .turn_failure import obstruction
        if reason := obstruction(self.seat):
            return Delivery("withheld", reason)  # before any dismissal, interrupt or paste (law: one-door)
        if any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in text):
            return Delivery("withheld", "message contains unsupported terminal control characters; revise it")
        from . import paste_refusal
        screen = self.screen()
        if reading := self.initial_obstruction(screen):
            return Delivery('withheld', str(reading))
        if reason:=paste_refusal.refused(self.seat,text,screen):
            return Delivery('withheld',reason)
        screen = self.clear_known(screen)  # law: one-door
        if wall := self.wall(screen):
            return Delivery("withheld", wall)
        if hold := pending.held(self.seat, screen, proof):
            return Delivery("withheld", hold[0])
        if speed == "interrupt" and screen is not None and screen.working and not self.seat.session.human:
            self.interrupt()
            screen = self.settled()
        reading = self._reading(screen) if screen is None else self.reading(screen)
        if not reading.open:
            return Delivery("withheld", str(reading))
        pane = self.seat.pane()
        if proof and not proof.same_launch(self.seat):
            return Delivery("blind", "launch changed before paste; no keys sent")
        if verify is not None and not verify():
            return Delivery('withheld', 'bound source changed before paste; no keys sent')
        if self.human_wake:
            from .human_wake import eligible
            # Re-capture at the last pre-paste boundary; never clear a human draft.
            screen = self.screen()
            if reason := eligible(self.seat, screen, proof):
                return Delivery('withheld', reason)
            if not proof.same_launch(self.seat):
                return Delivery('blind', 'human wake launch changed before paste; no keys sent')
        owned=paste_refusal.owner(self,text,screen)
        if proof:
            proof.save(phase="pasting")
        self.tmux.paste(pane.id, text)
        shown = self.box_shows(self.harness.box(text))
        if shown is None or not self.matches(self.harness.box(text), shown):  # law: one-door
            if verify is None or verify():
                if reason:=paste_refusal.clear(self,text,owned,shown,BOX_SECONDS):
                    return Delivery('withheld',reason)
            seen = shown.box if shown else None
            if proof and proof.data.get('paste_clear'):
                return Delivery('blind','paste check failed; owned clear/empty outcome unconfirmed; inspect retained journal; never resent')
            return Delivery("blind", f"box showed {seen!r}, not the message; not submitted or cleared; needs a human")
        if proof and not proof.same_launch(self.seat):
            return Delivery("blind", "launch changed after paste; not submitted or cleared")
        if verify is not None and not verify():
            return Delivery('blind', 'bound source changed after paste; not submitted or cleared')
        if proof:
            proof.save(phase="submitting", shown=shown.box)
        self.before_submit = shown
        if self.human_wake:
            from . import post
            watch = post.watch_reading(self.seat.ship.runtime, self.seat.name)
            if watch.armed is not False or watch.generation != proof.data['generation']:
                return Delivery('blind', 'watch changed after waking paste; not submitted or cleared')
            if not post.Attempts(self.seat.ship.runtime).undelivered(post.Store(self.seat.ship.runtime), self.seat.name):
                return Delivery('blind', 'waiting message changed after waking paste; not submitted or cleared')
            current = self.seat.pane()
            if current is None or current.in_mode or not proof.same_launch(self.seat):
                return Delivery('blind', 'human wake pane changed after paste; not submitted or cleared')
        self.submit(pane.id)
        if proof:
            proof.save(phase="submitted")
        if speed == "now" and shown.working and self.harness.keys.send_now:
            self.tmux.keys(pane.id, *self.harness.keys.send_now)
        if not confirm:
            return Delivery("typed", f"submitted in {pane.id}")
        return self.confirm(text)  # law: one-door

    def control_keys(self, keys: tuple[str, ...], proof, verify) -> Delivery:
        """One adapter-proved mode-control queue, under the same human/draft/identity Door."""
        if self.seat.session.human:
            return Delivery('withheld', 'a human owns this session; no mode-control keys sent')
        self.proof = proof
        screen = self.screen()
        reading = self.reading(screen)
        if not reading.open or reading.working or screen.queued:
            return Delivery('withheld', str(reading))
        if not proof.same_launch(self.seat) or not verify():
            return Delivery('withheld', 'bound mode/source changed; no control key sent')
        pane = self.seat.pane()
        self.tmux.keys(pane.id, *keys)
        return Delivery('typed', 'one native mode-control queue sent')

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
        columns = self.harness.box_columns(pane.width) if pane else None
        return box.matches(screen.box, columns, self.harness.wrap_slack)

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

    def interrupt(self) -> str:
        if self.seat.session.human:
            return f"{self.seat.name}: a human owns this session; no interrupt key sent"
        pane = self.seat.pane()
        key = self.harness.keys.interrupt
        if pane is None:
            return f"{self.seat.name} is not up"
        if key is None:
            return f"{self.harness.name} has no interrupt key"
        self.tmux.keys(pane.id, *key.split())  # a program that wants the key twice names the sequence
        return f"{self.seat.name} interrupted with {key} in {pane.id}"

    def confirm(self, text: str, seconds: float = CONFIRM_SECONDS) -> Delivery:
        """Only a fresh exact input in the owned record confirms acceptance."""
        deadline = time.monotonic() + seconds
        while True:
            result = None
            if self.proof:
                result = self.proof.confirmed(self.seat)
            elif (record := self.seat.record()) is not None:
                try:
                    result = input_receipt.confirmed(self.harness, record, text, self.since)
                except OSError:
                    pass
            if result:
                return Delivery(result, "exact payload in the record after paste")
            if self.proof and self.before_submit is not None and self.proof.same_launch(self.seat):
                screen = self.screen()
                notice = self.harness.queued_input(text, self.before_submit, screen) if screen else None
                if notice and self.proof.same_launch(self.seat):
                    self.proof.save(screen_queue=True)
                    return Delivery("queued", notice)
            if time.monotonic() >= deadline:
                return Delivery("pending" if self.proof else "blind", "submitted once; waiting for record confirmation")
            time.sleep(0.2)


def choice_cell(text: str, choice: str) -> tuple[int, int] | None:
    """The 1-based cell at the middle of ``choice``, found by its text exactly once on the screen."""
    rows = text.split("\n")
    found = [(y, row.index(choice)) for y, row in enumerate(rows) if choice in row]
    if len(found) != 1 or rows[found[0][0]].count(choice) != 1:
        return None
    y, at = found[0]
    left = _cells(rows[y][:at])
    if left is None:
        return None  # an unmeasured width before the choice: no place is guessed
    return left + len(choice) // 2 + 1, y + 1
