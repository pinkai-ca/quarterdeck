"""The Harness interface: everything an adapter knows about one agent program.

An adapter passes Quarterdeck's own files and overrides on the program's command line and
never edits the program's configuration or the user's files. It reads the least that
changes most: the record and the command line carry the facts; the screen is read only
for the box and the dialogs.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from dataclasses import dataclass, replace
from pathlib import Path
from .limits import Usage, Wall, Window

from ..ship import Pool, SeatSpec

from .json_record import RecordError, entries, when
from .home import credential, key_environment, merged, ship_template
from .types import Box, BoxScroll, Control, ControlAnswer, ControlContext, Credential, Input, Interruption, Join, Keys, Launch, PasteClearRule, Screen, State, Tokens, ToolCall, Turn
from .capabilities import (DefaultMetadata, EffortCapability, reject_default_overrides,
                           reject_unclassified_overrides, validate_selection)


def record_fingerprint(record: Path) -> tuple:
    """Presence, inode, size and mtime of a record and of its SQLite write-ahead log."""
    from ..record_location import resolve
    record = resolve(record)
    def stat_of(path: Path) -> tuple:
        try:
            s = path.stat()
        except FileNotFoundError:
            return (False, 0, 0, 0, 0, 0)
        return (True, s.st_ino, s.st_size, s.st_mtime_ns, s.st_dev, s.st_ctime_ns)
    return (stat_of(record), stat_of(Path(f"{record}-wal")))


def trimmed(lines: list[str]) -> list[str]:
    """Drop only trailing blank display rows; preserve text, padding and internal newlines."""
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    return lines[:end]


def same_name(shown: str, wanted: str) -> bool:
    """``Sonnet 5.5`` and ``GPT-6-Astra`` name ``claude-sonnet-5-5`` and ``gpt-6-astra``: compare letters and digits only."""

    def key(name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", name.lower().removeprefix("claude-"))

    a, b = key(shown), key(wanted)
    return bool(a) and (a == b or b.endswith(a) or b.startswith(a))


GRANT_KINDS = (
    "self-renewing",  # an access grant the program renews on use: its short expiry needs no login
    "fixed",  # a grant that needs the human's login at its end
    "unknown",  # not measured: shown as unknown, never counted as a login needed
)


def program_version(argv: tuple[str, ...]) -> str | None:
    """The installed version a program prints for ``--version``, read every time: programs update under a running ship.

    Read from the standard output first and the error stream second: pi 0.74.2 prints its version on the latter.
    """
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for stream in (done.stdout, done.stderr):
        match = re.search(r"(?<![\d.])\d+\.\d+(?:\.\d+)?[A-Za-z0-9.+_-]*(?=\s|$)", stream)
        if match:
            return match.group(0)
    return None


def program_where(harness) -> str | None:
    """The program an adapter settles once, as a seat launches it: its path, and the file a link there reaches."""
    program = harness.selected_program() if hasattr(harness, "selected_program") else None
    if program is None:
        return None
    try:
        target = program.resolve(strict=True)
    except (OSError, RuntimeError):
        target = None
    return f"{program} -> {target}" if target is not None and target != program else str(program)


def _version_in_runtime(harness, runtime: Path) -> str | None:
    """Only the adapter's explicit private-version-home flag creates a context."""
    version = harness.version
    if not getattr(harness, "private_version_home", False):
        return version()
    eligible = True
    try:
        for path in (runtime, runtime / ".git"):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                eligible = False
                break
    except OSError:
        eligible = False
    if not eligible:
        return version()
    try:
        temporary = tempfile.TemporaryDirectory(prefix="version-", dir=runtime)
    except OSError:
        return version()  # Grok's no-context route executes nothing
    with temporary as home:
        return version(home=Path(home))  # never retry a version call that has executed


@dataclass(frozen=True)
class SetupMetadata:
    """Informational setup facts declared by an adapter; no machine/account lookup."""
    display_name: str = ""
    subscription_home: str | None = None
    key_env: str | None = None
    key_provider: str | None = None
    key_endpoint: str | None = None
    anonymous_models_endpoint: str | None = None
    seat_limit: int | None = None
    fresh_after_compactions: int | None = None


class Harness(ABC):
    """One agent program, as the framework knows it."""

    name: str
    program: str
    native_provider: str | None = None
    display_name: str | None = None
    setup = SetupMetadata()
    opening_prompt_turn: bool = False  # a fresh brief is itself a model turn
    waking_inapplicability: str | None = None  # explicit reason to omit only the added live waking step
    synthetic: bool = False
    credential_file: str | None = None
    # A program that writes its refreshed sign-in through the seat's link, into the pool's own file, gets a copy of the
    # pool's file instead: its refresh lands in its own home, and the service alone puts the newest live grant in the
    # pool's place and brings every older copy up to it (credential.reseed).
    credential_copy: bool = False
    # Informational current-vendor-doc hints only: never an executable auth path or version proof.
    sign_in_command: tuple[str, ...] | None = None
    sign_in_home_variable: str | None = None  # only when docs establish credential-home selection
    sign_in_note: str = "Native sign-in command and declared-home behavior unknown; ask the person."
    sign_in_sources: tuple[str, ...] = ()
    usage_interval: int = 60
    background_usage: bool = False
    usage_from_records = None  # optional reader of (joined record, saved launch time) sources
    headline_window: tuple[str | None, str] | None = None
    headline_fallback: tuple[str | None, str] | None = None  # shown when a good read carries no headline window
    screen_walls: bool = False
    home_variable: str | None
    keys: Keys
    # Variables this program sets for what it runs, naming the session that ran it. A seat must not inherit
    # them from whoever started the ship's tmux server: each launch removes every adapter's, but for a name
    # the seat's own adapter sets in its launch.
    inherited_markers: tuple[str, ...] = ()
    # Variables this program's own launch removes from what the seat inherits, where the program reads them
    # to draw a screen other than the one measured. Only the seat's own adapter's go; other programs keep theirs.
    launch_unset: tuple[str, ...] = ()
    # The provider's safeguard can refuse, and so lose, a conversation that reads instructions planted for a reader:
    # such a program never reads the pull-request screen's outsider text ([pr_screen] reader).
    refuses_planted_instructions: bool = False
    reads_usage: bool = False  # True where usage() reads the provider's windows; a pool of such a program is read before a move
    reads_queue_error: bool = False  # state() can report unresolved native queue identity to automatic status
    remote_control: bool = False  # True where the program can open a seat to the operator's phone and claude.ai as well
    wrap_slack: int = 0  # columns the program keeps clear at the right when it wraps at a word, under the width where it breaks a word
    human_tools: tuple[str, ...] = ()
    human_tool_policy: str = "No native question-tool suppression proved for this adapter."
    styled_screen: bool = False  # opt-in SGR evidence in the same atomic identity/screen reading
    private_usage_home: bool = False  # native usage client needs framework-owned settings and state
    private_version_home: bool = False  # version lookup needs a fresh empty runtime-owned home
    record_time_resolution: float = 0.0  # seconds; zero retains exact receipt-time comparisons
    efforts = EffortCapability()
    defaults = DefaultMetadata()
    model_selectors: tuple[str, ...] = ("model",)
    effort_selectors: tuple[str, ...] = ("effort",)
    selection_templates: tuple[str, ...] = ("settings.json",)
    unclassified_default_overrides = True

    def effort_capability(self, version: str | None = None) -> EffortCapability:
        """Historical facts and a distinct exact-version reading; no program call."""
        return replace(self.efforts, version=version,
                       measured=(self.efforts.kind != 'unknown' and version is not None
                                 and version in self.efforts.measured_versions))

    def default_metadata(self) -> DefaultMetadata:
        return self.defaults

    def setup_metadata(self) -> SetupMetadata:
        return replace(self.setup, display_name=self.display_name or self.name)

    def check_defaults(self, seat: SeatSpec, ship_home: Path | None = None) -> None:
        """Read selector conflicts before changing a running seat or its home."""
        validate_selection(seat, self)
        if seat.model_mode != "program-default" and seat.effort_mode != "program-default":
            return
        for filename in self.selection_templates:
            config = ship_template(ship_home, filename)
            reject_default_overrides(seat, config, scope=f"template {filename}",
                                     model_keys=self.model_selectors, effort_keys=self.effort_selectors)
            if self.unclassified_default_overrides:
                reject_unclassified_overrides(seat, config, scope=f"template {filename}")

    def recorded_paste(self, text: str, record: Path, since: datetime) -> str:
        """The form a payload pasted at ``since`` takes in this record, where the adapter has measured a change.

        The default is the payload unchanged. Receipts accept the payload itself or this one form, never
        a near match; a version the adapter has not measured has no form but the payload.
        """
        return text

    def paste_ignored(self, text: str, record: Path, since: datetime) -> frozenset[str]:
        """Characters the adapter measured its program to drop from this payload, pasted at ``since``, as it
        records it, where its context does not keep them. Receipts remove exactly these from both the recorded
        text and each form before comparing. The default is none.
        """
        return frozenset()

    def thinking_recovery(self, home: Path, evidence: dict | None, version: str | None, seat: SeatSpec, pool: Pool):
        """A proved native off/restore client, or no supported automatic recovery."""
        return None

    def compactions(self, record: Path):
        from .compactions import Compactions
        return Compactions()

    def waiting_for_human(self, state: State) -> bool:
        """A complete nonempty question-only set; a displayed first tool proves no exclusivity."""
        return (state.turn_open is True and bool(state.pending_tools)
                and all(name in self.human_tools for name in state.pending_tools))

    def drill_settings(self, slow_acceptance: bool = False) -> dict:
        """Program settings for the synthetic drill; real programs supply none."""
        return {}

    def queues_while_working(self, seat: SeatSpec) -> bool:
        """Whether a message typed during a turn is taken and queued, or refused until it ends."""
        return True

    def accepts(self, pool: Pool) -> str | None:
        """Why this program cannot run on a pool, or None when it can.

        By default a program runs on the pools of its own provider, a subscription or a key; a
        program that speaks to any OpenAI-compatible endpoint says so by taking endpoint pools instead.
        """
        from ..providers import display
        from . import native_name
        company = display(pool.provider)
        native = native_name(pool.provider)
        if pool.kind == "subscription" and (self.native_provider != pool.provider or self.name != native):
            return f"pool {pool.name} is a subscription for {company}; use its native harness {native or '(not installed)'}"
        if pool.kind == "endpoint":
            return f"pool {pool.name} is an endpoint operated by {company}; {self.name} requires its native {display(self.native_provider) if self.native_provider else 'provider'} subscription or key"
        if self.native_provider != pool.provider:
            return f"pool {pool.name} pays {company}; harness {self.name} requires its own provider's key (native harness {native or 'not installed'})"
        return None

    def queued_input(self, text: str, before: Screen, after: Screen) -> str | None:
        """A newly visible native queue after one matched paste, never a record receipt."""
        return None

    def queued_waiting(self, text: str, screen: Screen, *, record: Path | None = None,
                       now: datetime | None = None) -> bool:
        """A previously acknowledged input still visibly queued, awaiting its exact record receipt."""
        return False
    # -- launch ------------------------------------------------------------------------

    @abstractmethod
    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        """Write a seat home from the ship's files: settings, trust, no self-update, nothing of the user's."""

    @abstractmethod
    def launch(
        self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None
    ) -> Launch:
        """The exact command line and environment, with or without resume."""

    def preflight_resume(self, seat: SeatSpec, home: Path, resume_id: Callable[[], str | None]) -> None:
        """Check a program-specific resume requirement before retiring its current launch."""

    def preflight(self, home: Path | None = None, cwd: Path | None = None) -> None:
        """Read-only launch readiness, also used by doctor; raise ShipError to refuse.

        No harness program or provider call and no writes; a bounded local metadata
        query may be used. Omitted context checks only
        global requirements, never an inferred operator home or working directory.
        """

    def workspace_notice(self, home: Path, cwd: Path) -> str | None:
        """Read-only workspace information, never a content-based launch gate.

        The default supplies no notice and performs no inventory or native call.
        An adapter reports incomplete inventory as information, not a refusal.
        """
        return None

    def version(self, home: Path | None = None) -> str | None:
        """Optional empty runtime-owned context; default adapters keep their existing invocation."""
        return program_version((self.program, "--version"))

    paste_clear_rules: tuple[PasteClearRule, ...] = ()

    def paste_clear_rule(self, version: str) -> PasteClearRule | None:
        return next((rule for rule in self.paste_clear_rules if rule.version == version), None)

    def paste_clear_keys(self, text: str, screen: Screen, columns: int, version: str, cursor: tuple[int, int]):
        """An adapter-proved clear for a whole visible unsubmitted paste, or unknown."""
        return None

    def owns_launch(self, argv: tuple[str, ...] | list[str]) -> bool:
        return bool(argv) and Path(argv[0]).name == Path(self.program).name

    def freshness(self, path: Path) -> tuple[float, float] | None:
        return None

    answers_questions: bool = False  # declares a question with a fixed answer (Interruption.click) to watch for
    answer_seconds: float | None = None  # measured clearance bound; None uses the Door's default
    # Versions whose composer drawn just after that question is answered has been measured (see Screen.fresh).
    fresh_versions: frozenset[str] = frozenset()

    def seeded_acknowledgement(self, question: str, launched, home: Path | None) -> bool:
        """The launch record and the home both carry this adapter's own mark that ``question`` was settled before
        the program started, on a version whose composer in that case is measured. None by default."""
        return False
    # How long after its launch a program may take to draw its input box. Within it a frame with no box,
    # no dialog and no unknown layout is the program still starting, not an obstruction.
    start_seconds: float = 60.0

    def question_cleared(self, text: str, question: Interruption) -> bool:
        """After the framework answered ``question``: it is gone and an empty composer is drawn."""
        screen = self.screen(text)
        return screen.interruption is None and screen.box is not None and not screen.box.strip()

    def startup_ready(self, home: Path | None) -> bool:
        """Any program-specific startup decision required before its composer can take input."""
        return True

    def synthetic_ready(self, home: Path) -> bool:
        """A synthetic program may acknowledge this launch instead of a timed observation."""
        return False

    grant_kind: str = "unknown"  # one of GRANT_KINDS: what the pool's grant needs at its end

    def grant_expiry(self, path: Path) -> datetime | None:
        """A fixed grant's end, or a self-renewing grant's short access expiry; read as metadata, never a token."""
        return None

    def renewal_end(self, path: Path) -> datetime | None:
        """A self-renewing grant: the end of the grant that renews it, only from a field the adapter knows by name."""
        return None

    def renewal_failed(self, path: Path) -> bool | None:
        """A self-renewing grant: True only where the file shows its renewal failed; None where it cannot say."""
        return None

    model_scoped_usage = False
    model_scoped_quota = False

    def usage_scope(self, group: str | None, model: str | None) -> bool | None:
        """True when a window applies; None when its model scope cannot be established."""
        return True  # Existing grouped adapters keep their pool-wide semantics.

    def model_matches(self, requested: str, observed: str | None) -> bool | None:
        """Exact identities by default; aliases require adapter-owned native evidence."""
        return requested == observed if observed else None

    def screen_model_consistent(self, requested: str, shown: str | None, effort: str | None = None) -> bool | None:
        """Whether a model the screen shows agrees with the requested one; by default only an exact match does.

        It never confirms a model: the record does. An adapter whose screen shows less than its record
        may call a shown family consistent; a screen naming another model is never consistent.
        """
        return self.model_matches(requested, shown)

    def quota_scope(self, event: Wall, model: str | None) -> bool | None:
        """A refusal at a model group's limit applies as that group's window does; one with no group, to every model."""
        return True if event.group is None else self.usage_scope(event.group, model)

    def quota_scope_label(self, event: Wall) -> str | None:
        """An event's scope label, where the adapter distinguishes native quota models."""
        return None

    def shared_usage_key(self, pool: Pool) -> tuple | None:
        return None

    def price_turn(self, turn: Turn, snapshot: dict | None) -> Turn:
        """A record's stated cost by default; adapters that synthesize costs qualify theirs."""
        return turn

    def headline_windows(self) -> tuple[tuple[str | None, str], ...]:
        return (self.headline_window,) if self.headline_window else ()

    def is_headline(self, group: str | None, window: str) -> bool:
        return (group, window) in self.headline_windows()

    def quota_wall(self, text: str, at: datetime) -> Wall | None:
        return None
    # -- record ------------------------------------------------------------------------

    retirement_process_names: tuple[str, ...] | None = None

    def retirement_bundle(self, home: Path, record: Path, kept: Path):
        """An atomically movable (source, destination, files) bundle, else unsupported.

        The adapter alone recognizes its native layout and ancillary data. Generic
        consumers require accounted, old, closed sources before using this bundle.
        """
        return None

    @abstractmethod
    def records(self, home: Path) -> list[Path]:
        """Every record file in a home, oldest first."""

    @abstractmethod
    def first_timestamp(self, record: Path) -> datetime | None:
        pass

    @abstractmethod
    def session_id(self, record: Path) -> str | None:
        pass

    reader = None

    @classmethod
    def state(cls, record: Path) -> State:
        return cls.read(record)[0]

    def state_for_relaunch(self, record: Path, launched_at: datetime | None) -> State:
        """Launch-scoped relaunch admission; ordinary adapters retain their record evidence."""
        return self.state(record)

    @classmethod
    def turns(cls, record: Path) -> list[Turn]:
        return cls.read(record)[1]

    @classmethod
    def read(cls, record: Path) -> tuple[State, list[Turn]]:
        """The live state and ended turns from one reading by this adapter's reader."""
        from . import reading_scope
        return reading_scope.read(cls, record, lambda: cls.reader(record))

    def inputs(self, record: Path) -> list[Input]:
        """Timestamped owned-record evidence; optional ``skipped`` diagnostics contain no row contents."""
        raise NotImplementedError

    def attribution_inputs(self, record: Path) -> list[Input]:
        """Recorded message consumption for attribution; never broadens delivery receipts."""
        return self.inputs(record)

    def person_inputs(self, record: Path, excluded: tuple[str, ...]) -> list[datetime] | None:
        """When the person typed into a human seat, from its record; None where this adapter declares
        no way to tell the program's own notices from the person's input, so quiet time stays unknown."""
        return None

    def join(self, home: Path, launched_at: datetime, excluded: tuple[Path, ...] = ()) -> Join:
        """The first record written after the launch; ambiguity reported, never guessed."""
        after = []
        for record in self.records(home):
            from ..record_location import retired
            if record in excluded or retired(record):
                continue
            first = self.first_timestamp(record)
            if first is not None and first >= launched_at:
                after.append((first, record))
        if not after:
            return Join(None, "no record yet")
        after.sort()
        if len(after) > 1 and after[1][0] == after[0][0]:
            names = ", ".join(r.name for _, r in after[:2])
            return Join(None, f"ambiguous: {names} were first written at the same moment")  # law: one-record
        return Join(after[0][1])
    # -- screen ------------------------------------------------------------------------

    @abstractmethod
    def screen(self, text: str) -> Screen:
        pass

    box_geometry_known = False  # adapters opt in only with an established text-column declaration
    box_scroll: BoxScroll | None = None  # no partial viewport without a measured native mark and anchor
    minimum_pane_heights: dict[str, int] = {}  # measured empty-box heights, by installed version; diagnostic only

    def minimum_pane_rows(self, text: str, pane_width: int, version: str | None) -> int | None:
        """A measured pane bound for this payload and version, separate from text-only row bounds."""
        return None

    def missing_box_known(self, screen: Screen) -> bool:
        """An otherwise recognized screen whose missing box can get a geometry diagnostic."""
        return False

    def minimum_box_rows(self, text: str, pane_width: int) -> int | None:
        """Diagnostic only; the ordinary Box comparator still decides whether the whole paste is shown."""
        if not self.box_geometry_known:
            return None
        return self.box(text).minimum_rows(self.box_columns(pane_width))

    def box_columns(self, pane_width: int) -> int | None:
        """Text-area columns for a recognized ordinary box, never a guessed width."""
        return pane_width - 2

    def box_columns_for_screen(self, pane_width: int, screen: Screen) -> int | None:
        """An adapter may measure its composer from this already-read screen."""
        return self.box_columns(pane_width)

    def box_comparison(self, screen: Screen, pane_width: int) -> str | None:
        """The observed box, with only adapter-proved outside padding excluded."""
        return screen.box

    def human_wake_box_margin(self, pane_width: int) -> int | None:
        """Proved non-input columns for one waking row; unknown refuses waking."""
        return None

    def empty_cell(self, screen: Screen) -> tuple[int, int] | None:
        """Where the terminal cursor sits, 0-based (column, row), in this program's empty composer, for a box the
        reader read as padding only: a program that draws typed spaces like its padding is read empty only with
        the cursor there (the Door reads it). None, the default, leaves the box as the reader read it."""
        return None

    def check_tool_evidence(self, record: Path) -> dict:
        """Private check-only call/results; adapters opt in to a bounded decoder."""
        return dict(status='unknown', reason='adapter tool call/results unsupported', pairs=[])

    @abstractmethod
    def box(self, text: str) -> Box:
        pass

    def controls(self) -> tuple[Control, ...]:
        return ()

    def interruptions(self) -> tuple[Interruption, ...]:
        return ()

    def control(self, name: str) -> Control | None:
        return next((c for c in self.controls() if c.name == name), None)

    def model_identity(self, value: str | None, *, version: str | None = None) -> str | None:
        """Literal identifiers by default; only an adapter may declare proved display aliases."""
        if (not isinstance(value, str) or not value or len(value) > 200
                or value != value.strip() or not all(char.isprintable() for char in value)):
            return None
        return value

    def control_context(self, seat, control: Control, value: str, screen: Screen,
                        *, version: str | None = None, question: str | None = None) -> ControlContext | None:
        """Current mode/focus/display provenance; unsupported or unknown returns no authority."""
        return None

    def record_model(self, record: Path) -> str | None:
        """The adapter's native model identity from this record, not a display-name guess."""
        state = self.state(record)
        return self.model_identity(state.model)

    def listed_models(self, home: Path, *, version: str | None = None) -> dict[str, str] | None:
        """The program's measured native list; None means unknown, never an empty list."""
        return None

    def model_control_origin(self, seat, record: Path | None, value: str) -> dict | None:
        """Request-specific record boundary and display binding, or unsupported."""
        return None

    def model_control_join(self, seat, record: Path, origin: dict) -> dict | None:
        """Resolve a supported initially absent record through this seat's ordinary one-record join."""
        return origin if origin.get('path') is not None else None

    def model_control_result(self, seat, record: Path, value: str, origin: dict) -> str | None:
        """A new result linked to this request's exact command, never an old model reading."""
        return None

    def model_control_current(self, record: Path, origin: dict) -> bool | None:
        """Whether native input since the command boundary still permits its confirmation key."""
        return None

    def control_answer(self, control: Control, value: str, screen: Screen,
                       *, version: str | None = None, context: ControlContext | None = None) -> ControlAnswer | None:
        """A value-matched option and proved keys, or no authority to answer this frame."""
        return None

    def recognize(self, screen: str) -> Interruption | None:
        return next((i for i in self.interruptions() if i.seen_in(screen)), None)

    def confirms(self, control: Control, value: str, screen: Screen, state: State) -> bool | None:
        """Whether the program's resulting state shows the change; None when it cannot be read."""
        same = same_name if control.name == "model" else (lambda shown, wanted: shown == wanted)
        if control.confirmed_by == "screen":
            shown = screen.shows.get(control.name) if screen else None
            return None if shown is None else same(shown, value)
        if control.confirmed_by == "record":
            reported = getattr(state, control.name, None) if state else None
            return None if reported is None else same(reported, value)
        return None
    # -- usage -------------------------------------------------------------------------

    def usage(self, pool: Pool) -> Usage | None:
        return None

    def usage_in_home(self, pool: Pool, home: Path) -> Usage | None:
        """A native usage client's owned context; ordinary readers keep their old call."""
        return self.usage(pool)
