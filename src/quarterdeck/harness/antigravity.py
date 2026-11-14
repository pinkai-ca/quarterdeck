"""Antigravity (``agy``), record/screen contracts on 1.5.2 and 1.5.5; input geometry on 1.5.9.

The program has no home switch: its state lives under ``~/.gemini/antigravity-cli``, so a seat passes
its settings on the command line and joins the one conversation that appears after its launch, and
only a record proven to be the seat's own is ever opened (law: one-record). A conversation is a
SQLite database in WAL mode holding protobuf blobs. It is read from its bytes into an in-memory
snapshot, so nothing is written beside the program's files (law: one-folder). The fields read are
listed in docs/ANTIGRAVITY.md.
"""

from __future__ import annotations

from .base import SetupMetadata
from .capabilities import DefaultMetadata, EffortCapability, SuggestedValue

from dataclasses import replace
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..ship import Pool, SeatSpec
from .antigravity_quota import banner, fetch_usage, unknown
from .antigravity_record import OWNED, _CACHE, _parse_usage, first_timestamp, inputs, read, records, session_id, state, turns
from .base import Box, BoxScroll, Control, Credential, Harness, Interruption, Join, Keys, Launch, Screen, ship_template, trimmed
from .limits import Usage

STATE_DIR = Path.home() / ".gemini" / "antigravity-cli"
BEFORE = "conversations.before"  # written in the seat home at launch: the conversations that already existed

RULE = re.compile(r"^─{20,}\s*$")
# 1.5.5, measured: after a turn the program may draw a survey about itself in place of the input box, the footer kept.
# The ship gives no rating: the door presses the program's own "[0] Skip", chosen by its text. The program asks for no
# mouse reports and ignores a click, so the choice is pressed by the key its own label names. Measured: one press
# draws the empty box. The program may ask again later in the same launch.
FEEDBACK_SURVEY = (r"(?m)^ How's the CLI experience so far\? Help us improve:\s*\n"
                   r" \[1\] Good +\[2\] Fine +\[3\] Bad +\[0\] Skip\s*$")
SURVEY = Interruption("feedback survey about the program (How's the CLI experience so far?)", FEEDBACK_SURVEY,
                      click="[0] Skip", by_label=True, recurs=True)
# The survey in any other form: named for a person, and nothing is pressed.
SURVEY_ALTERED = Interruption("feedback survey about the program, not as measured; nothing pressed",
                              r"How's the CLI experience")
BG_TASK = re.compile(r"^\s*● \[\d{2}:\d{2}:\d{2}\] .+$")  # a background task's line, drawn in a box of its own
STATUS_LINE = re.compile(r"(?:^|\s{2,})([A-Z]\w*(?: [\w().-]+)*) · (low|medium|high|xhigh|max)(?: · \d+ task\(s\) · /tasks)?\s*$")
WORKING = re.compile(r"esc to cancel|^[⠁-⣿]\s+\w+ing", re.M)
QUEUED_PLACEHOLDER = "Press up to edit queued messages"
QUEUED_LINE = re.compile(r"^▸ (.*?)\s*\(alt\+m to send immediately\)\s*$")


class AntigravityHarness(Harness):
    waking_inapplicability = "Google pool reading"
    human_tool_policy = "Antigravity: no supported native question-tool exclusion proved; interactive asks still need a human."
    efforts = EffortCapability()  # CLI vocabulary is not measured
    defaults = DefaultMetadata(model_default=True, model=SuggestedValue(None, "program-default"),
                               effort=SuggestedValue(None, "program-default"))

    name = "antigravity"
    display_name = 'Antigravity'
    setup = SetupMetadata(seat_limit=1)
    native_provider = "google"
    opening_prompt_turn = True
    answers_questions = True  # its feedback survey, skipped by the ship's orders: no rating is given
    program = "agy"
    sign_in_command = ("agy",)
    sign_in_note = ("Current docs describe browser sign-in on launch with shared OS-keyring storage. "
                    "A separate login subcommand and credential-home selector are unknown; ask the person "
                    "for the installed version's sign-in steps.")
    sign_in_sources = ("https://antigravity.google/docs/install?tab=cli",)
    home_variable = None  # no home switch: the seat runs in the program's own state directory
    reads_usage = True
    usage_interval = 300
    background_usage = True
    model_scoped_usage = True
    model_scoped_quota = True
    headline_window = ("Gemini 4 Argon", "7d")
    screen_walls = True
    box_geometry_known = True
    wrap_slack = 1  # 1.5.9: words fit through 108/76 columns at pane widths 112/80; hard wraps use 109/77
    box_scroll = BoxScroll(mark=r'↑ (?P<hidden>[1-9][0-9]*) more lines', anchor='bottom', placement='row')
    record_limits = ("cost is unknown unless the record states it; effort and child-turn counts are not exposed; "
                     "a step without a completion time leaves its duration unknown")
    keys = Keys(submit="Enter", send_now=("M-m",), interrupt="Escape")

    def usage_scope(self, group: str | None, model: str | None) -> bool | None:
        from .antigravity_models import applies
        return applies(group, model)

    def model_matches(self, requested: str, observed: str | None) -> bool | None:
        from .antigravity_models import same_model
        return same_model(requested, observed)

    def screen_model_consistent(self, requested: str, shown: str | None, effort: str | None = None) -> bool | None:
        from .antigravity_models import screen_consistent
        return screen_consistent(requested, shown, effort)

    def model_identity(self, value: str | None, *, version: str | None = None) -> str | None:
        from .antigravity_models import MODEL_IDENTITIES
        value = super().model_identity(value, version=version)
        return MODEL_IDENTITIES.get(value, value)

    def quota_scope(self, event, model: str | None) -> bool | None:
        from .antigravity_models import event_applies
        return event_applies(event, model)

    def quota_scope_label(self, event) -> str:
        return event.group or (f"model {event.model}" if event.model else
                               "all models" if event.all_models else "scope unknown")

    def is_headline(self, group: str | None, window: str) -> bool:
        return True  # Every reported group/bucket, including a retained unknown measurement.

    def usage(self, pool: Pool) -> Usage:
        if pool.config_dir is not None:
            return replace(unknown("a Google account directory is not read; /usage reads the sign-in the program itself holds",
                                   datetime.now(timezone.utc)), called=False)
        return fetch_usage()

    def shared_usage_key(self, pool: Pool) -> tuple | None:
        return (self.name, "default-sign-in") if pool.config_dir is None else None

    def quota_wall(self, text: str, at: datetime):
        from .antigravity_quota import wall
        return wall(text, "individual-quota", at)

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        """Our settings for the session, passed on the command line; nothing of the program's own is written."""
        self.check_defaults(seat, ship_home)
        home.mkdir(parents=True, exist_ok=True)
        settings = {"trustedWorkspaces": [str(seat.cwd)] if seat.cwd else []}
        settings = {**ship_template(ship_home, "settings.json"), **settings}
        settings.update(seat.settings)
        (home / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
        return None

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        argv = [self.program, "--dangerously-skip-permissions", "--settings", str(home / "settings.json")]
        if seat.model:
            argv += ["--model", seat.model]
        if seat.effort:
            argv += ["--effort", seat.effort]
        if resume:
            argv += ["--conversation", resume]
        opening = "\n\n".join(filter(None, [brief.read_text().strip() if not resume else None, first]))
        if opening:
            argv += ["--prompt-interactive", opening]
        if not resume:  # a new conversation is about to appear: note which ones are there already
            (home / BEFORE).write_text("".join(f"{p.name}\n" for p in sorted(self._conversations())))
        return Launch(tuple(argv), {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"})

    # -- record: the one conversation that appeared after the launch ---------------------------

    @staticmethod
    def _conversations() -> list[Path]:
        return [p for p in (STATE_DIR / "conversations").glob("*.db") if p.is_file()]

    def _appeared(self, home: Path) -> list[Path]:
        """The conversations the shared folder gained since the launch noted what was there."""
        noted = set((home / BEFORE).read_text().split()) if (home / BEFORE).is_file() else set()
        return sorted((p for p in self._conversations() if p.name not in noted), key=lambda p: p.stat().st_mtime)

    def join(self, home: Path, launched_at: datetime, excluded: tuple[Path, ...] = ()) -> Join:
        """The one conversation that appeared since the launch; two are reported, never guessed."""
        appeared = [p for p in self._appeared(home) if p not in excluded]
        if not appeared:
            return Join(None, "no record yet")
        if len(appeared) > 1:
            return Join(None, f"ambiguous: {', '.join(p.name for p in appeared)} appeared since the launch")  # law: one-record
        return Join(appeared[0])

    inputs = staticmethod(inputs)
    records = staticmethod(records)
    first_timestamp = staticmethod(first_timestamp)
    session_id = staticmethod(session_id)
    reader = staticmethod(read)

    # -- screen ---------------------------------------------------------------------------

    def screen(self, text: str) -> Screen:
        lines = text.rstrip().split("\n")
        rules = [i for i, line in enumerate(lines) if RULE.match(line)]
        box, queued = None, 1 if QUEUED_PLACEHOLDER in text else 0  # the placeholder sits on the status line here
        top = rules[-2] if len(rules) >= 2 else len(lines)
        if len(rules) >= 2:
            top, bottom = rules[-2], rules[-1]
            tasks = lines[top + 1:bottom]
            if len(rules) >= 3 and tasks and all(BG_TASK.match(line) for line in tasks):
                top, bottom = rules[-3], rules[-2]  # the lowest box holds background tasks; the input box is above it
            span = lines[top + 1:bottom]
            if span and (span[0].rstrip() == ">" or span[0].startswith("> ")):
                body = [line[2:] if line.startswith(("> ", "  ")) else line.lstrip(">") for line in span]
                body = trimmed(body)
                box = "\n".join(body)
                if box.rstrip(" ") == QUEUED_PLACEHOLDER:
                    box, queued = "", max(queued, 1)
        status = STATUS_LINE.search(lines[-1]) if lines else None
        shows = {"model": status.group(1).strip(), "effort": status.group(2)} if status else {}
        return Screen(text, box=box, working=bool(WORKING.search(text)), interruption=self.recognize(text) if box is None else None,
                      queued=queued, shows=shows, echoes=self.echoes(lines[:top]), quota=banner(text))

    @staticmethod
    def echoes(lines: list[str]) -> tuple[str, ...]:
        """Inputs the transcript shows: ``> `` blocks above the box with their indented lines, and a
        queued message's ``▸ …(alt+m to send immediately)`` line, cut short, as an echo ending in an ellipsis."""
        found: list[list[str]] = []
        block: list[str] | None = None
        for line in lines:
            queued = QUEUED_LINE.match(line)
            if line.startswith("> "):
                block = [line[2:]]
                found.append(block)
            elif queued:
                found.append([queued.group(1).rstrip(".") + "…"])
                block = None
            elif block is not None and (line.startswith("  ") or not line.strip()) and not line.lstrip().startswith("⎿"):
                block.append(line[2:] if line.startswith("  ") else "")  # a paragraph break continues the echo; ⎿ is the program's own line
            else:
                block = None
        return tuple("\n".join(b).strip("\n") for b in found)

    def box_columns(self, pane_width: int) -> int:
        # 1.5.9: two prompt/indent cells and one reserved cursor cell.
        # At pane widths 112/80, a 221-character ruler wraps 109+109+3 / 77+77+67.
        return pane_width - 3

    def box(self, text: str) -> Box:
        """Keep the 1.5.2/1.5.5 counters and the measured 1.5.9 forms.

        1.5.9 removes all terminal LF, folds 16 or more content lines to their
        line count, or a single line over 1000 characters to its character count.
        Only those exact additional counts are admitted; record receipts still
        compare the original payload through the ordinary path.
        """
        chars = {len(text.removesuffix('\n'))}
        lines = {text.count('\n')}
        content = text.rstrip('\n')
        count = content.count('\n') + 1
        if count >= 16:
            lines.add(count)
        elif count == 1 and len(content) > 1000:
            chars.add(len(content))
        char_counts = '|'.join(str(value) for value in sorted(chars))
        line_counts = '|'.join(str(value) for value in sorted(lines))
        return Box(text, folded=rf"\[Pasted text #\d+ (?:(?:{char_counts}) chars|\+(?:{line_counts}) lines)\]")

    def queued_input(self, text: str, before: Screen, after: Screen) -> str | None:
        """1.5.5 shows an envelope preview and edit hint before persisting the input.

        This proves queue visibility only after the Door matched the entire paste. Require
        a previously empty queue, the complete transport identity in a unique preview,
        and a now-empty recognized box. A shortened body never proves record delivery.
        """
        if (before.queued or after.box != "" or after.unknown or after.interruption
                or QUEUED_PLACEHOLDER not in after.text):
            return None
        header = text.split("\n", 1)[0]
        identity = re.match(r"^qd > (?:order|report|question|answer|ack) from [^·\n]+ · "
                            r"(?:ord|rep|que|ans|ack)-[0-9a-f]{4}(?= · |$)", header)
        if identity is None:
            return None
        previews = [match.group(1).removesuffix("...").removesuffix("…")
                    for line in after.text.splitlines() if (match := QUEUED_LINE.match(line))]
        if (len(previews) == 1 and previews[0].startswith(identity.group())
                and header.startswith(previews[0])):
            return "native queue preview and edit hint match the submitted envelope; awaiting exact record receipt"
        return None

    def controls(self) -> tuple[Control, ...]:
        return (
            Control("model", launch="--model <value>", session=None, confirmed_by="screen"),
            Control("effort", launch="--effort <value>", session=None, confirmed_by="screen"),
        )

    def interruptions(self) -> tuple[Interruption, ...]:
        return (
            Interruption("trust dialog", r"Do you trust the contents of this project\?", key="Enter"),
            Interruption("login", r"Sign in|Log in to|Authenticate", key=None),
            SURVEY,
            SURVEY_ALTERED,
        )
