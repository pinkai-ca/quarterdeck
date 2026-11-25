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

import os
import secrets
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import copy_state, credential, endpoint, fleet, harness as harnesses, install, post, providers, sandbox, servicectl, shutdown, watch
from . import key_proof, keyfile
from .drill import Check, wait_for
from .harness import claude_sign_in
from .harness.base import _version_in_runtime, program_version, program_where
from .harness.capabilities import validate_selection
from .seat import program_path, temporary_seat
from .ship import CARRY_BYTES, Pool, SeatSpec, Ship, ShipError
from . import ship as shipmod
from .tmux import Tmux

PATIENCE = 600.0  # a fix is a turn of real work



def checks(ship: Ship) -> list[Check]:
    """Every check, passed or failed, each with what it found or what would lift it."""
    from .harness import claude_extra_usage as extra_usage

    found = [Check("ship file", True, f"{ship.file.name}: {len(ship.seats)} seats, {len(ship.pools)} pools, {len(ship.sessions)} sessions; "
                   f"carry_bytes={getattr(ship, 'carry_bytes', CARRY_BYTES)} UTF-8 bytes (first message and index-page policy bound)")]
    shell = shell_check()
    if shell is not None:
        found.append(shell)
    for program, flag in (("tmux", "-V"), ("git", "--version")):
        version = program_version((program, flag))
        found.append(Check(program, version is not None, f"{program} {version}" if version else f"{program} is not on the PATH; install it"))
    found.append(framework_check(ship))
    versions = {}
    programs = {seat.for_pool(pool).harness for seat in ship.seats.values() for pool in seat.pools}
    programs.update(ship.program_pins)  # billing may use a pin before a seat is declared
    for name in sorted(programs):
        row, version = program_observation(name, ship.runtime, ship=ship)
        found.append(row)
        versions[name] = version

    account_readings = {row['pool']: extra_usage.describe(row) for row in extra_usage.readings(ship)}
    for pool in ship.pools.values():
        checked = pool_check(pool)
        native = harnesses.native_name(pool.provider)
        identity = f"provider {providers.display(pool.provider)} · {pool.kind} · native harness {native or 'none'}"
        detail = f"{identity} · {checked.detail}{selected_program(ship, native, versions)}"
        if pool.name in account_readings:
            detail += f" · {account_readings[pool.name]}"
        found.append(Check(checked.name, checked.passed, detail))
        if pool.declared_provider != pool.provider:
            found.append(Check(f"provider spelling {pool.name}", True,
                               f'in [pools.{pool.name}] change provider = "{pool.declared_provider}" to provider = "{pool.provider}"; '
                               "the old spelling remains readable for compatibility"))
    found.extend(shared_subscription_checks(ship))
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
        if not alive and (reason := shutdown.startup_diagnosis(ship.runtime)):
            found.append(Check("delivery startup", False, reason))
    low = watch.disks([ship.runtime, ship.workspace, Path(tempfile.gettempdir())])
    found.append(Check("disk", not low, "room on every filesystem the ship writes to" if not low
                       else "nearly full: " + ", ".join(f"{mount} {pct}%" for mount, pct in low)))
    return found


def _subscription_identity(pool: Pool):
    if pool.kind != "subscription" or pool.config_dir is None:
        return None
    try:
        adapter = harnesses.native_for(pool.provider)
        if not adapter.credential_file:
            return None
        info = pool.config_dir.stat()
    except (OSError, ShipError, ValueError):
        return None
    if stat.S_ISDIR(info.st_mode):
        return (info.st_dev, info.st_ino), adapter.credential_file, adapter.name
    return None


def shared_subscription_checks(ship: Ship) -> list[Check]:
    """Names-only advisory. A peer's credential and runtime are never opened."""
    local = [(pool.name, identity) for pool in ship.pools.values()
             if (identity := _subscription_identity(pool)) is not None]
    found = []
    for peer, pool in fleet.subscription_homes(ship):
        match = _subscription_identity(pool)
        if match is None:
            continue
        for name, identity in local:
            if identity[0] != match[0]:
                continue
            if identity[1] != match[1]:
                detail = "same subscription directory with separate credential files"
            elif identity[2] != match[2]:
                detail = "incompatible credential formats collide at the same filename"
            else:
                detail = "shared subscription credential; current qd installations coordinate refresh writes"
            found.append(Check(f"subscription overlap {name}", True, f"{peer}/{pool.name}: {detail}. "
                               "Discovery covers known fleet declarations only. Native sign-ins and older qd "
                               "versions can still race, and programs may keep credentials they already opened."))
    return found


def selected_program(ship: Ship, name: str | None, versions: dict[str, str | None]) -> str:
    """Beside a pool: the program its adapter settles once, the path a seat launches by, and its version."""
    if (pin := getattr(ship, "program_pins", {}).get(name)) is not None:
        version = versions.get(name)  # the program row already verified this pin
        return f" · runs {pin.path} · {name} {version}" if version else ""
    harness = harnesses.get(name) if name else None
    where = program_where(harness) if harness is not None else None
    if where is None:
        return ""
    if name not in versions:
        versions[name] = _version_in_runtime(harness, ship.runtime)
    return f" · runs {where} · {harness.program} {versions[name] or 'version unknown'}"


def shell_check() -> Check | None:
    """A warning, never a failure: QD_SHIP set in a shell that is no seat."""
    if not os.environ.get("QD_SHIP") or os.environ.get("QD_SEAT"):
        return None
    return Check("QD_SHIP in this shell", True,
                 "warning: QD_SHIP is set in a shell that is no seat. The default ship replaces it: with no --ship, "
                 "qd acts on the ship qd default marks and says so. Exported from a start-up file, it would point every "
                 "shell, and anything started from one, at that ship unawares, and would still win over the mark after "
                 "the ship is moved or another is chosen; unset it there")


def program_observation(name: str, runtime: Path | None = None, *, ship: Ship | None = None) -> tuple[Check, str | None]:
    """One program/version observation shared by doctor and setup readiness."""
    reader = shipmod.program_adapter(ship, harnesses.get(name))
    pin = getattr(reader, 'program_pin', None)
    if pin is None and shutil.which(reader.program, path=program_path()) is None:
        return (Check(f"program {name}", False,
                      f"{reader.program} is not on the PATH qd runs with; install it, or put it on that PATH"), None)
    try:
        version = _version_in_runtime(reader, runtime) if runtime is not None else reader.version()
    except shipmod.ProgramPinError as error:
        return Check(f"program {name}", False, str(error)), None
    if pin is not None and version is None:
        return Check(f"program {name}", False, f"{name} pin {pin.path}: version could not be read"), None
    proven = runtime is not None and version is not None and (
        pin.proof(runtime, version) if pin is not None else runtime / "proven" / f"{name}-{version}").exists()
    note = "synthetic program" if reader.synthetic else ("proven by qd check" if proven else f"unproven: qd check {name} proves it")
    if pin is not None:
        note += f" · runs {pin.path}"
    return Check(f"program {name}", True, f"{reader.program} {version or 'version unknown'} · {note}"), version


def program_check(name: str, runtime: Path | None = None) -> Check:
    """The same program/version row for doctor and setup; no inferred runtime before creation."""
    return program_observation(name, runtime)[0]


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
                folder = getattr(ship, 'folder', None)
                if folder is None and 'program-default' in (selected.model_mode, selected.effort_mode):
                    raise ShipError('ship folder unavailable; cannot check program-default template provenance')
                reader.check_defaults(selected, folder / 'homes' / reader.name if folder is not None else None)
                found.append(Check(f"seat {seat.name} pool {choice} effort", True,
                                   note or 'declared selection compatible; no model/account check'))
                if selected.model and selected.model_mode != 'program-default':
                    models = reader.listed_models(home, version=(versions or {}).get(reader.name))
                    detail = (f'declared {selected.model}; program model list unknown (not measured or cache unavailable)'
                              if models is None else
                              f'declared {selected.model}; ' +
                              ('listed by the program' if selected.model in models else
                               'not listed by the program; listed ids: ' + ', '.join(sorted(models))))
                    if models is None:
                        prior = found[-1]
                        found[-1] = Check(prior.name, prior.passed, prior.detail + '; ' + detail)
                    else:
                        found.append(Check(f"seat {seat.name} pool {choice} model", selected.model in models, detail))
                reader.preflight(home, cwd)
            except (ShipError, OSError) as error:
                found.append(Check(f"seat {seat.name} pool {choice} readiness", False, str(error)))
            notice = reader.workspace_notice(home, cwd)
            if notice:
                found.append(Check(f"seat {seat.name} pool {choice} workspace notice", True, notice))
    return found


def framework_check(ship: Ship) -> Check:
    """Selected copy and observed service identity are distinct read-only evidence.

    First installation belongs to ``qd up``. Doctor passes the first-copy notice,
    so ``doctor --fix`` leaves it out of the failures it dispatches.
    """
    if ship.code == 'checkout':
        try:
            head = install.git(install.source_repo(ship), 'rev-parse', '--short', 'HEAD').decode().strip()
        except ShipError:
            return Check('framework', False, 'follows the checkout; checkout commit unknown')
        return Check('framework', True, f'follows the checkout at {head}')
    selected = copy_state.select(ship.folder, mode=ship.code, mode_known=True)
    data, fact = selected.state, selected.fact
    lines = []
    if fact:
        lines.append(f"selected copy {fact['commit'][:7]} since {fact['at'][:16]}")
        if fact.get('safety') != install.COPY_SAFETY:
            lines.append('historical copy evidence; its own handler has old in-place management')
    elif not selected.first_copy:
        lines.append('current installed commit unknown')
    if selected.warning:
        lines.append(selected.warning)
    copy_ready = selected.qd is not None or selected.first_copy
    if cutover_problem := install.route_cutover.doctor(ship, data or {}):
        lines.append(cutover_problem)
        copy_ready = False
    if selected.first_copy:
        try:
            install.source_repo(ship)
        except (ShipError, OSError):
            lines.append('source repository unavailable; run qd sync or qd upgrade from a Quarterdeck checkout '
                         'to make the first copy')
    previous = copy_state.previous(data)
    if previous:
        try:
            reason = copy_state.usable(ship.runtime / 'venv.prev', previous)
        except (OSError, ValueError, TypeError):
            reason = 'previous directory identity unknown'
        lines.append(f"previous {previous['commit'][:7]} · "
                     + (f'unavailable: {reason}' if reason else 'qd sync --back returns to it'))
    else:
        lines.append('no proven previous copy; a successful sync creates one')
    if data and data.get('last_failure'):
        failure = data['last_failure']
        if isinstance(failure, dict):
            # Only bounded installer vocabulary is displayed, never raw corrupt file content.
            phase = failure.get('phase')
            known = {'preparing', 'stopping', 'snapshotting', 'backing_up', 'backup_ready', 'installing',
                     'resources', 'swapping', 'proving', 'target_ready', 'activated', 'cleanup',
                     'recovering', 'removing_failed_live', 'removing_failed_first_install', 'restored',
                     'discarding_failed_previous', 'status lines', 'rebrief'}
            lines.append('last copy failure: ' + (phase if isinstance(phase, str) and phase in known else 'phase unknown'))
            reason = failure.get('reason')
            reasons = {'copy operation did not complete', 'status-line update pending', 'rebrief confirmation pending',
                       'origin proof did not complete', '--version proof did not complete',
                       'laws proof did not complete', '--help proof did not complete'}
            step = failure.get('step')
            if isinstance(reason, str) and reason in reasons | install.FAILURE_CAUSES:
                lines.append(reason + (f' at {step}' if isinstance(step, str) and step in install.FAILURE_STEPS else ''))
        else:
            lines.append('last copy failure unknown')
    identity = servicectl.service_identity(ship.runtime)
    observed = identity.get('copy') if identity else None
    identity_ok = True
    if isinstance(observed, dict) and copy_state.COMMIT.fullmatch(str(observed.get('commit', ''))):
        lines.append(f"observed service commit {observed['commit'][:7]}")
        if fact and observed.get('commit') != fact['commit']:
            lines.append('observed service differs from the selected copy')
            identity_ok = False
    elif servicectl.alive(ship.runtime):
        lines.append('service owns its lock; loaded commit unknown')
        identity_ok = False
    else:
        lines.append('service down; no running commit observed')
    if fact and 'instance' in fact and observed is not None:
        identity_ok = observed == {key: fact.get(key) for key in ('commit', 'instance', 'package_digest')}
        if not identity_ok:
            lines.append('observed service copy instance is unknown or mismatched')
    return Check('framework', copy_ready and identity_ok,
                 ' · '.join(lines))


def pool_check(pool: Pool) -> Check:
    """A pool is ready when the credential it names is there, or its endpoint answers; expiry is read as metadata, never a token.

    A pool paid by a key is ready when its key is in the key file and one call that spends nothing does not refuse it."""
    if pool.paid_by_key:
        line = keyfile.status_line(pool).removeprefix(f"pool {pool.name}: ")
        if keyfile.problem(pool) is not None:
            return Check(f"pool {pool.name}", False, line)
        passed, proof = key_proof.prove(pool)
        note = " · other accounts can read the key file; 0600 is advised" if keyfile.open_to_others(pool) else ""
        return Check(f"pool {pool.name}", passed, f"{line} · {proof}{note}")
    if pool.kind == "endpoint":
        try:
            return Check(f"pool {pool.name}", True, endpoint.describe(pool))
        except ShipError as error:
            return Check(f"pool {pool.name}", False, f"{error}; start the server, or point the pool at one that answers")
    try:
        adapter = harnesses.native_for(pool.provider)
        adapter.preflight()
    except ShipError as error:
        return Check(f"pool {pool.name}", False, str(error))
    file = credential.pool_file(pool)
    if file is None:
        return Check(f"pool {pool.name}", True, "the program holds its own sign-in")
    if not file.exists():
        return Check(f"pool {pool.name}", False, f"no credential at {file}; sign in to the program there")
    sign_in = claude_sign_in.observe(adapter, file)
    now = datetime.now(timezone.utc)
    if sign_in is not None and sign_in.access_expired(now) and not sign_in.can_renew(now):
        return Check(f"pool {pool.name}", False, f"login needed at {file.parent} (its access expired and cannot renew)")
    return Check(f"pool {pool.name}", True, f"signed in at {file.parent}{credential.grant_detail(pool)}")


def fix(ship: Ship, failed: list[Check], tmux: Tmux) -> list[Check]:
    """Open a throwaway seat on the first pool whose program runs, hand it the failures as an order, and wait for its report."""
    searched = program_path()
    candidates, refused = [], []
    for pool in ship.pools.values():
        for name in harnesses.NAMES:
            reader = shipmod.program_adapter(ship, harnesses.get(name))
            if keyfile.accepts(reader, pool) is not None:
                continue
            try:
                if getattr(reader, 'program_pin', None) is not None:
                    reader.selected_program()
                elif shutil.which(reader.program, path=searched) is None:
                    continue
            except shipmod.ProgramPinError as error:
                refused.append(str(error))
                continue
            candidates.append((pool, reader))
    available = next(((pool, reader) for pool, reader in candidates if keyfile.usable(pool)), None)
    if available is None and candidates:
        # Only pools whose key cannot be read could carry its seat: that is the cause, not a missing program.
        unusable = candidates[0][0]
        return [Check("seat opened", False, f"doctor: its pool {unusable.name}: {keyfile.problem(unusable)}; nothing was started")]
    if available is None:
        if refused:
            return [Check("seat opened", False, ' · '.join(dict.fromkeys(refused)))]
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
