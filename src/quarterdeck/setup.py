"""Guided setup, with one replaceable boundary for all machine/account/live operations.

Answers are declarations, never secret values. Creation and reruns share the ship editor
and qd set's application path; the conversation does not maintain another configuration.
"""
from __future__ import annotations

import argparse
import re
import shlex
import shutil
import sys
import tomllib
from pathlib import Path

from . import doctor, harness, providers, ship as ships, standing
from .drill import Check
from .ship import Ship, ShipError
from .setup_answers import Conversation, MAPS, SCALARS, ROLES, MISSING, read_answers
from . import setup_proposal


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
    parser.add_argument("--operator", help="what the crew calls you; suggested Admiral")
    parser.add_argument("--office", help="office session name; suggested Office")
    parser.add_argument("--seat", action="append", metavar="ROLE.FIELD=ANSWER", help="per-seat name, harness, model, effort or pools (JSON list)")
    parser.add_argument("--edit", action="append", metavar="FIELD=VALUE", help="revise a proposal field; JSON for structured values")
    parser.add_argument("--use-defaults", choices=("yes", "no"), help="explicitly use established suggestions for missing declaration answers")
    parser.add_argument("--accept", choices=("yes", "no"), help="accept the complete proposal before any writes; separate from check/start consent")


class Operations:
    """All version/account/check/launch/application interactions; replace whole in scripted tests."""
    def discover(self, defaults: dict) -> dict[str, tuple[Check, str]]:
        from .seat import program_path
        result = {}
        for name in harness.NAMES:
            reader = harness.get(name)
            if reader.synthetic:
                continue
            found = shutil.which(reader.program, path=program_path())
            row = Check(f"program {name}", found is not None,
                        f"{reader.program} found; version unchecked" if found else f"{reader.program} is not on PATH; choose an installed program")
            info = "sign-in unknown; the native program must complete its own sign-in"
            home = self.subscription_home(name, defaults)
            if home and reader.credential_file:
                info = ("credential present; valid sign-in unknown" if self.credential_present(home, reader.credential_file)
                        else "no credential in the documented sign-in directory")
            result[name] = row, info
        return result

    def subscription_home(self, program: str, defaults: dict) -> str | None:
        table = defaults.get("pools", {}).get(program, {})
        # Official Grok config docs name this location, not an installed-version auth guarantee.
        return table.get("config_dir", table.get("home", "~/.grok" if program == "grok" else None))

    def credential_present(self, home: str, filename: str) -> bool:
        return (Path(home).expanduser() / filename).is_file()

    def workspace_default(self) -> str | None:
        return setup_proposal.workspace_default()

    def default_metadata(self, program: str):
        return harness.get(program).default_metadata()

    def local_models(self, url: str) -> list[str]:
        """Only the person's loopback server; no key lookup or remote/provider request."""
        from . import endpoint
        url = safe_endpoint(url)
        if not setup_proposal.local_endpoint(url):
            return []
        try:
            return [model.id for model in endpoint.models(url, allow_redirects=False)]
        except ShipError:
            return []

    def offered_models(self, url: str, provider: str) -> list[str]:
        """The selected provider's anonymous listing only; no completion, account or key lookup."""
        url = safe_endpoint(url)
        if setup_proposal.local_endpoint(url):
            return self.local_models(url)
        if providers.canonical(provider) != "openrouter" or url != "https://openrouter.ai/api/v1":
            return []
        from . import endpoint
        try:
            return [model.id for model in endpoint.models(url, allow_redirects=False)]
        except ShipError:
            return []

    def readiness(self, ship: Ship, selected: list[str]) -> list[Check]:
        observations = {name: doctor.program_observation(name, ship.runtime) for name in selected}
        rows = [row for row, _ in observations.values()]
        versions = {name: version for name, (_, version) in observations.items()}
        pools = {pool for seat in ship.seats.values() for pool in seat.pools
                 if seat.for_pool(pool).harness in selected}
        rows += [doctor.pool_check(ship.pools[name]) for name in sorted(pools)]
        # Use the existing doctor's context-sensitive readiness and notices unchanged.
        from dataclasses import replace
        chosen = replace(ship, seats={name: seat for name, seat in ship.seats.items()
                                     if any(seat.for_pool(pool).harness in selected for pool in seat.pools)})
        return rows + doctor.seat_readiness(chosen, versions=versions)

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
    return value.rstrip("/")


def pool_answers(conversation: Conversation, selected: list[str], defaults: dict,
                 *, operations: Operations, existing: Ship | None = None) -> tuple[dict, dict]:
    pools, models = {}, {}
    key_names = {"claude": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY", "grok": "XAI_API_KEY"}
    for name in selected:
        reader = harness.get(name)
        saved = next((existing.pools[pool] for seat in existing.seats.values() for pool in seat.pools
                      if seat.for_pool(pool).harness == name), None) if existing else None
        default_kind = saved.kind if saved else "subscription" if reader.native_provider else "endpoint"
        if saved and name in ("opencode", "pi") and saved.provider == "openrouter" and saved.api_key:
            default_kind = "key"
        prompt = (f"What does {setup_proposal.LABELS.get(name, name)} run on? "
                  "a subscription you are signed in to; an API key; a local model "
                  "(subscription, key, endpoint)")
        kind = conversation.choice(f"payment.{name}", prompt, ("subscription", "key", "endpoint"), default_kind)
        endpoint_key = kind == "key" and name in ("opencode", "pi")
        compatible_saved = saved is not None and (saved.kind == kind or endpoint_key and saved.kind == "endpoint")
        provider = saved.provider if compatible_saved else reader.native_provider
        if endpoint_key and provider is None:
            provider = "openrouter"
        if provider is None or kind in ("key", "endpoint"):
            provider = conversation.ask(f"provider.{name}", f"Who provides {name}'s key, or operates its model?", provider)
        values = ({**tomllib.loads(existing.file.read_text())["pools"][saved.name]}
                  if compatible_saved and provider == saved.provider else {})
        values["provider"] = provider
        if kind == "subscription" and reader.credential_file:
            home = str(saved.config_dir) if saved and saved.config_dir else operations.subscription_home(name, defaults)
            home = conversation.ask(f"subscription_home.{name}", f"Folder holding {name}'s native sign-in", home)
            values.pop("home", None)
            values["config_dir"] = str(Path(home).expanduser().resolve())
            present = operations.credential_present(values["config_dir"], reader.credential_file)
            conversation.say("Credential file present; sign-in not checked." if present else "Credential file not found; sign-in will need attention.")
        if kind == "key":
            variable = saved.api_key.removeprefix("env:") if saved and saved.api_key and saved.api_key.startswith("env:") else "OPENROUTER_API_KEY" if endpoint_key and providers.canonical(provider) == "openrouter" else key_names.get(name)
            variable = conversation.ask(f"key_env.{name}", f"{name}'s API key variable NAME (never the key)", variable)
            values["api_key"] = key_reference(variable)
        if kind == "endpoint" or endpoint_key:
            given = conversation.supplied(f"endpoint.{name}")
            documented = "https://openrouter.ai/api/v1" if providers.canonical(provider) == "openrouter" else None
            candidate = given if given is not MISSING else saved.endpoint if saved and saved.endpoint else documented or "http://127.0.0.1:8000/v1"
            served = operations.offered_models(candidate, provider)
            suggestion = candidate if served or candidate == documented or saved and saved.endpoint == candidate else None
            url = safe_endpoint(conversation.ask(f"endpoint.{name}", f"URL serving {name}'s model", suggestion))
            if url != candidate:
                served = operations.offered_models(url, provider)
            if setup_proposal.local_endpoint(url) and not served:
                conversation.say("No model listing from your local server; check its URL/server or supply the model by name.")
            elif documented == url and not served:
                conversation.say("No model suggestion from the anonymous listing; enter a model name.")
            elif len(served) > 1:
                conversation.say("The listing has several models; choose one by name.")
            values["endpoint"] = url
            declared_model = conversation.supplied(f"model.{name}")
            if declared_model is not MISSING:
                models[name] = conversation.ask(f"model.{name}", f"Model served for {name}")
            elif len(served) == 1:
                models[name] = served[0]
            if not endpoint_key:
                variable = None
                if saved:
                    if not saved.api_key:
                        variable = "none"
                    elif saved.api_key.startswith("env:"):
                        variable = saved.api_key.removeprefix("env:")
                variable = conversation.ask(f"key_env.{name}", f"{name}'s key variable NAME, or none if no key is needed", variable)
                if variable != "none":
                    values["api_key"] = key_reference(variable)
                else:
                    values.pop("api_key", None)
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
        if section.startswith("seats.") and re.match(r"\s*((model|effort)(?:_mode)?|fresh_after_compactions)\s*=", line):
            continue
        lines.append(line)
    result = "\n".join(lines) + "\n"
    for table, key, value in edits:
        result = ships.edit(result, table, key, value)
    return result


def validate_declaration(ship: Ship) -> list[str]:
    """Setup's measured one-seat exception; generic ship/runtime reader policy is unchanged."""
    ships._validate(ship)
    from .harness.capabilities import validate_selection
    notes = []
    for seat in ship.seats.values():
        for pool in seat.pools:
            selected = seat.for_pool(pool)
            note = validate_selection(selected, harness.get(selected.harness))
            if note:
                notes.append(f"{seat.name}, pool {pool}: {note}")
    antigravity = [seat.name for seat in ship.seats.values()
                   if any(seat.for_pool(pool).harness == "antigravity" for pool in seat.pools)]
    if len(antigravity) > 1:
        raise ShipError("Antigravity is limited to one declared seat in setup; proposed seats: "
                        + ", ".join(antigravity)
                        + ". Choose other programs for the remaining roles and rerun qd setup.")
    return notes


def sign_in_hints(ship: Ship, selected: list[str]) -> list[str]:
    """Render current documentation, never execute it or infer readiness from it."""
    hints, seen = [], set()
    for seat in ship.seats.values():
        for name in seat.pools:
            program, pool = seat.for_pool(name).harness, ship.pools[name]
            if program not in selected or pool.kind != "subscription" or (program, name) in seen:
                continue
            seen.add((program, name))
            reader = harness.get(program)
            hints.append(f"{program}, pool {name}: {reader.sign_in_note}")
            if reader.sign_in_command is not None:
                command = shlex.join(reader.sign_in_command)
                if reader.sign_in_home_variable and pool.config_dir is not None:
                    # _pool expands ~ but permits relative homes; show the same absolute target.
                    command = f"{reader.sign_in_home_variable}={shlex.quote(str(pool.config_dir.absolute()))} {command}"
                elif pool.config_dir is not None:
                    hints.append(f"Declared sign-in directory: {pool.config_dir}; its sign-in invocation remains unknown.")
                elif reader.credential_file:
                    hints.append("Declared sign-in directory unknown; ask the person before any native sign-in.")
                hints.append(f"Current official documentation command (display only): {command}")
                hints.append("Installed-version support is unknown; current docs do not prove this command for an older version. "
                             "Ask the person to confirm the native steps; qd cannot answer its account questions.")
            hints.extend(f"Official sign-in reference: {url}" for url in reader.sign_in_sources)
    return hints


def preview_update(ship: Ship, edits: list) -> tuple[dict, list]:
    """Reject unsupported qd set representations BEFORE any additions or existing-seat writes."""
    existing_edits, whole = {}, []
    original = tomllib.loads(ship.file.read_text())
    for table, key, value in edits:
        if table.startswith("seats.") and table.split(".")[1] in ship.seats and key in ("model_mode", "effort_mode") and value == "program-default":
            field = key.removesuffix("_mode")
            if field in original["seats"][table.split(".")[1]]:
                raise ShipError(f"{table}.{field}: changing to program-default requires removing the named {field}; "
                                "qd set cannot unset it. Edit that declaration explicitly before rerunning setup; nothing was written.")
    for table, key, value in edits:
        if table.startswith("seats.") and table.split(".")[1] in ship.seats:
            name = table.split(".")[1]
            if original["seats"][name].get(key) == value:
                continue
            if ship.seat(name).pool_choices and key in ("harness", "model", "effort", "model_mode", "effort_mode", "pool"):
                raise ShipError(f"seats.{name}.{key}: qd set cannot rewrite inline pool choices; edit the declaration explicitly before rerunning setup; nothing was written")
            from .cli import parse_value
            if parse_value(str(value)) != value:
                raise ShipError(f"seat {name}: qd set cannot express this {key} as plain text; choose a text value without commas, boolean or integer spellings")
            existing_edits.setdefault(name, []).append(f"{key}={value}")
        else:
            whole.append((table, key, value))
    text = ship.file.read_text()
    for table, key, value in edits:
        text = ships.edit(text, table, key, value)
    proposed = ships._from_text(text, ship.folder)
    for key, session in ship.sessions.items():
        if key in proposed.sessions and proposed.sessions[key].title != session.title:
            raise ShipError(f"answer name/session {key}: changing title {session.title!r} to {proposed.sessions[key].title!r} "
                            "needs an explicit declaration/session transition; qd set does not apply it; nothing was written")
    return existing_edits, whole


def update(ship: Ship, edits: list, operations: Operations, conversation: Conversation) -> tuple[Ship, list[str]]:
    existing_edits, whole = preview_update(ship, edits)
    # Validate the entire proposal before any additions; qd set still owns existing-seat refusals/application.
    text = ship.file.read_text()
    for table, key, value in edits:
        text = ships.edit(text, table, key, value)
    validate_declaration(ships._from_text(text, ship.folder))
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
    while True:
        proposal = setup_proposal.build(conversation, operations, text, orders, defaults, available)
        data = setup_proposal.summary(proposal.ship, setup_proposal.display_control, existing=proposal.existing)
        for note in validate_declaration(proposal.ship):
            conversation.say(note)
        accepted = conversation.review(lambda: setup_proposal.render_summary(data))
        if accepted is not None:
            break
    def finish(lines):
        if args.json:
            from .output import Reading
            return Reading({**data, "result": lines}, lambda reading: reading["result"])
        return lines
    if not accepted:
        return finish(["Setup stopped before acceptance; no ship changes were written."])
    folder = proposal.ship.folder
    rerun = shlex.join(["qd", "--ship", str(folder), "setup", "--existing", "keep",
                       "--use-defaults", "yes", "--accept", "yes"])
    if proposal.mode == "new":
        ship = ships.create(folder, proposal.text, proposal.orders)
        conversation.say(f"Created and committed {ship.file} and orders.md; runtime is ignored.")
    elif proposal.mode == "update":
        ship, pending = update(proposal.existing, proposal.edits, operations, conversation)
        if pending:
            return finish(["Declaration changes followed qd set's application/defer path. Apply the reported relaunch steps (may spend turns):",
                           *pending, f"Then rerun: {rerun} --check yes --start yes"])
    else:
        ship = proposal.existing
    selected = sorted({seat.for_pool(pool).harness for seat in ship.seats.values() for pool in seat.pools})
    rows = operations.readiness(ship, selected)
    for row in rows:
        conversation.say(str(row))
    failed = [row for row in rows if not row.passed]
    if failed:
        for hint in sign_in_hints(ship, selected):
            conversation.say(hint)
        if conversation.interactive and conversation.choice("wait_for_sign_in", "Pause while you fix readiness yourself? yes or no", ("yes", "no"), "no") == "yes":
            conversation.read("Complete the native steps yourself, then press Enter to re-read readiness: ")
            rows = operations.readiness(ship, selected)
            for row in rows:
                conversation.say(str(row))
            failed = [row for row in rows if not row.passed]
        if failed:
            raise ShipError("readiness refused: " + "; ".join(f"{row.name}: {row.detail}" for row in failed)
                            + f"; resolve those reasons, then rerun {rerun} --check yes --start yes")
    consent = conversation.choice("check", f"Run qd check for {', '.join(selected)}? This spends turns. yes or no", ("yes", "no"), "no")
    if consent == "no":
        return finish([f"Ship saved; live checks declined. To continue: {rerun} --check yes --start yes"])
    for program in selected:
        conversation.say(f"Checking {program}; this spends turns.")
        rows = operations.check(ship, program)
        for row in rows:
            conversation.say(str(row))
        if not rows or not all(row.passed for row in rows):
            reason = "; ".join(f"{row.name}: {row.detail}" for row in rows if not row.passed) or "no proof rows returned"
            raise ShipError(f"qd check {program} failed: {reason}; resolve that reason, then rerun {rerun} --check yes --start yes")
    start = conversation.choice("start", "Bring up the crew? Opening briefs may spend turns. yes or no", ("yes", "no"), "no")
    if start == "no":
        return finish([f"Checks passed; crew not started. Start with: qd --ship {shlex.quote(str(folder))} up"])
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
        raise ShipError(f"crew readiness is not confirmed by status; resolve the status reasons and rerun {rerun} --check yes --start yes")
    return finish([f"Desk access: {operations.access(ship)} (desk window)",
            'First order to type at the desk: "Read this workspace and propose one useful next change."',
            f"Status: qd --ship {shlex.quote(str(folder))} status"])
