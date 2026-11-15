"""A seat jailed with bubblewrap: the operator's home hidden, the workspace and the seat's own folder writable.

A model served locally is trusted less than the subscription programs, so a seat on an endpoint pool
runs in a jail unless the ship says ``sandbox = false``, and any seat does with ``sandbox = true``. The
jail is one ``bwrap`` command around the program's own: the root read-only; a private ``/tmp``; the
operator's home replaced by an empty one, with only the folders the program and ``qd`` resolve through
bound back read-only; the workspace and the seat's own folder writable; the ship folder read-only with
the other seats' folders hidden; only that seat's drop folder writable, the rest of the post
store read-only, so ``qd post`` writes a drop and the service files it; and that seat's own check
proof folder writable, never another seat's. In a project's ``ships/`` and
``office/``, which are empty and read-only inside, only the ship's own folder and its peers' ship files
found at the launch are bound back, so a ship raised later is not there at all; a fleet folder missing at
the launch is made first, empty, so it is fenced too, and a workspace inside one is refused. The
program's home is the seat home. The operator's files, sign-ins and the other seats' records are not
merely out of bounds but absent, so nothing the model does inside can read or change them.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import post_dir, standing
from .ship import Ship, ShipError

PROGRAM = "bwrap"
TMP_BYTES = 256 * 1024 * 1024  # a private /tmp lives in memory; this caps what a runaway write can take
HOME_BYTES = 16 * 1024 * 1024  # the empty home stands in memory too; bounded, so a jailed seat cannot fill the machine
HIDDEN_BYTES = 1024 * 1024  # every other tmpfs inside is a fence or a hidden folder, holding mount points only
# What a jailed seat sees of its ship's runtime, read-only: the post (its own drop writable on top), its own brief, the
# ship's own qd (bin, venv, the previous copy and the record that selects between them) and the service's heartbeat.
# The rest of runtime/ is an empty folder inside: the ledger, the logs, the evidence, every sign-in a usage reader or a
# parked credential holds, and whatever the runtime holds later. Its own seat folder and check folder are writable.
# The runtime's own .git is the empty fence repository that keeps git run above it out; a jailed qd post checks it.
# The post is shown as the seat's own view of it (post_view), never whole: see Jail.post_view.
RUNTIME_SHOWN = ('.git', 'bin', 'venv', 'venv.prev', 'installed.json', 'service.heartbeat')
# Of the briefs, only the seat's own: every seat's brief lies in that folder, the counsel's included.
RUN_BYTES = 16 * 1024 * 1024  # the jail's own /run: a private runtime folder and the resolver's file, nothing more
MNT_BYTES = 16 * 1024 * 1024  # an empty /mnt: the operator's own disks and the machine's shared scratch are hidden
MNT = Path('/mnt')
# Ways into the operator's desktop session that a program finds by its environment; none is passed into a jail.
SESSION_VARIABLES = ('DBUS_SESSION_BUS_ADDRESS', 'DISPLAY', 'WAYLAND_DISPLAY', 'SSH_AUTH_SOCK', 'GPG_AGENT_INFO',
                     'XAUTHORITY')
HOPS = 20  # a chain of links longer than this is nobody's install
# What the jail hides besides the operator's home: its own empty /tmp and its fresh /dev, whose /dev/shm is empty.
# A folder the launch needs below one of them is bound back read-only, as one below the home is; the mount
# root itself and /dev/shm itself never are.
HIDDEN = (Path('/tmp'), Path('/dev'))
NEVER = (Path('/tmp'), Path('/dev'), Path('/dev/shm'))
FRAMEWORK_TOOLS = ("qd", "node")  # what a seat's program needs beside itself: qd to post, node where the program is JavaScript


def seats_hidden(argv: object) -> bool | None:
    """Whether a saved managed launch hides sibling seats; malformed evidence is unknown.

    The jail is the outer command, including on launches made before a sync; behind an endpoint seat's relay, the
    jail is the command the relay starts.
    Reading one's own launch needs no access to another seat or to the host's pids.
    """
    if (not isinstance(argv, (list, tuple)) or not argv or not argv[0]
            or any(not isinstance(arg, str) for arg in argv)):
        return None
    if list(argv[1:4]) == ['-I', '-m', 'quarterdeck.endpoint_relay'] and '--' in argv:
        argv = argv[list(argv).index('--') + 1:]  # an endpoint seat's jail runs behind its relay
        if not argv:
            return None
    return Path(argv[0]).name == PROGRAM


def available() -> bool:
    return shutil.which(PROGRAM) is not None


OWN_WORK = "work"  # the one folder of its own ship a jailed seat may work in, as the forum's reviewer does
LOCKS = "locks"  # runtime/locks: the service's own lock files, hidden inside every jail


@dataclass(frozen=True)
class Jail:
    """What a jailed seat may see and touch."""

    home: Path  # the operator's home, replaced by an empty one
    seat_home: Path  # the program's home inside the jail
    seat_folder: Path  # runtime/seats/<seat>, writable
    workspace: Path  # writable
    ship_folder: Path  # read-only, the other seats hidden
    runtime: Path
    read_only: tuple[Path, ...] = ()  # the program's own folders under the operator's home, bound back
    writable: tuple[Path, ...] = ()  # this seat's own drop folder and its own check proof folder
    environment: tuple[tuple[str, str], ...] = ()  # set inside the jail: the git identity a commit from the seat carries
    declarations: tuple[Path, ...] = ()  # peer ship files only, under otherwise empty read-only folders
    post_view: Path | None = None  # the seat's own view of the post, bound read-only in the post's place
    foreign_runtimes: tuple[Path, ...] = ()  # other ships' runtimes and test-run folders inside a bound folder, each an empty tmpfs
    endpoint: str | None = None  # the pool's model endpoint, relayed into the jail's own network; None: no way out


def git_identity(workspace: Path) -> tuple[tuple[str, str], ...]:
    """The name and address git would sign a commit with in the workspace, read outside the jail.

    Inside, the operator's home is gone and with it the global git configuration, so a commit from
    the seat would have no author. The workspace's identity travels in as the author and committer
    variables, and the commit reads as every other commit of the repository does.
    """
    found = []
    for key, variables in (("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")), ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"))):
        try:
            value = subprocess.run(["git", "-C", str(workspace), "config", "--get", key], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            value = ""
        found += [(variable, value) for variable in variables if value]
    return tuple(found)


def ship_runtime_of(folder: Path) -> Path | None:
    """The ship runtime a folder is or lies inside: a folder named runtime beside a ship.toml."""
    for candidate in (folder, *folder.parents):
        if candidate.name == 'runtime' and (candidate.parent / 'ship.toml').is_file():
            return candidate
    return None


def held_runtimes(folder: Path, depth: int = 3) -> list[Path]:
    """Ship runtimes inside a folder, looked for a few levels down: a ship folder, or a ships/ folder of them."""
    found = []
    if (folder / 'ship.toml').is_file() and (folder / 'runtime').is_dir() and not (folder / 'runtime').is_symlink():
        found.append(folder / 'runtime')
    if depth <= 0:
        return found
    try:
        entries = [Path(entry.path) for entry in os.scandir(folder)
                   if entry.is_dir(follow_symlinks=False) and entry.name != 'runtime'
                   and not (entry.name.startswith('.') and entry.name != '.venv')]
    except OSError:
        return found
    for entry in entries:
        found += held_runtimes(entry, depth - 1)
    return found


TEST_RUNS = '.testruns'  # the test tool's folder in a checkout: its runs' live tmux servers listen there


def held_test_runs(folder: Path, depth: int = 3) -> list[Path]:
    """Test-run folders inside a folder, a few levels down: a checkout's .testruns, whose runs hold live sockets.

    A path socket ignores the jail's own network, so a folder bound into a jail must not show them.
    """
    found = []
    candidate = folder / TEST_RUNS
    if candidate.is_dir() and not candidate.is_symlink():
        found.append(candidate)
    if depth <= 0:
        return found
    try:
        entries = [Path(entry.path) for entry in os.scandir(folder)
                   if entry.is_dir(follow_symlinks=False) and not entry.name.startswith('.') and entry.name != 'runtime']
    except OSError:
        return found
    for entry in entries:
        found += held_test_runs(entry, depth - 1)
    return found


def inside_foreign_runtime(folder: Path, ship: Ship) -> Path | None:
    """Another ship's runtime that the folder is, or lies inside. Such a folder is never bound into a jail.

    Every seat of a ship has its own runtime/bin first on PATH, so a ship raised or checked from such a shell finds
    that runtime on its PATH. Bound back, it would show that ship's post, its seats and its live tmux socket.
    """
    inside = ship_runtime_of(folder)
    if inside is not None and inside.resolve() != ship.runtime.resolve():
        return inside
    return None


def bindable(folder: Path, ship: Ship, fleet: tuple[Path, ...]) -> bool:
    """Whether a folder may be bound back into a jail, judged by this one form of its path.

    Never this ship's folder or a folder inside it: the ship folder is bound once, at its own path, with its runtime
    fenced. Never another ship's runtime, nor a folder inside one. Never a folder inside the fleet (ships/, office/, a
    peer's folder): the fence shows only this ship's folder and the peers' ship files there, whatever the mount order.
    """
    roots = {*fleet, *(root.resolve() for root in fleet)}
    own = {ship.folder, ship.folder.resolve()}
    return (not any(folder == place or place in folder.parents for place in own)
            and inside_foreign_runtime(folder, ship) is None
            and not any(folder == root or folder.is_relative_to(root) for root in roots))


def reaches_through_link(folder: Path, ship: Ship, fleet: tuple[Path, ...]) -> bool:
    """Whether a folder whose path passes through a link holds, at its real place, the fleet or this ship's folder.

    bwrap binds the link's target at the link's path, where no fence lies: the fleet's fence and this ship's runtime
    fence lie at their own paths only. Reached directly, such a folder (the framework's checkout) keeps the fences.
    Another ship's runtime outside the fleet is covered at the link's path as anywhere else (held_runtimes).
    """
    real = Path(os.path.realpath(folder))
    if real == folder:
        return False
    places = {*fleet, *(root.resolve() for root in fleet), ship.folder, ship.folder.resolve()}
    return any(place == real or real in place.parents for place in places)


def endpoint_parts(endpoint: str) -> tuple[str, int, str | None]:
    """The host and port a jail's relay carries, and the name its hosts file maps to the jail's loopback.

    A name is resolved by the relay outside; inside, the jail's hosts file sends it to the forwarder on loopback, so
    TLS still checks the real name. An address must be loopback: no other address exists inside the jail.
    """
    import ipaddress
    from urllib.parse import urlsplit

    parts = urlsplit(endpoint)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ShipError(f'endpoint {endpoint!r}: a jailed seat reaches an http or https endpoint by host and port')
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        name = None if parts.hostname == 'localhost' else parts.hostname
        return parts.hostname, port, name
    if not address.is_loopback:
        raise ShipError(f'endpoint {endpoint!r}: a jail has only its own loopback, so an endpoint by address must be '
                        'on loopback; name a remote endpoint by its host name, which the jail relays')
    return parts.hostname, port, None


def plan(ship: Ship, seat_name: str, seat_folder: Path, seat_home: Path, workspace: Path, program: str, path: str,
         home: Path | None = None, endpoint: str | None = None, pool=None, synthetic: bool = False) -> Jail:
    """The jail for one seat, or a refusal naming what would lift it."""
    if not available():
        raise ShipError(f"{seat_name}: sandboxed, and {PROGRAM} is not on the PATH; install bubblewrap, or set sandbox = false on the seat")
    if pool is not None and pool.kind != 'endpoint' and not synthetic:
        # A jail has a network of its own, loopback only: a program that must reach its provider cannot run in one.
        raise ShipError(f"{seat_name}: sandbox = true on pool {pool.name}, a {pool.kind} pool: a jailed seat has no "
                        "network; only an endpoint pool's model is relayed in. Set sandbox = false on the seat, "
                        "or give it an endpoint pool")
    home = (home or Path.home()).resolve()
    workspace = workspace.resolve()
    if home.is_relative_to(workspace):
        raise ShipError(f"{seat_name}: sandboxed with its working directory at {workspace}, which contains the operator home and jails nothing; give the seat a cwd below the home or outside its ancestors")
    if workspace == ship.folder.resolve():
        raise ShipError(f"{seat_name}: sandboxed with the ship folder as its working directory; "
                        f"a jailed seat reads the ship folder and cannot write it. In {ship.file}, "
                        'set workspace under [ship] to an existing project folder outside the ship folder, '
                        'for example workspace = "/path/to/your/project". '
                        'If this seat has its own cwd setting, change that to the project folder too.')
    fleet = fleet_roots(ship.folder)
    own_work = ship.folder.resolve() / OWN_WORK
    if inside := next((root for root in fleet if workspace.is_relative_to(root.resolve())), None):
        # The one place in the fleet a workspace may lie: the ship's own work folder, as the forum's reviewer has it,
        # bound writable after the ship's own read-only folder. No other folder of the ship: homes/ holds settings and
        # code every unjailed seat of a program loads, .git runs hooks when qd commits the declaration, and roles/ and
        # orders.md go into unjailed seats' briefs, so a jail writing there would escape it. Anywhere else in the fleet
        # the fence would leave the workspace absent, or read-only, inside.
        if not workspace.is_relative_to(own_work):
            raise ShipError(f"{seat_name}: sandboxed with its working directory at {workspace}, inside the fleet folder "
                            f"{inside}; a jailed seat may work inside its ship's folder only in {own_work}. Give the "
                            f"seat a working directory there, or outside {inside}")
    refuse_shared_workspace(ship, seat_name, workspace)
    programs = (program, *FRAMEWORK_TOOLS)
    qd = shutil.which('qd', path=path)
    if qd is not None and (elsewhere := ship_runtime_of(Path(os.path.realpath(qd)))) is not None \
            and elsewhere.resolve() != ship.runtime.resolve():
        raise ShipError(f"{seat_name}: sandboxed, and its qd resolves to {os.path.realpath(qd)}, inside another ship's "
                        f"runtime ({elsewhere}); no jail binds another ship's runtime, so the seat could not post. "
                        "Run qd from this ship's own installed copy, or from the framework's checkout")
    folders = [f for f in (*program_folders(programs, path, home), *hidden_folders(programs, path, home))
               if f != workspace and workspace not in f.parents
               and all(bindable(place, ship, fleet) for place in (f, Path(os.path.realpath(f))))
               and not reaches_through_link(f, ship, fleet)]
    # A folder bound back that holds another ship's runtime (the framework's checkout holds every ship's) keeps it
    # covered by an empty tmpfs; this ship's own runtime is shown as always, with its seats and tmux hidden.
    # So does a test run's folder (.testruns): its live tmux servers listen there, and a path socket ignores the
    # jail's own network.
    covered = {runtime for folder in (*folders, workspace) for runtime in held_runtimes(folder)
               if runtime.resolve() != ship.runtime.resolve()}
    covered |= {runs for folder in (*folders, workspace) for runs in held_test_runs(folder)}
    # Inside a fenced fleet folder (ships/, office/) another ship's runtime is already absent: the fence is an empty
    # tmpfs holding only this ship's folder and the peers' ship files, read-only once they are in. A cover there would
    # need a mount point made in that read-only fence, and stop the launch; so it gets none. Only a runtime or a test
    # run reached through a bound folder that is not fenced gets its own tmpfs.
    covered = {path for path in covered if not any(path == root or path.is_relative_to(root) for root in fleet)}
    foreign = tuple(sorted(covered, key=lambda r: (len(r.parts), str(r))))
    drop = ship.runtime / "post" / "drop" / seat_name
    post_dir.ensure_directory(drop)  # the one writable post folder, so it must exist before the launch
    (ship.runtime / "tmux").mkdir(mode=0o700, exist_ok=True)  # hidden inside: the mount needs its place in the read-only ship
    (ship.runtime / LOCKS).mkdir(mode=0o700, exist_ok=True)  # hidden inside: the service's own locks, out of every jail's reach
    from .fleet import declaration_files
    declarations = tuple(file for file in declaration_files(ship) if file.parent != ship.folder)
    proof = ensure_check_proof_folder(ship.runtime, seat_name)  # a link is refused here, before any bind
    from . import post_view
    view = post_view.refresh(ship.runtime, seat_name)  # what of the post the seat may see, current at its launch
    if endpoint is not None:
        endpoint_parts(endpoint)  # refused here, before anything starts, when the jail could not reach it
    jail = Jail(home, seat_home, seat_folder, workspace, ship.folder, ship.runtime, tuple(folders),
                (drop, proof), git_identity(workspace), declarations, view, foreign, endpoint)
    ssh = Path(os.path.realpath(home / '.ssh'))
    if os.path.lexists(home / '.ssh') and not ssh.is_relative_to(Path(os.path.realpath(home))):
        # The home's hiding covers ~/.ssh only where it lies: a link out of the home would leave its keys and sockets
        # wherever that place is shown.
        raise ShipError(f"{seat_name}: sandboxed, and the operator's ~/.ssh leads outside the home ({ssh}); the jail "
                        "hides ~/.ssh only as part of the home. Keep ~/.ssh a folder in the home, or run the seat open")
    for folder in (*jail.read_only, jail.workspace, *jail.writable):
        real = Path(os.path.realpath(folder))
        if ssh.is_relative_to(real):
            raise ShipError(f"{seat_name}: sandboxed, and {folder} ({real}) holds the operator's ~/.ssh ({ssh}); no "
                            "jail binds it, since its sockets and keys reach the operator's servers. Keep the seat's "
                            "programs, PATH and working directory away from it")
        if real == ssh or real.is_relative_to(ssh):
            # Its connection-sharing sockets give a server's session to anything that reaches them; no jail binds it.
            raise ShipError(f"{seat_name}: sandboxed, and {folder} is the operator's ~/.ssh or lies inside it "
                            f"({real}); no jail binds it, since its sockets and keys reach the operator's servers. "
                            "Keep the seat's programs, PATH and working directory out of ~/.ssh")
    if covered := covering(jail):
        what, folder = covered
        raise ShipError(f"{seat_name}: sandboxed with the operator home at {home}, and {what} {folder} is that home or "
                        "contains it; bound inside the jail it would show the hidden home again, so it jails nothing. "
                        f"Keep {what} out of the home's own path: below the home, or elsewhere")
    for folder in fleet:
        fence_point(seat_name, folder)
    return jail


def is_framework_checkout(folder: Path) -> bool:
    """Whether a folder is a checkout of the framework, by what it holds."""
    return ((folder / '.git').exists() and (folder / 'pyproject.toml').is_file()
            and (folder / 'src' / 'quarterdeck' / 'sandbox.py').is_file())


def framework_checkouts(ship: Ship) -> tuple[Path, ...]:
    """The framework checkouts a seat's own cwd must never be or hold, by real path.

    The project folder that holds the fleet (``ships/`` and ``office/``) when it is a checkout, as in the ship's
    own layout, and the checkout this code runs from when it runs from one. A project that holds a fleet and is
    not a checkout is the fleet's own business: its fence keeps the other ships out of a jail working there.
    """
    found = []
    for folder in (ship.folder, Path(os.path.realpath(ship.folder))):  # by its real folder, and as it is given
        if roots := layout_roots(folder):
            found.append(roots[0].parent)
    found.append(Path(__file__).resolve().parents[2])
    return tuple(dict.fromkeys(Path(os.path.realpath(folder)) for folder in found if is_framework_checkout(folder)))


def refuse_shared_workspace(ship: Ship, seat_name: str, workspace: Path) -> None:
    """Refuse a jailed seat whose declared cwd is not its own, by real path (links resolved).

    A jail binds its working directory writable. Where a seat declares a cwd of its own (a hand's
    ``work/hands/<name>``) and that folder is another declared seat's, holds one, or lies inside another seat's declared
    cwd, the jailed seat would write the other seat's work, which that seat reads and may run outside any jail. The
    framework's checkout is refused first: a cwd that is the checkout or holds it, and a cwd inside it anywhere but
    ``work/hands/<name>`` or below, since the rest of it is the framework's code, its history and hooks, the workers'
    worktrees and the captain's queue, which unjailed seats read and run. The office's own seats may also work at
    ``office/work/<name>`` or below, as its jailed desk does, when the office is a real folder of the checkout. A seat at the ship's common workspace, which
    declares no cwd, and qd check's temporary seats, which no ship file declares, are not bound, nor counted as another
    seat. Nor is a declared cwd that is the common workspace or holds it, as a seat at a folder above the project has:
    lying inside it is lying inside the common workspace, which the checkout rule already rules.
    """
    spec = ship.seats.get(seat_name)
    if spec is None or spec.cwd is None or spec.temporary:
        return
    real = Path(os.path.realpath(workspace))
    for checkout in framework_checkouts(ship):
        if checkout.is_relative_to(real):
            what = 'is' if checkout == real else 'holds'
            raise ShipError(f"{seat_name}: sandboxed with its working directory at {real}, which {what} the "
                            f"framework's checkout {checkout}; a jailed seat never works there, since it holds the "
                            f"framework's code and every ship's folder. Give {seat_name} a cwd of its own, such as "
                            "a folder under the checkout's work/hands/")
        if real.is_relative_to(checkout):
            inside = real.relative_to(checkout).parts
            # The office's own seats also work in a folder of their own under the office's work/ (its desk's
            # work/desk), when the office is a real folder of the checkout and not a link to another place in it.
            office = checkout / 'office'
            own_office = (inside[:2] == ('office', 'work') and len(inside) >= 3 and not office.is_symlink()
                          and office.is_dir() and Path(os.path.realpath(ship.folder)) == office)
            if (len(inside) < 3 or inside[:2] != ('work', 'hands')) and not own_office:
                raise ShipError(f"{seat_name}: sandboxed with its working directory at {real}, inside the framework's "
                                f"checkout {checkout}; a jailed seat works there only in a folder of its own under "
                                f"{checkout / 'work' / 'hands'}, since the rest of the checkout is the framework's "
                                f"code, history and worktrees. Give {seat_name} a cwd such as "
                                f"{checkout / 'work' / 'hands' / seat_name}")
    common = Path(os.path.realpath(ship.workspace))
    for other, theirs_spec in ship.seats.items():
        if other == seat_name or theirs_spec.temporary:
            continue
        theirs = Path(os.path.realpath(ship.cwd_of(theirs_spec)))
        if theirs == real or theirs.is_relative_to(real):
            what = 'is also' if theirs == real else 'holds'
            raise ShipError(f"{seat_name}: sandboxed with its working directory at {real}, which {what} seat "
                            f"{other}'s working directory ({theirs}); a jailed seat works in a folder of its own. "
                            f"Give {seat_name} a cwd that no other seat uses and that holds no other seat's")
        if theirs_spec.cwd is not None and real.is_relative_to(theirs) and not common.is_relative_to(theirs):
            raise ShipError(f"{seat_name}: sandboxed with its working directory at {real}, inside seat {other}'s "
                            f"declared working directory ({theirs}); a jailed seat works in a folder of its own. "
                            f"Give {seat_name} a cwd outside every other seat's")


def own_work_folder(ship: Ship, spec) -> Path | None:
    """A seat's declared cwd below its ship's work/, as the declaration writes it (no link followed), or None."""
    declared = spec.declared_cwd
    own = Path(os.path.normpath(ship.folder / OWN_WORK))
    if declared is None or declared == own or not declared.is_relative_to(own):
        return None
    return declared


def check_own_work(ship: Ship, seat_name: str, spec, *, make: bool = False) -> None:
    """Check a jailed seat's own working folder under its ship's work/, as declared, before anything of a start.

    The path is walked as the declaration writes it, with lstat from the ship folder down, never through a link:
    each step must be a real folder of this user, and the folder itself is set to 0700 when its mode is anything
    else. A link, anything but a folder, or another user's folder refuses, as does a folder the ship, when it was
    read, found somewhere else (a link since removed). With ``make``, as at qd init, a missing step is made, the
    folder itself empty and 0700; at a launch a missing folder refuses, since qd makes nothing outside its folder
    there. A declared cwd elsewhere that reaches the ship's work/ through a link (an absolute path through a link to
    the office) refuses too: the walk could not see it. The caller says whether the seat is jailed.

    law: one-folder. A folder is made only at qd init, inside the office, where the office's repository ignores it.
    """
    declared = spec.declared_cwd
    if declared is None:
        return
    base = Path(os.path.normpath(ship.folder))
    folder = own_work_folder(ship, spec)
    if folder is None:
        work = Path(os.path.realpath(base / OWN_WORK))
        real = Path(os.path.realpath(declared))
        if real.is_relative_to(work) and real != work:
            raise ShipError(f"{seat_name}: its working directory {declared} reaches {real}, inside its ship's work "
                            "folder, through a link; a jailed seat's working folder is declared as the path under "
                            f"its ship's folder and reached without links. Declare it as "
                            f"{base / OWN_WORK / real.relative_to(work)}, then start again")
        return
    remedy = f"put a private folder of your own at {folder} (mkdir -p -m 700 {folder}), then start again"
    part = base
    for name in folder.relative_to(base).parts:
        part = part / name
        try:
            info = os.lstat(part)
        except FileNotFoundError:
            if not make:
                raise ShipError(f"{seat_name}: its working folder {folder} does not exist, and qd makes nothing outside "
                                f"its folder at a launch. Run qd init, which makes the office's, or {remedy}") from None
            try:
                os.mkdir(part, 0o700) if part == folder else os.mkdir(part)
            except FileExistsError:
                pass  # made meanwhile: judged below, as it is
            info = os.lstat(part)
        if stat.S_ISLNK(info.st_mode):
            raise ShipError(f"{seat_name}: {part} is a link; a jailed seat's working folder is a real folder of its "
                            f"ship, reached without links. Remove the link and {remedy}")
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ShipError(f"{seat_name}: {part} is not this user's own folder; a jailed seat's working folder is a "
                            f"real folder of its ship, its operator's. Remove it and {remedy}")
    if spec.cwd is not None and Path(os.path.realpath(spec.cwd)) != Path(os.path.realpath(folder)):
        raise ShipError(f"{seat_name}: its working folder {folder} was found at {spec.cwd} when the ship was read, "
                        "through a link since changed; start again, so the ship is read afresh")
    if stat.S_IMODE(info.st_mode) != 0o700:
        # Private, as when it is made: through a descriptor of the folder walked, never a link put there since.
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino) != (info.st_dev, info.st_ino):
                raise ShipError(f"{seat_name}: {folder} changed while it was checked; start again")
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)


def fence_point(seat_name: str, folder: Path) -> None:
    """Make a missing fleet folder, empty, so the jail can fence it; refuse one that is not a real folder.

    The fence is an empty tmpfs at the folder's own path, and bwrap can make that mount point only where the
    jail can write. A folder missing at launch under a parent seen read-only inside (the root, or a folder
    bound back such as the framework's own checkout) would get no fence, and a ship raised there later would
    show inside. So the folder is made before the launch, and a launch that cannot make it is refused.
    """
    # law: one-folder
    # The fence's empty mount point is inside the project, where the office's own
    # declaration belongs; qd init accepts that empty folder.
    try:
        folder.mkdir()
    except FileExistsError:
        pass
    except OSError as error:
        raise ShipError(f"{seat_name}: sandboxed, and the fleet folder {folder} is missing and cannot be made "
                        f"({error.strerror or type(error).__name__}); the jail fences it only where it exists, so a ship "
                        f"raised there later would show inside. Create {folder} as an empty folder, then start the seat") from None
    if folder.is_symlink() or not folder.is_dir():
        raise ShipError(f"{seat_name}: sandboxed, and the fleet folder {folder} is not a real folder; the jail fences "
                        f"only a real folder. Make {folder} a folder of the project, not a link or a file")


def fleet_roots(ship_folder: Path) -> tuple[Path, ...]:
    """The project's ``ships/`` and ``office/`` when this ship sits in that layout, else nothing.

    ``covering`` refuses a jail when one of these is the home or contains it. The layout is read from the ship folder's
    real path, links resolved, so an office given through a link of another name is still the checkout's office; and
    from the folder as given only when its real path shows no layout, as for an office that is itself a link.
    """
    return layout_roots(Path(os.path.realpath(ship_folder))) or layout_roots(Path(ship_folder))


def layout_roots(folder: Path) -> tuple[Path, ...]:
    """``ships/`` and ``office/`` beside a folder named as a ship's in that layout, read from the path as it is."""
    if folder.parent.name == 'ships':
        project = folder.parent.parent
    elif folder.name == 'office':
        project = folder.parent
    else:
        return ()
    return (project / 'ships', project / 'office')


def check_proof_folder(runtime: Path, seat_name: str) -> Path:
    """``runtime/check/<seat>``: the only check directory this seat's jail may write."""
    folder = runtime / "check" / seat_name
    if (not seat_name or seat_name in (".", "..") or "/" in seat_name or "\\" in seat_name
            or folder.parent != runtime / "check"):
        raise ShipError(f"{seat_name}: check proof folder must be one directory under the check folder")
    return folder


def check_proof_file(runtime: Path, seat_name: str) -> Path:
    """The regular file ``qd check`` asks this seat to write, inside its own proof folder."""
    return check_proof_folder(runtime, seat_name) / f"{seat_name}.txt"


def ensure_check_proof_folder(runtime: Path, seat_name: str) -> Path:
    """The seat's check folder: a real directory, created when absent.

    ``runtime/check`` is examined first, then ``runtime/check/<seat>``. Each is used when ``lstat`` says it
    is a directory, created when it is absent, and refused by path otherwise. A link is never followed and
    never removed. The runtime itself is the ship's own, made when absent as before; no jail writes it.
    """
    folder = check_proof_folder(runtime, seat_name)
    runtime.mkdir(parents=True, exist_ok=True)
    _ensure_real_directory(runtime / "check", parent_checked=False)
    _ensure_real_directory(folder, parent_checked=True)
    return folder


def _ensure_real_directory(path: Path, *, parent_checked: bool) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        _mkdir_nofollow(path, parent_checked)
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ShipError(f"{path}: check folder must be a real directory")


def _mkdir_nofollow(path: Path, parent_checked: bool) -> None:
    """Create ``path`` under its opened parent, then refuse what is not a directory there.

    A checked parent, ``runtime/check``, is opened with ``O_NOFOLLOW`` as well, so a link swapped in after
    its ``lstat`` fails. The runtime above it is opened as the ship names it.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | (os.O_NOFOLLOW if parent_checked else 0)
    try:
        directory = os.open(path.parent, flags)
    except OSError as error:
        raise ShipError(f"{path.parent}: check folder must be a real directory") from error
    try:
        try:
            os.mkdir(path.name, dir_fd=directory)
        except FileExistsError:
            info = os.lstat(path.name, dir_fd=directory)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ShipError(f"{path}: check folder must be a real directory") from None
    finally:
        os.close(directory)


def covering(jail: Jail) -> tuple[str, Path] | None:
    """The first folder the jail would bind that is the operator home or contains it, named, or None.

    The home's empty tmpfs lies in depth order among the mounts, so an ancestor bound before it is covered again;
    a jail that binds one is refused all the same, since it would hold the home's own path open beside it. A
    folder inside the home that is bound back is allowed: that is how a program installed there runs.
    The fleet folders ``ships/`` and ``office/`` are named with the other mounts. One that is the home or
    contains it is refused the same way, including where the home's tmpfs would cover that path again.
    """
    named = [("the working directory", jail.workspace), ("the ship folder", jail.ship_folder),
             ("the seat's own folder", jail.seat_folder)]
    named += [("the bound-back folder", folder) for folder in jail.read_only]
    named += [("the writable folder", folder) for folder in jail.writable]
    named += [("the fleet folder", folder) for folder in fleet_roots(jail.ship_folder)]
    return next(((what, folder) for what, folder in named if covers(folder, jail.home)), None)


def program_folders(programs: tuple[str, ...], path: str, home: Path) -> list[Path]:
    """The folders under the operator's home that the programs resolve through, parents first.

    For each program found on the PATH: the folder of every link in its chain, the install prefix above a
    ``bin`` (not the home itself, nor ``~/.local``, whose ``share`` is the operator's), and the root above a
    ``node_modules``. Then every PATH entry, so the shell finds the operator's tools, and the framework's own
    interpreter, code and files, so ``qd`` runs inside.
    """
    return sorted((f for f in _resolved(programs, path, home) if home in f.parents and not covers(f, home)),
                  key=lambda f: (len(f.parts), str(f)))


def covers(folder: Path, home: Path) -> bool:
    """Whether binding ``folder`` would lay it over the hidden home: it is the home or contains it.

    Every bind back comes after the home's empty tmpfs, so such a folder would show the home again, read-only.
    A program installed inside the home is bound back by its own folders below the home, never by one above.
    """
    return home.is_relative_to(folder)


def _resolved(programs: tuple[str, ...], path: str, home: Path) -> set[Path]:
    """Every folder the programs resolve through, the PATH entries, and the framework's own interpreter and code."""
    found: set[Path] = set()
    for name in programs:
        file = shutil.which(name, path=path)
        if not file:
            continue
        hop = Path(file)
        for _ in range(HOPS):
            folder = hop.parent
            found.add(folder)
            if folder.name == "bin" and folder.parent not in (home, home / ".local", Path("/")):
                found.add(folder.parent)
            parts = hop.parts
            if "node_modules" in parts:
                found.add(Path(*parts[:parts.index("node_modules")]))
            if not hop.is_symlink():
                break
            target = Path(os.readlink(hop))
            hop = Path(os.path.normpath(target if target.is_absolute() else folder / target))
    found.update(Path(entry) for entry in path.split(os.pathsep) if entry and Path(entry).is_dir())
    found.add(Path(sys.prefix))
    found.add(Path(__file__).resolve().parent.parent)
    found.add(standing.PROJECT_DIR)
    return found


def hidden_folders(programs: tuple[str, ...], path: str, home: Path) -> list[Path]:
    """The same folders where they lie below the jail's own /tmp or fresh /dev, which hide them as the home is
    hidden: a test run's working storage in memory, or a program installed there. Each is bound back read-only;
    /tmp, /dev and /dev/shm themselves never are, and neither is a folder that is or contains the home, which
    would lay it over the hidden home: so the jail is no wider than on disk."""
    return sorted((f for f in _resolved(programs, path, home)
                   if f not in NEVER and any(root in f.parents for root in HIDDEN) and not covers(f, home)),
                  key=lambda f: (len(f.parts), str(f)))


def runtime_dir() -> Path:
    """The jail's own XDG_RUNTIME_DIR: the operator's path, empty and private inside."""
    return Path('/run/user') / str(os.getuid())


def session_hidden() -> list[str]:
    """The operator's desktop session, out of the jail's reach.

    A read-only mount does not stop a socket connect, so /run is an empty tmpfs: the session bus and the user
    manager, the keyring, gpg and ssh agents, wayland, the system bus and every other daemon's socket there are
    gone. Only the resolver's file is bound back, read-only, when /etc/resolv.conf leads into /run, and a private,
    empty runtime folder is made for the program. /mnt, the operator's own disks and the machine's shared
    scratch, is an empty tmpfs; what a seat is given under it is bound back after.
    """
    args = ["--size", str(RUN_BYTES), "--tmpfs", "/run"]
    resolver = Path(os.path.realpath('/etc/resolv.conf'))
    if resolver.is_relative_to('/run') and resolver.is_file() and not resolver.is_symlink():
        args += ["--ro-bind", str(resolver), str(resolver)]
    args += ["--perms", "0700", "--dir", str(runtime_dir())]
    if MNT.is_dir() and not MNT.is_symlink():
        # Before every other mount, so a workspace or a program's folder under /mnt is still bound back after it.
        args += ["--size", str(MNT_BYTES), "--tmpfs", str(MNT)]
    return args


def listening_sockets() -> list[Path]:
    """Path sockets that some process listens on, from the kernel's own table (what ``ss -xl`` lists)."""
    found = []
    try:
        lines = Path('/proc/net/unix').read_text().splitlines()[1:]
    except OSError:
        return found
    for line in lines:
        fields = line.split()
        # Num RefCount Protocol Flags Type St Inode Path: a listening socket has __SO_ACCEPTCON (0x10000) set.
        if len(fields) < 8 or not int(fields[3], 16) & 0x10000 or not fields[7].startswith('/'):
            continue
        found.append(Path(os.path.realpath(fields[7])))  # /var/run leads to /run: judge the real place
    return sorted(set(found))


def sockets_masked(jail: Jail) -> list[str]:
    """Every listening socket the jail would still show, with /dev/null bound over it so a connect fails.

    A path socket ignores the jail's own network. /run, /tmp, /dev, /mnt and the homes are emptied inside, and so are
    the covers (other ships' runtimes, test-run folders) and the runtime fence. A socket is masked when the deepest
    mount over it is one that shows it: a folder bound back from a home, the workspace, the seat's own folders or the
    root itself (a daemon's socket in a snap's run folder, for one). A folder bound through a link is matched by its
    real path and masked at the path it has inside. The sockets are those listening as the jail is
    built; the launch follows within the same moment, so one that vanishes in between is rare, and stops the launch
    rather than opening anything.
    """
    hidden = [Path('/run'), Path('/tmp'), Path('/dev'), Path('/proc'), Path('/mnt'), Path('/home'),
              Path(os.path.expanduser('~')), jail.home, jail.runtime, jail.runtime / 'tmux', jail.runtime / LOCKS,
              *getattr(jail, 'foreign_runtimes', ()), *fleet_roots(jail.ship_folder)]  # the fleet: an empty fence
    shown = [jail.workspace, jail.ship_folder, jail.seat_folder, *jail.read_only, *jail.writable,
             *(jail.runtime / name for name in RUNTIME_SHOWN)]

    def depth(path: Path, roots: list[Path]) -> int:
        return max((len(root.parts) for root in roots if path == root or path.is_relative_to(root)), default=-1)

    # A root may run through a link: bwrap binds the link's target at the link's path. So each root is matched by
    # its real path, and a socket is judged, and masked, at every path it shows inside: its own real path, and each
    # root as bound plus the socket's path relative to that root's real path.
    roots = [(root, Path(os.path.realpath(root))) for root in (*hidden, *shown)]
    args, masked = [], set()
    for real in listening_sockets():
        inside = {real}
        for root, resolved in roots:
            if real == resolved or real.is_relative_to(resolved):
                inside.add(root / real.relative_to(resolved))
        for path in sorted(inside):
            covered, bound = depth(path, hidden), depth(path, shown)
            if path in masked or covered >= 0 and covered >= bound:
                continue  # the deepest mount over it empties it: not in sight there
            try:
                if not stat.S_ISSOCK(path.stat().st_mode):
                    continue
            except OSError:
                continue
            masked.add(path)
            args += ["--ro-bind", "/dev/null", str(path)]
    return args


def command(jail: Jail, argv: list[str]) -> list[str]:
    """The ``bwrap`` command line around a program's own."""
    args = [PROGRAM, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--size", str(TMP_BYTES), "--tmpfs", "/tmp"]
    args += session_hidden()
    relay = None
    if jail.endpoint is not None:
        from . import endpoint_relay

        relay = endpoint_relay.folder_for(jail.seat_folder)
        # The relay's folder, made by the relay before the jail starts, inside the jail's own /run; and its hosts file.
        args += ["--bind", str(relay), endpoint_relay.INSIDE_FOLDER,
                 "--ro-bind", str(relay / endpoint_relay.HOSTS), "/etc/hosts"]
    # The operator's home is an empty tmpfs placed in depth order like every other mount: after any folder that
    # contains it, which it then covers again, and before the folders inside it that are bound back.
    mounts: list[tuple[str, Path]] = [("--tmpfs", jail.home)]
    mounts += [("--ro-bind-try", folder) for folder in jail.read_only]
    fleet_folders = fleet_roots(jail.ship_folder)
    # The ship's own tmux server is hidden like the other seats: its socket folder is an empty private
    # tmpfs inside, so nothing in the jail reaches the server, by its path or any other way.
    # The service's own locks are hidden the same way: a lock a jailed seat could take (flock needs no write
    # access) would let it stop the service's pass.
    mounts += [("--workspace", jail.workspace)]
    mounts += [("--fleet", folder) for folder in fleet_folders]
    mounts += [("--ro-bind", jail.ship_folder), ("--runtime", jail.runtime),
               ("--private-tmpfs", jail.runtime / "tmux"),
               ("--private-tmpfs", jail.runtime / LOCKS), ("--bind", jail.seat_folder)]
    mounts += [("--ro-bind-try", jail.runtime / name) for name in RUNTIME_SHOWN]
    mounts += [("--ro-bind-try", jail.runtime / "brief" / f"{jail.seat_folder.name}.md")]
    # The post: the seat's own view of it in the post's place, read-only, with its real drop writable over it.
    # A jail made without a view (only by hand, never by plan) is shown no post at all.
    if jail.post_view is not None:
        mounts += [("--view", jail.post_view)]
        # A check seat's own lease, the real files over the view's placeholders, for its own jailed qd inbox alone.
        from .post_view import lease_names
        mounts += [("--ro-bind-try", jail.runtime / "post" / "transient" / name)
                   for name in lease_names(jail.seat_folder.name)
                   if (jail.post_view / "transient" / name).exists()]
    mounts += [("--bind-try", folder) for folder in jail.writable]
    mounts += [("--declaration", file) for file in getattr(jail, 'declarations', ())]
    mounts += [("--tmpfs", runtime) for runtime in getattr(jail, 'foreign_runtimes', ())]

    def mount_key(mount):
        flag, folder = mount
        target = (folder.parent if flag == '--declaration' else jail.runtime / 'post' if flag == '--view' else folder)
        # Fence the fleet after the writable workspace, even a workspace inside
        # it; then restore its children in parent-first order. The home's tmpfs
        # stays in that depth order, and last when the path is the same.
        # A workspace in the jail's own ship folder (plan allows no other place in the fleet) is mounted in the
        # fleet's depth order, after that folder's read-only bind, so it stays writable.
        own = flag == '--workspace' and target.is_relative_to(jail.ship_folder)
        fleet = (flag != '--workspace' or own) and any(target.is_relative_to(root) for root in fleet_folders)
        return fleet, len(target.parts), str(target), mount == ('--tmpfs', jail.home)

    fenced = []
    for flag, folder in sorted(mounts, key=mount_key):  # a child mount is never hidden by its parent's
        if flag == '--workspace':
            flag = '--bind'
        if flag == '--declaration':
            args += ['--size', str(HIDDEN_BYTES), '--tmpfs', str(folder.parent), '--ro-bind', str(folder), str(folder),
                     '--remount-ro', str(folder.parent)]
        elif flag == '--fleet':
            # The whole fleet folder is an empty tmpfs, never the folder itself: a ship raised there after
            # this launch shows inside as nothing. Only this ship's own folder and the peers' ship files
            # found at launch are bound back into it, and it is made read-only once they are in place.
            if folder.exists() or any(folder.parent.is_relative_to(root)
                                      for root in (jail.workspace, jail.seat_folder, *jail.writable)):
                # law: one-folder
                # A missing folder's empty mount point stays inside the project; office/ is
                # where the office's own declaration belongs.
                args += ['--size', str(HIDDEN_BYTES), '--tmpfs', str(folder)]
                fenced.append(folder)
        elif flag == '--view':
            args += ['--ro-bind', str(folder), str(jail.runtime / 'post')]
        elif flag == '--runtime':
            # The runtime is fenced like the fleet: empty, bounded, and read-only once what is shown is in place.
            args += ['--size', str(HIDDEN_BYTES), '--tmpfs', str(folder)]
            fenced.append(folder)
        elif flag == "--private-tmpfs":
            args += ["--perms", "0700", "--size", str(HIDDEN_BYTES), "--tmpfs", str(folder)]
        elif flag == "--tmpfs":  # the operator's home, bounded like every tmpfs inside
            args += ["--size", str(HOME_BYTES if folder == jail.home else HIDDEN_BYTES), "--tmpfs", str(folder)]
        else:
            args += [flag, str(folder), str(folder)]
    for folder in fenced:  # after every mount inside it, which needed its mount point made there
        args += ['--remount-ro', str(folder)]
    args += sockets_masked(jail)
    for variable in SESSION_VARIABLES:
        args += ["--unsetenv", variable]
    args += ["--setenv", "XDG_RUNTIME_DIR", str(runtime_dir())]
    for variable, value in jail.environment:
        args += ["--setenv", variable, value]
    # The launch's TMPDIR may name a filesystem the jail mounts read-only; the program
    # uses the jail's own /tmp for scratch, including spooled tool output.
    args += ["--setenv", "TMPDIR", "/tmp", "--setenv", "TMP", "/tmp", "--setenv", "TEMP", "/tmp"]
    # A network of its own, loopback only: no abstract socket, no outside listener and no other host is reachable.
    args += ["--setenv", "HOME", str(jail.seat_home), "--unshare-pid", "--unshare-net", "--die-with-parent"]
    if relay is None:
        return args + ["--", *argv]
    from . import endpoint_relay

    host, port, name = endpoint_parts(jail.endpoint)
    python = os.path.realpath(sys.executable)  # the machine's own interpreter, which every jail sees read-only
    inside = [python, "-I", "-c", endpoint_relay.INSIDE, f"{endpoint_relay.INSIDE_FOLDER}/{endpoint_relay.SOCKET}",
              str(port), "--", *argv]
    return [sys.executable, "-I", "-m", "quarterdeck.endpoint_relay", str(relay), host, str(port), name or "", "--",
            *args, "--", *inside]
