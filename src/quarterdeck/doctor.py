"""qd doctor: is this machine ready to run the ship, and what to do about what is not.

The installation and the declaration are checked and a proof line printed per check: tmux and
git, the program each harness needs and its version, the sign-in each pool names and when its
grant expires, every seat's working directory, the runtime's fence, the service, and the disk.
With ``--fix`` the failures go to a throwaway seat on the first pool whose program runs, as an
order: fix what can be fixed from this machine and report. When it has reported, the checks run
again. This is the ship's own kind of help: a program that runs the ship can read a failure and
mend it, where a script could only name it. A seat that fixes spends tokens, so it opens only
when asked.
"""

from __future__ import annotations

import secrets
import shutil
import tempfile
from pathlib import Path

from . import credential, endpoint, harness as harnesses, install, post, providers, sandbox, servicectl, watch
from .drill import Check, wait_for
from .harness.base import _version_in_runtime, key_environment, program_version
from .harness.capabilities import validate_selection
from .seat import program_path, temporary_seat
from .ship import Pool, SeatSpec, Ship, ShipError
from .tmux import Tmux

PATIENCE = 600.0  # a fix is a turn of real work



def checks(ship: Ship) -> list[Check]:
    """Every check, passed or failed, each with what it found or what would lift it."""
    found = [Check("ship file", True, f"{ship.file.name}: {len(ship.seats)} seats, {len(ship.pools)} pools, {len(ship.sessions)} sessions")]
    for program, flag in (("tmux", "-V"), ("git", "--version")):
        version = program_version((program, flag))
        found.append(Check(program, version is not None, f"{program} {version}" if version else f"{program} is not on the PATH; install it"))
    found.append(framework_check(ship))
    searched = program_path()
    versions = {}
    for name in sorted({seat.for_pool(pool).harness for seat in ship.seats.values() for pool in seat.pools}):
        harness = harnesses.get(name)
        program = harness.program
        if shutil.which(program, path=searched) is None:
            found.append(Check(f"program {name}", False, f"{program} is not on the PATH qd runs with; install it, or put it on that PATH"))
            continue
        version = _version_in_runtime(harness, ship.runtime)
        versions[name] = version
        proven = version is not None and (ship.runtime / "proven" / f"{name}-{version}").exists()
        note = "synthetic program" if harness.synthetic else ("proven by qd check" if proven else f"unproven: qd check {name} proves it")
        found.append(Check(f"program {name}", True, f"{program} {version or 'version unknown'} · {note}"))
    for pool in ship.pools.values():
        checked = pool_check(pool)
        native = harnesses.native_name(pool.provider)
        identity = f"provider {providers.display(pool.provider)} · {pool.kind} · native harness {native or 'none'}"
        found.append(Check(checked.name, checked.passed, f"{identity} · {checked.detail}"))
        if pool.declared_provider != pool.provider:
            found.append(Check(f"provider spelling {pool.name}", True,
                               f'in [pools.{pool.name}] change provider = "{pool.declared_provider}" to provider = "{pool.provider}"; '
                               "the old spelling remains readable for compatibility"))
    if any(ship.jailed(seat.for_pool(pool), ship.pools[pool])
           for seat in ship.seats.values() for pool in seat.pools):
        version = program_version((sandbox.PROGRAM, "--version"))
        found.append(Check("sandbox", version is not None, f"bubblewrap {version}: jailed seats run in it" if version
                           else f"{sandbox.PROGRAM} is not on the PATH, and a seat is jailed; install bubblewrap, or set sandbox = false on it"))
    found.extend(seat_readiness(ship, versions=versions))
    for seat in ship.seats.values():
        cwd = ship.cwd_of(seat)
        found.append(Check(f"seat {seat.name}", cwd.is_dir(), f"works in {cwd}" if cwd.is_dir()
                           else f"working directory {cwd} does not exist; qd creates nothing outside its folder"))
    if ship.runtime.exists():
        fenced = (ship.runtime / ".git").is_dir()
        found.append(Check("runtime fence", fenced, "runtime/ is its own repository" if fenced else "runtime/ is not fenced; qd up fences it"))
        alive = servicectl.alive(ship.runtime)
        found.append(Check("service", alive, "alive" if alive else "down; qd up starts it"))
    low = watch.disks([ship.runtime, ship.workspace, Path(tempfile.gettempdir())])
    found.append(Check("disk", not low, "room on every filesystem the ship writes to" if not low
                       else "nearly full: " + ", ".join(f"{mount} {pct}%" for mount, pct in low)))
    return found


def seat_readiness(ship: Ship, *, versions: dict[str, str | None] | None = None) -> list[Check]:
    """Readiness failures and passing information notices; no seat is launched."""
    found = []
    for seat in ship.seats.values():
        for choice in seat.pools:
            selected = seat.for_pool(choice)
            reader = harnesses.get(selected.harness)
            home = ship.runtime / "seats" / seat.name / "home"
            cwd = ship.cwd_of(selected)
            try:
                note = validate_selection(selected, reader,
                                          version=(versions or {}).get(reader.name), installed=True)
                reader.check_defaults(selected, ship.folder / 'homes' / reader.name)
                found.append(Check(f"seat {seat.name} pool {choice} effort", True,
                                   note or 'declared selection compatible; no model/account check'))
                reader.preflight(home, cwd)
            except ShipError as error:
                found.append(Check(f"seat {seat.name} pool {choice} readiness", False, str(error)))
            notice = reader.workspace_notice(home, cwd)
            if notice:
                found.append(Check(f"seat {seat.name} pool {choice} workspace notice", True, notice))
    return found


def framework_check(ship: Ship) -> Check:
    """Which framework the ship runs: its own installed copy, or the checkout it sits in."""
    copy = install.installed(ship.runtime)
    if copy:
        if copy.get('proof') == 'failed':
            return Check("framework", False, f"installed target {copy['commit'][:7]} code retained; "
                         f"{copy.get('proof_command', 'installed')} proof failed; upgrade did not restart service")
        return Check("framework", True, f"the ship runs its installed copy, commit {copy['commit'][:7]} since {copy['at'][:16]}; qd upgrade adopts another")
    try:
        head = install.git(install.source_repo(ship), "rev-parse", "--short", "HEAD").decode().strip()
    except ShipError as error:
        return Check("framework", True, f"the ship runs this qd; {error}")
    return Check("framework", True, f"the ship runs the checkout at {head}; qd upgrade makes it an installed copy")


def pool_check(pool: Pool) -> Check:
    """A pool is ready when the credential it names is there, or its endpoint answers; expiry is read as metadata, never a token."""
    if pool.kind == "endpoint":
        try:
            return Check(f"pool {pool.name}", True, endpoint.describe(pool))
        except ShipError as error:
            return Check(f"pool {pool.name}", False, f"{error}; start the server, or point the pool at one that answers")
    try:
        harnesses.native_for(pool.provider).preflight()
    except ShipError as error:
        return Check(f"pool {pool.name}", False, str(error))
    if pool.kind == "key":
        try:
            key_environment(pool, "KEY")
            return Check(f"pool {pool.name}", True, f"key present in {pool.api_key}")
        except ShipError as error:
            return Check(f"pool {pool.name}", False, str(error))
    file = credential.pool_file(pool)
    if file is None:
        return Check(f"pool {pool.name}", True, "the program holds its own sign-in")
    if not file.exists():
        return Check(f"pool {pool.name}", False, f"no credential at {file}; sign in to the program there")
    expiry = credential.grant_expiry(pool)
    when = f"; its grant expires {expiry.astimezone():%m-%d %H:%M}" if expiry else ""
    return Check(f"pool {pool.name}", True, f"signed in at {file.parent}{when}")


def fix(ship: Ship, failed: list[Check], tmux: Tmux) -> list[Check]:
    """Open a throwaway seat on the first pool whose program runs, hand it the failures as an order, and wait for its report."""
    searched = program_path()
    available = next(((pool, reader) for pool in ship.pools.values() for name in harnesses.NAMES
                      if (reader := harnesses.get(name)).accepts(pool) is None
                      and shutil.which(reader.program, path=searched)), None)
    if available is None:
        return [Check("seat opened", False, "no declared pool's program is on the PATH, so no seat can be asked; install one first")]
    pool, reader = available
    run = secrets.token_hex(2)
    seat_name, session_name = f"doctor-{run}", f"qd-doctor-{run}"
    spec = SeatSpec(seat_name, session_name, "engineering", reader.name, (pool.name,), persona="McCoy", cwd=ship.folder)
    store = post.Store(ship.runtime)
    body = ("qd doctor ran its checks on this machine, and these failed:\n"
            + "\n".join(f"- {check.name}: {check.detail}" for check in failed)
            + "\n\nFix what can be fixed from this machine: install or sign in to what is missing, make what is absent, "
              "free what is full. Change nothing of the ship's declaration without saying so. Then report on this order, "
              "saying what you changed and what you could not.")
    orders: list[post.Message] = []
    with temporary_seat(ship, tmux, spec, orders, "qd doctor") as seat:
        order = store.new("order", post.SHIP, seat_name, body, subject="qd doctor")
        orders.append(order)
        proof = seat.up(resume=False)  # the order travels as the seat's first message
        closed = wait_for(lambda: store.closing_report(order), PATIENCE)
        found = [Check("seat opened", True, proof),
                 Check("seat reported", closed is not None, closed.body[:160] if closed else f"no report within {PATIENCE:.0f}s")]
    return found
