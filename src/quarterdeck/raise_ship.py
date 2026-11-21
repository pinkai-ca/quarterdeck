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

from . import keyfile, setup, ship as ships, standing
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


def recorded_programs(project: Path) -> tuple[set[tuple[int, str]], tuple[tuple[int, Path], ...]]:
    """Read only current seat launch identities, including the separate office.

    Returns the (pid, start) identities, and each record whose start is missing or not a
    decimal start time with its pid: such a record can be matched by its pid alone.
    """
    def entries(folder):
        try:
            return sorted(folder.iterdir())
        except FileNotFoundError:
            return []

    programs, startless = set(), []
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
                if type(pid) is not int or pid <= 0:
                    raise ValueError("unreadable launch process identity")
                if isinstance(start, str) and start.isdecimal():
                    programs.add((pid, start))
                else:
                    startless.append((pid, path))
    except (OSError, UnicodeError, ValueError, AttributeError) as error:
        raise ShipError(f"cannot read the project's recorded seat programs ({type(error).__name__}); "
                        "no yes accepted; retain the launch records for inspection") from None
    return programs, tuple(startless)


def require_person_process(project: Path) -> list[str]:
    """Check live ancestry at a new yes; cached consent remains a known trust limit.

    Returns a line for each launch record without a readable start time that was ignored,
    its pid being no pid of this ancestry; the answer names them.
    """
    return _person_process(project)[0]


def _person_process(project: Path) -> tuple[list[str], tuple[tuple[int, str, int], ...]]:
    pid = os.getpid()
    before = process_ancestry(pid)
    programs, startless = recorded_programs(project)
    after = process_ancestry(pid)
    if before != after:
        raise ShipError("the approving process's ancestry changed; no yes accepted; ask again from the person's shell")
    if any((ancestor, start) in programs for ancestor, start, _ in after):
        raise ShipError("a recorded seat program owns this process ancestry; only the person's own shell can answer yes")
    ancestors = {ancestor for ancestor, _, _ in after}
    for recorded, path in startless:
        if recorded in ancestors:
            raise ShipError(f"a recorded seat program without a readable start time ({path}) has pid {recorded}, "
                            "which is in this process ancestry; only the person's own shell can answer yes")
    return [f"a recorded seat program without a readable start time was ignored: {path} "
            f"(its pid {recorded} is not in this process ancestry)" for recorded, path in startless], after


def controlling_terminal(*, proc: Path = Path("/proc")) -> int | None:
    """This process's controlling terminal as a device number, or None when it has none (detached)."""
    try:
        text = (proc / "self" / "stat").read_text()
        tty_nr = int(text.rsplit(")", 1)[1].split()[4])
    except (OSError, UnicodeError, ValueError, IndexError):
        raise ShipError("cannot read this process's controlling terminal; no yes accepted") from None
    if tty_nr == 0:
        return None
    return os.makedev((tty_nr >> 8) & 0xfff, (tty_nr & 0xff) | ((tty_nr >> 12) & 0xfff00))


def seat_terminals(project: Path) -> dict[str, set[int]]:
    """The terminal devices of every pane on each ship's tmux server in the project, the office's included.

    Each ship's own server, a default server still holding that ship's sessions, and a private test
    server named by QD_TMUX_SOCKET, all read through qd's own socket handling, never TMUX.
    """
    from .tmux import Tmux, TmuxError, ship_sessions, ship_socket
    from .tmux_socket import private_endpoint
    folders = [project / "office", *sorted(p for p in (project / "ships").glob("*") if p.is_dir())]
    found: dict[str, set[int]] = {}
    for folder in folders:
        if not (folder / "ship.toml").is_file():
            continue
        try:
            current = ships.load(folder)
            own = ship_socket(current)
            servers = [Tmux(str(own), ship=True, name=current.name)] if own.exists() else []
            servers += [old for old in Tmux.defaults(own) if ship_sessions(old, current)]
            named = os.environ.get("QD_TMUX_SOCKET")
            if named and private_endpoint(named):
                servers.append(Tmux(named))
            devices = set()
            for server in servers:
                for path in server.pane_ttys():
                    try:
                        devices.add(os.stat(path).st_rdev)
                    except OSError:
                        continue  # a pane whose terminal is gone is no terminal a yes can come from
        except (ShipError, TmuxError, OSError) as error:
            raise ShipError(f"cannot read the panes of {folder.name}'s tmux server ({type(error).__name__}); "
                            "no yes accepted") from None
        found[current.name] = devices
    return found


def approving_terminal(project: Path) -> str:
    """The person's own terminal a yes comes from, or a refusal naming why.

    stdin must be a terminal and this process's controlling terminal, and no pane of any ship's tmux
    server: a seat's pane, a pipe and a detached process are refused. A same-user process that opens a
    pseudo-terminal of its own passes; that limit is the design's, named in docs/design/structures.md.
    """
    try:
        if not os.isatty(0):
            raise ShipError("no yes accepted: stdin is not a terminal; answer yes from the person's own terminal")
        name, device = os.ttyname(0), os.fstat(0).st_rdev
    except OSError:
        raise ShipError("no yes accepted: stdin's terminal cannot be read; answer yes from the person's own terminal") from None
    controlling = controlling_terminal()
    if controlling is None:
        raise ShipError("no yes accepted: this process has no controlling terminal (it is detached); "
                        "answer yes from the person's own terminal")
    if controlling != device:
        raise ShipError(f"no yes accepted: stdin ({name}) is not this process's controlling terminal; "
                        "answer yes from the person's own terminal")
    for ship_name, devices in seat_terminals(project).items():
        if device in devices:
            raise ShipError(f"no yes accepted: {name} is a pane of ship {ship_name}'s tmux server; "
                            "only the person's own terminal can answer yes")
    return name


def person_approval(project: Path) -> dict:
    """Every check of a new yes, and what the answer record keeps of it: the terminal, the process, its ancestry."""
    terminal = approving_terminal(project)
    notes, ancestry = _person_process(project)
    return {"terminal": terminal, "process": {"pid": ancestry[0][0], "start": ancestry[0][1]},
            "ancestry": [list(entry) for entry in ancestry], "notes": notes}


def tell_desk(office: ships.Ship, draft: "Proposal", answer: Path | None) -> str:
    """Post a crew's start to the office's desk before it starts; the open order shows on the status line."""
    from . import post, raise_notice
    desks = sorted(name for name, spec in office.seats.items() if spec.role == "desk")
    if len(desks) != 1:
        raise ShipError(f"the office needs one desk to tell of a raise; found {len(desks)}; no crew started")
    store = post.Store(office.runtime)
    if (earlier := raise_notice.told(office.runtime, draft.name, answer, store=store, desk=desks[0],
                                     ship_folder=draft.folder)) is not None:
        return earlier  # one notice per ship and yes: a rerun resuming on the same yes posts no second order
    saved = json.loads(answer.read_text()) if answer is not None else {}
    terminal = saved.get("terminal", "a terminal not recorded")
    yes = (f"a yes from {terminal}" if "terminal" in saved  # a hand-written record has none: said outright
           else "a yes with no terminal recorded: this yes did not pass the terminal check")
    body = (f"Ship {draft.name} was raised at {draft.folder} and its crew is starting, on {yes} "
            f"(answer record {answer}). If {office.operator or 'the person'} did not raise it, stop it with "
            f"qd --ship {draft.folder} down and tell them. Report on this order once it is seen; "
            "that clears it from the status line.")
    message = store.new("order", post.SHIP, desks[0], body, subject=raise_notice.SUBJECT)
    raise_notice.record(office.runtime, draft.name, message.id, terminal, answer)
    return message.id


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
        if "api_key" in pool and keyfile.name_of(pool["api_key"]) is None:
            raise ShipError(f"office pool {chosen}: raise accepts keys:<name> references only, never key values")
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
        approved, notes = False, []
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
                saved = {"format": 1, "digest": draft.digest, "answer": response, "by": "person"}
                if response == "yes":
                    approval = person_approval(office.folder.parent)
                    notes = approval.pop("notes")
                    saved.update(approval)  # every yes is seen: its terminal, its process and their ancestry
                answer = records / f"{draft.digest}.answer-{len(answers) + 1:08d}.json"
                write_once(answer, saved)
                approved = response == "yes"
        if not approved:
            state = "declined" if answer is not None else "pending the person's answer"
            return [draft.summary, f"Raise {state}; no ship started. Answer recorded in {records}.",
                    "From the person's own shell (QD_SEAT absent): " + approval_command(args, office, draft.digest)]
        folder = draft.folder
        lines = list(notes)
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
        told = tell_desk(office, draft, answer)
        lines.append(f"the desk is told on {told}; its report on that order clears it from the status line")
        result = operations.up(current)
        return lines + ([result] if isinstance(result, str) else result) + [f"Fleet address: {draft.name}"]
