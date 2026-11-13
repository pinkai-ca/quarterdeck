"""Read-only setup discovery and the single renderer for a proposed ship."""
from __future__ import annotations

import ipaddress
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import harness, providers, standing
from .ship import Ship, ShipError
from .setup_answers import MISSING, ROLES, put

ROLE_LABELS = {"desk": "Desk", "captain": "Captain", "engineering": "Engineer"}


@dataclass
class Proposal:
    ship: Ship
    text: str
    orders: str
    edits: list
    existing: Ship | None
    mode: str


def suggested(control) -> tuple[str, str | None]:
    return (control.mode, control.value) if control is not None else ("program-default", None)


def display_control(seat, field: str) -> str:
    from .harness.capabilities import default_label
    if getattr(seat, field + "_mode", None) == "program-default":
        return default_label(unchecked=False)
    return getattr(seat, field) or "unknown"


def compatible_legacy(conversation, role: str, program: str) -> None:
    """Old keys remain harness/program choices; conflicting additive answers refuse by name."""
    legacy_key = {"engineering": "engineer"}.get(role, role)
    legacy = conversation.supplied(legacy_key)
    key = f"seats.{role}.harness"
    if legacy is not MISSING and legacy != program:
        raise ShipError(f"answers {legacy_key} and {key} disagree; both name the program, not the seat's display name")
    legacy_model = conversation.supplied(f"model.{program}")
    if legacy_model is not MISSING:
        model_key = f"seats.{role}.model"
        current, mode = conversation.supplied(model_key), conversation.supplied(model_key + "_mode")
        if mode == "program-default" or current is not MISSING and current != legacy_model:
            raise ShipError(f"answers model.{program} and {model_key}/{model_key}_mode disagree; choose one model declaration")
        if current is MISSING:
            put(conversation.answers, model_key, legacy_model)


def keep_unchanged(c, ship: Ship) -> None:
    """A kept declaration never silently drops a supplied/edit answer."""
    raw = tomllib.loads(ship.file.read_text())
    def same(field, actual):
        answer = c.supplied(field)
        if answer is not MISSING and answer != actual:
            raise ShipError(f"answer {field} changes an existing ship; choose existing=update before editing it")
    for role in ROLES:
        if role not in ship.seats:
            continue
        seat = ship.seats[role]
        same({"engineering": "engineer"}.get(role, role), seat.harness)
        for field in ("name", "harness", "model", "effort", "model_mode", "effort_mode"):
            same(f"seats.{role}.{field}", seat.display if field == "name" else getattr(seat, field, None))
        pools = raw["seats"][role]["pool"]
        same(f"seats.{role}.pools", pools if isinstance(pools, list) else [pools])
    seen = set()
    for seat in ship.seats.values():
        for name in seat.pools:
            effective, pool = seat.for_pool(name), ship.pools[name]
            same(f"model.{effective.harness}", effective.model)
            if effective.harness in seen:
                continue
            seen.add(effective.harness)
            paid = c.supplied(f"payment.{effective.harness}")
            paid = "endpoint" if paid == "local" else paid
            if paid is not MISSING and paid != pool.kind and not (paid == "key" and harness.get(effective.harness).setup_metadata().key_endpoint is not None and pool.endpoint and pool.api_key):
                raise ShipError(f"answer payment.{effective.harness} changes a kept ship; choose existing=update")
            provider = c.supplied(f"provider.{effective.harness}")
            if provider is not MISSING and providers.canonical(provider) != pool.provider:
                raise ShipError(f"answer provider.{effective.harness} changes a kept ship; choose existing=update")
            same(f"endpoint.{effective.harness}", pool.endpoint)
            variable = pool.api_key.removeprefix("env:") if pool.api_key and pool.api_key.startswith("env:") else None if pool.api_key else "none"
            same(f"key_env.{effective.harness}", variable)
            home = c.supplied(f"subscription_home.{effective.harness}")
            if home is not MISSING and (pool.config_dir is None or Path(home).expanduser().resolve() != pool.config_dir.resolve()):
                raise ShipError(f"answer subscription_home.{effective.harness} changes a kept ship; choose existing=update")
    programs = c.supplied("programs")
    if programs is not MISSING and set(part.strip() for part in programs.split(",")) != seen:
        raise ShipError("answer programs changes a kept ship; choose existing=update")


def build(conversation, operations, text: str, orders: str, defaults: dict, available: dict) -> Proposal:
    """Build/validate in memory. No create, update, readiness or native-version operation is allowed here."""
    from . import setup, ship as ships
    c = conversation
    project = operations.project_folder().resolve()

    def ship_folder(value):
        folder = Path(value).expanduser().resolve()
        if not folder.is_relative_to(project):
            raise ShipError(f"answer folder must be inside the Quarterdeck project folder {project}; "
                            "the one-folder law keeps ship declarations and runtime there; "
                            f"choose {project / 'ships' / 'NAME'} or another folder inside it")
        return folder

    explicit_folder = c.supplied("folder")
    existing = None
    if explicit_folder is not MISSING:
        path = ship_folder(explicit_folder)
        if path.exists():
            existing = ships.load(path)
    operator = c.ask("operator", "What should the crew call you?", existing.operator if existing else "Admiral")
    c.say("Ship name suggestions: aurora, discovery, endeavour.")
    name = c.ask("name", "Ship name", existing.name if existing else "aurora")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.: -]*", name):
        raise ShipError("answer name must start with a letter or digit and contain a plain ship name")
    saved_office = existing.session_of(existing.seat("desk")).title if existing and "desk" in existing.seats else "Office"
    office = c.ask("office", "Office name", saved_office)
    for char in (".", ":"):
        if char in office:
            raise ShipError(f"answer office contains {char!r}; choose a tmux session name without that character")
    workspace = Path(c.ask("workspace", "Project folder", str(existing.workspace) if existing else operations.workspace_default())).expanduser().resolve()
    if not workspace.is_dir():
        raise ShipError("answer workspace is not an existing project folder; choose an existing directory")
    folder = ship_folder(c.ask("folder", "Folder for this ship", str(project / "ships" / name)))
    if folder.exists():
        existing = ships.load(folder)
        if not existing.is_repository:
            raise ShipError(f"{folder} is not its own git repository; initialize and commit its declaration yourself before rerunning setup")
        try:
            ships._git(folder, "rev-parse", "--verify", "HEAD")
        except ShipError:
            raise ShipError(f"{folder} has no first commit; resolve git identity and commit ship.toml, orders.md and .gitignore yourself before rerunning setup") from None
        mode = c.choice("existing", "Keep this ship, or update it?", ("keep", "update"), "keep")
        if mode == "keep":
            if (name, operator, workspace) != (existing.name, existing.operator, existing.workspace):
                raise ShipError("answer existing=keep conflicts with name/operator/workspace changes; choose update to apply them")
            keep_unchanged(c, existing)
            if office != saved_office:
                raise ShipError("answer office changes a kept ship; choose existing=update")
            setup.validate_declaration(existing)
            if "desk" not in existing.seats:
                raise ShipError("existing ship has no desk seat; add one before rerunning setup")
            return Proposal(existing, existing.file.read_text(), orders, [], existing, mode)
        if office != saved_office:
            raise ShipError("answer office changes an existing session title; edit the declaration and apply its session change explicitly before rerunning setup; nothing was written")
        for key in ("office", "bridge"):
            if key not in existing.sessions:
                raise ShipError(f"existing ship has no {key} session; use its own arrangement rather than setup's default crew")
    else:
        mode = "new"
        if not folder.parent.is_dir():
            raise ShipError("answer folder needs an existing parent; setup creates only its own ship folder")
    candidates = (setup.program_list(c.ask("programs", "Programs"), available) if c.supplied("programs") is not MISSING
                  else sorted(name for name, row in available.items() if row[0].passed))
    if not candidates:
        raise ShipError("no registered program found on PATH; install a supported program before rerunning setup")
    if len(candidates) == 1:
        hints = harness.get(candidates[0]).setup_metadata()
        if hints.seat_limit is not None and hints.seat_limit < len(ROLES):
            count = "one" if hints.seat_limit == 1 else str(hints.seat_limit)
            raise ShipError(f"{hints.display_name} is limited to {count} declared seat in setup; choose other programs for the remaining roles")
    preferred = sorted(candidates, key=lambda program: ("credential present" not in available[program][1], program))[0]
    c.say("Model and effort suggestions have not been checked against your account.")
    seats = {}
    for role in ROLES:
        saved = existing.seats.get(role) if existing else None
        sample = defaults["seats"][role]
        fields = {"name": c.ask(f"seats.{role}.name", f"{ROLE_LABELS[role]} name", saved.display if saved else sample.get("persona", role))}
        legacy = c.supplied({"engineering": "engineer"}.get(role, role))
        if legacy is not MISSING and c.supplied(f"seats.{role}.harness") is MISSING:
            put(c.answers, f"seats.{role}.harness", legacy)
        role_default = preferred
        limit = harness.get(preferred).setup_metadata().seat_limit
        if limit is not None and sum(fields["harness"] == preferred for fields in seats.values()) >= limit:
            role_default = next((program for program in candidates
                                 if harness.get(program).setup_metadata().seat_limit is None
                                 or sum(fields["harness"] == program for fields in seats.values())
                                 < harness.get(program).setup_metadata().seat_limit), None)
            if role_default is None:
                raise ShipError("selected programs' declared seat limits cannot supply the crew; choose another program")
        program = c.choice(f"seats.{role}.harness", f"{ROLE_LABELS[role]} harness ({', '.join(candidates)})", candidates,
                           legacy if legacy is not MISSING else saved.harness if saved and saved.harness in candidates else role_default)
        fields["harness"] = program
        compatible_legacy(c, role, program)
        metadata = operations.default_metadata(program)
        for field in ("model", "effort"):
            suggestion = suggested(getattr(metadata, field))
            if saved and saved.harness == program:
                old_mode, old_value = getattr(saved, field + "_mode", None), getattr(saved, field)
                if old_mode == "program-default":
                    suggestion = ("program-default", None)
                elif old_value:
                    suggestion = ("named", old_value)
                    if c.supplied(f"seats.{role}.{field}") is MISSING and c.supplied(f"seats.{role}.{field}_mode") is MISSING:
                        put(c.answers, f"seats.{role}.{field}", old_value)
                        if old_mode is not None:
                            put(c.answers, f"seats.{role}.{field}_mode", old_mode)
            if field == "model" and not metadata.model_default and c.supplied(f"seats.{role}.model") is MISSING and c.supplied(f"seats.{role}.model_mode") is MISSING:
                c.say("Model: choose after the local model's URL.")
                continue
            fields.update(c.control(role, field, suggestion))
        seats[role] = fields
    selected = list(dict.fromkeys([fields["harness"] for fields in seats.values()] + (candidates if c.supplied("programs") is not MISSING else [])))
    pools, served = setup.pool_answers(c, selected, defaults, operations=operations, existing=existing)
    names, edits = {}, [("ship", "name", name), ("ship", "operator", operator), ("ship", "workspace", str(workspace))]
    if not existing and office != "Office":
        edits.append(("sessions.office", "title", office))
    pool_harnesses = {}
    if existing:
        for seat in existing.seats.values():
            for pool in seat.pools:
                pool_harnesses.setdefault(pool, set()).add(seat.for_pool(pool).harness)
    def declare_pool(program, values):
        pool_name, index = program, 1
        while existing and pool_name in existing.pools and existing.pools[pool_name] != ships._pool(pool_name, values):
            pool_name, index = f"{program}-setup-{index}", index + 1
        names[program] = pool_name
        pool_harnesses.setdefault(pool_name, set()).add(program)
        edits.extend((f"pools.{pool_name}", key, value) for key, value in values.items())
        return pool_name
    for program, values in pools.items():
        declare_pool(program, values)
    # Keep the historical explicit programs inclusion behavior for unused programs.
    for program in selected:
        if program not in {fields["harness"] for fields in seats.values()}:
            key = f"worker-{program}"
            if existing and key in existing.seats and existing.seats[key].role != "worker":
                raise ShipError(f"seat {key} already has another role; use that ship's own arrangement")
            metadata = operations.default_metadata(program)
            model, effort = suggested(metadata.model), suggested(metadata.effort)
            seats[key] = {"harness": program, "model_mode": model[0], "effort_mode": effort[0]}
            if metadata.model is None and not metadata.model_default:
                model = ("named", c.ask(f"model.{program}", f"Model for {program}", served.get(program)))
                seats[key]["model_mode"] = model[0]
            if model[1] is not None:
                seats[key]["model"] = model[1]
            if effort[1] is not None:
                seats[key]["effort"] = effort[1]
    pool_specs = dict(existing.pools) if existing else {}
    pool_specs.update({names[program]: ships._pool(names[program], values) for program, values in pools.items()})
    for role, fields in seats.items():
        program, primary = fields["harness"], names[fields["harness"]]
        if "model" not in fields and "model_mode" not in fields:
            key = f"seats.{role}.model"
            fields["model"] = c.ask(key, f"Model for {ROLE_LABELS.get(role, role)}", served.get(program))
            fields["model_mode"] = "named"
        elif not operations.default_metadata(program).model_default and fields.get("model_mode") == "program-default":
            raise ShipError(f"answer seats.{role}.model_mode: {program} needs an explicit served model")
        default_pools = [primary] + [name for name, pool in pool_specs.items() if name != primary
                                   and program in pool_harnesses.get(name, ())
                                   and pool.kind == pool_specs[primary].kind and harness.get(program).accepts(pool) is None]
        if role in ROLES:
            saved = existing.seats.get(role) if existing else None
            if saved and saved.harness == program and primary in saved.pools:
                raw = tomllib.loads(existing.file.read_text())["seats"][role]["pool"]
                default_pools = raw if isinstance(raw, list) else [raw]
            choices = c.value(f"seats.{role}.pools", f"{ROLE_LABELS[role]} fallback order (comma-separated pools; /other adds another harness)", default_pools)
        else:
            choices = default_pools
        if not isinstance(choices, list) or not choices:
            raise ShipError(f"answer seats.{role}.pools must be a nonempty ordered list")
        for index, choice in enumerate(choices):
            if isinstance(choice, dict):
                path = f"seats.{role}.pools.{index}"
                chosen_harness = c.ask(path + ".harness", f"Harness for pool choice {index + 1}")
                if chosen_harness not in candidates:
                    raise ShipError(f"answer {path}.harness: choose a found program; cross-harness fallback must be explicit")
                requested_pool = c.supplied(path + ".pool")
                if requested_pool is not MISSING and not isinstance(requested_pool, str):
                    raise ShipError(f"answer {path}.pool must be a declared pool name")
                if requested_pool not in pool_specs and chosen_harness not in names:
                    added, _ = setup.pool_answers(c, [chosen_harness], defaults, operations=operations, existing=existing)
                    new_name = declare_pool(chosen_harness, added[chosen_harness])
                    pool_specs[new_name] = ships._pool(new_name, added[chosen_harness])
                requested_pool = c.ask(path + ".pool", f"Pool for choice {index + 1}", names.get(chosen_harness))
                if requested_pool not in pool_specs:
                    raise ShipError(f"answer {path}.pool: no declared pool {requested_pool}; choose {', '.join(pool_specs)}")
                metadata = operations.default_metadata(chosen_harness)
                for field in ("model", "effort"):
                    if field == "model" and not metadata.model_default and c.supplied(path + ".model_mode") == "program-default":
                        raise ShipError(f"answer {path}.model_mode: {chosen_harness} needs a named served model")
                    if field == "model" and not metadata.model_default and c.supplied(path + ".model") is MISSING:
                        choice[field] = c.ask(path + ".model", f"Model for pool choice {index + 1}")
                    else:
                        choice.update(c.control(role, f"pools.{index}.{field}", suggested(getattr(metadata, field))))
        if isinstance(choices[0], dict):
            for field in ("harness", "model", "effort"):
                if choices[0].get(field) != fields.get(field) or field != "harness" and choices[0].get(field + "_mode", "named") != fields.get(field + "_mode", "named"):
                    raise ShipError(f"answer seats.{role}.pools.0.{field} disagrees with seats.{role}.{field}; edit the primary choice together")
        fields["pool"] = choices[0] if len(choices) == 1 and isinstance(choices[0], str) else choices
        saved = existing.seats.get(role) if existing else None
        fresh_default = harness.get(program).setup_metadata().fresh_after_compactions
        if fresh_default is not None and (saved is None or harness.get(saved.harness).setup_metadata().fresh_after_compactions is None):
            fields["fresh_after_compactions"] = fresh_default
        # The shared writer cannot remove a key. A move to another harness keeps
        # the saved threshold and the proposal explicitly names its lack of effect.
        table = f"seats.{role}"
        edits.extend((table, key if key != "name" else "persona", value) for key, value in fields.items())
        if not existing or role not in existing.seats:
            edits += [(table, "role", role if role in ROLES else "worker"),
                      (table, "session", "office" if role == "desk" else "bridge")]
    proposed_text = existing.file.read_text() if existing else text
    if not existing:
        proposed_text = setup.new_text(proposed_text, edits)
    else:
        setup.preview_update(existing, edits)
        for table, key, value in edits:
            proposed_text = ships.edit(proposed_text, table, key, value)
    proposed = ships._from_text(proposed_text, folder)
    setup.validate_declaration(proposed)
    for pool in proposed.pools.values():
        if pool.endpoint:
            setup.safe_endpoint(pool.endpoint)
    return Proposal(proposed, proposed_text, orders, edits, existing, mode)


def git_root(folder: Path, flag: str) -> Path | None:
    """Bounded Git metadata only; never initializes or changes a project."""
    try:
        done = subprocess.run(["git", "-C", str(folder), "rev-parse", flag],
                              capture_output=True, text=True, timeout=5)
        if done.returncode or not done.stdout.strip():
            return None
        path = Path(done.stdout.strip())
        return (path if path.is_absolute() else folder / path).resolve()
    except (OSError, subprocess.TimeoutExpired):
        return None


def workspace_default() -> str | None:
    cwd = Path.cwd().resolve()
    root = git_root(cwd, "--show-toplevel")
    if root is None:
        return None
    framework = standing.project_dir().resolve()
    own_common = git_root(framework, "--git-common-dir")
    if root == framework or own_common is not None and git_root(cwd, "--git-common-dir") == own_common:
        return None
    # Also recognize a separate framework clone when running from packaged resources.
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text()).get("project", {})
        if project.get("name") == "quarterdeck" and (root / "src" / "quarterdeck").is_dir():
            return None
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        return None  # an unreadable/ambiguous project gets no silent suggestion
    return str(cwd)


def local_endpoint(url: str) -> bool:
    """Only the person's loopback server is eligible for pre-accept informational discovery."""
    host = urlsplit(url).hostname
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def summary(ship: Ship, display_control, *, existing: Ship | None = None) -> dict:
    """Ordered summary fields for both plain text and JSON; control modes belong to the shared contract."""
    rows, runs, fallbacks = [], {}, {}
    unchecked = False
    for seat in ship.seats.values():
        selected = seat.for_pool(seat.pools[0])
        model, effort = (display_control(selected, name) for name in ("model", "effort"))
        unchecked |= "program's default" in (model, effort)
        label = ROLE_LABELS.get(seat.role, seat.name)
        row = {"seat": label, "name": seat.display,
               "harness": harness.get(selected.harness).setup_metadata().display_name, "model": model, "effort": effort}
        threshold = seat.fresh_after_compactions
        if threshold is not None:
            row["fresh_after_compactions"] = threshold
            if harness.get(seat.harness).setup_metadata().fresh_after_compactions is None:
                detail = f"no effect, {seat.harness} does not count them (retained value: {threshold})"
            else:
                detail = str(threshold) if threshold else "disabled"
                saved = existing.seats.get(seat.name) if existing else None
                if saved and harness.get(saved.harness).setup_metadata().fresh_after_compactions is None:
                    detail += f" (default added for {seat.harness})"
            if existing is not None:
                row["fresh_start_note"] = "fresh after compactions: " + detail
        rows.append(row)
        for pool_name in seat.pools:
            effective, pool = seat.for_pool(pool_name), ship.pools[pool_name]
            program = harness.get(effective.harness).setup_metadata().display_name
            if pool.kind == "subscription":
                detail = "a subscription you are signed in to"
                if pool.config_dir is not None:
                    detail += f" ({pool.config_dir})"
            elif pool.kind == "key":
                # The setup validator accepts only env:NAME; never render a direct key value.
                variable = pool.api_key.removeprefix("env:") if pool.api_key and pool.api_key.startswith("env:") else "variable unknown"
                detail = f"an API key ({variable})"
            else:
                if local_endpoint(pool.endpoint):
                    detail = f"a local model at {pool.endpoint}"
                elif pool.api_key and pool.api_key.startswith("env:"):
                    detail = f"an API key ({pool.api_key.removeprefix('env:')}) at {pool.endpoint}"
                else:
                    detail = f"a model at {pool.endpoint}"
            if detail not in runs.setdefault(program, []):
                runs[program].append(detail)
        if len(seat.pools) > 1:
            fallbacks[seat.name] = list(seat.pools)
    office = ship.session_of(ship.seat("desk")).title
    result = {"operator_name": ship.operator, "office_name": office, "ship_name": ship.name, "project_path": str(ship.workspace),
              "seats": rows, "runs_on": {name: "; ".join(values) for name, values in runs.items()}}
    if unchecked:
        result["note"] = "The program's default is not checked against the account."
    if fallbacks:
        result["fallback_order"] = fallbacks
    return result


def render_summary(data: dict) -> list[str]:
    """The whole presentation lives here: five columns, then one Runs on line per harness."""
    project_path = Path(data["project_path"])
    home = Path.home()
    shown_project = str(Path("~") / project_path.relative_to(home)) if project_path.is_relative_to(home) else str(project_path)
    lines = [f"Operator name:  {data['operator_name']}", f"Office name:    {data['office_name']}", "",
             f"Ship name:      {data['ship_name']}", f"Project path:   {shown_project}"]
    fields, headings = ("seat", "name", "harness", "model", "effort"), ("Seat", "Name", "Harness", "Model", "Effort")
    table = [headings] + [tuple(row[field] for field in fields) for row in data["seats"]]
    widths = [max(len(row[index]) for row in table) for index in range(len(fields))]
    lines.append("")
    for row in table:
        line = "   ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip()
        lines.append(line)
    lines += ["", "Runs on"]
    width = max((len(name) for name in data["runs_on"]), default=0)
    for name, value in data["runs_on"].items():
        shown = value.replace(f"({home}/", "(~/").replace(f"({home})", "(~)")
        lines.append(f"  {name:<{width}}   {shown}")
    if "note" in data:
        lines += ["", data["note"]]
    for row in data["seats"]:
        if note := row.get("fresh_start_note"):
            lines.append(f"  {row['name']} {note}")
    for name, pools in data.get("fallback_order", {}).items():
        lines.append(f"  {name} fallback order: " + " -> ".join(pools))
    return lines
