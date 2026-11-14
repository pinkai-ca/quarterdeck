"""The Door: the one path anything is typed through (law: one-door).

It clears what it knows how to clear, refuses what it does not, types once into an empty
box, checks the box as the program shows it, submits, and confirms by the record.
Into a session the human owns it types only when a message waits there, no watch is armed
and the box is empty. Only the service calls ``deliver``.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from . import ledger as ledgermod, pending, input_receipt, quota, pane_obstruction
from .harness.base import Screen
from .harness.types import _cells
from .quota import reset_text, stamp
from .seat import Seat
from .ship import ShipError
from .tmux import TmuxError

CONFIRM_SECONDS = 5.0
BOX_SECONDS = 2.0
SETTLE_SECONDS = 0.1
ANSWER_SECONDS = 5.0
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
    diagnostic: str = ""  # reason metadata only; never changes whether the door is open
    display_reason: str = ""  # presentation only; admission and repeated-frame confirmation keep reason

    def __str__(self) -> str:
        if self.open:
            text = "open, queues" if self.working else "open"
        elif self.waiting:
            text = self.reason
        else:
            text = f"obstructed: {self.display_reason or self.reason}, {self.clears}"
        return f"{text}; {self.diagnostic}" if self.diagnostic else text


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
        self.after_paste = None
        self.captured_pane = None
        self.human_wake = False
        self.since = datetime.now(timezone.utc)

    def screen(self) -> Screen | None:
        self.captured_pane = None
        if hasattr(self.seat, 'read_pane'):
            found = self.seat.read_pane()
            self.captured_pane = found[0] if found else None
            return self.known(self.padded(self.harness.screen(found[1]))) if found else None
        pane = self.seat.pane()
        if pane is None or pane.dead:
            return None
        self.captured_pane = pane
        return self.known(self.padded(self.harness.screen(self.tmux.capture(pane.id))))

    def padded(self, screen: Screen | None) -> Screen | None:
        """A composer whose one row is only padding, in a program that draws typed spaces like its padding,
        reads empty only where the visible terminal cursor sits on the empty composer's first cell, as the adapter
        names it. The cursor is read with a second frame in one bracket of identical readings; that frame must
        read the same box with the cursor on that same cell. A person's typed spaces move the cursor, so they stay
        text in the box and the human waking line is never typed after them. A cursor that cannot be read, hidden,
        moving or elsewhere leaves the box as the reader read it."""
        cell = self.harness.empty_cell(screen) if screen is not None else None
        read = getattr(self.seat, 'read_control_frame', None)
        if cell is None or read is None:
            return screen
        frame = read()
        if (frame is None or frame.cursor_visible is not True or frame.cursor != cell
                or (self.captured_pane is not None and frame.pane.pid != self.captured_pane.pid)):
            return screen
        again = self.harness.screen(frame.text)
        if again.box != screen.box or again.working != screen.working or self.harness.empty_cell(again) != cell:
            return screen
        return replace(screen, box="")

    def known(self, screen: Screen | None) -> Screen | None:
        """A composer the adapter knows only as fresh reads as the measured empty idle composer where this
        launch's own evidence settles its question: the framework's recorded press that cleared it, or the
        adapter's own seed named in the launch record and marked in the home. Without that evidence it stays
        unknown, so every reader of the door's capture, the human waking line's included, sees one reading.
        The measured form draws nothing under its footer, no working sign: it reads not working."""
        if screen is not None and screen.fresh is not None and (
                self.answered_and_cleared(screen.fresh) or self.seeded(screen.fresh)):
            return replace(screen, box="", unknown=None, fresh=None, working=False)  # the measured form, in its own launch
        return screen

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
                    scope = quota.model_scope(self.seat, pool, running=True, screen=screen)
                    known = book.quota.blocked(self.seat.name, pool, scope=scope) or next(
                        iter(book.walls(self.seat.ship, scope=scope, pool=pool)), None)
                    if known:
                        return known
                    if shown is not None and book.quota.recognized(self.seat.name, shown):
                        return None  # an old banner the record has already placed in time
                except sqlite3.OperationalError:
                    pass  # a book written by an older service: the screen still speaks
        if shown is not None:
            # This is the provider's actual refusal on this screen, not another model's cached cap.
            model = quota.model_scope(self.seat, pool, running=True, screen=screen) if self.harness.model_scoped_quota else None
            label = f" (model {model.model})" if model is not None and model.model else ""
            if self.harness.model_scoped_quota and not label:
                label = " (own refusal; model unknown)"
            return f"{pool} {shown.reason}{label} · {reset_text(stamp(shown.resets_at), from_screen=True)}"
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
            from .human_wake import candidate_hold, eligible, screen_hold
            if reason := candidate_hold(self.seat, self.proof):
                return Reading(False, reason, "needs a human")
            if screen is None:
                if reason := eligible(self.seat, proof=self.proof):
                    return Reading(False, reason, "needs a human")
                screen = self.screen()
            if reason := screen_hold(screen):
                return Reading(False, reason, "needs a human")
        screen = screen or self.screen()
        return self._reading(screen)

    def _reading(self, screen: Screen | None) -> Reading:
        """Read this captured frame, including absence, without taking another capture."""
        reading = replace(self._base_reading(screen), diagnostic=self.pane_obstruction() or "")
        display, reason = self.pane_diagnostic(screen)
        if display or reason:
            diagnostic = '; '.join(item for item in (reading.diagnostic, reason) if item)
            if screen.unknown is None or reading.reason != f'unknown dialog: {screen.unknown}':
                display = ''
            elif reading.diagnostic and getattr(self.harness, 'minimum_pane_heights', {}):
                display = 'input box needs a taller pane'
            reading = replace(reading, diagnostic=diagnostic, display_reason=display)
        return reading

    def pane_diagnostic(self, screen: Screen | None) -> tuple[str, str]:
        """Measured empty-box geometry for this captured pane and its own launch, never admission."""
        heights = getattr(self.harness, 'minimum_pane_heights', {})
        pane = self.captured_pane
        rows = getattr(pane, 'height', None)
        if (not heights or screen is None or screen.box is not None or screen.interruption is not None
                or not self.harness.missing_box_known(screen)
                or pane is None or getattr(pane, 'dead', None) is not False or type(rows) is not int or rows <= 0):
            return '', ''
        try:
            launched = getattr(self.seat, 'launched', lambda: None)()
        except (OSError, ValueError, TypeError):
            return '', ''
        version = getattr(launched, 'version', None)
        pane_id, pid = getattr(pane, 'id', None), getattr(pane, 'pid', None)
        if (launched is None or not isinstance(pane_id, str) or type(pid) is not int
                or getattr(launched, 'pane', None) != pane_id or getattr(launched, 'pid', None) != pid):
            return '', ''
        label = f'{self.harness.display_name} {version or "(version unknown)"}'
        display = f'unknown dialog: {label}: {screen.unknown.removeprefix(self.harness.display_name + " ")}' if screen.unknown else ''
        if (height := heights.get(version)) is None or rows >= height:
            return display, ''
        return 'input box needs a taller pane', f'pane {rows} rows; {label} draws its box from {height} rows'

    def _base_reading(self, screen: Screen | None) -> Reading:
        """The existing admission reading; geometry diagnostics confer no authority."""
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
        screen = self.known(screen)
        if screen.unknown is not None:
            return Reading(False, f"unknown dialog: {screen.unknown}", "needs a human")
        if screen.starting:
            return Reading(False, "starting, waiting for the input box", "time")
        if screen.box is None and not screen.text.strip():
            return Reading(False, "blank screen, the program is starting or drawing", "time")
        if screen.box is None:
            if self.starting_up():
                return Reading(False, "starting, the input box is not drawn yet", "time")
            return Reading(False, "box not found on the screen", "needs a human")
        if screen.box.strip():
            return Reading(False, "text in the box", "needs a human")
        if screen.working and not self.harness.queues_while_working(self.seat.spec):
            return Reading(False, "working, program does not queue", "time")
        if not self.harness.startup_ready(getattr(self.seat, 'home', None)):
            return Reading(False, "starting, waiting for the program's startup choice", "time")
        return Reading(True, working=screen.working)

    def starting_up(self) -> bool:
        """Within the launch's start-up bound: a boxless frame then is the program drawing, not a fault."""
        launched = self.seat.launched()
        try:
            at = datetime.fromisoformat(launched.at) if launched else None
        except (TypeError, ValueError):
            return False
        if at is None or at.tzinfo is None:
            return False
        return 0 <= (datetime.now(timezone.utc) - at).total_seconds() < self.harness.start_seconds

    def short_pane_reason(self, text, pane) -> str:
        return self._short_pane_geometry(text, pane.width, pane.height) if pane is not None else ""

    def _short_pane_geometry(self, text, width, height) -> str:
        if self.seat.session.human:
            return ""
        measure = getattr(self.harness, "minimum_box_rows", None)
        if (measure is None or type(width) is not int or type(height) is not int
                or width < 1 or height < 1):
            return ""
        pane_measure = getattr(self.harness, 'minimum_pane_rows', None)
        if pane_measure is not None and getattr(self.harness, 'minimum_pane_heights', {}):
            try:
                launched = getattr(self.seat, 'launched', lambda: None)()
            except (OSError, ValueError, TypeError):
                launched = None
            version = getattr(launched, 'version', None)
            if type(rows := pane_measure(text, width, version)) is int:
                return (f'pane {height} rows; {self.harness.display_name} {version} draws this box from {rows} rows'
                        if rows > height else '')
        rows = measure(text, width)
        if type(rows) is not int or rows <= height:
            return ""
        return (f"pane too short: at least {rows} rows needed for the unfolded message "
                f"({height} available); enlarge the pane")

    def _diagnostic_identity(self):
        launched = self.seat.launched()
        return (dict(pane=launched.pane, pid=launched.pid, launch=launched.at,
                     harness=self.harness.name) if launched is not None else None)

    def pane_obstruction(self) -> str | None:
        """Read reason metadata without another pane query, capture, or evidence transition."""
        if self.seat.session.human:
            return None
        try:
            saved = pane_obstruction.reading(self.seat)
            if saved is None:
                return None
            value, journal = saved
            identity = self._diagnostic_identity()
            if identity is None or value['identity'] != identity:
                return None
            pane = self.captured_pane
            geometry = value['geometry']
            if pane is not None and (pane.dead or pane.id != identity['pane'] or pane.pid != identity['pid']
                                     or geometry != [pane.width, pane.height]):
                return None
            if reason := self._short_pane_geometry(journal['payload'], *geometry):
                return reason if pane is not None else f"last failed paste: {reason}"
        except (OSError, ValueError, KeyError, TypeError, AttributeError, TmuxError):
            pass  # optional diagnostic unknown; the ordinary admission reading is unchanged
        return None

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

    def seeded(self, question: str) -> bool:
        """This launch's record and its home carry the adapter's own evidence that ``question`` was settled before launch."""
        launched = self.seat.launched()
        return launched is not None and self.harness.seeded_acknowledgement(question, launched, getattr(self.seat, 'home', None))

    def _question(self, screen: Screen) -> tuple[str, str]:
        """A question the framework answers: once per launch, on a screen that reads not working with its box read;
        after that a screen still showing it needs a human."""
        question = screen.interruption
        launched = self.seat.launched()
        done = self.last_answer(launched, question)
        if done is not None and not (question.recurs and done.get("outcome") == "cleared"):
            return (f"{question.name}: {done.get('choice')} was pressed at {done.get('at')} and the question did not clear; "
                    "nothing more is pressed", "needs a human")
        if screen.unknown is not None:
            return f"{question.name}; {screen.unknown}; nothing is pressed", "needs a human"
        if screen.working:
            return f"{question.name}; answered {question.click} once the turn under it ends", "time"
        return f"{question.name}; the framework answers it {question.click}", "dismissable"

    @staticmethod
    def last_answer(launched, question) -> dict | None:
        """This launch's latest recorded answer to ``question``, if any."""
        found = [item for item in ((launched.answered or []) if launched else []) if item.get("question") == question.name]
        return found[-1] if found else None

    def answer(self, screen: Screen) -> Screen | None:
        """Press the one choice the orders fix for this question, by its text, once in this launch (law: one-door).

        Only on an empty box, only where the program has asked for SGR mouse reports, and only on the frame
        read just before the press. The press is recorded in the launch before it is sent, then its outcome.
        A ``by_label`` question stands where the box was: it is pressed by the key its choice's own text names,
        found once on the screen. A ``recurs`` question is answered again only after a press that cleared it.
        """
        question = screen.interruption
        launched = self.seat.launched()
        done = self.last_answer(launched, question)
        if launched is None or (done is not None and not (question.recurs and done.get("outcome") == "cleared")):
            return screen  # once per launch, or once per showing: never pressed again over a press that did not clear
        found = self.seat.read_pane()
        if found is None:
            return screen
        pane, raw = found
        current = self.harness.screen(raw)
        box_read = current.box is None if question.by_label else current.box is not None and not current.box.strip()
        if current.interruption is not question or current.text != screen.text or current.working or not box_read:
            return current  # changed under us, or a turn still running (unmeasured there): nothing is pressed
        cell = choice_cell(current.text, question.click)
        if question.by_label:
            if cell is None or question.label_key is None:
                return current  # the choice not found once by its own text: nothing is pressed
        else:
            modes = self.tmux.mouse_modes(pane.id, pane.pid)
            if cell is None or not modes or not modes["sgr"] or not (modes["button"] or modes["all"] or modes["standard"]):
                return current
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = dict(question=question.name, choice=question.click, at=at, outcome="pressed")
        recorded = self.seat.record_answer(launched, entry)
        if recorded is None:
            return current
        if question.by_label:
            self.tmux.keys(pane.id, question.label_key)
        else:
            self.tmux.click(pane.id, *cell)
        after, cleared = current, False
        answer_seconds = getattr(self.harness, "answer_seconds", None)
        deadline = time.monotonic() + (ANSWER_SECONDS if answer_seconds is None else answer_seconds)
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
                and screen.interruption.click is not None
                and (screen.box is None if screen.interruption.by_label else screen.box is not None and not screen.box.strip())):
            return self.answer(screen)
        if not pending.held(self.seat, screen, self.proof) and screen and screen.interruption and screen.interruption.dismissable and not (screen.box or "").strip():
            self.tmux.keys(self.seat.pane().id, screen.interruption.key)
            time.sleep(SETTLE_SECONDS)
            screen = self.screen()
        return screen

    def deliver(self, text: str, speed: str | None = None, confirm: bool = True, proof=None, verify=None, *, human_wake: bool = False) -> Delivery:
        from . import post_view, shutdown
        # A jailed seat reads the post only through its view: bring it up to date before anything is typed, so the
        # message is there when the seat opens it. The message itself is typed whole either way.
        runtime = getattr(getattr(self.seat, 'ship', None), 'runtime', None)
        if runtime is not None and post_view.folder(runtime, self.seat.name).is_dir():
            try:
                post_view.refresh(runtime, self.seat.name)
            except (OSError, ValueError, ShipError):
                pass  # the service's next pass refreshes it again
        with shutdown.delivery(proof):
            if proof and shutdown.requested():
                return Delivery('withheld', 'service stopping before paste')
            return self._deliver(text, speed, confirm, proof, verify, human_wake=human_wake)

    def _deliver(self, text: str, speed: str | None = None, confirm: bool = True, proof=None, verify=None, *, human_wake: bool = False) -> Delivery:
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
        self.after_paste = None
        self.since = datetime.now(timezone.utc)
        from .turn_failure import obstruction
        if reason := obstruction(self.seat):
            return Delivery("withheld", reason)  # before any dismissal, interrupt or paste (law: one-door)
        if any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in text):
            return Delivery("withheld", "message contains unsupported terminal control characters; revise it")
        from . import paste_refusal
        if self.human_wake:
            from .human_wake import eligible
            if reason := eligible(self.seat, proof=proof):
                return Delivery('withheld', reason)
        screen = self.screen()
        if reading := self.initial_obstruction(screen):
            return Delivery('withheld', str(reading))
        if not self.human_wake and (reason:=paste_refusal.refused(self.seat,text,screen)):
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
            from .human_wake import eligible, screen_hold, width_hold
            # Exclude metadata holds and prove the record before the last capture.
            if reason := eligible(self.seat, proof=proof):
                return Delivery('withheld', reason)
            screen = self.screen()
            if reason := screen_hold(screen):
                return Delivery('withheld', reason)
            if reason := width_hold(self.harness, self.captured_pane):
                return Delivery('withheld', reason)
            if not proof.same_launch(self.seat):
                return Delivery('blind', 'human wake launch changed before paste; no keys sent')
        from . import shutdown
        if proof and shutdown.requested():
            if shutdown.never_pasted(proof.data):
                proof.save(phase="cancelled", resolution="service stopped before paste; never typed")
            return Delivery("withheld", "service stopping before paste")
        owned=paste_refusal.owner(self,text,screen)
        if proof:
            proof.save(phase="pasting")
        self.tmux.paste(pane.id, text)
        shown = self.box_shows(self.harness.box(text))
        self.after_paste = shown
        if shown is None or not self.matches(self.harness.box(text), shown):  # law: one-door
            unread = shown is None or shown.box is None
            if unread and proof and not self.seat.session.human:
                try:  # what the reader could not read stays visible; a person's own screen is never kept
                    proof.save(paste_screen=pending.screen_evidence(shown))
                except (OSError, ValueError, TypeError):
                    pass  # optional diagnosis; never alters paste/clear/submit decisions
            current = self.captured_pane
            diagnostic = self.short_pane_reason(text, current)
            if (diagnostic and proof and current and current.id == pane.id and current.pid == pane.pid
                    and (current.width, current.height) == (pane.width, pane.height)
                    and proof.data.get('identity') == self._diagnostic_identity()):
                try:
                    proof.save(paste_geometry=[current.width, current.height])
                    pane_obstruction.publish(proof)
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # optional reason metadata; never alter paste/clear/submit decisions
            if verify is None or verify():
                if reason:=paste_refusal.clear(self,text,owned,shown,BOX_SECONDS):
                    return Delivery('withheld', f'{diagnostic}; {reason}' if diagnostic else reason)
            seen = shown.box if shown else None
            if proof and proof.data.get('paste_clear'):
                reason = 'paste check failed; owned clear/empty outcome unconfirmed; inspect retained journal; never resent'
                return Delivery('blind', f'{diagnostic}; {reason}' if diagnostic else reason)
            reason = f"box showed {seen!r}, not the message; not submitted or cleared; needs a human"
            if unread and proof and 'paste_screen' in proof.data:
                reason = (f"box showed None ({proof.data['paste_screen']['reading']}), not the message; not "
                          "submitted or cleared; the screen read is kept in its journal; needs a human")
            return Delivery("blind", f'{diagnostic}; {reason}' if diagnostic else reason)
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
        if proof and shutdown.requested():
            return Delivery("pending", "submitted once before service stop; record confirmation remains pending")
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

    def answer_control(self, control, value, answer, verify, *, question=None) -> Delivery:
        """Answer only this request's adapter-proved option in a fresh, owned control question."""
        from .harness.types import ControlInterruption

        self.proof = None
        if answer is None or self.seat.session.human or verify() is not True:
            return Delivery('withheld', 'control confirmation ownership is not current')
        launched, pane = self.seat.launched(), self.seat.pane()
        if launched is None or pane is None or pane.dead:
            return Delivery('withheld', 'control confirmation launch is not current')
        screen = self.screen()
        if (screen is None or not isinstance(screen.interruption, ControlInterruption)
                or screen.interruption.control != control.name):
            return Delivery('withheld', 'the owned control question is not shown')
        if question is None or hashlib.sha256(screen.text.encode()).hexdigest() != question:
            return Delivery('withheld', 'control question provenance unknown or changed; no answer sent')
        context = self.harness.control_context(self.seat, control, value, screen, version=launched.version,
                                               question=question)
        expected = self.harness.control_answer(control, value, screen, version=launched.version, context=context)
        if expected != answer or not answer.keys or answer.control != control.name or answer.value != value:
            return Delivery('withheld', 'control confirmation option/value is not proved')
        reading = self.reading(screen)
        if (reading.reason != screen.interruption.name or reading.open or screen.working or screen.queued
                or (screen.box or '').strip()):
            return Delivery('withheld', str(reading))
        final = self.screen()
        if (final is None or not isinstance(final.interruption, ControlInterruption)
                or final.interruption.control != control.name):
            return Delivery('withheld', 'control question changed; no answer sent')
        if hashlib.sha256(final.text.encode()).hexdigest() != question:
            return Delivery('withheld', 'control question changed; no answer sent')
        final_context = self.harness.control_context(self.seat, control, value, final, version=launched.version,
                                                     question=question)
        if (final_context != context or self.harness.control_answer(
                control, value, final, version=launched.version, context=final_context) != answer):
            return Delivery('withheld', 'control option/context changed; no answer sent')
        final_reading = self.reading(final)
        if (final_reading.reason != final.interruption.name or final_reading.open
                or final.working or final.queued or (final.box or '').strip()):
            return Delivery('withheld', str(final_reading))
        current = self.seat.pane()
        if (self.seat.launched() != launched or current is None or current.dead
                or (current.id, current.pid) != (pane.id, pane.pid) or verify() is not True):
            return Delivery('withheld', 'control confirmation source changed; no answer sent')
        self.tmux.keys(pane.id, *answer.keys)
        return Delivery('typed', 'program asked; ship answered yes')

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
        if box.matches(screen.box, columns, self.harness.wrap_slack):
            return True
        scroll = getattr(self.harness, 'box_scroll', None)
        height = getattr(pane, 'height', None)
        return bool(scroll and self.harness.box_geometry_known and type(height) is int
                    and screen.box is not None and 0 < len(screen.box.splitlines()) <= height
                    and scroll.matches(box, screen.box, columns, self.harness.wrap_slack))

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
            from . import shutdown
            if self.proof and shutdown.requested() and self.proof.data["phase"] in ("submitted", "queued"):
                return Delivery("pending", "submitted once before service stop; record confirmation remains pending")
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
