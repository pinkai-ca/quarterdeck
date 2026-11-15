"""Declare a template ship after the person approves its exact proposal.

Discovery remains a read of ship folders. The office keeps consent, not another
fleet configuration; every raised ship owns its declaration and runtime.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys

from . import setup, ship as ships, standing
from .ship import ShipError

KINDS = ("default", "forum", "cascade", "shuttle")


def process_ancestry(pid: int, *, proc: Path = Path("/proc")) -> tuple[tuple[int, str, int], ...]:
    """Read each Linux process identity and parent through the observed root."""
    chain, seen = [], set()
    try:
        while pid:
            if pid in seen or len(seen) >= 4096:
                raise ValueError("cyclic or excessive ancestry")
            seen.add(pid)
            text = (proc / str(pid) / "stat").read_text()
            identity, tail = text.rsplit(")", 1)
            if int(identity.split("(", 1)[0].strip()) != pid:
                raise ValueError("process identity changed")
            fields = tail.split()
            parent, start = int(fields[1]), fields[19]
            if parent < 0 or not start.isdecimal():
                raise ValueError("invalid process identity")
            chain.append((pid, start, parent))
            pid = parent
    except (OSError, UnicodeError, ValueError, IndexError) as error:
        raise ShipError(f"cannot read the approving process's ancestry ({type(error).__name__}); "
                        "no yes accepted; use a readable person-owned shell") from None
    if not chain:
        raise ShipError("cannot read an empty approving process ancestry; no yes accepted")
    return tuple(chain)


def recorded_programs(project: Path) -> set[tuple[int, str]]:
    """Read only current seat launch identities, including the separate office."""
    def entries(folder):
        try:
            return sorted(folder.iterdir())
        except FileNotFoundError:
            return []

    programs = set()
    try:
        folders = [project / "office", *[p for p in entries(project / "ships") if p.is_dir()]]
        for folder in folders:
            for seat in entries(folder / "runtime" / "trusted"):  # trusted_files: what qd wrote, out of every jail's reach
                if not seat.is_dir():
                    continue
                path = seat / "launch.json"
                try:
                    data = json.loads(path.read_text())
                except FileNotFoundError:
                    continue  # no recorded program at this seat
                pid, start = data.get("pid"), data.get("start")
                if type(pid) is not int or pid <= 0 or not isinstance(start, str) or not start.isdecimal():
                    raise ValueError("unreadable launch process identity")
                programs.add((pid, start))
    except (OSError, UnicodeError, ValueError, AttributeError) as error:
        raise ShipError(f"cannot read the project's recorded seat programs ({type(error).__name__}); "
                        "no yes accepted; retain the launch records for inspection") from None
    return programs


def require_person_process(project: Path) -> None:
    """Check live ancestry at a new yes; cached consent remains a known trust limit."""
    pid = os.getpid()
    before = process_ancestry(pid)
    programs = recorded_programs(project)
    after = process_ancestry(pid)
    if before != after:
        raise ShipError("the approving process's ancestry changed; no yes accepted; ask again from the person's shell")
    if any((ancestor, start) in programs for ancestor, start, _ in after):
        raise ShipError("a recorded seat program owns this process ancestry; only the person's own shell can answer yes")


def arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("kind", choices=KINDS)
    parser.add_argument("name", help="the new folder name and fleet address")
    parser.add_argument("--pool", action="append", default=[], metavar="TEMPLATE_POOL=OFFICE_POOL",
                        help="use a pool declared in the office; unchanged names are reused")
    parser.add_argument("--model", action="append", default=[], metavar="SEAT=MODEL")
    parser.add_argument("--workspace", help="existing project folder for this crew")
    parser.add_argument("--approve", metavar="DIGEST", help="the exact pending proposal the person is answering")
    parser.add_argument("--accept", choices=("yes", "no"), help="the person's explicit answer, from their own shell")


def assignments(values: list[str], label: str) -> dict[str, str]:
    result = {}
    for value in values:
        key, equals, answer = value.partition("=")
        if not equals or not key or not answer or key in result:
            raise ShipError(f"--{label} needs distinct NAME=VALUE entries")
        result[key] = answer
    return result


@dataclass(frozen=True)
class Proposal:
    kind: str
    name: str
    folder: Path
    files: dict[str, str]

    @property
    def digest(self) -> str:
        value = [self.kind, self.name, str(self.folder), self.files]
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    @property
    def summary(self) -> str:
        return (f"Raise {self.kind} ship {self.name} at {self.folder}\n"
                f"Fleet address: {self.name}\nFiles: {', '.join(sorted(self.files))}\n"
                "The crew's opening briefs may spend subscription turns.\n"
                "Complete declaration (credential references only):\n" + self.files["ship.toml"])


def proposal(args: argparse.Namespace, office: ships.Ship) -> Proposal:
    """Read and validate a complete template and pool references, without writes."""
    if (args.kind not in KINDS or not ships.valid_name(args.name)
            or "@" in args.name or args.name in ships.TEMPLATES):
        raise ShipError("choose default, forum, cascade or shuttle and a new non-template ship folder name")
    folder = office.folder.parent / "ships" / args.name
    if folder.parent.is_symlink() or folder.is_symlink():
        raise ShipError("raised ships need real project folders, not links")
    source = standing.project_dir() / "ships" / args.kind
    names = ["ship.toml", "orders.md"]
    names += [name for name in ("README.md", "mission-record.md", "work/README.md") if (source / name).is_file()]
    names += [str(path.relative_to(source)) for path in sorted((source / "roles").glob("*.md"))]
    try:
        files = {name: (source / name).read_text() for name in names}
    except (OSError, UnicodeError) as error:
        raise ShipError(f"{args.kind} template unavailable ({type(error).__name__}); install its resources") from None
    text = files["ship.toml"]
    import tomllib
    data = tomllib.loads(text)
    selected = assignments(args.pool, "pool")
    models = assignments(args.model, "model")
    if set(selected) - set(data.get("pools", {})):
        raise ShipError("--pool names a pool absent from the selected template")
    if set(models) - set(data.get("seats", {})):
        raise ShipError("--model names a seat absent from the selected template")
    # Remove the examples' payment tables. Credentials themselves are never read.
    kept, table = [], ""
    for line in text.splitlines():
        if match := ships._HEADER.match(line):
            table = match.group(1).strip()
        if not table.startswith("pools."):
            kept.append(line)
    text = "\n".join(kept) + "\n"
    office_data = tomllib.loads(office.file.read_text())
    for alias in data.get("pools", {}):
        chosen = selected.get(alias, alias)
        if chosen not in office.pools:
            raise ShipError(f"template pool {alias} needs an office pool; pass --pool {alias}=OFFICE_POOL")
        pool = dict(office_data["pools"][chosen])
        if "api_key" in pool and not re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", pool["api_key"]):
            raise ShipError(f"office pool {chosen}: raise accepts env:NAME key references only, never key values")
        if "endpoint" in pool:
            pool["endpoint"] = setup.safe_endpoint(pool["endpoint"])
        for key in ("config_dir", "home"):
            if key in pool:
                path = Path(pool[key]).expanduser()
                pool[key] = str((office.folder / path).resolve())
        for key, value in pool.items():
            text = ships.edit(text, f"pools.{alias}", key, value)
    text = ships.edit(text, "ship", "name", args.name)
    text = ships.edit(text, "ship", "operator", office.operator)
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else office.folder.parent
    if not workspace.is_dir():
        raise ShipError("--workspace must name an existing folder")
    text = ships.edit(text, "ship", "workspace", str(workspace))
    for seat, model in models.items():
        text = ships.edit(text, f"seats.{seat}", "model", model)
    current = ships._from_text(text, folder)
    ships._validate(current)
    if any(spec.model and ("REPLACE_" in spec.model or spec.model == "local-model")
           for spec in current.seats.values()):
        raise ShipError("the template needs an available model; pass --model SEAT=MODEL before approval")
    files["ship.toml"] = text
    files[".gitignore"] = "runtime/\n"
    return Proposal(args.kind, args.name, folder, files)


def office_for(args: argparse.Namespace, operations: setup.Operations) -> tuple[ships.Ship, bool]:
    caller = os.environ.get("QD_SEAT")
    if caller:
        origin = os.environ.get("QD_SHIP")
        if not origin:
            raise ShipError("a seat raises ships only from its declared office desk")
        office = ships.load(Path(origin).expanduser().resolve())
        if office.kind != "office" or caller not in office.seats or office.seat(caller).role != "desk":
            raise ShipError("only the person or the office desk raises a ship; a crew seat cannot")
        if args.ship and Path(args.ship).expanduser().resolve() != office.folder:
            raise ShipError("raise must use the caller's own office")
    elif args.ship:
        office = ships.load(Path(args.ship).expanduser().resolve())
    else:
        office = ships.load(setup.office_folder(operations.project_folder()))
    if office.kind != "office" or office.folder.name != "office" or office.folder.is_symlink():
        raise ShipError("raise needs the project's office at project/office; run qd init first")
    return office, not bool(caller)


@contextmanager
def locked(runtime: Path, name: str):
    """One raise of a name at a time: a lock of the office's own, hidden from every jail, waited for within a bound."""
    from . import ship_locks
    with ship_locks.held(ship_locks.path(runtime, "raise", f"{name}.lock"), f"raise {name}"):
        yield


def write_once(path: Path, value: dict) -> None:
    """Publish a complete immutable consent record while the name's lock is held."""
    if path.exists():
        return
    ships._write_atomically(path, json.dumps(value, sort_keys=True, indent=2) + "\n")


def approval_command(args: argparse.Namespace, office: ships.Ship, digest: str) -> str:
    command = ["qd", "--ship", str(office.folder), "raise", args.kind, args.name]
    for flag in ("pool", "model"):
        for entry in getattr(args, flag):
            command += [f"--{flag}", entry]
    if args.workspace:
        command += ["--workspace", str(Path(args.workspace).expanduser().resolve())]
    return shlex.join([*command, "--approve", digest, "--accept", "yes"])


def run(args: argparse.Namespace, *, operations: setup.Operations | None = None,
        interactive: bool | None = None, read=None, write=None) -> list[str]:
    operations = operations or setup.Operations()
    read, write = read or input, write or print
    office, person = office_for(args, operations)
    if not person and args.accept is not None:
        raise ShipError("the desk cannot answer for the person; use the person's own shell")
    draft = proposal(args, office)
    if args.approve and args.approve != draft.digest:
        raise ShipError("the proposal changed; ask the person again using the new proposal digest")
    ships.fence(office)
    records = office.runtime / "raise" / draft.name
    records.mkdir(parents=True, exist_ok=True)
    with locked(office.runtime, draft.name):
        request = records / f"{draft.digest}.request.json"
        answers = sorted(records.glob(f"{draft.digest}.answer-*.json"))
        answer = answers[-1] if answers else None
        write_once(request, {"format": 1, "digest": draft.digest, "summary": draft.summary})
        approved = False
        if answer is not None:
            saved = json.loads(answer.read_text())
            if saved.get("format") != 1 or saved.get("digest") != draft.digest or saved.get("answer") not in ("yes", "no"):
                raise ShipError("unreadable office raise answer; retain it and ask the person to inspect it")
            approved = saved["answer"] == "yes"
        if person and (args.accept is not None or not approved):
            response = args.accept
            if response is None and (sys.stdin.isatty() if interactive is None else interactive):
                write(draft.summary)
                response = read("Raise this exact ship and start its crew? [yes/no] ").strip().lower()
                if response not in ("yes", "no"):
                    raise ShipError("answer yes or no; no ship was raised")
            if response is not None:
                if response == "yes":
                    require_person_process(office.folder.parent)
                answer = records / f"{draft.digest}.answer-{len(answers) + 1:08d}.json"
                write_once(answer, {"format": 1, "digest": draft.digest, "answer": response, "by": "person"})
                approved = response == "yes"
        if not approved:
            state = "declined" if answer is not None else "pending the person's answer"
            return [draft.summary, f"Raise {state}; no ship started. Answer recorded in {records}.",
                    "From the person's own shell (QD_SEAT absent): " + approval_command(args, office, draft.digest)]
        folder = draft.folder
        lines = []
        if folder.exists():
            if any(not (folder / name).is_file() or (folder / name).read_text() != text
                   for name, text in draft.files.items()):
                raise ShipError(f"{folder} differs from the approved proposal; nothing is overwritten. "
                                "Ask the person again about those changes; retain this ship and choose a new name")
            current = ships.load(folder)
            if not current.is_repository:
                raise ShipError("the matching ship needs its own committed repository before raise can resume it")
            ships._git(folder, "rev-parse", "--verify", "HEAD")
            lines.append(f"{draft.name} already declared; resuming without edits")
        else:
            folder.parent.mkdir(exist_ok=True)
            folder.mkdir()
            for name, text in draft.files.items():
                path = folder / name
                path.parent.mkdir(parents=True, exist_ok=True)
                ships._write_atomically(path, text)
            try:
                ships._git(folder, "init", "--quiet")
                commit = ships.commit(folder, list(draft.files), f"qd raise: declare {draft.kind} {draft.name}")
            except ShipError as error:
                raise ShipError(f"ship saved at {folder}, but its first commit is unfinished: {error}; "
                                "commit only its declaration and template files before rerunning raise") from None
            current = ships.load(folder)
            lines.append(f"{draft.name} created at {folder} · committed {commit} · runtime ignored")
        result = operations.up(current)
        return lines + ([result] if isinstance(result, str) else result) + [f"Fleet address: {draft.name}"]
