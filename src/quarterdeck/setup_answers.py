"""One answer resolver for setup's conversation, flags, file and summary edits."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from .ship import ShipError

MAPS = ("payment", "subscription_home", "provider", "key_env", "endpoint", "model")
SCALARS = ("programs", "desk", "captain", "engineer", "workspace", "name", "folder",
           "existing", "check", "start", "wait_for_sign_in", "operator", "office", "accept", "use_defaults")
ROLES = ("desk", "captain", "engineering")
SEAT_FIELDS = ("name", "harness", "model", "effort", "model_mode", "effort_mode", "pools")
MISSING = object()
# What the office alone reads (setup.first_step, setup_proposal.build with desk_only): its two consents, the use of
# defaults, and the desk's own answers, which a run with a crew carries into the office the same way: the operator, the
# office's title, the desk's seat (``desk`` is its program) and that program's pool, in the maps keyed by program.
OFFICE_SCALARS = ("accept", "start", "use_defaults", "operator", "office", "desk")
OFFICE_SEATS = ("desk",)
# Every other answer only a crew reads, so only such an answer asks for a crew. Derived, so a new answer counts for the
# crew unless the office reads it too.
CREW_SCALARS = tuple(name for name in SCALARS if name not in OFFICE_SCALARS)
CREW_SEATS = tuple(role for role in ROLES if role not in OFFICE_SEATS)


def asks_for_crew(answers: dict) -> bool:
    """Whether an answer only a crew reads was given: the office alone is set up otherwise."""
    seats = answers.get("seats")
    return (any(answers.get(name) for name in CREW_SCALARS)
            or isinstance(seats, dict) and any(role in CREW_SEATS for role in seats))


def names_office(answers: dict) -> bool:
    """Whether an answer of the office's own declaration was given, beyond its consents and the use of defaults."""
    seats = answers.get("seats")
    return (any(answers.get(name) for name in OFFICE_SCALARS if name not in ("accept", "start", "use_defaults"))
            or isinstance(seats, dict) and any(role in OFFICE_SEATS for role in seats)
            or any(answers.get(name) for name in MAPS))


def field_parts(field: str) -> list[str]:
    parts = field.split(".")
    valid = (len(parts) == 1 and parts[0] in SCALARS
             or len(parts) == 2 and parts[0] in MAPS and bool(parts[1])
             or len(parts) >= 3 and parts[0] == "seats" and parts[1] in ROLES
             and parts[2] in SEAT_FIELDS and (len(parts) == 3 or parts[2] == "pools"))
    if not valid:
        raise ShipError(f"unknown setup answer {field}; use a documented answer name")
    return parts


def get(answers: dict, field: str):
    value = answers
    for part in field.split("."):
        if isinstance(value, dict):
            value = value.get(part, MISSING)
        elif isinstance(value, list) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            return MISSING
    return value


def discard(answers: dict, field: str) -> None:
    parts = field.split(".")
    target = answers
    for part in parts[:-1]:
        if isinstance(target, dict) and part in target:
            target = target[part]
        elif isinstance(target, list) and part.isdecimal() and int(part) < len(target):
            target = target[int(part)]
        else:
            return
    if isinstance(target, dict):
        target.pop(parts[-1], None)


def put(answers: dict, field: str, value) -> None:
    parts = field_parts(field)
    target = answers
    for part in parts[:-1]:
        if isinstance(target, list):
            if not part.isdecimal() or int(part) >= len(target):
                raise ShipError(f"answer {field}: pool choice position does not exist")
            target = target[int(part)]
        elif isinstance(target, dict):
            target = target.setdefault(part, {})
        else:
            raise ShipError(f"answer {field}: parent is not an object")
    if isinstance(target, dict):
        target[parts[-1]] = copy.deepcopy(value)
    elif isinstance(target, list) and parts[-1].isdecimal() and int(parts[-1]) < len(target):
        target[int(parts[-1])] = copy.deepcopy(value)
    else:
        raise ShipError(f"answer {field}: pool choice position does not exist")


def text_or_json(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def revise(answers: dict, field: str, value) -> None:
    """A new-field mode edit changes the draft; existing application still owns its refusals."""
    field_parts(field)
    if value is None:
        raise ShipError(f"answer {field}: null/unset is not a setup answer; use an explicit supported mode")
    put(answers, field, value)
    aliases = {"desk": "desk", "captain": "captain", "engineer": "engineering"}
    if field in aliases and get(answers, f"seats.{aliases[field]}.harness") is not MISSING:
        put(answers, f"seats.{aliases[field]}.harness", value)
    if field.startswith("seats.") and field.endswith(".harness"):
        alias = {"engineering": "engineer"}.get(field.split(".")[1], field.split(".")[1])
        if get(answers, alias) is not MISSING:
            answers[alias] = value
    if field.startswith("seats."):
        if field.endswith((".model_mode", ".effort_mode")) and value == "program-default":
            discard(answers, field.removesuffix("_mode"))
        elif field.endswith((".model", ".effort")) and get(answers, field + "_mode") == "program-default":
            put(answers, field + "_mode", "named")


def validate(answers: dict) -> None:
    if not isinstance(answers, dict) or set(answers) - set(SCALARS + MAPS + ("seats", "edits")):
        raise ShipError("answers file must contain documented setup answer names only")
    for name, value in answers.items():
        if name in SCALARS and (not isinstance(value, str) or not value.strip()):
            raise ShipError(f"answer {name} must be nonempty text")
        if name in MAPS and (not isinstance(value, dict)
                            or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v.strip()
                                   for k, v in value.items())):
            raise ShipError(f"answer {name} must map program names to nonempty text")
    for role, fields in answers.get("seats", {}).items() if isinstance(answers.get("seats", {}), dict) else [("", None)]:
        if role not in ROLES or not isinstance(fields, dict) or set(fields) - set(SEAT_FIELDS):
            raise ShipError(f"answer seats.{role}: choose a documented seat and its fields")
        for name, value in fields.items():
            if name == "pools":
                if not isinstance(value, list) or not value:
                    raise ShipError(f"answer seats.{role}.pools must be a nonempty ordered list")
            elif not isinstance(value, str) or not value.strip():
                raise ShipError(f"answer seats.{role}.{name} must be nonempty text")
    for name in ("accept", "use_defaults", "check", "start", "wait_for_sign_in"):
        if name in answers and answers[name] not in ("yes", "no"):
            raise ShipError(f"answer {name}: choose yes or no")


def read_answers(args: argparse.Namespace) -> dict:
    answers = {}
    if args.answers:
        try:
            answers = json.loads(Path(args.answers).expanduser().read_text())
        except (OSError, ValueError):
            raise ShipError("cannot read answers file as a JSON object; supply --answers FILE") from None
    validate(answers)
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
    for entry in getattr(args, "seat", None) or []:
        field, equals, raw = entry.partition("=")
        if not equals or not raw:
            raise ShipError("--seat needs ROLE.FIELD=ANSWER")
        put(answers, "seats." + field, text_or_json(raw))
    edits = answers.pop("edits", [])
    if not isinstance(edits, list):
        raise ShipError("answer edits must be an ordered list of field/value objects")
    for entry in getattr(args, "edit", None) or []:
        field, equals, raw = entry.partition("=")
        if not equals or not raw:
            raise ShipError("--edit needs FIELD=VALUE (JSON for structured values)")
        edits.append({"field": field, "value": text_or_json(raw)})
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"field", "value"} or not isinstance(edit["field"], str):
            raise ShipError("answer edits must contain exactly field and value")
        if edit["field"] in ("accept", "check", "start"):
            raise ShipError(f"answer edits: supply {edit['field']} separately as explicit consent")
        revise(answers, edit["field"], edit["value"])
    if args.ship:
        if answers.get("folder") and Path(answers["folder"]).expanduser().resolve() != Path(args.ship).expanduser().resolve():
            raise ShipError("--ship and --folder name different folders; choose one target")
        answers["folder"] = args.ship
    validate(answers)
    return answers


class Conversation:
    def __init__(self, answers: dict, *, interactive: bool, read=input, write=print):
        self.answers, self.interactive, self.read, self.write = copy.deepcopy(answers), interactive, read, write
        self.suggestions = set()

    def say(self, message: str) -> None:
        self.write(message)

    def supplied(self, field: str):
        return get(self.answers, field)

    def missing(self, field: str):
        parts = field.split(".")
        if parts[0] == "seats":
            flag = "--seat " + ".".join(parts[1:]) + "=ANSWER"
        elif len(parts) == 2:
            flag = f"--{parts[0].replace('_', '-')} {parts[1]}=ANSWER"
        else:
            flag = f"--{field.replace('_', '-')} ANSWER"
        raise ShipError(f"missing answer {field}; pass {flag} or --answers FILE")

    def value(self, field: str, prompt: str, default=MISSING, again: str | None = None):
        """``again``: with no suggestion, an empty answer in a conversation says this line and asks again."""
        value = self.supplied(field)
        if value is MISSING:
            defaults_allowed = self.answers.get("use_defaults") == "yes" and field not in ("accept", "check", "start")
            if not self.interactive:
                if not defaults_allowed or default is MISSING:
                    self.missing(field)
                value = default
                self.suggestions.add(field)
            else:
                shown = "" if default is MISSING else " [" + (default if isinstance(default, str) else json.dumps(default)) + "]"
                while True:
                    try:
                        raw = self.read(prompt + shown + ": ").strip()
                    except (EOFError, KeyboardInterrupt):
                        raise ShipError(f"setup stopped at answer {field}; rerun qd setup to continue") from None
                    if raw or default is not MISSING or again is None:
                        break
                    self.say(again)
                if not raw:
                    if default is MISSING:
                        self.missing(field)
                    value = default
                    self.suggestions.add(field)
                else:
                    value = text_or_json(raw) if isinstance(default, (list, dict)) else raw
                    if isinstance(default, list) and field.endswith(".pools") and isinstance(value, str):
                        value = [{} if part.strip() == "/other" else part.strip() for part in value.split(",")]
            put(self.answers, field, value)
        return self.supplied(field)

    def ask(self, name: str, prompt: str, default: str | None = None, again: str | None = None) -> str:
        value = self.value(name, prompt, MISSING if default is None else default, again)
        if not isinstance(value, str) or not value.strip():
            raise ShipError(f"answer {name} must be nonempty text; supply {name} again")
        if "\n" in value or "\r" in value:
            raise ShipError(f"answer {name} must fit on one line")
        return value.strip()

    def choice(self, name: str, prompt: str, choices, default: str | None = None) -> str:
        value = self.ask(name, prompt, default)
        if value not in choices:
            raise ShipError(f"answer {name}: choose {', '.join(choices)}; rerun with that answer")
        return value

    def control(self, role: str, field: str, suggestion: tuple[str, str | None]) -> dict:
        """A UI action selects the NEW mode; supplied legacy strings are never magic words."""
        key = f"seats.{role}.{field}"
        mode_key = key + "_mode"
        output = field.rsplit(".", 1)[-1]
        mode, value = self.supplied(mode_key), self.supplied(key)
        if mode is not MISSING:
            if mode not in ("named", "program-default"):
                raise ShipError(f"answer {mode_key}: choose named or program-default")
            if mode == "program-default":
                if value is not MISSING:
                    raise ShipError(f"answer {key} must be absent with {mode_key}=program-default")
                return {output + "_mode": mode}
            return {output + "_mode": mode, output: self.ask(key, field.capitalize())}
        if value is not MISSING:
            return {output: self.ask(key, field.capitalize())}
        suggested_mode, suggested_value = suggestion
        if not self.interactive:
            if self.answers.get("use_defaults") != "yes":
                self.missing(key)
            put(self.answers, mode_key, suggested_mode)
            self.suggestions.add(mode_key)
            if suggested_mode == "named":
                put(self.answers, key, suggested_value)
                self.suggestions.add(key)
                return {output + "_mode": suggested_mode, output: suggested_value}
            return {output + "_mode": suggested_mode}
        shown = suggested_value if suggested_mode == "named" else "program's default"
        try:
            raw = self.read(f"{field.capitalize()} (enter a name, or /default for the program's default) [{shown}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise ShipError(f"setup stopped at answer {key}; rerun qd setup to continue") from None
        chosen_mode = suggested_mode if not raw else "program-default" if raw == "/default" else "named"
        put(self.answers, mode_key, chosen_mode)
        if not raw:
            self.suggestions.add(mode_key)
        result = {output + "_mode": chosen_mode}
        if chosen_mode == "named":
            chosen_value = raw or suggested_value
            if not chosen_value:
                self.missing(key)
            put(self.answers, key, chosen_value)
            result[output] = chosen_value
            if not raw:
                self.suggestions.add(key)
        return result

    def review(self, render) -> bool | None:
        """Only the caller can apply a proposal, after this returns explicit acceptance."""
        while True:
            for line in render():
                self.say(line)
            if not self.interactive or self.supplied("accept") is not MISSING:
                return self.choice("accept", "Accept this ship? yes or no", ("yes", "no")) == "yes"
            try:
                action = self.read("Change an item (FIELD=VALUE), accept, or stop [accept]: ").strip()
            except (EOFError, KeyboardInterrupt):
                return False
            if action in ("", "accept", "yes"):
                return True
            if action in ("stop", "no"):
                return False
            field, equals, raw = action.partition("=")
            if not equals or not raw or field in ("accept", "check", "start"):
                raise ShipError("summary edit needs a documented FIELD=VALUE, accept, or stop")
            revise(self.answers, field, text_or_json(raw))
            self.suggestions.discard(field)
            dependent = set()
            if field in ("name", "workspace"):
                dependent.add("folder")
            if field.endswith(".harness") or field in ("desk", "captain", "engineer"):
                role = field.split(".")[1] if field.startswith("seats.") else {"engineer": "engineering"}.get(field, field)
                dependent.update(f"seats.{role}.{key}" for key in SEAT_FIELDS if key != "name" and key != "harness")
            if field.startswith(("payment.", "endpoint.")):
                dependent.update(name for name in self.suggestions if name.startswith("seats.") and not name.endswith((".name", ".harness")))
            if field.startswith("model."):
                dependent.update(name for name in self.suggestions if name.endswith((".model", ".model_mode")))
            for name in dependent & self.suggestions:
                discard(self.answers, name)
            self.suggestions.difference_update(dependent)
            validate(self.answers)
            # The proposal owner rebuilds/revalidates after each edit before asking acceptance again.
            return None
