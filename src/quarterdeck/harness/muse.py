"""Muse Code's measured native home, record and input interfaces."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..ship import Pool, SeatSpec, ShipError
from .base import Harness, program_version
from .capabilities import EffortCapability
from .home import credential, ship_template
from .limits import Usage
from .types import Control, Keys, Launch
from . import muse_home, muse_program, muse_record, muse_screen

AUTH_PENDING = "Muse startup input gate is not yet composed; live launch and usage held"


class MuseHarness(Harness):
    name = program = "muse"
    native_provider = "meta"
    credential_file = "auth.json"
    launch_unset = ("META_API_KEY", "MUSE_AUTH_PATH", "MUSE_LOGIN")
    check_model = "muse-wonder-1.0"
    check_effort = "minimal"
    minimal_check_task = "Write OK to proof.txt."
    requires_deferred_opening = True
    effort_selectors = ("reasoning_effort",)
    efforts = EffortCapability("levels", ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
                               ("1.4.20",), complete=False)
    home_variable = None
    opening_prompt_turn = True
    private_version_home = True
    styled_screen = True
    exact_input_text = True
    strict_usage_interval = True
    usage_interval = 1800
    background_usage = True
    reads_usage = False  # never demand-read a destination gate
    keys = Keys(interrupt="Escape")
    human_tools = ("request_user_input",)
    human_tool_policy = "--disable-request-user-input outside human sessions; effective tool policy awaits offline proof."
    reader = staticmethod(muse_record.Reader.read)
    records = staticmethod(muse_record.records)
    first_timestamp = staticmethod(muse_record.first_timestamp)
    session_id = staticmethod(muse_record.session_id)
    inputs = staticmethod(muse_record.inputs)

    def accepts(self, pool: Pool) -> str | None:
        if pool.kind != "subscription" or pool.provider != "meta":
            return "Muse currently requires a Meta subscription pool; key and endpoint behavior is unproved"
        return None

    def check_defaults(self, seat: SeatSpec, ship_home: Path | None = None) -> None:
        super().check_defaults(seat, ship_home)
        self._settings(seat, ship_template(ship_home, "settings.json"))

    def check_model_reason(self, model: str | None) -> str | None:
        if muse_home.contributor(model):
            return "Muse contributor model is not allowed for a seat or check; no launch"
        return None

    def preflight(self, home: Path | None = None, cwd: Path | None = None) -> None:
        from ..seat import program_path
        muse_program.binary(path=program_path())
        if home is not None:
            muse_home.directory(home)

    def selected_program(self) -> Path | None:
        from ..seat import program_path
        try:
            return muse_program.binary(path=program_path())
        except ShipError:
            return None

    def version(self, home: Path | None = None) -> str | None:
        if home is None:
            return None  # a marker selects an executable, not an executed version proof
        from ..seat import program_path
        try:
            program = muse_program.binary(path=program_path())
            if home.is_symlink() or not home.is_dir() or any(home.iterdir()):
                return None
        except (ShipError, OSError):
            return None
        # A version observation gets the same native off setting as a seat;
        # it never receives a model, credential or opening prompt.
        muse_home.prepare(home)
        from .. import durable
        durable.replace_file(home / "config/muse/settings.json",
                             json.dumps(dict(schema_version=1, contributor_mode_enabled=False)) + "\n")
        environment = muse_home.environment(home)
        # Independent help/version measurements found no writes or auth load.
        return program_version(("env", "-i", f"PATH={program_path()}",
                                *(f"{key}={value}" for key, value in environment.items()),
                                str(program), "--version"))

    def _settings(self, seat: SeatSpec, template: dict) -> dict:
        settings = muse_home.configuration(seat.model, seat.effort, template, seat.settings)
        provider = seat.settings.get("provider", template.get("provider", "meta"))
        if provider not in ("echo", "meta"):
            raise ShipError("Muse provider must be echo or meta")
        settings["provider"] = provider
        return settings

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None):
        settings = self._settings(seat, ship_template(ship_home, "settings.json"))
        self.preflight(home, seat.cwd)
        # law: one-folder -- only the native HOME/XDG roots, without an adapter jail.
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        muse_home.prepare(home)
        path = home / "config/muse/settings.json"
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ShipError(f"{path}: Muse settings are not an owned regular file")
        muse_home.write_settings(home, settings)
        if settings["provider"] == "echo":
            return None
        if pool.config_dir is None or not (pool.config_dir / "auth.json").is_file():
            raise ShipError("Muse pool needs its native config_dir containing auth.json; sign in there first")
        attached = home / "auth.json"
        native = home / "config/muse/auth.json"
        for link in (attached, native):
            if link.exists() and not link.is_symlink():
                raise ShipError(f"{link}: detached Muse credential retained; needs credential review before launch")
        result = credential(pool, attached, "META_API_KEY")
        if native.is_symlink():
            if native.readlink() != attached:
                raise ShipError(f"{native}: Muse credential link has another target; needs credential review")
        else:
            native.symlink_to(attached)
        return result

    def preflight_resume(self, seat, home, resume_id) -> None:
        raise ShipError("Muse resume brief carriage is unmeasured; existing launch retained")

    def launch(self, seat, pool, home, brief, resume, first, *, offline: bool = False) -> Launch:
        if offline:
            raise ShipError('Muse has no adapter-owned offline wrapper; use the ship jail or test tool')
        if resume:
            raise ShipError("Muse resume brief carriage is unmeasured; no launch")
        self.preflight(home, seat.cwd)
        settings_path = home / "config/muse/settings.json"
        if settings_path.is_symlink():
            raise ShipError("Muse launch settings are not an owned regular file")
        try:
            settings = json.loads(settings_path.read_text())
        except (OSError, ValueError):
            raise ShipError("Muse launch settings are unreadable") from None
        settings = self._settings(seat, settings)
        if muse_home.seeded_settings(home) is None:
            raise ShipError('Muse own contributor-off settings changed since framework seed; no launch')
        from ..seat import program_path
        program = muse_program.binary(path=program_path())
        argv = [str(program), "--provider", settings["provider"], "--model", settings["model"],
                "--contributor-mode", "off", "--approval-mode", "never", "--approval-judge", "off",
                "--disable-approval", "--disable-sandbox", "--trust-workspace",
                "-c", "context.foreign_personal_rules=false", "-c", "context.foreign_personal_skills=false"]
        effort = settings.get("reasoning_effort")
        if effort:
            argv += ["--reasoning-effort", effort]
        if not seat.human_session:
            argv += ["--disable-request-user-input"]
        # No system-instruction switch was measured: the complete brief and
        # opening messages wait for the startup proof and the ordinary Door.
        prompt = "\n\n".join(part for part in (brief.read_text(), first) if part)
        environment = muse_home.environment(home)
        # Remove inherited operator sign-in redirects without assuming whether
        # this native version implements them. QD_* and transport env survive.
        return Launch(tuple(argv), environment,
                      notice=self.workspace_notice(home, seat.cwd), deferred_opening=prompt)

    def opening_readiness(self, home, screen):
        if screen is None or screen.starting:
            return False, 'Muse startup screen unavailable; no opening input'
        if re.search(muse_screen.label('Contributor mode is on'), screen.text):
            return False, 'Muse contributor mode is on; launch opening held; needs a human'
        if (screen.unknown is not None or screen.interruption is not None
                or screen.working is not False or screen.box != ''):
            return False, 'Muse startup is not a measured empty idle composer; no opening input'
        if re.search(muse_screen.label(muse_screen.CONTRIBUTOR_OFF), screen.text):
            muse_home.remember_off(home)
            return True, 'Muse contributor-off proof: positive off line on this launch'
        if muse_home.previously_off(home) and muse_home.seeded_settings(home) is not None:
            return True, 'Muse contributor-off proof: same proved home, unchanged framework off setting and noncontributor model'
        return False, 'Muse contributor-off proof unavailable; no opening input'

    def opening_current(self, home):
        return muse_home.seeded_settings(home) is not None

    def owns_launch(self, argv) -> bool:
        native = tuple(argv)
        return bool(native and re.fullmatch(r"muse-bin-" + muse_program.RELEASE.pattern, Path(native[0]).name))

    def workspace_notice(self, home, cwd) -> str:
        return ("Muse trusts the declared workspace; workspace AGENTS.md and native tooling may load. "
                "Foreign personal context keys are set off; interactive effect awaits validation. "
                "Muse 1.4.20 uses this seat's HOME and XDG roots. "
                "Its own settings seed contributor_mode_enabled=false; the contributor-on screen holds all input. "
                "Subscription auth is linked to the declared pool; native rewrite through that link was observed. "
                "Resume brief carriage remains unproved.")

    def screen(self, text):
        return muse_screen.read(text)

    def box_columns(self, pane_width):
        return pane_width - 2  # prefix-derived bound; native wrap budget is separate

    def box(self, text):
        return muse_screen.VisibleBox(text)

    def input_obstruction(self, text, screen):
        return muse_screen.input_shape_reason(text)

    def expected_input(self, item, text):
        if isinstance(item, muse_record.CollapsedInput):
            return None  # collapsed/final-LF projection has not been measured at 1.4.20
        return text

    def queues_while_working(self, seat):
        # S3 measured steer/new-run behavior, but its exact queue receipt
        # fields are not yet supplied. Preserve uncertainty instead of sending
        # a message this reader cannot confirm while it sits in the inbox.
        return False

    def controls(self):
        return (Control("model", "--model {value}", None, "record"),
                Control("effort", "--reasoning-effort {value}", None, "record"))

    def interruptions(self):
        return (*muse_screen.MODEL_CHOICES, muse_screen.TRUST)

    def delivery_obstruction(self, state, screen):
        if screen is not None and screen.interruption in muse_screen.MODEL_CHOICES:
            return f"{screen.interruption.name}; all input held; needs a human"
        if ((state is not None and muse_home.contributor(state.model))
                or (screen is not None and muse_home.contributor(screen.shows.get("model")))):
            return "Muse selected a contributor model; delivery held; needs a human"
        if screen is None:
            return "Muse screen activity unavailable; input held"
        if screen.interruption is not None:
            return screen.interruption.name
        if screen.unknown is not None:
            return f"unknown dialog: {screen.unknown}"
        if screen.starting:
            return "starting, waiting for the input box"
        if screen.box is None:
            return "box not found on the screen"
        if screen.working is None:
            return "current activity unknown"
        return None

    def interrupt_obstruction(self, state, screen):
        if (screen is None or screen.working is None or screen.unknown is not None or screen.starting
                or screen.interruption is not None or screen.box is None):
            return "Muse screen is not a clear working composer; no Escape sent"
        active = state.turn_open if state is not None else None
        if active is None and screen is not None and screen.unknown is None and screen.box is not None:
            active = screen.working
        return None if active is True else "Muse current activity is not proved working; no Escape sent"

    def usage(self, pool):
        return Usage((), datetime.now(timezone.utc),
                     'Muse native periodic usage client awaits auth-link review; no call made', called=False)

    def check_usage_in_home(self, pool, home):
        from . import muse_usage
        from ..seat import program_path
        self.preflight(home)
        if muse_home.seeded_settings(home) is None:
            return Usage((), datetime.now(timezone.utc), 'Muse check settings changed; no usage call', called=False)
        return muse_usage.read_native(muse_program.binary(path=program_path()), home,
                                      {"PATH": program_path(), **muse_home.environment(home)})
