"""Guided setup, with one replaceable boundary for all machine/account/live operations.

Answers are declarations, never secret values. Creation and reruns share the ship editor
and qd set's application path; the conversation does not maintain another configuration.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import tomllib
from pathlib import Path

from . import doctor, harness, providers, ship as ships, standing
from .drill import Check
from .ship import Ship, ShipError

MAPS = ("payment", "subscription_home", "provider", "key_env", "endpoint", "model")
SCALARS = ("programs", "desk", "captain", "engineer", "workspace", "name", "folder",
           "existing", "check", "start", "wait_for_sign_in")
ROLES = ("desk", "captain", "engineering")


def arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--answers", help="JSON object with the same answer names as flags (hyphens become underscores)")
    parser.add_argument("--programs", help="comma-separated registered programs, for example claude,codex")
    for name, help_text in (
        ("payment", "subscription, key or endpoint"),
        ("subscription-home", "existing native sign-in directory; never copied"),
        ("provider", "company for a key, or declared endpoint operator"),
        ("key-env", "environment variable NAME holding a key; never its value"),
        ("endpoint", "OpenAI-compatible URL without credentials"),
        ("model", "explicit model name (required for an endpoint)"),
    ):
        parser.add_argument(f"--{name}", action="append", metavar="PROGRAM=ANSWER", help=help_text)
    parser.add_argument("--desk", help="program the person talks to")
    parser.add_argument("--captain", help="program for the captain; interactive proposes a choice")
    parser.add_argument("--engineer", help="program for engineering; interactive proposes a choice")
    parser.add_argument("--workspace", help="existing folder the crew works on")
    parser.add_argument("--name", help="ship name")
    parser.add_argument("--folder", help="new ship folder with an existing parent; or use global --ship")
    parser.add_argument("--existing", choices=("keep", "update"), help="explicit rerun choice; never overwrite an existing ship")
    parser.add_argument("--check", choices=("yes", "no"), help="consent to qd check for EVERY chosen program; spends turns")
    parser.add_argument("--start", choices=("yes", "no"), help="bring up the crew after successful checks (opening briefs may spend turns)")
    parser.add_argument("--wait-for-sign-in", choices=("yes", "no"), help="pause for the person on a readiness failure; never sign in automatically")


class Conversation:
    def __init__(self, answers: dict, *, interactive: bool, read=input, write=print):
        self.answers, self.interactive, self.read, self.write = answers, interactive, read, write

    def say(self, message: str) -> None:
        self.write(message)

    def ask(self, name: str, prompt: str, default: str | None = None) -> str:
        parts = name.split(".")
        value = self.answers.get(parts[0])
        if len(parts) == 2:
            value = value.get(parts[1]) if isinstance(value, dict) else None
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ShipError(f"answer {name} must be nonempty text; supply {name} again")
            return value.strip()
        if not self.interactive:
            raise ShipError(f"missing answer {name}; pass --{parts[0].replace('_', '-')} "
                            + (f"{parts[1]}=ANSWER" if len(parts) == 2 else "ANSWER") + " or --answers FILE")
        try:
            value = self.read(prompt + (f" [{default}]" if default is not None else "") + ": ").strip()
        except (EOFError, KeyboardInterrupt):
            raise ShipError(f"setup stopped at answer {name}; rerun qd setup to continue") from None
        value = value or default
        if not value:
            raise ShipError(f"missing answer {name}; rerun qd setup and supply it")
        return value

    def choice(self, name: str, prompt: str, choices, default: str | None = None) -> str:
        value = self.ask(name, prompt, default)
        if value not in choices:
            raise ShipError(f"answer {name}: choose {', '.join(choices)}; rerun with that answer")
        return value


def read_answers(args: argparse.Namespace) -> dict:
    answers = {}
    if args.answers:
        try:
            answers = json.loads(Path(args.answers).expanduser().read_text())
        except (OSError, ValueError):
            raise ShipError("cannot read answers file as a JSON object; supply --answers FILE") from None
        if not isinstance(answers, dict) or set(answers) - set(SCALARS + MAPS):
            raise ShipError("answers file must be an object containing setup answer names only")
        for name, value in answers.items():
            if name in MAPS:
                if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
                    raise ShipError(f"answer {name} must map program names to text")
            elif not isinstance(value, str):
                raise ShipError(f"answer {name} must be text")
    for name in SCALARS:
        if (value := getattr(args, name, None)) is not None:
            answers[name] = value
    for name in MAPS:
        merged = dict(answers.get(name, {}))
        for entry in getattr(args, name, None) or []:
            program, equals, value = entry.partition("=")
            if not equals or not program or not value:
                raise ShipError(f"--{name.replace('_', '-')} needs PROGRAM=ANSWER")
            merged[program] = value
        answers[name] = merged
    if args.ship:
        if answers.get("folder") and Path(answers["folder"]).expanduser().resolve() != Path(args.ship).expanduser().resolve():
            raise ShipError("--ship and --folder name different folders; choose one target")
        answers["folder"] = args.ship
    return answers


class Operations:
    """All version/account/check/launch/application interactions; replace whole in scripted tests."""
    def discover(self, defaults: dict) -> dict[str, tuple[Check, str]]:
        from . import credential
        result = {}
        for name in harness.NAMES:
            reader = harness.get(name)
            if reader.synthetic:
                continue
            row = doctor.program_check(name)
            info = "sign-in unknown; the native program must complete its own sign-in"
            table = defaults.get("pools", {}).get(name)
            if table and reader.credential_file:
                pool = ships._pool(name, table)
                file = credential.pool_file(pool)
                if file is not None:
                    info = ("credential present; valid sign-in unknown" if file.is_file()
                            else "no credential in the default template's sign-in directory")
            result[name] = row, info
        return result

    def readiness(self, ship: Ship, selected: list[str]) -> list[Check]:
        rows = [doctor.program_check(name, ship.runtime) for name in selected]
        pools = {pool for seat in ship.seats.values() for pool in seat.pools
                 if seat.for_pool(pool).harness in selected}
        rows += [doctor.pool_check(ship.pools[name]) for name in sorted(pools)]
        # Use the existing doctor's context-sensitive readiness and notices unchanged.
        from dataclasses import replace
        chosen = replace(ship, seats={name: seat for name, seat in ship.seats.items()
                                     if any(seat.for_pool(pool).harness in selected for pool in seat.pools)})
        return rows + doctor.seat_readiness(chosen)

    def check(self, ship: Ship, name: str) -> list[Check]:
        from . import check
        from .tmux import Tmux
        return check.check(ship, name, Tmux.default())

    def change_seat(self, ship: Ship, name: str, changes: list[str]) -> list[str]:
        from .cli import set_seat
        return set_seat(ship, name, changes)

    def up(self, ship: Ship):
        from .cli import cmd_up
        return cmd_up(argparse.Namespace(ship=str(ship.folder), seat=None, fresh=False))

    def status(self, ship: Ship):
        from .cli import cmd_status
        return cmd_status(argparse.Namespace(ship=str(ship.folder), line=False))

    def access(self, ship: Ship) -> str:
        from .tmux import Tmux
        return Tmux.default().attach_command(ship.session_of(ship.seat("desk")).title)


def template() -> tuple[str, str, dict]:
    folder = standing.project_dir() / "ships" / "default"
    try:
        text, orders = (folder / "ship.toml").read_text(), (folder / "orders.md").read_text()
        return text, orders, tomllib.loads(text)
    except (OSError, ValueError):
        raise ShipError("default ship template unavailable; install this version's packaged resources and rerun qd setup") from None


def program_list(value: str, available: dict) -> list[str]:
    names = [part.strip() for part in value.split(",")]
    if not all(names) or len(set(names)) != len(names):
        raise ShipError("answer programs must name distinct comma-separated programs")
    for name in names:
        if name not in available:
            raise ShipError(f"program {name!r} is not registered; choose {', '.join(available)}")
        if not available[name][0].passed:
            raise ShipError(f"{available[name][0].detail}; rerun qd setup after putting that program on PATH")
    return names


def safe_endpoint(value: str) -> str:
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in ("http", "https") and parsed.hostname and not (
            parsed.username or parsed.password or parsed.query or parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ShipError("endpoint must be an http(s) URL without credentials, query or fragment; use key_env for a key's variable NAME")
    if parsed.hostname.casefold() == "openrouter.ai" or parsed.hostname.casefold().endswith(".openrouter.ai"):
        raise ShipError("setup does not use OpenRouter; choose your own subscription, key or endpoint")
    return value.rstrip("/")


def pool_answers(conversation: Conversation, selected: list[str], defaults: dict) -> tuple[dict, dict]:
    pools, models = {}, {}
    for name in selected:
        reader = harness.get(name)
        kind = conversation.choice(f"payment.{name}", f"How is {name} paid? subscription, key or endpoint",
                                   ("subscription", "key", "endpoint"))
        provider = reader.native_provider
        if provider is None or kind in ("key", "endpoint"):
            provider = conversation.ask(f"provider.{name}", f"Who provides {name}'s key, or operates its endpoint?", provider)
        if provider and providers.canonical(provider) == "openrouter":
            raise ShipError("setup does not configure OpenRouter; choose your own subscription, key or endpoint operator")
        values = {"provider": provider}
        if kind == "subscription" and reader.credential_file:
            default = defaults.get("pools", {}).get(name, {})
            home = conversation.ask(f"subscription_home.{name}", f"Which existing folder holds {name}'s native sign-in?",
                                    default.get("config_dir", default.get("home")))
            values["config_dir"] = str(Path(home).expanduser().resolve())
        if kind == "key":
            variable = conversation.ask(f"key_env.{name}", f"Environment variable NAME holding {name}'s key (never paste the key)")
            values["api_key"] = key_reference(variable)
        if kind == "endpoint":
            values["endpoint"] = safe_endpoint(conversation.ask(f"endpoint.{name}", f"URL for {name}'s compatible endpoint"))
            models[name] = conversation.ask(f"model.{name}", f"Model served by {name}'s endpoint")
            # An unauthenticated local endpoint is explicit: an empty answer is never interpreted as no key.
            variable = conversation.ask(f"key_env.{name}", f"Key environment variable NAME for {name}, or none if no key is needed")
            if variable != "none":
                values["api_key"] = key_reference(variable)
        elif name in conversation.answers.get("model", {}):
            models[name] = conversation.ask(f"model.{name}", f"Model for {name}")
        pool = ships._pool(name, values)
        if reason := reader.accepts(pool):
            raise ShipError(f"{reason}; choose a payment method supported by {name} and rerun qd setup")
        pools[name] = values
    return pools, models


def key_reference(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ShipError("key_env must be an environment variable NAME, never a key value; set the variable outside qd and pass its name")
    return f"env:{name}"


def new_text(text: str, edits: list[tuple[str, str, object]]) -> str:
    # Keep the default crew's roles, personas, sessions and orders, without its fixed program choices.
    lines, section = [], ""
    for line in text.splitlines():
        match = ships._HEADER.match(line)
        if match:
            section = match.group(1).strip()
        if section.startswith("pools."):
            continue
        if section.startswith("seats.") and re.match(r"\s*(model|effort)\s*=", line):
            continue
        lines.append(line)
    result = "\n".join(lines) + "\n"
    for table, key, value in edits:
        result = ships.edit(result, table, key, value)
    return result


def declaration(conversation: Conversation, selected: list[str], pools: dict, models: dict,
                name: str, workspace: Path, existing: Ship | None = None) -> tuple[list, dict]:
    desk = conversation.choice("desk", "Which program will you talk to at the desk?", selected, selected[0])
    other = next((program for program in selected if program != desk), desk)
    conversation.say(f"Proposed crew: desk={desk}, captain={other}, engineering={other}; choose each below.")
    captain = conversation.choice("captain", "Program for the captain", selected, other)
    engineer = conversation.choice("engineer", "Program for engineering", selected, other)
    edits = [("ship", "name", name), ("ship", "workspace", str(workspace))]
    names = {}
    for program, values in pools.items():
        pool_name = program
        if existing:
            # Add a new payment declaration rather than deleting or mutating a pool shared by other seats.
            index = 1
            while pool_name in existing.pools:
                current = existing.pools[pool_name]
                proposed = ships._pool(pool_name, values)
                if current == proposed:
                    break
                pool_name = f"{program}-setup-{index}"
                index += 1
        names[program] = pool_name
        edits.extend((f"pools.{pool_name}", key, value) for key, value in values.items())
    mapping = dict(zip(ROLES, (desk, captain, engineer)))
    # Every selected program has a seat, even when more programs are selected than the three default roles.
    seats = {role: (program, "office" if role == "desk" else "bridge", role) for role, program in mapping.items()}
    for program in selected:
        if program not in mapping.values():
            seat_name = f"worker-{program}"
            if existing and seat_name in existing.seats and existing.seat(seat_name).role != "worker":
                raise ShipError(f"seat {seat_name} already has another role; choose programs through qd set instead")
            seats[seat_name] = (program, "bridge", "worker")
    for seat, (program, session, role) in seats.items():
        table = f"seats.{seat}"
        edits += [(table, "harness", program), (table, "pool", names[program])]
        if not existing or seat not in existing.seats:
            edits += [(table, "session", session), (table, "role", role)]
        if program in models:
            edits.append((table, "model", models[program]))
        if existing and seat in existing.seats and existing.seat(seat).harness != program:
            # Do not carry another program's model or effort into a newly selected program.
            current = existing.seat(seat)
            if current.effort or (current.model and program not in models):
                raise ShipError(f"seat {seat}: changing its program needs an explicit model answer and compatible effort; "
                                "use qd set for that change before rerunning setup")
    return edits, mapping


def update(ship: Ship, edits: list, operations: Operations, conversation: Conversation) -> tuple[Ship, list[str]]:
    existing_edits, whole = {}, []
    original = tomllib.loads(ship.file.read_text())
    for table, key, value in edits:
        if table.startswith("seats.") and table.split(".")[1] in ship.seats:
            name = table.split(".")[1]
            if original["seats"][name].get(key) == value:
                continue
            from .cli import parse_value
            if parse_value(str(value)) != value:
                raise ShipError(f"seat {name}: qd set cannot express this {key} as plain text; choose a text value without commas, boolean or integer spellings")
            existing_edits.setdefault(name, []).append(f"{key}={value}")
        else:
            whole.append((table, key, value))
    # Validate the entire proposal before any additions; qd set still owns existing-seat refusals/application.
    text = ship.file.read_text()
    for table, key, value in edits:
        text = ships.edit(text, table, key, value)
    ships._validate(ships._from_text(text, ship.folder))
    for line in ships.set_values(ship, whole):
        conversation.say(line)
    pending = []
    proposed = ships._from_text(text, ship.folder)
    for name, changes in existing_edits.items():
        current, future = ship.seat(name), proposed.seat(name)
        changed = current != future
        for line in operations.change_seat(ships.load(ship.folder), name, changes):
            conversation.say(line)
        if changed:
            verb = "fresh" if current.harness != future.harness else "restart"
            pending.append(shlex.join(["qd", "--ship", str(ship.folder), verb, name]))
    return ships.load(ship.folder), pending


def run(args: argparse.Namespace, *, operations: Operations | None = None,
        conversation: Conversation | None = None) -> list[str]:
    operations = operations or Operations()
    conversation = conversation or Conversation(read_answers(args), interactive=sys.stdin.isatty() and not args.json,
                                                write=lambda line: print(line, file=sys.stderr if args.json else sys.stdout, flush=True))
    text, orders, defaults = template()
    available = operations.discover(defaults)
    for name, (row, sign_in) in available.items():
        conversation.say(f"{row} · {sign_in}")
    folder = Path(conversation.ask("folder", "Folder for this ship (its parent must exist)")).expanduser().absolute()
    rerun = shlex.join(["qd", "--ship", str(folder), "setup"])
    existing = None
    if folder.exists():
        existing = ships.load(folder)
        conversation.say(f"Existing ship {existing.name} in {folder}; workspace {existing.workspace}")
        for seat in existing.seats.values():
            conversation.say(f"  {seat.name}: {seat.harness}, pools {', '.join(seat.pools)}")
        mode = conversation.choice("existing", "Keep this declaration, or update it?", ("keep", "update"), "keep")
    else:
        mode = "new"
    if mode == "keep":
        ship = existing
        selected = sorted({seat.for_pool(pool).harness for seat in ship.seats.values() for pool in seat.pools})
        if "desk" not in ship.seats:
            raise ShipError("existing ship has no desk seat; add one before rerunning setup")
    else:
        selected = program_list(conversation.ask("programs", "Which programs go in this crew? (comma separated)"), available)
        pools, models = pool_answers(conversation, selected, defaults)
        workspace = Path(conversation.ask("workspace", "Existing workspace folder")).expanduser().resolve()
        if not workspace.is_dir():
            raise ShipError("workspace is not an existing directory; create it yourself or choose an existing workspace")
        name = conversation.ask("name", "Ship name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise ShipError("ship name must use letters, numbers, underscores or hyphens; choose a plain name")
        edits, _ = declaration(conversation, selected, pools, models, name, workspace, existing)
        if existing:
            for session in ("bridge", "office"):
                if session not in existing.sessions:
                    raise ShipError(f"existing ship has no {session} session; use its own qd set arrangement instead")
            ship, pending = update(existing, edits, operations, conversation)
            if pending:
                return ["Declaration changes followed qd set's application/defer path. Before continuing, apply the reported relaunch steps (may spend turns):",
                        *pending, f"Then rerun: {rerun} --existing keep --check yes --start yes"]
        else:
            if not folder.parent.is_dir():
                raise ShipError("ship folder's parent does not exist; choose an existing parent (setup writes only the ship folder)")
            ship = ships.create(folder, new_text(text, edits), orders)
            conversation.say(f"Created and committed {ship.file} and orders.md; runtime is ignored.")
    for pool in ship.pools.values():
        if pool.provider == "openrouter":
            raise ShipError("setup does not use OpenRouter; choose your own subscription, key or endpoint")
        if pool.endpoint:
            safe_endpoint(pool.endpoint)
    rows = operations.readiness(ship, selected)
    for row in rows:
        conversation.say(str(row))
    failed = [row for row in rows if not row.passed]
    if failed:
        for program in selected:
            conversation.say(f"If sign-in is needed, complete it yourself in the native {harness.get(program).program} program. qd cannot answer its account questions.")
        if conversation.interactive and conversation.choice("wait_for_sign_in", "Pause while you fix readiness yourself? yes or no", ("yes", "no"), "no") == "yes":
            conversation.read("Complete the native steps yourself, then press Enter to re-read readiness: ")
            rows = operations.readiness(ship, selected)
            for row in rows:
                conversation.say(str(row))
            failed = [row for row in rows if not row.passed]
        if failed:
            raise ShipError("readiness refused: " + "; ".join(f"{row.name}: {row.detail}" for row in failed)
                            + f"; resolve those reasons, then rerun {rerun} --existing keep --check yes --start yes")
    consent = conversation.choice("check", f"Run qd check for {', '.join(selected)}? This spends turns. yes or no", ("yes", "no"))
    if consent == "no":
        return [f"Ship saved; live checks declined. To continue: {rerun} --existing keep --check yes --start yes"]
    for program in selected:
        conversation.say(f"Checking {program}; this spends turns.")
        rows = operations.check(ship, program)
        for row in rows:
            conversation.say(str(row))
        if not rows or not all(row.passed for row in rows):
            reason = "; ".join(f"{row.name}: {row.detail}" for row in rows if not row.passed) or "no proof rows returned"
            raise ShipError(f"qd check {program} failed: {reason}; resolve that reason, then rerun {rerun} --existing keep --check yes --start yes")
    start = conversation.choice("start", "Bring up the crew? Opening briefs may spend turns. yes or no", ("yes", "no"))
    if start == "no":
        return [f"Checks passed; crew not started. Start with: qd --ship {shlex.quote(str(folder))} up"]
    result = operations.up(ship)
    for line in ([result] if isinstance(result, str) else result):
        conversation.say(line)
    reading = operations.status(ship)
    rendered = reading.render(reading.data)
    for line in ([rendered] if isinstance(rendered, str) else rendered):
        conversation.say(line)
    observed = reading.data.get("seats", [])
    unavailable = {"off", "unknown", "failed", "refused", "needs a human"}
    if (reading.data.get("service") != "ok" or {row.name for row in observed} != set(ship.seats)
            or any(row.activity.state in unavailable for row in observed)):
        raise ShipError(f"crew readiness is not confirmed by status; resolve the status reasons and rerun {rerun} --existing keep --check yes --start yes")
    return [f"Desk access: {operations.access(ship)} (desk window)",
            'First order to type at the desk: "Read this workspace and propose one useful next change."',
            f"Status: qd --ship {shlex.quote(str(folder))} status"]
