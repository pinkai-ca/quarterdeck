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

from . import doctor, harness, keyfile, providers, ship as ships, standing
from .drill import Check
from .ship import Ship, ShipError
from .setup_answers import Conversation, MAPS, SCALARS, ROLES, MISSING, read_answers
from . import setup_answers, setup_proposal


def arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--answers", help="JSON object with the same answer names as flags (hyphens become underscores)")
    parser.add_argument("--programs", help="comma-separated registered program names")
    for name, help_text in (
        ("payment", "subscription, key or local (endpoint is also accepted)"),
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
    parser.add_argument("--folder", help="new ship folder directly under the Quarterdeck project's ships/ (default: ships/NAME); or use global --ship")
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
        # A documented adapter hint is not an installed-version sign-in guarantee.
        return table.get("config_dir", table.get("home", harness.get(program).setup_metadata().subscription_home))

    def credential_present(self, home: str, filename: str) -> bool:
        return (Path(home).expanduser() / filename).is_file()

    def project_folder(self) -> Path:
        from . import install
        try:
            return install.source_repo()
        except ShipError:
            # A packaged entry has no checkout beside its module. The person's
            # current checkout still identifies the project before an office exists.
            for folder in (Path.cwd(), *Path.cwd().parents):
                if ((folder / ".git").exists() and (folder / "pyproject.toml").is_file()
                        and (folder / "src/quarterdeck/install.py").is_file()):
                    return folder.resolve()
            raise ShipError("cannot identify the Quarterdeck project folder; run init or setup from the Quarterdeck checkout") from None


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
        facts = [harness.get(name).setup_metadata() for name in harness.NAMES]
        if not any(item.key_provider == providers.canonical(provider)
                   and item.anonymous_models_endpoint == url for item in facts):
            return []
        from . import endpoint
        try:
            return [model.id for model in endpoint.models(url, allow_redirects=False)]
        except ShipError:
            return []

    def readiness(self, ship: Ship, selected: list[str]) -> list[Check]:
        observations = {name: doctor.program_observation(name, ship.runtime, ship=ship) for name in selected}
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
        return check.check(ship, name, Tmux.for_ship(ship))

    def change_seat(self, ship: Ship, name: str, changes: list[str]) -> list[str]:
        from .cli import set_seat
        return set_seat(ship, name, changes)

    def up(self, ship: Ship):
        from .cli import cmd_up
        return cmd_up(argparse.Namespace(ship=str(ship.folder), seat=None, fresh=False))

    def up_desk(self, ship: Ship):
        from .cli import cmd_up, seat_of
        desk = seat_of(ship, "desk", launching=True)
        pane = desk.pane()
        if pane is not None and not pane.dead:
            from . import service
            from .seat_command import qd_command
            return [f"desk already up in {pane.id}", service.start(ship, qd=Path(qd_command(ship.folder)))]
        return cmd_up(argparse.Namespace(ship=str(ship.folder), seat="desk", fresh=False))

    def status(self, ship: Ship):
        from .cli import cmd_status
        return cmd_status(argparse.Namespace(ship=str(ship.folder), line=False))

    def access(self, ship: Ship) -> str:
        return shlex.join(["qd", "--ship", str(ship.folder), "attach", ship.seat("desk").session])


def template(kind: str = "default") -> tuple[str, str, dict]:
    folder = standing.project_dir() / "ships" / kind
    try:
        text, orders = (folder / "ship.toml").read_text(), (folder / "orders.md").read_text()
        return text, orders, tomllib.loads(text)
    except (OSError, ValueError):
        raise ShipError(f"{kind} ship template unavailable; install this version's packaged resources and rerun qd setup") from None


# The working folder a jailed office desk is given: its own, under the office's work/. The desk writes nothing there but
# posts, which go through its drop; the folder is only somewhere its program may start that the jail lets it write. It
# is not the office's folder (read-only in a jail), not the project (another ship's), and no other seat's.
JAILED_DESK_WORK = "work/desk"


def jailed_work_folders(office: Ship) -> list[Path]:
    """The folders under the office's own work/ that its jailed seats work in, as the declaration writes them, never
    through a link: loading a ship resolves a seat's cwd, so a link planted at work/desk would otherwise read as its
    target. A seat's own folder below work/ is one (sandbox.own_work_folder); so is work/ itself, or a folder below it,
    where a jailed seat works there in common (sandbox.shared_work), as a seat whose cwd is work/ does. A seat there
    works alone in it: the plan refuses a jailed seat whose folder holds another seat's (refuse_shared_workspace). Each
    is the folder qd init makes (make_jailed_work), and any of them has the office's repository ignore work/."""
    from . import sandbox
    return [folder for spec in office.seats.values() if office.jailed(spec)
            and (folder := sandbox.own_work_folder(office, spec) or sandbox.shared_work(office, spec)) is not None]


def make_jailed_work(office: Ship) -> None:
    """Make each jailed office seat's own working folder before its launch: empty and 0700 when absent.

    One already there, from an earlier start, is kept with what the seat left in it, but only as this user's real
    folder: a link, anything but a folder, or another user's is refused, and a mode looser than 0700 is set to 0700.
    Every later launch of a jailed seat checks it again the same way (sandbox.check_own_work), without making it.

    law: one-folder. The folder lies inside the office, in the project; it is ignored by the office's repository, and
    nothing is written in it.
    """
    from . import sandbox
    for name, spec in office.seats.items():
        if office.jailed(spec):
            sandbox.check_own_work(office, name, spec, make=True)


def office_folder(project: Path) -> Path:
    """The single office lives at the project top; no alias or alternate location."""
    project = project.resolve()
    folder = project / "office"
    if folder.is_symlink():
        raise ShipError(f"the office must be a real folder at {folder}, not a link; restore its directory before qd init")
    return folder


def embedded_office(project: Path) -> str | None:
    """The ship that still holds the person's desk in a human session, or None. An unreadable ship refuses."""
    for name in ships.candidates(project):
        folder = project / "ships" / name
        try:
            current = ships.load(folder)
        except ShipError as error:
            raise ShipError(f"cannot establish whether {folder} still holds the office: {error}; "
                            "resolve its declaration before creating an office") from None
        if any(seat.role == "desk" and current.session_of(seat).human
               for seat in current.seats.values()):
            return name
    return None


def refuse_embedded_office(project: Path) -> None:
    """An existing person's desk is moved explicitly, never by first-time setup."""
    if (name := embedded_office(project)) is not None:
        raise ShipError(f"the office still lives in ship {name} at {project / 'ships' / name}; "
                        "moving it is the person's step once forwarding is proven; "
                        "init and setup leave that ship untouched and create no second office")


def _names_existing_ship(answers: dict) -> bool:
    """A run that keeps or updates a ship already there: --ship (or its folder) and --existing keep or update."""
    folder = answers.get("folder")
    return (answers.get("existing") in ("keep", "update") and isinstance(folder, str) and bool(folder)
            and (Path(folder).expanduser() / "ship.toml").is_file())


def git_identity(project: Path) -> dict[str, str]:
    """Ask Git for an explicit, usable identity before setup writes a declaration."""
    identity = {}
    for role in ("author", "committer"):
        try:
            resolved = ships._git(project, "-c", "user.useConfigOnly=true", "var", f"GIT_{role.upper()}_IDENT")
            match = re.fullmatch(r"(.+) <([^<>]+)> -?\d+ [+-]\d{4}", resolved)
            if match is None:
                raise ShipError("Git returned an incomplete identity")
        except ShipError:
            raise ShipError("Git needs your name and email before setup can write files. "
                            "Set your own identity, then rerun setup: "
                            "git config --global user.name \"Your Name\" && "
                            "git config --global user.email \"you@example.com\"") from None
        identity[f"{role}.name"], identity[f"{role}.email"] = match.groups()
    return identity


def init_office(args: argparse.Namespace, *, operations: Operations | None = None, start: bool = True,
                declaration: str | None = None) -> list[str]:
    """Copy only the office declarations, commit them, then use normal up for its desk."""
    operations = operations or Operations()
    project = operations.project_folder().resolve()
    if getattr(args, "ship", None):
        raise ShipError("qd init initializes the project's office, not --ship; run it without --ship")
    folder = office_folder(project)
    lines = []
    if not (folder / "ship.toml").exists():
        refuse_embedded_office(project)
    if not folder.exists() or (folder.is_dir() and not any(folder.iterdir())):
        if not folder.parent.is_dir():
            raise ShipError(f"office parent {folder.parent} must already exist; create it before qd init")
        source = standing.project_dir() / "ships" / "office"
        names = ("ship.toml", "orders.md", "README.md", "roles/desk.md")
        try:
            files = {name: (source / name).read_text() for name in names}
        except (OSError, UnicodeError) as error:
            raise ShipError(f"office template unavailable ({type(error).__name__}); install this version's packaged resources") from None
        if declaration is not None:
            files["ship.toml"] = declaration
        declared = ships._from_text(files["ship.toml"], folder)
        ships._validate(declared)
        # A jailed seat's own working folder under work/ is ignored with the runtime: nothing in it is the office's.
        ignored = "/runtime/\n" + ("/work/\n" if jailed_work_folders(declared) else "")
        identity = git_identity(project)
        # law: one-folder
        # The fence's empty mount point is inside the project, where the office's
        # own declaration belongs; accept only that empty folder, never its files.
        try:
            folder.mkdir()
        except FileExistsError:
            if folder.is_symlink() or not folder.is_dir() or any(folder.iterdir()):
                raise ShipError(f"{folder} is not an empty office folder; leave its existing files untouched") from None
        for name, text in {**files, ".gitignore": ignored}.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            ships._write_atomically(path, text)
        try:
            ships._git(folder, "init", "--quiet")
            commit = ships.commit(folder, [*names, ".gitignore"], "qd init: declare office", identity=identity)
        except ShipError as error:
            raise ShipError(f"office files saved in {folder}, but its first commit is unfinished: {error}; "
                            "resolve git identity and commit only its declaration, orders, cards, README and .gitignore before rerunning qd init") from None
        lines.append(f"office created at {folder} · committed {commit} · runtime ignored")
    office = ships.load(folder)
    if office.kind != "office":
        raise ShipError(f"{folder} is not kind = \"office\"; choose the project's office, not a crew ship")
    if not office.is_repository:
        raise ShipError(f"{folder} needs its own git repository; initialize and commit its declaration before qd init")
    ships._git(folder, "rev-parse", "--verify", "HEAD")
    if not start:
        return lines + ["Office saved; desk not started. Run qd init to start it."]
    make_jailed_work(office)
    result = operations.up_desk(office)
    lines.extend([result] if isinstance(result, str) else result)
    lines += [f"Desk access: {operations.access(office)}", "Office ready; tell the desk what you want to work on."]
    return lines


def conversation_for(args: argparse.Namespace) -> Conversation:
    return Conversation(read_answers(args), interactive=sys.stdin.isatty() and not args.json and not args.answers,
                        write=lambda line: print(line, file=sys.stderr if args.json else sys.stdout, flush=True))


def first_step(args: argparse.Namespace, *, operations: Operations | None = None,
               conversation: Conversation | None = None):
    """Create the office before the crew, sharing answers and a single acceptance."""
    operations = operations or Operations()
    folder = office_folder(operations.project_folder())
    if (folder / "ship.toml").exists():
        if ships.load(folder).kind != "office":
            raise ShipError(f"{folder} is not an office; resolve it before qd setup")
        return run(args, operations=operations, conversation=conversation)
    conversation = conversation or conversation_for(args)
    answers = conversation.answers
    if _names_existing_ship(answers) and embedded_office(operations.project_folder().resolve()) is not None:
        # The office still lives in a ship and has no folder of its own (SHIPS.md: it stays there until the person
        # moves it). Keeping or updating an existing ship runs the crew wizard on it, as before offices existed: no
        # office is made and nothing is moved. Any other run still goes toward an office, and is refused, naming it.
        return run(args, operations=operations, conversation=conversation)
    crew = setup_answers.asks_for_crew(answers)
    if conversation.interactive:
        conversation.say("Setup creates your office first. You can stop there or add a ship and its crew in this run.")
        try:
            choice = conversation.read("Continue with a ship and crew after the office? yes or no [yes]: ").strip() or "yes"
        except (EOFError, KeyboardInterrupt):
            raise ShipError("setup stopped before office creation; rerun qd setup to continue") from None
        if choice not in ("yes", "no"):
            raise ShipError("choose yes or no for continuing with a ship and crew; rerun qd setup")
        crew = choice == "yes"
    if crew:
        return run(args, operations=operations, conversation=conversation, initialize_office=True)
    if setup_answers.names_office(answers):
        # The office's own answers are applied, never dropped: its desk is built as a run with a crew builds it.
        text, orders, defaults = template()
        available = operations.discover(defaults)
        for name, (row, sign_in) in available.items():
            conversation.say(f"{row} · {sign_in}")
        proposal = setup_proposal.build(conversation, operations, text, orders, defaults, available, desk_only=folder)
        office, declaration = setup_proposal.office_declaration(proposal, folder)
        runs = {office.seat("desk").for_pool(pool).harness for pool in office.seat("desk").pools}
        unused = [f"{name}.{program}" for name in MAPS for program in answers.get(name) or {}
                  if program not in runs]
        if unused:
            raise ShipError(f"answer {', '.join(unused)} names a program the office's desk does not run; drop it, "
                            "or add a crew answer to set up a crew with it; nothing was written")
    else:
        declaration, _, _ = template("office")
        office = ships._from_text(declaration, folder)
        ships._validate(office)
    conversation.say(f"Office proposal (not written): {folder}")
    summary = setup_proposal.summary(office, setup_proposal.display_control)
    for line in setup_proposal.render_summary(summary):
        conversation.say(line)
    accepted = conversation.choice("accept", "Create this project's office? yes or no", ("yes", "no"))
    if accepted == "no":
        return ["Setup stopped before acceptance; no office changes were written."]
    start = conversation.choice("start", "Start the office's desk? Opening its brief may spend turns. yes or no",
                                ("yes", "no"))
    return init_office(argparse.Namespace(ship=None), operations=operations, start=start == "yes",
                       declaration=declaration)


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
    from .ship import endpoint_fits
    if not endpoint_fits(value):
        raise ShipError("endpoint must be an http(s) URL without credentials, query or fragment; use key_env for a key's variable NAME")
    return value.rstrip("/")


def pool_answers(conversation: Conversation, selected: list[str], defaults: dict,
                 *, operations: Operations, existing: Ship | None = None,
                 fallbacks: dict | None = None) -> tuple[dict, dict]:
    """Each program's pool and served model; ``fallbacks`` gains the key a subscription falls back to, where one is named."""
    pools, models = {}, {}
    for name in selected:
        reader = harness.get(name)
        hints = reader.setup_metadata()
        saved = next((existing.pools[pool] for seat in existing.seats.values() for pool in seat.pools
                      if seat.for_pool(pool).harness == name), None) if existing else None
        default_kind = saved.kind if saved else "subscription" if reader.native_provider else "endpoint"
        if saved and hints.key_endpoint and saved.provider == hints.key_provider and saved.api_key:
            default_kind = "key"
        if default_kind == "endpoint":
            default_kind = "local"
        prompt = (f"What does {hints.display_name} run on? "
                  "a subscription you are signed in to; an API key; a local model "
                  "(subscription, key, local)")
        kind = conversation.choice(f"payment.{name}", prompt, ("subscription", "key", "local", "endpoint"), default_kind)
        kind = "endpoint" if kind == "local" else kind
        endpoint_key = kind == "key" and hints.key_endpoint is not None
        compatible_saved = saved is not None and (saved.kind == kind or endpoint_key and saved.kind == "endpoint")
        provider = saved.provider if compatible_saved else reader.native_provider
        if endpoint_key and provider is None:
            provider = hints.key_provider
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
            suggested_key = keyfile.name_of(saved.api_key) if saved and saved.api_key else suggested_key_name(provider)
            variable = conversation.ask(f"key_env.{name}", f"{name}'s key NAME in {keyfile.SHOWN} (never the key)", suggested_key)
            values["api_key"] = key_reference(variable)
        if kind == "endpoint" or endpoint_key:
            given = conversation.supplied(f"endpoint.{name}")
            documented = hints.key_endpoint if providers.canonical(provider) == hints.key_provider else None
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
                    else:
                        variable = keyfile.name_of(saved.api_key)
                variable = conversation.ask(f"key_env.{name}", f"{name}'s key NAME in {keyfile.SHOWN}, or none if no key is needed", variable)
                if variable != "none":
                    values["api_key"] = key_reference(variable)
                else:
                    values.pop("api_key", None)
        pool = ships._pool(name, values)
        if reason := keyfile.accepts(reader, pool):
            raise ShipError(f"{reason}; choose a payment method supported by {name} and rerun qd setup")
        pools[name] = values
        if fallbacks is not None and kind == "subscription":
            key_pool = ships.Pool(f"{name}-key", provider, api_key=f"{keyfile.PREFIX}{name}")
            if keyfile.accepts(reader, key_pool) is None:
                chosen = conversation.supplied(f"key_fallback.{name}")
                if chosen is not MISSING and chosen != "none":
                    fallbacks[name] = {"provider": provider, "api_key": key_reference(chosen)}
                elif chosen is MISSING and conversation.interactive:
                    conversation.say(f"An API key can take over when this subscription is spent: answer "
                                     f"key_fallback.{name} with its name in {keyfile.SHOWN}.")
    return pools, models


def suggested_key_name(provider: str | None) -> str | None:
    """A provider's usual name in the key file, where its canonical spelling is a valid one."""
    name = providers.canonical(provider) if provider else None
    return name if keyfile.name_of(f"{keyfile.PREFIX}{name}") else None


def key_reference(name: str | None) -> str:
    """A pool's ``api_key``. What was answered is never echoed: a pasted key would be printed."""
    reference = f"{keyfile.PREFIX}{name}"
    if keyfile.name_of(reference) is None:
        raise ShipError(f"key_env must be the NAME of a key in {keyfile.SHOWN} (lowercase letters, digits and hyphens), "
                        "never a key value; put the key in that file and pass its name")
    return reference


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
    programs = {seat.for_pool(pool).harness for seat in ship.seats.values() for pool in seat.pools}
    for program in sorted(programs):
        hints = harness.get(program).setup_metadata()
        if hints.seat_limit is None:
            continue
        selected = [seat.name for seat in ship.seats.values()
                    if any(seat.for_pool(pool).harness == program for pool in seat.pools)]
        if len(selected) > hints.seat_limit:
            count = "one" if hints.seat_limit == 1 else str(hints.seat_limit)
            raise ShipError(f"{hints.display_name} is limited to {count} declared seat"
                            + ("" if hints.seat_limit == 1 else "s") + " in setup; proposed seats: "
                            + ", ".join(selected) + ". Choose other programs for the remaining roles and rerun qd setup.")
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
        conversation: Conversation | None = None, initialize_office: bool = False) -> list[str]:
    operations = operations or Operations()
    conversation = conversation or conversation_for(args)
    text, orders, defaults = template()
    office_path = office_folder(operations.project_folder())
    office = ships.load(office_path) if (office_path / "ship.toml").exists() else None
    if office is not None and office.kind != "office":
        raise ShipError(f"{office_path} is not an office; resolve it before qd setup")
    if office is not None or initialize_office:
        orders = orders.replace("through the desk.", "through the office's desk (desk@office).")
    available = operations.discover(defaults)
    for name, (row, sign_in) in available.items():
        conversation.say(f"{row} · {sign_in}")
    while True:
        proposal = setup_proposal.build(conversation, operations, text, orders, defaults, available,
                                        include_desk=office is None, project_office=office)
        if initialize_office:
            proposal = setup_proposal.separate_office(proposal, office_path)
        elif office is not None and any(seat.role == "desk" for seat in proposal.ship.seats.values()):
            raise ShipError("this ship still holds a desk; move it explicitly before using the office's crew wizard")
        data = setup_proposal.summary(proposal.ship, setup_proposal.display_control, existing=proposal.existing,
                                      office=proposal.office or office)
        for note in validate_declaration(proposal.ship):
            conversation.say(note)
        accepted = conversation.review(lambda: setup_proposal.render_summary(data))
        if accepted is not None:
            break
    office_lines = []
    def finish(lines):
        if args.json:
            from .output import Reading
            return Reading({**data, "result": office_lines + lines}, lambda reading: reading["result"])
        return lines
    if not accepted:
        return finish(["Setup stopped before acceptance; no ship changes were written."])
    identity = git_identity(operations.project_folder()) if proposal.mode == "new" else None
    if proposal.mode == "update" and ships.commits(proposal.existing, proposal.edits):
        # Only where the update will commit, by the rule its writes commit by (ship.set_values): an update that
        # changes nothing commits nothing, and needs no identity.
        git_identity(proposal.ship.folder)
    if initialize_office:
        start_office = conversation.choice("start", "Start the office and crew? Opening briefs may spend turns. yes or no",
                                           ("yes", "no"), "yes")
        try:
            office_lines = init_office(argparse.Namespace(ship=None), operations=operations,
                                       start=start_office == "yes", declaration=proposal.office_text)
        except ShipError as error:
            if not (office_path / "ship.toml").exists():
                raise
            # Its own words, and the next step: the office is kept, and setup continues from it when run again.
            raise ShipError(f"the office at {office_path} is saved, but its desk did not start: {error}. "
                            "Resolve that, then run qd init to start the desk, and qd setup again for the crew; both "
                            "continue from the saved office") from None
        for line in office_lines:
            conversation.say(line)
    folder = proposal.ship.folder
    rerun = shlex.join(["qd", "--ship", str(folder), "setup", "--existing", "keep",
                       "--use-defaults", "yes", "--accept", "yes"])
    if proposal.mode == "new":
        ship = ships.create(folder, proposal.text, proposal.orders, identity=identity)
        conversation.say(f"Created and committed {ship.file} and orders.md; runtime is ignored.")
        ships.mark_default(folder.name, project=operations.project_folder())
        conversation.say(f"{folder.name} marked as your default ship: qd acts on it when no ship is named")
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
    consent = conversation.choice("check", f"Run qd check for {', '.join(selected)}? This spends turns on each program's subscription. yes or no", ("yes", "no"), "yes")
    if consent == "no":
        return finish([f"Ship saved; live checks declined. To continue: {rerun} --check yes --start yes"])
    for program in selected:
        conversation.say(f"Checking {program}; this spends turns on its subscription.")
        rows = operations.check(ship, program)
        for row in rows:
            conversation.say(str(row))
        if not rows or not all(row.passed for row in rows):
            reason = "; ".join(f"{row.name}: {row.detail}" for row in rows if not row.passed) or "no proof rows returned"
            raise ShipError(f"qd check {program} failed: {reason}; resolve that reason, then rerun {rerun} --check yes --start yes")
    start = conversation.choice("start", "Bring up the crew? Opening briefs may spend turns on each program's subscription. yes or no", ("yes", "no"), "yes")
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
    desk_ship = ships.load(office_path) if office is not None or initialize_office else ship
    return finish([f"Desk access: {operations.access(desk_ship)} (desk window)",
            'First order to type at the desk: "Read this workspace and propose one useful next change."',
            f"Status: qd --ship {shlex.quote(str(folder))} status"])
