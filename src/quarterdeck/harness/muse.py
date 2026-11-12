"""Muse Code: registered offline launch, with live authentication still unproved."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..ship import Pool, SeatSpec, ShipError
from .base import Harness, program_version
from .home import ship_template
from .limits import Usage
from .types import Control, Keys, Launch
from . import muse_home, muse_mask, muse_program, muse_record, muse_screen

AUTH_PENDING = "Muse authentication location and refresh metadata are unproved; live launch and usage held"
NATIVE_ENV = ("/usr/bin/env", "-u", "META_API_KEY", "-u", "MUSE_AUTH_PATH", "-u", "MUSE_LOGIN", "--")


class MuseHarness(Harness):
    name = program = "muse"
    native_provider = "meta"
    home_variable = None
    opening_prompt_turn = True
    private_version_home = True
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

    def preflight(self, home: Path | None = None, cwd: Path | None = None) -> None:
        if home is None:
            raise ShipError(AUTH_PENDING)  # doctor must not say the program holds a proved sign-in
        muse_mask.preflight(home)

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
        if settings["provider"] != "echo":
            raise ShipError(AUTH_PENDING)
        self.preflight(home, seat.cwd)
        # law: one-folder -- every native root and fixed-path mask belongs here.
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("user-home", "config/muse", "data", "cache", "state"):
            path = home / name
            muse_mask.real_directory(path, missing=True)
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        muse_mask.prepare(home)
        path = home / "config/muse/settings.json"
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ShipError(f"{path}: Muse settings are not an owned regular file")
        path.write_text(json.dumps(settings, indent=2) + "\n")
        return None  # no credential path or credential freshness is invented

    def preflight_resume(self, seat, home, resume_id) -> None:
        raise ShipError("Muse resume brief carriage is unmeasured; existing launch retained")

    def launch(self, seat, pool, home, brief, resume, first, *, offline: bool = False) -> Launch:
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
        if settings["provider"] != "echo":
            raise ShipError(AUTH_PENDING)
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
        # No system-instruction switch was measured: carry the complete brief
        # and opening messages as the fresh positional prompt, one native turn.
        prompt = brief.read_text() + ("\n\n" + first if first else "")
        argv.append(prompt)  # law: context-is-cache
        environment = muse_home.environment(home)
        environment["TMPDIR"] = str(home / "mask/tmp")
        # Remove inherited operator sign-in redirects without assuming whether
        # this native version implements them. QD_* and transport env survive.
        return Launch(muse_mask.command(home, seat.cwd, (*NATIVE_ENV, *argv), offline=offline), environment,
                      notice=self.workspace_notice(home, seat.cwd))

    def owns_launch(self, argv) -> bool:
        native = muse_mask.native_argv(argv)
        if native is not None and native[:len(NATIVE_ENV)] == NATIVE_ENV:
            native = native[len(NATIVE_ENV):]
        else:
            return False
        return bool(native and re.fullmatch(r"muse-bin-" + muse_program.RELEASE.pattern, Path(native[0]).name))

    def workspace_notice(self, home, cwd) -> str:
        return ("Muse trusts the declared workspace; workspace AGENTS.md and native tooling may load. "
                "Foreign personal context keys are set off; interactive effect awaits validation. "
                "Fixed write paths and passwd-home auth fallback are masked into this seat; offline proof pending. "
                "Only echo is enabled; live credentials and resume remain unproved.")

    def screen(self, text):
        return muse_screen.read(text)

    def box_columns(self, pane_width):
        return None  # wrapping/folding is not established

    def box(self, text):
        return muse_screen.VisibleBox(text)

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
        return None

    def interrupt_obstruction(self, state, screen):
        if (screen is None or screen.unknown is not None or screen.starting
                or screen.interruption is not None or screen.box is None):
            return "Muse screen is not a clear working composer; no Escape sent"
        active = state.turn_open if state is not None else None
        if active is None and screen is not None and screen.unknown is None and screen.box is not None:
            active = screen.working
        return None if active is True else "Muse current activity is not proved working; no Escape sent"

    def usage(self, pool):
        return Usage((), datetime.now(timezone.utc), AUTH_PENDING, called=False)
