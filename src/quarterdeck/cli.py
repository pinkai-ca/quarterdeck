"""qd: one verb per act, every act followed by a proof line, every reading available as JSON.

Written for an agent at the keyboard as much as for a person: a proof line says what was
done and how it was confirmed, and every refusal names the reason and what would lift it.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from . import (__version__, check, doctor, drill, harness as harnesses, install, ledger, post, sandbox, seat as seatmod, service,
               ship as shipmod, standing, watch)
from .output import Reading
from . import output, ledger_reading
from .ship import ShipError
from .tmux import Tmux, TmuxError

RESTART_CONTROLS = {"pool": "restart", "harness": "fresh", "cwd": "restart", "persona": "restart"}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "run"):
        parser.print_help()
        return 0
    if args.run is not cmd_upgrade:
        handoff(args, argv if argv is not None else sys.argv[1:])
    try:
        result = args.run(args)
    except (ShipError, TmuxError) as error:
        print(f"qd: {error}", file=sys.stderr)
        return 2
    emit(args, result)
    return getattr(args, "exit_code", 0)


def handoff(args: argparse.Namespace, argv: list[str]) -> None:
    """A ship with an installed copy of the framework is run by that copy, whichever qd was typed."""
    if not (args.ship or os.environ.get("QD_SHIP")):
        return
    try:
        folder = shipmod.find(args.ship)
    except ShipError:
        return  # the verb says what is wrong with the ship folder
    install.handoff(folder, argv)


def emit(args: argparse.Namespace, result: Any) -> None:
    output.emit(result, json_output=args.json)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qd", description=__doc__.split("\n\n")[0])
    parser.add_argument("--ship", help="the ship folder (default: $QD_SHIP)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--version", action="version", version=f"qd {__version__}")
    verbs = parser.add_subparsers(title="verbs", metavar="<verb>")

    def verb(name: str, run, help: str) -> argparse.ArgumentParser:
        p = verbs.add_parser(name, help=help, description=help)
        p.set_defaults(run=run)
        return p

    p = verb("up", cmd_up, "bring up the ship (its service and every seat marked up, resuming) or one seat")
    p.add_argument("seat", nargs="?")
    p.add_argument("--fresh", action="store_true", help="without resume: a new conversation")
    p = verb("down", cmd_down, "end one seat, or every seat of the ship and its service")
    p.add_argument("seat", nargs="?")
    p = verb("restart", cmd_restart, "relaunch a seat with resume: the conversation continues")
    p.add_argument("seat", nargs="?", help="default: this seat (QD_SEAT)")
    p = verb("fresh", cmd_fresh, "relaunch a seat without resume: a new conversation, briefed, with its open messages")
    p.add_argument("seat", nargs="?", help="default: this seat (QD_SEAT)")
    p = verb('fresh-ready', cmd_fresh_ready, 'attest a natural break with an open handoff, or cancel prepared readiness')
    p.add_argument('seat', nargs='?', help='default: this seat; outside-seat cancellation is supported')
    p.add_argument('--request')
    p.add_argument('--handoff')
    p.add_argument('--at-break', action='store_true', help='attest no review/signature/landing is half done; finish this turn without new work')
    p.add_argument('--cancel', action='store_true')
    p = verb("stop", cmd_stop, "interrupt a seat's turn, nothing more")
    p.add_argument("seat", nargs="?", help="default: this seat (QD_SEAT)")
    p = verb("set", cmd_set, "change a seat: writes the ship file, then applies what it can")
    p.add_argument("seat", nargs="?", help="default: this seat (QD_SEAT)")
    p.add_argument("changes", nargs="*", metavar="KEY=VALUE", help="model=…, effort=…, pool=a,b, up=true")
    p.add_argument("--adopt", action="store_true", help="write what the seat is running into the ship file")
    p = verb("controls", cmd_controls, "list a seat's controls with the form each has")
    p.add_argument("seat", nargs="?", help="default: this seat (QD_SEAT)")
    p = verb("cascade", cmd_cascade, "start the declared ring once for a question to its orchestrator")
    p.add_argument("--re", required=True, help="the question's id or short id")
    p.add_argument("--efforts", help="fewer declared effort laps, comma separated, in the order named")
    verb("laws", cmd_laws, "the laws, each with the lines of code and test that carry it")
    p = verb("brief", cmd_brief, "print exactly what a seat reads at boot")
    p.add_argument("seat", nargs="?", help="default: this seat (QD_SEAT)")
    p.add_argument("--sources", action="store_true", help="say which file each part came from")
    p = verb("status", cmd_status, "every seat on one screen, then the open orders")
    p.add_argument("--line", action="store_true", help="the one line every ship session's status bar shows")
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
    p = verb("inbox", cmd_inbox, "print a seat's inbox, or wait for what arrives next")
    p.add_argument("seat", nargs="?", help="default: this seat")
    p.add_argument("--wait", action="store_true", help="block until something new arrives, print it in full, exit")
    p = verb("ledger", cmd_ledger, "what the crew spends: the pools, or turns rolled up --by seat, order or pool")
    p.add_argument("--by", choices=("seat", "order", "pool"), help="roll the turns up by this")
    p.add_argument("--tools", action="store_true", help="tool calls by name: count, seconds, failures")
    verb("service", cmd_service, "run the ship's service in the foreground (qd up starts it detached)")
    p = verb("drill", cmd_drill, "the all-hands drill: on the drydock's simulator, or on --ship with real programs")
    p.add_argument("--keep", action="store_true", help="keep the temporary ship and its tmux server for a look")
    p = verb("check", cmd_check, "prove the installed version of a program live, on a throwaway seat (spends tokens)")
    p.add_argument("harness", choices=[n for n in harnesses.NAMES if not harnesses.get(n).synthetic])
    p = verb("doctor", cmd_doctor, "is this machine ready to run the ship: tmux, git, programs, sign-ins, folders, the service, disk")
    p.add_argument("--fix", action="store_true",
                   help="hand the failures to a throwaway seat on the first pool whose program runs, then check again (spends tokens)")
    p = verb("upgrade", cmd_upgrade, "install the framework at a commit as the ship's own copy and run the service on it")
    p.add_argument("ref", nargs="?", default="HEAD", help="the commit to install (default: the checkout's HEAD); the previous one rolls back")
    return parser


# -- helpers -----------------------------------------------------------------------------


def load_ship(args: argparse.Namespace) -> shipmod.Ship:
    return shipmod.load(shipmod.find(args.ship))


def seat_of(ship: shipmod.Ship, name: str) -> seatmod.Seat:
    return seatmod.Seat(ship, ship.seat(name), Tmux.default())


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


def cmd_up(args: argparse.Namespace) -> list[str] | str:
    ship = load_ship(args)
    if args.seat:
        proof = seat_of(ship, args.seat).up(resume=not args.fresh)
        return [proof, service.start(ship)]
    return seatmod.bring_up(ship, Tmux.default()) + [service.start(ship)]


def cmd_down(args: argparse.Namespace) -> list[str] | str:
    ship = load_ship(args)
    if args.seat:
        return seat_of(ship, args.seat).down()
    return seatmod.bring_down(ship, Tmux.default()) + [service.stop(ship.runtime)]


def cmd_restart(args: argparse.Namespace) -> str:
    return relaunch(load_ship(args), args.seat, resume=True)


def cmd_fresh(args: argparse.Namespace) -> str:
    return relaunch(load_ship(args), args.seat, resume=False)


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


def relaunch(ship: shipmod.Ship, name: str | None, resume: bool) -> str:
    """Relaunch now when the seat is idle; when it is working, the service does it at the turn's end."""
    seat = seat_of(ship, this_seat(ship, name))
    if seat.is_working():
        return service.request(ship.runtime, "relaunch", seat=seat.name, resume=resume)
    return seat.restart() if resume else seat.fresh()


def cmd_stop(args: argparse.Namespace) -> str:
    ship = load_ship(args)
    seat = seat_of(ship, this_seat(ship, args.seat))
    if seat.session.human:
        raise ShipError(f"{seat.name} sits in a human's session; the ship never presses its keys")
    return service.request(ship.runtime, "stop", seat=seat.name)


def cmd_set(args: argparse.Namespace) -> list[str]:
    ship = load_ship(args)
    changes = list(args.changes)
    if args.seat and "=" in args.seat:  # qd set model=x from inside a seat: the seat is this one
        changes.insert(0, args.seat)
        args.seat = None
    seat = seat_of(ship, this_seat(ship, args.seat))
    if args.adopt:
        return adopt(ship, seat)
    if not changes:
        raise ShipError("nothing to set: give KEY=VALUE pairs, as in model=MODEL, or --adopt")
    if seat.spec.pool_choices:
        for change in changes:
            key = change.partition("=")[0].removeprefix("settings.")
            if change.startswith("settings.") or key in {"harness", "model", "effort"} or key not in shipmod.SEAT_KEYS:
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
    return service.request(ship.runtime, "select", seat=seat.name, pool=pool)


def adopt(ship: shipmod.Ship, seat: seatmod.Seat) -> list[str]:
    """Write what the seat is running, model and effort as its record and screen report them, into the ship file."""
    if seat.spec.pool_choices:
        raise ShipError("inline pool choices: --adopt is unavailable; edit each choice's model and effort in ship.toml")
    running = watch.running_settings(seat)
    if not running:
        raise ShipError(f"{seat.name}: nothing to adopt; its record and screen show no model or effort yet")
    return [shipmod.set_value(ship, f"seats.{seat.name}", key, value) for key, value in running.items()]


def apply_control(ship: shipmod.Ship, seat: seatmod.Seat, key: str, value: str) -> str:
    """Hand a written change to the service, or say which relaunch applies it."""
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
        return f"  {key} written; the ship never types in a human's session: qd restart {seat.name} applies it, or type {control.command(value)} there"
    if not seat.is_up:
        return f"  {key} written; {seat.name} is down, so qd up {seat.name} applies it"
    return "  " + service.request(ship.runtime, "control", seat=seat.name, name=key, value=value)


def cmd_controls(args: argparse.Namespace) -> Reading:
    ship = load_ship(args)
    seat = seat_of(ship, this_seat(ship, args.seat))
    return Reading({"seat": seat.name, "controls": seat.harness.controls(), "relaunch": RESTART_CONTROLS}, render_controls)


def render_controls(data: dict) -> list[str]:
    rows = [f"{'control':<10} {'at launch':<22} {'in session':<18} confirmed by"]
    for c in data["controls"]:
        rows.append(f"{c.name:<10} {c.launch or '-':<22} {c.session or '-':<18} {c.confirmed_by}")
    for key, how in data["relaunch"].items():
        rows.append(f"{key:<10} {'the ship file':<22} {'-':<18} qd {how}")
    return rows


def cmd_laws(args: argparse.Namespace) -> Reading:
    return Reading(standing.law_reading(), standing.render_laws)


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
        try:
            return Reading(watch.facts(ship, Tmux.default()), watch.Facts.line)
        except (OSError, ValueError, TypeError, KeyError):
            state = watch.service_state(ship.runtime)
            lead = 'SERVICE DOWN' if state == 'down' else f'service {state}'
            return Reading(dict(service=state, facts=None, error='status evidence unavailable'),
                           lambda _: lead + ' · status unknown: evidence unavailable; inspect qd status')
    return Reading(watch.status_reading(ship, Tmux.default()), watch.render_table)


def cmd_watch(args: argparse.Namespace) -> Reading | str:
    ship = load_ship(args)
    if not args.seat:
        return Reading(watch.status_reading(ship, Tmux.default()), watch.render_table)
    seat = seatmod.read_seat(ship, ship.seat(args.seat), Tmux.default())
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
    if to.lower() == ship.operator.lower():
        to = ship.operator.lower()
    if to in ship.groups:
        if args.kind != 'order':
            raise ShipError(f'group {to}: {args.kind} is unsupported; groups accept orders only for now')
        if not body.strip():
            raise ShipError('an empty group order: give the brief, --body-file, or - for stdin')
        from . import group_post
        root, copies = group_post.submit(ship, store, to, sender, body, re=args.re,
                                         subject=args.subject, due=args.due, speed=args.speed)
        return (f'{root.short} to group {to} · request to {root.to} · '
                f'{len(copies)} body-identical copies stored · common id {root.id} · '
                f'{delivery_note(ship, root.to)}')
    if to not in ship.seats and to not in (ship.operator.lower(), post.SHIP):
        raise ShipError(f"no seat {to!r} to address; declared: {', '.join(ship.seats)}, "
                        f"the operator {ship.operator.lower()}, or {post.SHIP} for the ship's own notices")
    message = store.new(args.kind, sender, to, body, re=args.re, subject=args.subject, due=args.due, speed=args.speed)
    if message.kind == "ack":
        return f"{message.short} to {to} · stored · claims {post.short_of(message.re) if message.re else 'nothing'} for the ledger; an ack wakes nobody"
    return f"{message.short} to {to} · stored · {delivery_note(ship, to)}"


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
        return "service down; qd up restarts it, then it is typed"
    return f"the service types it into {to}'s pane"


def cmd_inbox(args: argparse.Namespace) -> Reading | None:
    ship = load_ship(args)
    seat = this_seat(ship, args.seat, operator_too=True)
    if not args.wait:
        return Reading(post.inbox_reading(post.Store(ship.runtime), seat),
                       lambda data: post.render_inbox_reading(data).rstrip("\n"))
    if seat in ship.seats and not ship.session_of(ship.seat(seat)).human:
        raise ShipError(f"{seat} is reached through its door; --wait is for seats in a human's session")
    shipmod.fence(ship)
    arrived = post.wait_batch(ship.runtime, seat)
    if arrived is None:
        return None
    messages, opened = arrived
    batch = Reading({"seat": seat, "messages": messages}, lambda data: post.typed(data["messages"]).rstrip("\n"))
    emit(args, batch)  # serialization, output and flush must all succeed before the receipt
    post.printed(ship.runtime, opened)
    return None


def cmd_service(args: argparse.Namespace) -> None:
    ship = load_ship(args)
    service.Service(ship, Tmux.default()).run()


def cmd_ledger(args: argparse.Namespace) -> Reading:
    ship = load_ship(args)
    book = ledger.Ledger.reading(ship.runtime / "ledger.sqlite")  # a reading never writes the book
    if book is None:
        data = ledger_reading.missing(by=args.by, tools=args.tools, ship=ship)
    else:
        with book:
            data = ledger_reading.collect(book, ship, by=args.by, tools=args.tools)
    return Reading(data, ledger_reading.render)


def cmd_check(args: argparse.Namespace) -> list[str]:
    ship = load_ship(args)
    checks = check.check(ship, args.harness, Tmux.default())
    args.exit_code = 0 if all(c.passed for c in checks) else 1
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
        lines += ["", "the failures handed to a seat:"] + [str(c) for c in doctor.fix(ship, failed, Tmux.default())]
        found = doctor.checks(ship)
        lines += ["", "checked again:"] + [str(c) for c in found]
    args.exit_code = 0 if all(c.passed for c in found) else 1
    from .providers import identities
    return lines if args.fix else Reading({"checks": found, **identities(ship)}, lambda d: [str(c) for c in d["checks"]])


def cmd_upgrade(args: argparse.Namespace) -> list[str]:
    return install.upgrade(load_ship(args), args.ref, Tmux.default())


def cmd_drill(args: argparse.Namespace) -> list[str]:
    ship = shipmod.load(shipmod.find(args.ship)) if args.ship or os.environ.get("QD_SHIP") else None
    passed, lines = drill.run(ship, keep=args.keep)
    args.exit_code = 0 if passed else 1
    return lines
