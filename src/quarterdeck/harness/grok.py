"""Grok Build: a private GROK_HOME, native ACP records and program-owned billing."""

from __future__ import annotations

from .base import SetupMetadata
from .capabilities import DefaultMetadata, EffortCapability, SuggestedValue

import copy
import json
import os
import re
import shutil
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from ..ship import Pool, SeatSpec, ShipError, literal
from .. import durable
from .base import Harness, program_version
from .home import credential, key_environment, merged, ship_template
from .json_record import when
from .limits import Usage
from .types import Box, Control, Credential, Interruption, Keys, Launch, Screen
from . import grok_notice, grok_record, grok_screen, grok_usage, grok_workspace

VENDORS = ("claude", "cursor", "codex")
IMPORTS = ("rules", "hooks", "mcps", "skills", "agents", "sessions")
COMPAT_ENV = {f"GROK_{vendor.upper()}_{kind.upper()}_ENABLED": "0"
              for vendor in VENDORS for kind in IMPORTS}
ENV = {**COMPAT_ENV, "GROK_DISABLE_AUTOUPDATER": "1", "GROK_PROMPT_SUGGESTIONS": "0",
       "GROK_MEMORY": "0", "GROK_DISABLE_EXPERIMENTS": "1", "GROK_SUBAGENT_BUNDLE": "0",
       "GROK_TELEMETRY_ENABLED": "0", "GROK_TELEMETRY_TRACE_UPLOAD": "0", "DISABLE_TELEMETRY": "1",
       "GROK_STORAGE_MODE": "local"}
MCP_POLICY_KEYS = ("allow_managed_mcp_servers_only", "enable_all_project_mcp_servers", "allowed_mcp_servers",
                   "allowManagedMcpServersOnly", "enableAllProjectMcpServers", "allowedMcpServers")
SETTINGS = {
    "cli": {"auto_update": False, "use_leader": False},
    "compat": {vendor: {kind: False for kind in IMPORTS} for vendor in VENDORS},
    "keys": {"vim": "off"},
    "memory": {"enabled": False},
    "memory_v2": {"enabled": False, "batch_dream_enabled": False},
    "features": {"managed_config": False, "telemetry": False, "subagent_bundle": False},
    "telemetry": {"trace_upload": False},
    "relay": {"enabled": False},
    "ui": {"prompt_suggestions": False, "multiline_default": False,
           "follow_up_behavior": "queue", "combine_queued_prompts": False,
           "screen_mode": "fullscreen", "permission_mode": "always-approve"},
}
SYSTEM_CONFIG = Path("/etc/grok")
INHERITED_REDIRECTS = ("GROK_AGENT", "GROK_MANAGED_CONFIG_URL", "GROK_FOLDER_TRUST", *grok_workspace.GIT_REDIRECTS)


def present(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise ShipError(f"{path}: native configuration presence is unreadable") from None


def no_admin_layers(home: Path | None) -> None:
    paths = [SYSTEM_CONFIG / "managed_config.toml", SYSTEM_CONFIG / "requirements.toml"]
    if home is not None:
        paths += [home / "managed_config.toml", home / "requirements.toml"]
    for path in paths:
        if present(path):
            raise ShipError(f"{path}: unmanaged Grok admin configuration; retained; resolve before launch")


def private_environment(home: Path) -> dict[str, str]:
    user_home = home / "user-home"
    return {**ENV, "GROK_HOME": str(home), "HOME": str(user_home),
            "XDG_CONFIG_HOME": str(user_home / "config"), "XDG_DATA_HOME": str(user_home / "data"),
            "XDG_CACHE_HOME": str(user_home / "cache"), "XDG_STATE_HOME": str(user_home / "state"),
            # Guide 05: a nonempty JSON overlay wins over GROK_CONFIG_PATH. Neither
            # inherited redirect is allowed to select an operator-owned overlay.
            "GROK_CONFIG": "{}", "GROK_CONFIG_PATH": str(home / "config.toml")}


def build_private_user_home(home: Path) -> None:
    user_home = home / "user-home"
    for path in (user_home, *(user_home / name for name in ("config", "data", "cache", "state"))):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ShipError(f"{path}: private Grok user home is not an owned directory")
        path.mkdir(parents=True, exist_ok=True)


def config_text(config: dict) -> str:
    """Native configuration, including ship-owned nested tables and lists of tables."""
    def value(item):
        if isinstance(item, dict):
            return "{ " + ", ".join(f"{literal(k)} = {value(v)}" for k, v in item.items()) + " }"
        if isinstance(item, (list, tuple)):
            return "[" + ", ".join(value(v) for v in item) + "]"
        if isinstance(item, datetime):
            return item.isoformat()
        return literal(item)
    return "\n".join(f"{literal(key)} = {value(item)}" for key, item in config.items()) + "\n"


def auth_metadata(path: Path) -> dict | None:
    """Only a single identifiable native refresh grant can establish freshness."""
    try:
        data = json.loads(path.read_text())
        grants = [grant for grant in data.values() if isinstance(grant, dict)
                  and isinstance(grant.get("refresh_token"), str) and grant["refresh_token"]]
        return grants[0] if len(grants) == 1 else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def no_extra_rules(config: dict, source: str) -> None:
    """The guide's extra rule directories load independently of workspace trust."""
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ShipError(f"{source}: paths must be a table")
    if "extra_rule_dirs" in paths:
        raise ShipError(f"{source}: paths.extra_rule_dirs is not allowed in a Grok seat home")
    for key, value in config.items():
        if isinstance(value, dict):
            no_extra_rules(value, f"{source}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    no_extra_rules(item, f"{source}.{key}[{index}]")


def launch_home(home: Path, seat: SeatSpec) -> None:
    """Check the native home again on fresh, restart and resume launch paths."""
    no_extra_rules(seat.settings, "seat settings")
    try:
        config = tomllib.loads((home / "config.toml").read_text())
    except (OSError, ValueError, UnicodeError):
        raise ShipError(f"{home}: Grok home configuration unreadable; rebuild the seat home") from None
    no_extra_rules(config, "Grok home configuration")


class GrokBox(Box):
    """Measured visual projection, retaining every explicit blank input row."""

    def matches(self, shown: str | None, columns: int | None = None, slack: int = 0) -> bool:
        if not grok_screen.lf_layout(self.text):
            return False
        if self.text and not self.text.strip():
            return False  # native bare LF/spaces can paint exactly like no draft
        if self.text.endswith("\n"):
            if shown is None:
                return False
            if self.folded and re.fullmatch(self.folded, shown.strip()):
                return True  # visual comparison only; the exact record confirms delivery
            endings = 0
            for char in reversed(self.text):
                if char != "\n":
                    break
                endings += 1
            rows = shown.split("\n")
            painted = 0
            for row in reversed(rows):
                if row.strip(" "):
                    break
                painted += 1
            if painted != endings:
                return False
            # Account for every terminal blank row before asking the unchanged
            # shared matcher to verify the remaining visible text and wrapping.
            return Box(self.text[:-endings]).matches("\n".join(rows[:-endings]), columns, slack)
        return super().matches(shown, columns, slack)


PRIVACY_ACK_NOTICE = "Grok privacy banner acknowledged: the account's cached opt-out reads true (framework seed)"
PRIVACY_SEED_RECORD = "quarterdeck-privacy-seed.json"


def cached_coding_opt_out(home: Path, pool: Pool) -> bool:
    """Read only the native opt-out boolean from the declared subscription's own sign-in, for a home attached to it.

    A home is attached by its copy of the pool's file, or by a link naming that file (a home not rebuilt since seats
    held copies). The pool's own file is read, not the copy, which may wait one service pass to be brought up to it.
    The cache is not a fresh account query. Ambiguous accounts and every value except true
    leave the native question in place; no credential or account text is returned or logged.
    """
    if pool.api_key or pool.config_dir is None:
        return False
    link = home / "auth.json"
    try:
        if link.is_symlink() and Path(os.readlink(link)) != pool.config_dir / "auth.json":
            return False
        if not link.is_symlink() and not link.is_file():
            return False
        data = json.loads((pool.config_dir / "auth.json").read_text())
        if not isinstance(data, dict) or len(data) != 1:
            return False
        grant = next(iter(data.values()))
        return isinstance(grant, dict) and grant.get("coding_data_retention_opt_out") is True
    except (OSError, ValueError, UnicodeError):
        return False


def privacy_seed_record(home: Path) -> dict | None:
    """Only an explicit framework record establishes that an acknowledgement is ours."""
    path = home / PRIVACY_SEED_RECORD
    try:
        if path.is_symlink():
            raise ShipError("Grok privacy seed record is linked; retained for review")
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError):
        raise ShipError("Grok privacy seed record is unreadable; retained for review") from None
    if (not isinstance(data, dict) or set(data) != {"format", "writer", "privacy_banner_acked"}
            or type(data.get("format")) is not int or data["format"] != 1
            or data.get("writer") != "quarterdeck" or not isinstance(data.get("privacy_banner_acked"), str)):
        raise ShipError("Grok privacy seed record is unrecognized; retained for review")
    try:
        at = datetime.fromisoformat(data["privacy_banner_acked"].replace("Z", "+00:00"))
        if at.tzinfo is None:
            raise ValueError
    except ValueError:
        raise ShipError("Grok privacy seed record timestamp is unreadable; retained for review") from None
    return data


def acknowledge_cached_opt_out(home: Path, pool: Pool) -> str | None:
    """Reconcile only our seed on every rebuild/launch; native press state has no seed record."""
    seed = privacy_seed_record(home)
    opted_out = cached_coding_opt_out(home, pool)
    path = home / "config.toml"
    if seed is None and (not opted_out or program_privacy(path) is not None):
        return None  # never label an unmarked native acknowledgement as our own
    config = tomllib.loads(path.read_text())
    privacy = config.get("privacy")
    if privacy is not None and not isinstance(privacy, dict):
        raise ShipError("Grok privacy configuration is unrecognized; retained for review")
    if seed is None and privacy is not None and "privacy_banner_acked" in privacy:
        raise ShipError("Grok privacy acknowledgement is unrecognized; retained for review")
    if seed is not None and privacy is not None and "privacy_banner_acked" in privacy:
        if privacy["privacy_banner_acked"] != seed["privacy_banner_acked"]:
            raise ShipError("Grok privacy acknowledgement differs from its seed record; retained for review")
    record = home / PRIVACY_SEED_RECORD
    if not opted_out:
        # Clear the config first: interruption cannot leave an unmarked framework seed behind.
        if privacy is not None and "privacy_banner_acked" in privacy:
            privacy.pop("privacy_banner_acked")
            if not privacy:
                config.pop("privacy")
            durable.replace_file(path, config_text(config))
        record.unlink()
        durable.sync_folder(home)
        return None
    if seed is None:
        seed = {"format": 1, "writer": "quarterdeck",
                "privacy_banner_acked": datetime.now(timezone.utc).isoformat()}
        # Publish provenance first, so a cut before/after the config write can be reconciled.
        durable.replace_file(record, json.dumps(seed, sort_keys=True) + "\n")
    if privacy is None or "privacy_banner_acked" not in privacy:
        config.setdefault("privacy", {})["privacy_banner_acked"] = seed["privacy_banner_acked"]
        durable.replace_file(path, config_text(config))
    return PRIVACY_ACK_NOTICE


def program_privacy(path: Path) -> dict | None:
    """Read the measured acknowledgement without inferring its writer from the timestamp.

    Native acknowledgements without a framework seed record survive rebuilds. Framework
    seeds are separately reevaluated against the current flag at every rebuild and launch.
    """
    try:
        table = tomllib.loads(path.read_text()).get("privacy")
    except (OSError, ValueError, UnicodeError):
        return None
    acked = table.get("privacy_banner_acked") if isinstance(table, dict) else None
    if not isinstance(acked, str):
        return None
    try:
        datetime.fromisoformat(acked.replace("Z", "+00:00"))
    except ValueError:
        return None
    return {"privacy_banner_acked": acked}


class GrokHarness(Harness):
    box_geometry_known = True  # box_columns below declares the established text area

    def model_identity(self, value: str | None, *, version: str | None = None) -> str | None:
        value = super().model_identity(value, version=version)
        # 1.0.77's footer omits native-id distinctions (for example the build suffix).
        # Its measured display grammar proves no unique id; record ids remain literal.
        if value is not None and re.fullmatch(r'Grok [0-9.]+(?: Fast)?', value):
            return None
        return value

    efforts = EffortCapability()  # CLI vocabulary is not measured
    defaults = DefaultMetadata(model_default=True, model=SuggestedValue(None, "program-default"),
                               effort=SuggestedValue(None, "program-default"))
    selection_templates = ("config.toml",)

    name = "grok"
    display_name = 'Grok Build'
    setup = SetupMetadata(subscription_home="~/.grok", key_env="XAI_API_KEY")
    program = "grok"
    sign_in_command = ("grok", "login")
    sign_in_note = ("Current docs document GROK_HOME for configuration, but do not establish credential-home "
                    "selection here. Declared-home sign-in is unknown; ask the person. No home-specific command is established here.")
    sign_in_sources = ("https://docs.x.ai/build/cli/reference", "https://docs.x.ai/build/settings")
    native_provider = "xai"
    home_variable = "GROK_HOME"
    credential_file = "auth.json"
    # Measured 13 November on 1.0.77: Grok replaces its auth.json at the link's target, the pool's own file, so two seats
    # refreshing at once would race on the account holder's sign-in. Each seat holds a copy instead.
    credential_copy = True
    # Measured 11 November on 1.0.77: the program renews its short access grant on use, so expires_at
    # passing needs no login. The end of the grant that renews it and a failed renewal have no field
    # the adapter knows by name, so neither is read.
    grant_kind = "self-renewing"
    answers_questions = True  # its first-run training question, answered opt out by the ship's orders
    answer_seconds = 5.0  # measured: the training question was gone within one second of the press
    fresh_versions = frozenset({"1.0.77", "1.0.70"})  # M1 to M3 and M5: the composer after the press, before any turn
    # Measured 13 November on 1.0.77 (worker5): a home seeded with the acknowledgement starts with no
    # question, straight to that same fresh composer, frame for frame. 1.0.70 was not measured seeded.
    seeded_versions = frozenset({"1.0.77"})

    def seeded_acknowledgement(self, question: str, launched, home: Path | None) -> bool:
        """The training question was settled before launch by the framework's own seed, never by a guess.

        Both marks are needed: the launch record's notice ends with the seed's evidence, written only when the
        account's cached opt-out read true at this launch, and the home still holds the framework's seed record
        with the config's acknowledgement equal to it. A native acknowledgement with no seed record, a seed
        withdrawn since, or the notice's words alone (a workspace inventory can carry any name) are not enough.
        """
        if (question != grok_screen.TRAINING_QUESTION.name or home is None
                or launched.version not in self.seeded_versions
                or (launched.notice or "").rsplit("; ", 1)[-1] != PRIVACY_ACK_NOTICE):
            return False
        try:
            seed = privacy_seed_record(home)
        except ShipError:
            return False
        kept = program_privacy(home / "config.toml")
        return seed is not None and kept is not None and kept["privacy_banner_acked"] == seed["privacy_banner_acked"]
    keys = Keys(submit="Enter", interrupt="C-c")
    reads_usage = background_usage = True
    private_usage_home = True
    private_version_home = True
    styled_screen = True
    record_time_resolution = 1.0
    usage_interval = 1800  # unattended native ACP billing; conservative half-hour cadence
    headline_window = (None, "7d")
    human_tools = ("ask_user_question",)
    human_tool_policy = "ask_user_question is removed outside human sessions."
    reader = staticmethod(grok_record.Reader.read)
    records = staticmethod(grok_record.records)
    first_timestamp = staticmethod(grok_record.first_timestamp)
    session_id = staticmethod(grok_record.session_id)
    inputs = staticmethod(grok_record.inputs)

    def preflight(self, home: Path | None = None, cwd: Path | None = None) -> None:
        no_admin_layers(home)
        for name in INHERITED_REDIRECTS:
            if os.environ.get(name):
                raise ShipError(f"Grok inherited configuration redirect {name} is set; clear it before launch")
        if home is not None:
            grok_workspace.check(home, cwd, private_environment(home))

    def workspace_notice(self, home: Path, cwd: Path) -> str | None:
        return grok_notice.inventory(cwd, private_environment(home))

    def selected_program(self) -> Path | None:
        """The one Grok program this ship runs: the first ``grok`` on the PATH a seat launches with.

        A seat's launch, doctor's reading and qd check all take it from here, and the launch names it by its
        absolute path, so the program a seat runs is the one doctor showed and the check proved.
        """
        from ..seat import program_path

        selected = shutil.which(self.program, path=program_path())
        return Path(selected).absolute() if selected else None

    def version(self, home: Path | None = None) -> str | None:
        selected = self.selected_program()
        if selected is None:
            return None
        try:
            program = selected.resolve(strict=True)
            if home is None:
                match = re.fullmatch(r"grok-(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9][A-Za-z0-9.+_-]*)?)-linux-x86_64", program.name)
                return match[1] if match else None
            if home.is_symlink() or not home.is_dir() or any(home.iterdir()):
                return None
        except (OSError, RuntimeError):
            return None
        environment = {**ENV, "HOME": str(home), "GROK_HOME": str(home)}
        return program_version(("env", "-i", f"PATH={os.environ.get('PATH', '')}",
                                *(f"{key}={value}" for key, value in environment.items()), str(program), "--version"))

    def freshness(self, path: Path) -> tuple[float, float] | None:
        grant = auth_metadata(path)
        if grant is not None and (expiry := when(grant.get("expires_at"))) is not None:
            try:
                return expiry.timestamp(), path.stat().st_mtime
            except OSError:
                return None
        return None

    def grant_expiry(self, path: Path) -> datetime | None:
        """The short access expiry the program renews on use; never a login reading."""
        grant = auth_metadata(path)
        return when(grant.get("expires_at")) if grant else None

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        self.check_defaults(seat, ship_home)
        self.preflight(home, seat.cwd)
        template = ship_template(ship_home, "config.toml")
        no_extra_rules(template, "ship Grok template")
        no_extra_rules(seat.settings, "seat settings")
        home.mkdir(parents=True, exist_ok=True)
        build_private_user_home(home)
        settings = copy.deepcopy(merged(merged(SETTINGS, template), seat.settings))
        # These native switches describe the adapter's input/launch contract.
        settings = merged(settings, SETTINGS)
        for key in MCP_POLICY_KEYS:
            settings.pop(key, None)  # these config keys do not establish effective native policy
        skills = settings.setdefault("skills", {})
        if not isinstance(skills, dict):
            raise ShipError("Grok settings skills must be a table")
        ignored = skills.get("ignore", [])
        if not isinstance(ignored, list) or not all(isinstance(path, str) for path in ignored):
            raise ShipError("Grok settings skills.ignore must be a list of paths")
        skills["ignore"] = list(dict.fromkeys([*ignored, str(home / "bundled" / "skills")]))
        disabled = skills.get("disabled", [])
        if not isinstance(disabled, list) or not all(isinstance(name, str) for name in disabled):
            raise ShipError("Grok settings skills.disabled must be a list of names")
        skills["disabled"] = list(dict.fromkeys([*disabled, "resume-claude", "resume-codex", "resume-cursor"]))
        if not seat.human_session:
            settings.setdefault("features", {})["ask_user_question"] = False
        settings.pop("privacy", None)  # declarations never establish the account's opt-out
        if (kept := program_privacy(home / "config.toml")) is not None:
            settings["privacy"] = kept
        (home / "config.toml").write_text(config_text(settings))
        attached = credential(pool, home / self.credential_file, "XAI_API_KEY", copy=self.credential_copy)
        acknowledge_cached_opt_out(home, pool)
        return attached

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        workspace_args = grok_workspace.launch_args(seat.cwd)
        self.preflight(home, seat.cwd)
        launch_home(home, seat)
        privacy_notice = acknowledge_cached_opt_out(home, pool)
        program = self.selected_program()
        if program is None:
            raise ShipError(f"{self.program} is not on the PATH qd runs with; install it, or put it on that PATH")
        argv = [str(program), "--fullscreen", "--always-approve", "--sandbox", "off", "--no-memory",
                "--leader-socket", str(home / "leader.sock"), "--rules", brief.read_text()]
        argv += workspace_args
        if not seat.human_session:
            argv += ["--disallowed-tools", "ask_user_question"]
        if seat.model:
            argv += ["--model", seat.model]
        if seat.effort:
            argv += ["--reasoning-effort", seat.effort]
        if resume:
            argv += ["--resume", resume]
        if first:
            argv.append(first)
        notices = [self.workspace_notice(home, seat.cwd), privacy_notice]
        return Launch(tuple(argv), {**private_environment(home), **key_environment(pool, "XAI_API_KEY")},
                      notice="; ".join(filter(None, notices)) or None)

    def usage(self, pool: Pool) -> Usage:
        return Usage((), datetime.now(timezone.utc), "native billing requires a framework-owned home", called=False)

    def usage_in_home(self, pool: Pool, home: Path) -> Usage:
        try:
            self.preflight(home)
        except ShipError as error:
            return Usage((), datetime.now(timezone.utc), str(error), called=False)
        if pool.config_dir is None or not (pool.config_dir / self.credential_file).is_file():
            return Usage((), datetime.now(timezone.utc), "native billing sign-in unavailable", called=False)
        home.mkdir(parents=True, exist_ok=True)
        build_private_user_home(home)
        (home / "config.toml").write_text(config_text(SETTINGS))
        credential(pool, home / self.credential_file, "XAI_API_KEY", copy=self.credential_copy)
        acknowledge_cached_opt_out(home, pool)
        try:
            self.preflight(home, home)
        except ShipError as error:
            return Usage((), datetime.now(timezone.utc), str(error), called=False)
        return grok_usage.billing(str(self.selected_program() or self.program), home, private_environment(home))

    def screen(self, text: str) -> Screen:
        return grok_screen.read(text)

    def question_cleared(self, text: str, question: Interruption) -> bool:
        return question is grok_screen.TRAINING_QUESTION and grok_screen.question_cleared(text)

    def human_wake_box_margin(self, pane_width: int) -> int | None:
        return 10 if pane_width >= 14 else None

    def empty_cell(self, screen: Screen) -> tuple[int, int] | None:
        return grok_screen.empty_cell(screen)

    def box_columns(self, pane_width: int) -> int | None:
        # 1.0.77 fullscreen: margins, borders, prompt and one reserved cursor cell.
        # Native 80-column probes wrap 70/71/140 characters as 70, 70+1, 70+70.
        return pane_width - 10 if pane_width >= 14 else None

    def box(self, text: str) -> Box:
        # The counted chip is the program's visual projection of this payload.
        # Only inputs(record), never this comparison, can confirm delivery.
        lines = grok_screen.paste_lines(text)
        folded = (rf"\[Pasted: {lines} lines\]"
                  if grok_screen.lf_layout(text) and lines >= grok_screen.PASTE_FOLD_LINES else None)
        return GrokBox(text, folded=folded)

    def controls(self) -> tuple[Control, ...]:
        return (Control("model", "--model <value>", "/model {value}", "screen"),
                Control("effort", "--reasoning-effort <value>", "/effort {value}", "screen"))

    def interruptions(self):
        return (grok_screen.COMMANDS, grok_screen.MODEL_PICKER)
