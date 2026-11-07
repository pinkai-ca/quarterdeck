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
from dataclasses import replace
from pathlib import Path

from . import credential, harness as harnesses, post, servicectl, watch
from .drill import Check, wait_for
from .harness.base import key_environment, program_version
from .seat import Seat, program_path
from .ship import Pool, SeatSpec, Session, Ship, ShipError
from .tmux import Tmux

PATIENCE = 600.0  # a fix is a turn of real work
PROGRAMS = {"claude": "claude", "codex": "codex", "antigravity": "agy", "fake": "qd-fakeharness"}


def checks(ship: Ship) -> list[Check]:
    """Every check, passed or failed, each with what it found or what would lift it."""
    found = [Check("ship file", True, f"{ship.file.name}: {len(ship.seats)} seats, {len(ship.pools)} pools, {len(ship.sessions)} sessions")]
    for program, flag in (("tmux", "-V"), ("git", "--version")):
        version = program_version((program, flag))
        found.append(Check(program, version is not None, f"{program} {version}" if version else f"{program} is not on the PATH; install it"))
    searched = program_path()
    for name in sorted({seat.harness for seat in ship.seats.values()}):
        program = PROGRAMS[name]
        if shutil.which(program, path=searched) is None:
            found.append(Check(f"program {name}", False, f"{program} is not on the PATH qd runs with; install it, or put it on that PATH"))
            continue
        version = harnesses.get(name).version()
        proven = version is not None and (ship.runtime / "proven" / f"{name}-{version}").exists()
        note = "the fake harness" if name == "fake" else ("proven by qd check" if proven else f"unproven: qd check {name} proves it")
        found.append(Check(f"program {name}", True, f"{program} {version or 'version unknown'} · {note}"))
    found += [pool_check(pool) for pool in ship.pools.values()]
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


def pool_check(pool: Pool) -> Check:
    """A pool is ready when the credential it names is there; expiry is read as metadata, never a token."""
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
    pool = next((p for p in ship.pools.values() if p.provider in PROGRAMS and shutil.which(PROGRAMS[p.provider], path=searched)), None)
    if pool is None:
        return [Check("seat opened", False, "no declared pool's program is on the PATH, so no seat can be asked; install one first")]
    run = secrets.token_hex(2)
    seat_name, session_name = f"doctor-{run}", f"qd-doctor-{run}"
    spec = SeatSpec(seat_name, session_name, "engineering", pool.provider, (pool.name,), persona="McCoy", cwd=ship.folder)
    treated = replace(ship, sessions={**ship.sessions, session_name: Session(session_name, session_name)},
                      seats={**ship.seats, seat_name: spec})
    seat = Seat(treated, spec, tmux)
    store = post.Store(ship.runtime)
    body = ("qd doctor ran its checks on this machine, and these failed:\n"
            + "\n".join(f"- {check.name}: {check.detail}" for check in failed)
            + "\n\nFix what can be fixed from this machine: install or sign in to what is missing, make what is absent, "
              "free what is full. Change nothing of the ship's declaration without saying so. Then report on this order, "
              "saying what you changed and what you could not.")
    order = store.new("order", post.SHIP, seat_name, body, subject="qd doctor")
    opened = not tmux.has_session(session_name)
    try:
        proof = seat.up(resume=False)  # the order travels as the seat's first message
        closed = wait_for(lambda: store.closing_report(order), PATIENCE)
        found = [Check("seat opened", True, proof),
                 Check("seat reported", closed is not None, closed.body[:160] if closed else f"no report within {PATIENCE:.0f}s")]
    finally:
        seat.down()
        if opened and tmux.has_session(session_name):
            tmux.kill_session(session_name)  # only what this run opened
        if store.closing_report(order) is None:
            store.new("report", seat_name, post.SHIP, "qd doctor ended; closed by the doctor itself", re=order.id)
        shutil.rmtree(seat.folder, ignore_errors=True)
    return found
