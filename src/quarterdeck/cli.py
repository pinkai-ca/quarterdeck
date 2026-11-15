"""qd: one verb per act, every act followed by a proof line, every reading available as JSON.

Written for an agent at the keyboard as much as for a person: a proof line says what was
done and how it was confirmed, and every refusal names the reason and what would lift it.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import (__version__, check, copy_state, daily_live, doctor, drill, harness as harnesses, install, ledger, post, sandbox, seat as seatmod, service,
               ship as shipmod, standing, watch)
from .output import Reading
from . import output, ledger_reading, lifecycle
from .ship import ShipError
from .harness.capabilities import selection_label
from .tmux import CUTOVER, Tmux, TmuxError, ship_socket as shipsocket

RESTART_CONTROLS = {"pool": "restart", "harness": "fresh", "cwd": "restart", "persona": "restart"}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "run"):
        parser.print_help()
        return 0
    argv = list(argv if argv is not None else sys.argv[1:])
    argv = choose_default(args, argv)
    if (args.run not in (cmd_upgrade, cmd_service, cmd_setup, cmd_init, cmd_raise)
            and not (args.run is cmd_controls and getattr(args, "harness", None))):
        handoff(args, argv)
    try:
        result = args.run(args)
    except sqlite3.DatabaseError as error:
        from .ledger_recovery import malformed
        if not malformed(error):
            raise
        print('qd: ledger malformed; stop the service, then qd ledger --recover', file=sys.stderr)
        return 2
    except (ShipError, TmuxError) as error:
        note = f" (default ship: {args.default_ship})" if args.default_ship else ""
        print(f"qd: {error}{note}", file=sys.stderr)
        return 2
    emit(args, result)
    return getattr(args, "exit_code", 0)


HANDED = "QD_DEFAULT_SHIP"  # the folder the typed qd chose as the default, passed to the ship's own copy


def needs_ship(args: argparse.Namespace) -> bool:
    """Verbs that act on no ship, or on the practice ship when none is named, never take the default."""
    if args.run is cmd_upgrade and getattr(args, 'all_ships', False):
        return False
    return (args.run not in (cmd_laws, cmd_drill, cmd_default, cmd_setup, cmd_init, cmd_raise)
            and not (args.run is cmd_controls and getattr(args, 'harness', None)))


def choose_default(args: argparse.Namespace, argv: list[str]) -> list[str]:
    """With no --ship and no QD_SHIP, a person's shell acts on the default ship, named from then on.

    The choice is made once, here, and passed on as --ship, so the ship's own copy acts on the same folder; the
    copy learns it was the default from HANDED, which nothing launched afterwards inherits."""
    handed = os.environ.pop(HANDED, None)
    args.default_ship = None
    if handed and args.ship:
        if Path(args.ship).expanduser().resolve() == Path(handed).resolve():
            args.default_ship = Path(handed).name
        return argv
    if args.ship or os.environ.get("QD_SHIP") or os.environ.get("QD_SEAT") or not needs_ship(args):
        return argv
    try:
        folder, name = shipmod.choose(None)
    except ShipError:
        return argv  # the verb refuses, saying why
    args.ship, args.default_ship = str(folder), name
    return ["--ship", str(folder), *argv]


def handoff(args: argparse.Namespace, argv: list[str]) -> None:
    """A ship with an installed copy of the framework is run by that copy, whichever qd was typed."""
    if not (args.ship or os.environ.get("QD_SHIP")):
        return
    try:
        folder = shipmod.find(args.ship)
    except ShipError:
        return  # the verb says what is wrong with the ship folder
    if args.default_ship:
        os.environ[HANDED] = str(folder)
    try:
        install.handoff(folder, argv)
    finally:
        os.environ.pop(HANDED, None)  # reached only when this qd runs the verb itself


def emit(args: argparse.Namespace, result: Any) -> None:
    if getattr(args, "default_ship", None) and result is not None:
        result = output.with_default_ship(result, args.default_ship)
    output.emit(result, json_output=args.json)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qd", description=__doc__.split("\n\n")[0])
    parser.add_argument("--ship", help="the ship folder (default: $QD_SHIP, else the default ship: qd default)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--version", action="version", version=f"qd {__version__}")
    verbs = parser.add_subparsers(title="verbs", metavar="<verb>")

    def verb(name: str, run, help: str) -> argparse.ArgumentParser:
        p = verbs.add_parser(name, help=help, description=help)
        p.set_defaults(run=run)
        return p

    from . import setup
    p = verb("init", cmd_init, "create the project's office and bring up its desk alone")
    from . import raise_ship
    p = verb("raise", cmd_raise, "ask the person, declare a template ship and bring up its crew")
    raise_ship.arguments(p)
    p = verb("setup", cmd_setup, "guide setup with installed programs; ask before live checks spend turns")
    setup.arguments(p)

    p = verb("up", cmd_up, "bring up the ship (its service and every seat marked up, resuming) or one seat")
    p.add_argument("seat", nargs="?")
    p.add_argument("--fresh", action="store_true", help="without resume: a new conversation")
    p.add_argument("--pool", help="for this one launch of a down seat: one of its declared pool choices")
    p = verb("down", cmd_down, "end one seat, a check's seat left behind (check-<program>-<run>), or every seat of the ship and its service")
    p.add_argument("seat", nargs="?")
    p = verb("restart", cmd_restart, "relaunch a seat with resume: the conversation continues")
    p.add_argument("seat", nargs="?", help="default: this seat (QD_SEAT)")
    p.add_argument('--cancel', nargs='?', const='*', metavar='REQUEST', help='cancel this request id, or all pending relaunches of the seat')
    p = verb("fresh", cmd_fresh, "relaunch a seat without resume: a new conversation, briefed, with its open messages")
    p.add_argument("seat", nargs="?", help="default: this seat (QD_SEAT)")
    p.add_argument('--cancel', nargs='?', const='*', metavar='REQUEST', help='cancel this request id, or all pending relaunches of the seat')
    p = verb('fresh-ready', cmd_fresh_ready, 'attest a natural break with an open handoff, or cancel prepared readiness')
    p.add_argument('seat', nargs='?', help='default: this seat; outside-seat cancellation is supported')
    p.add_argument('--request')
    p.add_argument('--handoff')
    p.add_argument('--at-break', action='store_true', help='attest no review/signature/landing is half done; finish this turn without new work')
    p.add_argument('--cancel', action='store_true')
    p = verb("stop", cmd_stop, "interrupt a seat's turn, nothing more")
    p.add_argument("seat", nargs="?", help="default: this seat (QD_SEAT)")
    p = verb("submit", cmd_submit, "press Enter on a seat's box when it holds exactly one of its pending messages")
    p.add_argument("seat", help="the seat whose box holds the message")
    p = verb("press", cmd_press, "systems only: press a named choice of the dialog on a seat's screen, recorded first")
    p.add_argument("seat", help="the seat whose screen shows the dialog")
    p.add_argument("choice", help="the choice as the screen shows it, such as '1. Yes' or '[0] Skip'")
    p = verb("type", cmd_type, "systems only: type one line into a stalled seat's empty box and submit it, recorded first")
    p.add_argument("seat", help="the stalled seat")
    p.add_argument("line", help="the one line to type")
    p = verb("set", cmd_set, "change a seat: writes the ship file, then applies what it can")
    p.add_argument("seat", nargs="?", help="default: this seat (QD_SEAT)")
    p.add_argument("changes", nargs="*", metavar="KEY=VALUE", help="model=…, effort=…, pool=a,b, up=true")
    p.add_argument("--adopt", action="store_true", help="write what the seat is running into the ship file")
    p = verb("controls", cmd_controls, "list a seat's controls with the form each has")
    p.add_argument("seat", nargs="?", help="default: this seat (QD_SEAT)")
    p.add_argument("--harness", choices=harnesses.NAMES, help="read a program's capabilities without a ship")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print the result as JSON")
    p = verb("cascade", cmd_cascade, "start the declared ring once for a question to its orchestrator")
    p.add_argument("--re", required=True, help="the question's id or short id")
    p.add_argument("--efforts", help="fewer declared effort laps, comma separated, in the order named")
    verb("laws", cmd_laws, "the laws, each with the lines of code and test that carry it")
    p = verb("default", cmd_default, "the default ship a plain qd acts on: print it and how it was chosen, or mark another")
    p.add_argument("name", nargs="?", help="the ship's folder name under ships/")
    p = verb("brief", cmd_brief, "print exactly what a seat reads at boot")
    p.add_argument("seat", nargs="?", help="default: this seat (QD_SEAT)")
    p.add_argument("--sources", action="store_true", help="say which file each part came from")
    p = verb("attach", cmd_attach, "open the ship in this terminal: a session on the ship's own tmux server")
    p.add_argument("session", nargs="?", help="a session's name or title (default: the person's own session)")
    p = verb("status", cmd_status, "every seat on one screen, then the open orders")
    p.add_argument("--line", action="store_true", help="published operational alerts; stale or unavailable readings are marked")
    p = verb("watch", cmd_watch, "read a seat: activity from its record, the door from its screen")
    p.add_argument("seat", nargs="?")
    p.add_argument("--save-screen", action="store_true", help="save the screen for the adapter's next fixture")
    p = verb("post", cmd_post, "send a message: order, report, question, answer or ack; or resend one")
    p.add_argument("kind", choices=(*post.KINDS, "resend"))
    p.add_argument("body", nargs="*", help="the text; '-' reads it from stdin; with resend, the message id")
    p.add_argument("--to", help="the addressee seat, or declared group for an order (default: referenced sender)")
    p.add_argument("--re", help="the message this answers, by id or short id")
    p.add_argument("--from", dest="sender", help="the sender, from a shell that is no seat (default: the operator); a seat always posts as itself")
    p.add_argument("--subject")
    p.add_argument("--due")
    p.add_argument("--body-file", help="read the body from a file")
    speed = p.add_mutually_exclusive_group()
    speed.add_argument("--now", action="store_const", const="now", dest="speed", help="deliver mid-turn, read at the next step")
    speed.add_argument("--interrupt", action="store_const", const="interrupt", dest="speed", help="interrupt the turn, then deliver")
    p = verb("thread", cmd_thread, "print a thread in order, as far as this seat sent or received it")
    p.add_argument("message", help="a full or short message id")
    p = verb("inbox", cmd_inbox, "print a seat's inbox, or wait for what arrives next")
    p.add_argument("seat", nargs="?", help="default: this seat")
    inbox_mode = p.add_mutually_exclusive_group()
    inbox_mode.add_argument("--wait", action="store_true", help="block until something new arrives, print it in full, exit")
    inbox_mode.add_argument("--all", action="store_true", help="print the whole inbox history from the store to stdout")
    p = verb("ledger", cmd_ledger, "what the crew spends: the pools, or turns rolled up --by seat, order or pool")
    p.add_argument("--by", choices=("seat", "order", "pool"), help="roll the turns up by this")
    p.add_argument("--tools", action="store_true", help="tool calls by name: count, seconds, failures")
    recovery = p.add_mutually_exclusive_group()
    recovery.add_argument('--recover', action='store_true', help='check the ledger and preserve/rebuild it if malformed; service must be stopped')
    recovery.add_argument('--clear-recovery', action='store_true', help='acknowledge ledger recovery alarms; keep all evidence')
    verb("service", cmd_service, "run the ship's service in the foreground (qd up starts it detached)")
    p = verb("drill", cmd_drill, "the all-hands drill: on the drydock's simulator, or on --ship with real programs")
    p.add_argument("--keep", action="store_true", help="keep the temporary ship and its tmux server for a look")
    p = verb("check", cmd_check, "prove the installed version of a program live, on a throwaway seat (spends tokens)")
    p.add_argument("harness", choices=[n for n in harnesses.NAMES if not harnesses.get(n).synthetic])
    p.add_argument("--model", metavar="ID", help="launch the throwaway seat on this model; require observed confirmation")
    p.add_argument("--minimal", action="store_true",
                   help="minimal live check: one short task and report (spends tokens)")
    p = verb("daily-live", cmd_daily_live,
             "check programs the ship did not use today, on throwaway ships, and report once to the desk")
    p.add_argument("--units", action="store_true",
                   help="print the user timer and service; install nothing")
    p = verb("doctor", cmd_doctor, "is this machine ready to run the ship: tmux, git, programs, sign-ins, folders, the service, disk")
    p.add_argument("--fix", action="store_true",
                   help="hand the failures to a throwaway seat on the first pool whose program runs, then check again (spends tokens)")
    for name in ("sync", "upgrade"):
        help = "install the framework at a local commit as the ship's own copy and run the service on it; fetches nothing"
        if name == "upgrade":
            help = "alias for qd sync: " + help
        p = verb(name, cmd_upgrade, help)
        p.set_defaults(upgrade_alias=name == "upgrade")
        p.add_argument("ref", nargs="?", help="the local commit to install, forward or back (default: the checkout's HEAD)")
        p.add_argument('--back', action='store_true', help='return to the recorded previous copy, without rebuilding')
        p.add_argument('--force', action='store_true', help='install and prove even when the commit is unchanged')
        p.add_argument('--all', dest='all_ships', action='store_true',
                       help='sync project ships one at a time, default last, stopping on failure')
    return parser


# -- helpers -----------------------------------------------------------------------------


def load_ship(args: argparse.Namespace) -> shipmod.Ship:
    return shipmod.load(shipmod.find(args.ship))


def seat_of(ship: shipmod.Ship, name: str, *, launching: bool = False) -> seatmod.Seat:
    return seatmod.Seat(ship, ship.seat(name), Tmux.for_ship(ship), launching=launching)


def this_seat(ship: shipmod.Ship, name: str | None, operator_too: bool = False) -> str:
    """A seat by name, or this seat from the environment (QD_SEAT, set at every launch).

    Where the operator has an inbox too, the operator's name is accepted, and is the
    default for whoever runs ``qd`` outside a seat.
    """
    chosen = name or os.environ.get("QD_SEAT")
    if operator_too and (not chosen or chosen.lower() == ship.operator.lower()):
        return ship.operator.lower()
    if not chosen:
        raise ShipError("no seat named and QD_SEAT is not set; pass the seat")
    return ship.seat(chosen).name


def parse_value(raw: str) -> object:
    """``true``, ``42``, ``a,b`` (a list) or a string: the forms the ship file takes."""
    if raw in ("true", "false"):
        return raw == "true"
    if raw.lstrip("-").isdigit():
        return int(raw)
    if "," in raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    return raw


# -- seats -------------------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> list[str]:
    from . import setup
    try:
        return setup.first_step(args)
    except OSError as error:
        raise ShipError(f"setup could not access its requested files: {error}; correct the path or permissions and rerun qd setup") from None


def cmd_init(args: argparse.Namespace) -> list[str]:
    from . import setup
    try:
        return setup.init_office(args)
    except OSError as error:
        raise ShipError(f"office initialization could not access its files: {error}; correct the path or permissions and rerun qd init") from None


def cmd_raise(args: argparse.Namespace) -> list[str]:
    from . import raise_ship
    try:
        return raise_ship.run(args)
    except (OSError, ValueError) as error:
        raise ShipError(f"raise could not read or save its proposal: {error}; retain the office records and resolve the files before retrying") from None


def cmd_cascade(args: argparse.Namespace) -> str:
    ship = load_ship(args)
    if ship.cascade is None:
        raise ShipError("no [cascade] declared in ship.toml")
    caller = this_seat(ship, None, operator_too=True)
    if caller not in (ship.cascade.orchestrator, ship.operator.lower()):
        raise ShipError("only the orchestrator or operator starts the cascade")
    question = post.Store(ship.runtime).get(args.re)
    efforts = args.efforts.split(",") if args.efforts is not None else None
    return service.request(ship.runtime, "cascade", question=question.id, caller=caller, efforts=efforts)


@lifecycle.explicit()
def cmd_up(args: argparse.Namespace) -> list[str] | str:
    ship = load_ship(args)
    if args.seat:
        ship.seat(args.seat)
    elif getattr(args, 'pool', None):
        raise ShipError('--pool names one launch: give the seat, as in qd up <seat> --pool <choice>')
    proofs, reload_required = install.prepare_up(ship)
    delegated = install.delegate_up(ship, seat=args.seat, fresh=args.fresh, reload_required=reload_required,
                                    pool=getattr(args, 'pool', None))
    if delegated is not None:
        return proofs + delegated
    if args.seat:
        proof = seat_of(ship, args.seat, launching=True).up(resume=not args.fresh, pool=getattr(args, 'pool', None))
        return proofs + [proof, service.start(ship)]
    tmux = Tmux.for_ship(ship)
    ups = seatmod.bring_up(ship, tmux)
    from . import server_recovery
    if cleared := server_recovery.clear_by_person(ship.runtime):  # a person brought the whole ship up
        ups.append(cleared)
    return proofs + ups + [service.start(ship)] + ([where(tmux)] if tmux.ship else [])


def where(tmux: Tmux) -> str:
    """The server the ship runs on, as the last line of a whole ship's up."""
    if tmux.legacy:
        return f"tmux: this ship still runs on tmux's default server {tmux.socket}; {CUTOVER}"
    return f"tmux: the ship's own server {tmux.socket} · qd attach opens it"


def cmd_down(args: argparse.Namespace) -> list[str] | str:
    ship = load_ship(args)
    if args.seat:
        if args.seat not in ship.seats and seatmod.CHECK_SEAT.fullmatch(args.seat):
            return seatmod.down_leftover_check(ship, Tmux.for_ship(ship), args.seat)  # a check's seat it left behind
        return seat_of(ship, args.seat).down()
    tmux = Tmux.for_ship(ship)
    proofs = seatmod.bring_down(ship, tmux) + [service.stop(ship.runtime)]
    if tmux.legacy:
        proofs.append(f"tmux: brought down on tmux's default server {tmux.socket}, which is left running; "
                      f"qd up now starts this ship on its own server {shipsocket(ship)}")
    return proofs


def cmd_attach(args: argparse.Namespace) -> None:
    """Replace this process with a tmux client on the ship's server, in the session asked for."""
    ship = load_ship(args)
    tmux = Tmux.for_ship(ship)
    if args.session:
        chosen = ship.sessions.get(args.session) or next(
            (s for s in ship.sessions.values() if s.title == args.session), None)
        if chosen is None:
            raise ShipError(f"no session {args.session!r}; this ship has " +
                            ", ".join(f"{key} ({s.title})" for key, s in ship.sessions.items()))
    else:
        chosen = next((s for s in ship.sessions.values() if s.human), None) or next(iter(ship.sessions.values()))
    if not tmux.has_session(chosen.title):
        raise ShipError(f"session {chosen.title!r} is not running on {tmux.socket}; qd up brings the ship up")
    argv = tmux.attach_argv(chosen.title)
    env = dict(os.environ)
    inside = env.get("TMUX", "").split(",")[0]
    if inside and tmux.socket and os.path.realpath(inside) == os.path.realpath(tmux.socket):
        argv = [*argv[:argv.index("attach-session")], "switch-client", "-t", argv[-1]]
    else:
        env.pop("TMUX", None)  # a terminal inside another tmux opens this one as a client of its own
        env.pop("TMUX_PANE", None)
    sys.stdout.flush()
    os.execvpe(argv[0], argv, env)


def cmd_restart(args: argparse.Namespace) -> str:
    return relaunch(load_ship(args), args.seat, resume=True, cancel=getattr(args, 'cancel', None))


def cmd_fresh(args: argparse.Namespace) -> str:
    return relaunch(load_ship(args), args.seat, resume=False, cancel=getattr(args, 'cancel', None))


@lifecycle.explicit()
def cmd_fresh_ready(args: argparse.Namespace) -> str:
    from . import compaction
    ship = load_ship(args)
    seat = seat_of(ship, this_seat(ship, args.seat))
    if args.cancel:
        with seat._lifecycle():
            return compaction.disarm(seat, 'readiness cancelled; handoffs remain open')
    if os.environ.get('QD_SEAT') != seat.name or not args.at_break or not args.request or not args.handoff:
        raise ShipError('fresh-ready requires the seat itself, --request, --handoff and explicit --at-break')
    if not service.alive(ship.runtime):
        raise ShipError('service down; readiness not confirmed')
    act = compaction.prepare_ready(seat, args.request, args.handoff)
    return service.request(ship.runtime, 'fresh-ready', seat=seat.name, **act)


@lifecycle.explicit()
def relaunch(ship: shipmod.Ship, name: str | None, resume: bool, cancel=None) -> str:
    """Retain known-idle manual recovery; working seats use the service's queued proof."""
    from . import request_queue
    target = this_seat(ship, name)
    if cancel is not None:
        return request_queue.cancel(ship.runtime, target, None if cancel == '*' else cancel)
    seat = seat_of(ship, target)
    if seat.is_working():
        return service.request(ship.runtime, 'relaunch', seat=seat.name, resume=resume,
                               caller=os.environ.get('QD_SEAT'), origin=request_queue.launch_binding(seat))
    kind = 'restart' if resume else 'fresh'
    with request_queue.direct(ship.runtime, seat.name, kind) as retired:
        suffix = f'; retired pending relaunch request(s): {", ".join(retired)}' if retired else ''
        working = seat.is_working()
    if not working:
        try:
            proof = seat.restart() if resume else seat.fresh()
        except ShipError as error:
            if retired:
                raise ShipError(str(error) + suffix) from error
            raise
        return proof + suffix
    # The existing working guard is rechecked after writer coordination, so
    # waiting for that lock never turns a direct act into an unexamined restart.
    return service.request(ship.runtime, 'relaunch', seat=seat.name, resume=resume,
                           caller=os.environ.get('QD_SEAT'), origin=request_queue.launch_binding(seat)) + suffix


def cmd_stop(args: argparse.Namespace) -> str:
    ship = load_ship(args)
    seat = seat_of(ship, this_seat(ship, args.seat))
    if seat.session.human:
        raise ShipError(f"{seat.name} sits in a human's session; the ship sends no controls into a human's session")
    return service.request(ship.runtime, "stop", seat=seat.name)


def cmd_submit(args: argparse.Namespace) -> str:
    ship = load_ship(args)
    seat = seat_of(ship, args.seat)
    if seat.session.human:
        raise ShipError(f"{seat.name} sits in a human's session; nothing is pressed in a human's session")
    return service.request(ship.runtime, "submit", seat=seat.name)


def _act(args: argparse.Namespace, kind: str, **data) -> str:
    """qd press and qd type: refused here first when the caller may not act, then decided by the service."""
    from . import acts
    ship = load_ship(args)
    seat = seat_of(ship, args.seat)
    caller = os.environ.get("QD_SEAT") or None
    if reason := acts.authority(ship, caller, seat):
        raise ShipError(reason)
    return service.request(ship.runtime, kind, seat=seat.name, caller=caller, **data)


def cmd_press(args: argparse.Namespace) -> str:
    return _act(args, "press", choice=args.choice)


def cmd_type(args: argparse.Namespace) -> str:
    return _act(args, "type", line=args.line)


def cmd_set(args: argparse.Namespace) -> list[str]:
    return set_seat(load_ship(args), args.seat, list(args.changes), adopt_running=args.adopt)


def set_seat(ship: shipmod.Ship, name: str | None, changes: list[str], *, adopt_running: bool = False) -> list[str]:
    """The qd set selection/write/application path, also used by setup's existing-seat edits."""
    if name and "=" in name:  # qd set model=x from inside a seat: the seat is this one
        changes.insert(0, name)
        name = None
    seat = seat_of(ship, this_seat(ship, name))
    if adopt_running:
        return adopt(ship, seat)
    if not changes:
        raise ShipError("nothing to set: give KEY=VALUE pairs, as in model=MODEL, or --adopt")
    if seat.spec.pool_choices:
        for change in changes:
            key = change.partition("=")[0].removeprefix("settings.")
            if change.startswith("settings.") or key in {"harness", "model", "effort", "model_mode", "effort_mode"} or key not in shipmod.SEAT_KEYS:
                raise ShipError("inline pool choices: edit harness, model, effort and program settings in ship.toml; qd set cannot select their scope")
        if any(change.partition("=")[0] == "pool" for change in changes):
            return [select(ship, seat, changes)]
    parsed = []
    for change in changes:
        key, equals, raw = change.partition("=")
        if not equals:
            raise ShipError(f"{change!r} is not KEY=VALUE")
        key = key.removeprefix("settings.")
        table = f"seats.{seat.name}" if key in shipmod.SEAT_KEYS else f"seats.{seat.name}.settings"  # the program's own keys
        parsed.append((table, key, raw))
    # One edit: a seat's program and its pool are only valid together, so all are validated and written at once.
    from . import compaction
    edits = [(table, key, parse_value(raw)) for table, key, raw in parsed]
    if any(key in {'model_mode', 'effort_mode'} for _, key, _ in edits):
        candidate = ship.file.read_text()
        for table, key, value in edits:
            candidate = shipmod.edit(candidate, table, key, value)
        shipmod._validate(shipmod._from_text(candidate, ship.folder))  # before even a runtime fence/write
    threshold = next((value for _, key, value in edits if key == 'fresh_after_compactions'), None)
    if threshold is not None or seat.spec.fresh_after_compactions or compaction.path(ship.runtime, seat.name).exists():
        shipmod.fence(ship)
        with post.admission(ship.runtime, seat.name):
            written = shipmod.set_values(ship, edits)
            compaction.disarm_locked(seat, 'declaration changed; renew readiness',
                                     disabled=(threshold == 0) if threshold is not None else None,
                                     clear_transition=False)
    else:
        written = shipmod.set_values(ship, edits)
    switching = any(key == "harness" for _, key, _ in parsed)
    proofs = []
    for proof, (_, key, raw) in zip(written, parsed):
        proofs.append(proof)
        if key == "pool" and switching:
            proofs.append(f"  pool written; qd fresh {seat.name} applies it with the harness")
        else:
            proofs.append(apply_control(ship, seat, key, raw))
    return proofs


def select(ship: shipmod.Ship, seat: seatmod.Seat, changes: list[str]) -> str:
    """A seat with inline choices moves to one of them by the path failover uses; its declaration is unchanged.

    The selection is the seat's runtime state, so nothing is written to the ship file; the service checks
    the destination and the seat's idleness exactly as failover does, and refuses rather than waits.
    """
    if len(changes) != 1:
        raise ShipError(f"{seat.name}: select a pool alone, as in qd set {seat.name} pool=<choice>")
    pool = changes[0].partition("=")[2]
    if pool not in seat.spec.pools:
        raise ShipError(f"{seat.name}: {pool!r} is not one of its declared choices ({', '.join(seat.spec.pools)})")
    if not seat.is_up:
        return choose_for_launch(seat, pool)
    return service.request(ship.runtime, "select", seat=seat.name, pool=pool)


@lifecycle.explicit()
def choose_for_launch(seat: seatmod.Seat, pool: str) -> str:
    """A down seat has no turn to wait for: the choice is written, and its next qd up launches there."""
    with seat._lifecycle():
        if seat.is_up:
            raise ShipError(f"{seat.name} came up meanwhile; ask again, and the service moves it when idle")
        if seat.launched() is not None:
            raise ShipError(f"{seat.name}: its last launch is still on file, its pane gone; qd down {seat.name} "
                            "retires it, then qd set chooses the pool for the next launch")
        selected = seat.spec.for_pool(pool)
        seat._accept_pool(selected, seat.ship.pool(pool))
        seat.move_to(pool, explicit=True)
    return f"{seat.name}: pool {pool} chosen for its next launch (it is down); qd up {seat.name} launches it there"


def adopt(ship: shipmod.Ship, seat: seatmod.Seat) -> list[str]:
    """Write what the seat is running, model and effort as its record and screen report them, into the ship file."""
    if seat.spec.pool_choices:
        raise ShipError("inline pool choices: --adopt is unavailable; edit each choice's model and effort in ship.toml")
    running = watch.running_settings(seat)
    if not running:
        raise ShipError(f"{seat.name}: nothing to adopt; its record and screen show no model or effort yet")
    edits = []
    for key, value in running.items():
        edits.append((f"seats.{seat.name}", key, value))
        if getattr(seat.spec, f'{key}_mode') is not None:
            edits.append((f"seats.{seat.name}", f'{key}_mode', 'named'))
    return shipmod.set_values(ship, edits)


def apply_control(ship: shipmod.Ship, seat: seatmod.Seat, key: str, value: str) -> str:
    """Hand a written change to the service, or say which relaunch applies it."""
    if key in {'model_mode', 'effort_mode'}:
        return f"  {key} written; qd restart {seat.name} applies it"
    if key == 'fresh_after_compactions':
        return '  compaction threshold applies on the service tick; 0 disables it and leaves all posts open'
    control = seat.harness.control(key)
    if key in RESTART_CONTROLS:
        return f"  {key} written; qd {RESTART_CONTROLS[key]} {seat.name} applies it"
    if key not in shipmod.SEAT_KEYS:
        return f"  {key} written under [seats.{seat.name}.settings]; qd restart {seat.name} passes it to the program"
    if control is None or control.session is None or key == "up":
        return f"  {key} written; qd restart {seat.name} applies it" if key != "up" else "  up written"
    if seat.session.human:
        return f"  {key} written; the ship sends no controls into a human's session: qd restart {seat.name} applies it, or type {control.command(value)} there"
    if not seat.is_up:
        return f"  {key} written; {seat.name} is down, so qd up {seat.name} applies it"
    return "  " + service.request(ship.runtime, "control", seat=seat.name, name=key, value=value)


def cmd_controls(args: argparse.Namespace) -> Reading:
    if getattr(args, 'harness', None):
        if args.seat:
            raise ShipError('controls: choose a seat or --harness, not both')
        reader = harnesses.get(args.harness)
        return Reading({'harness': reader.name, 'controls': reader.controls(),
                        'effort': reader.effort_capability(), 'defaults': reader.default_metadata()},
                       render_harness_controls)
    ship = load_ship(args)
    seat = seat_of(ship, this_seat(ship, args.seat))
    return Reading({"seat": seat.name, "controls": seat.harness.controls(), "relaunch": RESTART_CONTROLS}, render_controls)


def render_harness_controls(data: dict) -> list[str]:
    facts, defaults = data['effort'], data['defaults']
    levels = ', '.join(facts.levels) if facts.kind == 'levels' else facts.kind
    rows = [f"{data['harness']}: effort {levels}",
            'installed version/support unknown; no program was called',
            f"measured versions: {', '.join(facts.measured_versions) or 'none'}"]
    for key in ('model', 'effort'):
        choice = getattr(defaults, key)
        if choice is not None:
            rows.append(f"suggested {key}: {selection_label(choice.value, choice.mode)} (account not checked)")
    rows.append(f"program model default: {'supported' if defaults.model_default else 'not supported'}")
    rows.extend(render_controls({'controls': data['controls'], 'relaunch': {}}))
    return rows


def render_controls(data: dict) -> list[str]:
    rows = [f"{'control':<10} {'at launch':<22} {'in session':<18} confirmed by"]
    for c in data["controls"]:
        rows.append(f"{c.name:<10} {c.launch or '-':<22} {c.session or '-':<18} {c.confirmed_by}")
    for key, how in data["relaunch"].items():
        rows.append(f"{key:<10} {'the ship file':<22} {'-':<18} qd {how}")
    return rows


def cmd_laws(args: argparse.Namespace) -> Reading:
    return Reading(standing.law_reading(), standing.render_laws)


def cmd_default(args: argparse.Namespace) -> Reading:
    ships = shipmod.ships_folder()
    if args.name:
        name = args.name
        given = Path(name).expanduser()
        if len(given.parts) > 1 and given.resolve().parent == ships.resolve():
            name = given.name  # ships/<name> or its full path names the same folder
        folder = shipmod.mark_default(name)
        data = dict(ship=folder.name, folder=folder, how="mark", marked=True, reason=None)
    else:
        try:
            folder, how = shipmod.default_ship()
            data = dict(ship=folder.name, folder=folder, how=how, marked=False, reason=None)
        except ShipError as error:
            args.exit_code = 1
            data = dict(ship=None, folder=None, how=None, marked=False, reason=str(error))
    data["overridden_by"] = ("QD_SEAT" if os.environ.get("QD_SEAT") else
                             "QD_SHIP" if os.environ.get("QD_SHIP") else None)
    return Reading(data, render_default)


def render_default(data: dict) -> list[str]:
    if data["ship"] is None:
        lines = [f"no default ship · {data['reason']}"]
    else:
        how = {"mark": "marked in ships/.default", "only ship": "the only ship under ships/, with no mark"}[data["how"]]
        lines = [f"default ship: {data['ship']} · {'now ' if data['marked'] else ''}{how} · {data['folder']}"]
    if data["overridden_by"] == "QD_SEAT":
        lines.append("this shell is a seat: it acts on its own QD_SHIP and never on the default ship")
    elif data["overridden_by"] == "QD_SHIP":
        lines.append("QD_SHIP is set in this shell and wins over the default ship; unset it to use the default")
    return lines


def cmd_brief(args: argparse.Namespace) -> Reading:
    ship = load_ship(args)
    brief = standing.brief(ship, ship.seat(this_seat(ship, args.seat)))
    data = {"text": brief.text, "sources": [{"name": name, "path": path} for name, path in brief.sources],
            "words": brief.words, "target_words": standing.BRIEF_WORDS}
    if args.sources:
        return Reading(data, render_sources)
    return Reading(data, lambda d: d["text"].rstrip("\n"))


def render_sources(data: dict) -> list[str]:
    return [f"{s['name']:<14} {s['path'] or '(built in)'}" for s in data["sources"]] + [
        f"{'words':<14} {data['words']} (target under {data['target_words']})"]


def cmd_status(args: argparse.Namespace) -> Reading:
    ship = load_ship(args)
    if args.line:
        warning = copy_state.select(ship.folder, mode=ship.code, mode_known=True).warning
        from .presentation import collected_line
        publication_age = None
        if not getattr(args, 'json', False):
            from .presentation import published_reading
            line, publication_age = published_reading(ship.folder, status_refresh=ship.status_refresh)
            if line is not None:
                return Reading(dict(line=line, copy_warning=warning),
                               lambda d: d['line'] + (' · ' + warning if warning else ''))
        try:
            facts = watch.facts(ship, Tmux.for_ship(ship))
            if warning:
                return Reading({**output.value(facts), 'copy_warning': warning},
                               lambda _: collected_line(facts.line(), publication_age) + ' · ' + warning)
            return Reading(facts, lambda f: collected_line(f.line(), publication_age))
        except sqlite3.DatabaseError as error:
            from .ledger_recovery import malformed
            if not malformed(error):
                raise
            state = watch.service_state(ship.runtime)
            lead = 'SERVICE DOWN' if state == 'down' else f'service {state}'
            reason = 'ledger malformed; stop the service, then qd ledger --recover'
            return Reading(dict(service=state, facts=None, error=reason),
                           lambda _: collected_line(lead + ' · ' + reason, publication_age))
        except (OSError, ValueError, TypeError, KeyError):
            state = watch.service_state(ship.runtime)
            lead = 'SERVICE DOWN' if state == 'down' else f'service {state}'
            return Reading(dict(service=state, facts=None, error='status evidence unavailable', copy_warning=warning),
                           lambda _: collected_line(lead + ' · status unknown: evidence unavailable; inspect qd status',
                                                   publication_age)
                           + (' · ' + warning if warning else ''))
    return Reading(watch.status_reading(ship, Tmux.for_ship(ship)), watch.render_table)


def cmd_watch(args: argparse.Namespace) -> Reading | str:
    ship = load_ship(args)
    if not args.seat:
        return Reading(watch.status_reading(ship, Tmux.for_ship(ship)), watch.render_table)
    seat = seatmod.read_seat(ship, ship.seat(args.seat), Tmux.for_ship(ship))
    if args.save_screen:
        if isinstance(seat, seatmod.UnresolvedSeat):
            raise seat.error
        return f"screen saved to {watch.save_screen(seat)}"
    return Reading(watch.seat_reading(seat), watch.render_full)


# -- post --------------------------------------------------------------------------------


def cmd_post(args: argparse.Namespace) -> str:
    ship = load_ship(args)
    shipmod.fence(ship)  # the first file written into a runtime finds the fence already there
    store = post.Store(ship.runtime)
    if args.to in ship.groups and args.kind != 'order':
        raise ShipError(f'group {args.to}: {args.kind} is unsupported; groups accept orders only for now')
    if args.kind == "resend":
        if len(args.body) != 1:
            raise ShipError("resend takes one message id")
        return service.request(ship.runtime, "resend", message=store.get(args.body[0]).id)
    body = read_body(args, preserve=args.to in ship.groups)
    seat_here = os.environ.get("QD_SEAT")
    if seat_here and args.sender and args.sender != seat_here:
        raise ShipError(f"this shell is seat {seat_here}, which posts as itself; --from names a sender only from a shell that is no seat")
    sender = seat_here or args.sender or ship.operator.lower()  # a seat's identity is the one its launch set, never a flag
    to = args.to or (store.get(args.re).sender if args.re else None)
    if not to:
        raise ShipError(f"a {args.kind} needs --to <seat>" + (" or --re <message>" if args.kind != "order" else ""))
    if '@' in to:
        from . import fleet_post
        recipient, destination = fleet_post.validate_target(ship, to)
        if destination == ship.folder.name:
            to = recipient
        else:
            message = store.prepare(args.kind, sender, to, body, re=args.re, subject=args.subject,
                                    due=args.due, speed=args.speed)
            post.write_drop(ship.runtime, message, seat=sender)  # law: can-post
            note = ('claims; an ack wakes nobody' if args.kind == 'ack'
                    else 'waiting for the addressee ship to fetch it')
            return f'{message.short} to {to} · filed by the service · {note}'
    if to.lower() == ship.operator.lower():
        to = ship.operator.lower()
    if to in ship.groups:
        if args.kind != 'order':
            raise ShipError(f'group {to}: {args.kind} is unsupported; groups accept orders only for now')
        if not body.strip():
            raise ShipError('an empty group order: give the brief, --body-file, or - for stdin')
        from . import group_post
        root, count = group_post.stage(ship, store, to, sender, body, re=args.re,
                                        subject=args.subject, due=args.due, speed=args.speed)
        return (f'{root.short} to group {to} · filed by the service · '
                f'{count} body-identical copies · common id {root.id} · '
                f'{delivery_note(ship, root.to)}')
    if to not in ship.seats and to not in getattr(ship, 'forward', {}) and to not in (ship.operator.lower(), post.SHIP):
        raise ShipError(f"no seat {to!r} to address; declared: {', '.join(ship.seats)}, "
                        f"the operator {ship.operator.lower()}, or {post.SHIP} for the ship's own notices")
    message = store.prepare(args.kind, sender, to, body, re=args.re, subject=args.subject, due=args.due, speed=args.speed)
    post.write_drop(ship.runtime, message)  # law: can-post
    if message.kind == "ack":
        return f"{message.short} to {to} · filed by the service · claims {post.short_of(message.re) if message.re else 'nothing'} for the ledger; an ack wakes nobody"
    return f"{message.short} to {to} · filed by the service · {delivery_note(ship, to)}"


def read_body(args: argparse.Namespace, *, preserve: bool = False) -> str:
    if preserve:
        try:
            if args.body_file:
                with open(args.body_file, encoding='utf-8', newline='') as stream:
                    return stream.read()
            if args.body == ['-']:
                raw = getattr(sys.stdin, 'buffer', None)
                return raw.read().decode('utf-8') if raw is not None else sys.stdin.read()
        except UnicodeError:
            raise ShipError('group brief must be valid UTF-8 text') from None
    if args.body_file:
        with open(args.body_file) as stream:
            body = stream.read()
        return body.strip()
    if args.body == ["-"]:
        return sys.stdin.read().strip()
    body = " ".join(args.body)
    body = body if preserve else body.strip()
    if not body and args.kind == "ack":
        return "claimed"  # an ack needs no words: it names the order it claims
    if not body:
        raise ShipError("an empty body: give the text, --body-file, or '-' for stdin")
    return body


def _post_view_restricted(ship: shipmod.Ship) -> bool:
    """The caller's own saved launch establishes its managed view, not the new declaration."""
    caller = os.environ.get("QD_SEAT")
    if not caller:
        return False
    try:
        launched = seat_of(ship, caller).launched()
    except (ShipError, OSError, ValueError, TypeError, AttributeError):
        return True
    return launched is None or sandbox.seats_hidden(launched.argv) is not False


def delivery_note(ship: shipmod.Ship, to: str) -> str:
    if to in getattr(ship, 'forward', {}):
        return f'forwarded to {ship.forward[to]}; waiting for the addressee ship to fetch it'
    if to not in ship.seats:
        return "addressed to the operator; read it with qd inbox"
    if _post_view_restricted(ship):  # law: record-first
        if ship.session_of(ship.seat(to)).human:
            return "human session; read it with qd inbox; watch state is not visible here"
        return "the service handles delivery; recipient state is not visible here"
    try:
        seat = seat_of(ship, to)
        if seat.session.human:
            armed = post.watch_armed(ship.runtime, to)
            return "watch armed, prints it" if armed else f"no watch armed on {to}; waits in the inbox"
        up = seat.is_up
    except TmuxError:
        return "the service handles delivery; tmux is out of reach from here"
    except (ShipError, OSError, ValueError, TypeError, AttributeError):
        return "the service handles delivery; recipient state is unknown here"
    if not up:
        return f"{to} is down; carried as its first message at qd up"
    if not service.alive(ship.runtime):
        return "service down; the drop waits, and qd up files it, then it is typed"
    return f"the service types it into {to}'s pane"


def _inbox_target(ship: shipmod.Ship, name: str | None):
    """Only inbox may admit a live check's otherwise undeclared temporary seat."""
    chosen = name or os.environ.get('QD_SEAT')
    if not chosen or chosen.lower() == ship.operator.lower():
        return ship.operator.lower(), True, None
    if chosen in ship.seats:
        return chosen, ship.session_of(ship.seat(chosen)).human, None
    from .pending import check_inbox_owner
    owner = check_inbox_owner(ship.runtime, chosen)

    def validate():
        if check_inbox_owner(ship.runtime, chosen) != owner:
            raise ShipError(f'{chosen}: temporary check inbox ownership changed')

    return chosen, owner['human'], validate


def cmd_thread(args: argparse.Namespace) -> Reading:
    from . import post_threads

    ship = load_ship(args)
    reader = this_seat(ship, None, operator_too=True)
    thread = post_threads.whole(post.Store(ship.runtime), args.message, reader)
    return Reading(thread, post_threads.render)


def cmd_inbox(args: argparse.Namespace) -> Reading | None:
    ship = load_ship(args)
    seat, human, validate = _inbox_target(ship, args.seat)
    if not args.wait:
        data = post.inbox_reading(post.Store(ship.runtime), seat, recent=not args.all)
        if validate is not None:
            validate()
        return Reading(data,
                       lambda data: post.render_inbox_reading(data).rstrip("\n"))
    if not human:
        raise ShipError(f"{seat} is reached through its door; --wait is for seats in a human's session")
    shipmod.fence(ship)
    arrived = (post.wait_batch(ship.runtime, seat, validate=validate) if validate is not None
               else post.wait_batch(ship.runtime, seat))
    if arrived is None:
        return None
    messages, opened = arrived
    if validate is not None:
        validate()
    # A message this watch's seat was given before (a print judged blind, a resend) says it is a second delivery.
    now = {attempt.path for attempt in opened}
    every = post.Attempts(ship.runtime)
    second = {m.id for m in messages if any(a.seat == seat and a.path not in now for a in every.for_message(m.id))}
    batch = Reading({"seat": seat, "messages": messages},
                    lambda data: post.typed(data["messages"], second=second).rstrip("\n"))
    emit(args, batch)  # serialization, output and flush must all succeed before the receipt
    if validate is not None:
        validate()
    post.printed(ship.runtime, opened)
    return None


def cmd_service(args: argparse.Namespace) -> None:
    ship = load_ship(args)
    # Recovery/exec precedes construction (and therefore the ledger and service lock).
    install.bootstrap_service(ship, ['--ship', str(ship.folder), 'service'])
    service.Service(ship, Tmux.for_ship(ship)).run()


def cmd_ledger(args: argparse.Namespace) -> Reading:
    ship = load_ship(args)
    from . import ledger_recovery
    if getattr(args, 'clear_recovery', False):
        cleared = ledger_recovery.acknowledge(ship.runtime)
        return Reading(dict(acknowledged=cleared), lambda d: 'ledger recovery acknowledged; evidence kept: ' + (', '.join(d['acknowledged']) or 'none pending'))
    if getattr(args, 'recover', False):
        if service.alive(ship.runtime):
            raise ShipError('stop the service before qd ledger --recover')
        with ledger.Ledger(ship.runtime / 'ledger.sqlite'):
            pass
        saved = [str(folder.relative_to(ship.runtime)) for folder in ledger_recovery.pending(ship.runtime)]
        return Reading(dict(evidence=saved), lambda d: 'ledger checked; recovery evidence: ' + (', '.join(d['evidence']) or 'none'))
    book = ledger.Ledger.reading(ship.runtime / "ledger.sqlite")  # healthy readers never write or scan the book
    if book is None:
        data = ledger_reading.missing(by=args.by, tools=args.tools, ship=ship)
    else:
        with book:
            data = ledger_reading.collect(book, ship, by=args.by, tools=args.tools)
    return Reading(data, ledger_reading.render)


def cmd_daily_live(args: argparse.Namespace) -> list[str]:
    ship = load_ship(args)
    if args.units:
        return daily_live.units(daily_live.installed_qd(ship), ship.folder).splitlines()
    message, failed = daily_live.run(ship)
    args.exit_code = 1 if failed else 0
    return [f"{message.short} to {message.to} · stored", message.body.rstrip("\n")]


def cmd_check(args: argparse.Namespace) -> list[str]:
    ship = load_ship(args)
    options = {"model": args.model} if getattr(args, "model", None) is not None else {}
    if getattr(args, 'minimal', False):
        options['minimal'] = True
    checks = check.check(ship, args.harness, Tmux.for_ship(ship), **options)
    args.exit_code = 0 if all(c.passed is not False for c in checks) else 1
    return [str(c) for c in checks]


def cmd_doctor(args: argparse.Namespace) -> Reading | list[str]:
    try:
        ship = load_ship(args)
    except ShipError as error:
        args.exit_code = 1
        return Reading({"checks": [drill.Check("ship file", False, str(error))]},
                       lambda d: [str(c) for c in d["checks"]])
    found = doctor.checks(ship)
    lines = [str(c) for c in found]
    failed = [c for c in found if not c.passed]
    if failed and args.fix:
        lines += ["", "the failures handed to a seat:"] + [str(c) for c in doctor.fix(ship, failed, Tmux.for_ship(ship))]
        found = doctor.checks(ship)
        lines += ["", "checked again:"] + [str(c) for c in found]
    args.exit_code = 0 if all(c.passed for c in found) else 1
    from .providers import identities
    return lines if args.fix else Reading({"checks": found, **identities(ship)}, lambda d: [str(c) for c in d["checks"]])


def cmd_upgrade(args: argparse.Namespace) -> list[str] | Reading:
    back, force = getattr(args, 'back', False), getattr(args, 'force', False)
    if getattr(args, 'all_ships', False):
        if back:
            raise ShipError('--all cannot be combined with --back; go back one ship at a time')
        if args.ship is not None:
            raise ShipError('--all selects project ships; omit --ship')
        from . import sync_all

        data = sync_all.run(standing.PROJECT_DIR, args.ref or 'HEAD', force=force)
        args.exit_code = 0 if data['ok'] else 2

        def render(value):
            lines = sync_all.render(value)
            if getattr(args, 'upgrade_alias', False):
                lines[-1] += ' · `upgrade` is now `qd sync`'
            return lines

        return Reading(data, render)
    if back and (args.ref is not None or force):
        raise ShipError('--back cannot be combined with a target or --force')
    ship = load_ship(args)
    proofs = install.upgrade(ship, args.ref or 'HEAD', Tmux.for_ship(ship), back=back, force=force)
    if getattr(args, "upgrade_alias", False):
        proofs[-1] += " · `upgrade` is now `qd sync`"
    return proofs


def cmd_drill(args: argparse.Namespace) -> list[str]:
    ship = shipmod.load(shipmod.find(args.ship)) if args.ship or os.environ.get("QD_SHIP") else None
    passed, lines = drill.run(ship, keep=args.keep)
    args.exit_code = 0 if passed else 1
    return lines
