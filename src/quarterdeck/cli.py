"""The ``qd`` command: one verb per act, every reading available as ``--json``.

Written for an agent at the keyboard as much as for a person: every act is followed by a
proof line saying what was done and how it was confirmed, and every refusal names the
reason and what would lift it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import (__version__, check, drill, harness as harnesses, ledger, post, seat as seatmod, service,
               ship as shipmod, standing, watch)
from .ship import ShipError
from .tmux import Tmux, TmuxError

RESTART_CONTROLS = {"pool": "restart", "harness": "fresh", "cwd": "restart", "persona": "restart"}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "run"):
        parser.print_help()
        return 0
    try:
        result = args.run(args)
    except (ShipError, TmuxError) as error:
        print(f"qd: {error}", file=sys.stderr)
        return 2
    emit(args, result)
    return getattr(args, "exit_code", 0)


def emit(args: argparse.Namespace, result: Any) -> None:
    """A proof line, a table, or the same as JSON when asked."""
    if result is None:
        return
    if args.json:
        print(json.dumps(result if not isinstance(result, str) else {"proof": result}, indent=2, default=str))
    elif isinstance(result, str):
        print(result)
    else:
        for line in result if isinstance(result, list) else [result]:
            print(line)


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
    p = verb("stop", cmd_stop, "interrupt a seat's turn, nothing more")
    p.add_argument("seat", nargs="?", help="default: this seat (QD_SEAT)")
    p = verb("set", cmd_set, "change a seat: writes the ship file, then applies what it can")
    p.add_argument("seat", nargs="?", help="default: this seat (QD_SEAT)")
    p.add_argument("changes", nargs="*", metavar="KEY=VALUE", help="model=…, effort=…, pool=a,b, up=true")
    p.add_argument("--adopt", action="store_true", help="write what the seat is running into the ship file")
    p = verb("controls", cmd_controls, "list a seat's controls with the form each has")
    p.add_argument("seat", nargs="?", help="default: this seat (QD_SEAT)")
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
    p.add_argument("--to", help="the addressee seat (the referenced message's sender when --re is given)")
    p.add_argument("--re", help="the message this answers, by id or short id")
    p.add_argument("--from", dest="sender", help="the sender (default: this seat, else the operator)")
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
    p = verb("drill", cmd_drill, "the all-hands drill: on the example ship's fake harness, or on --ship with real programs")
    p.add_argument("--keep", action="store_true", help="keep the temporary ship and its tmux server for a look")
    p = verb("check", cmd_check, "prove the installed version of a program live, on a throwaway seat (spends tokens)")
    p.add_argument("harness", choices=[n for n in harnesses.NAMES if n != "fake"])
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


def relaunch(ship: shipmod.Ship, name: str | None, resume: bool) -> str:
    """Relaunch now when the seat is idle; when it is working, the service does it at the turn's end."""
    seat = seat_of(ship, this_seat(ship, name))
    record = seat.record() if seat.is_up else None
    if record and seat.harness.state(record).turn_open:
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
        raise ShipError("nothing to set: give KEY=VALUE pairs, as in model=claude-opus-5-5, or --adopt")
    proofs = []
    for change in changes:
        key, equals, raw = change.partition("=")
        if not equals:
            raise ShipError(f"{change!r} is not KEY=VALUE")
        proofs.append(shipmod.set_value(ship, f"seats.{seat.name}", key, parse_value(raw)))
        proofs.append(apply_control(ship, seat, key, raw))
    return proofs


def adopt(ship: shipmod.Ship, seat: seatmod.Seat) -> list[str]:
    """Write what the seat is running, model and effort as its record and screen report them, into the ship file."""
    running = watch.running_settings(seat)
    if not running:
        raise ShipError(f"{seat.name}: nothing to adopt; its record and screen show no model or effort yet")
    return [shipmod.set_value(ship, f"seats.{seat.name}", key, value) for key, value in running.items()]


def apply_control(ship: shipmod.Ship, seat: seatmod.Seat, key: str, value: str) -> str:
    """Hand a written change to the service, or say which relaunch applies it."""
    control = seat.harness.control(key)
    if key in RESTART_CONTROLS:
        return f"  {key} written; qd {RESTART_CONTROLS[key]} {seat.name} applies it"
    if control is None or control.session is None or key == "up":
        return f"  {key} written; qd restart {seat.name} applies it" if key != "up" else "  up written"
    if seat.session.human:
        return f"  {key} written; the ship never types in a human's session: qd restart {seat.name} applies it, or type {control.command(value)} there"
    if not seat.is_up:
        return f"  {key} written; {seat.name} is down, so qd up {seat.name} applies it"
    return "  " + service.request(ship.runtime, "control", seat=seat.name, name=key, value=value)


def cmd_controls(args: argparse.Namespace) -> list[str]:
    ship = load_ship(args)
    seat = seat_of(ship, this_seat(ship, args.seat))
    rows = [f"{'control':<10} {'at launch':<22} {'in session':<18} confirmed by"]
    for c in seat.harness.controls():
        rows.append(f"{c.name:<10} {c.launch or '-':<22} {c.session or '-':<18} {c.confirmed_by}")
    for key, how in RESTART_CONTROLS.items():
        rows.append(f"{key:<10} {'the ship file':<22} {'-':<18} qd {how}")
    return rows


def cmd_brief(args: argparse.Namespace) -> str:
    ship = load_ship(args)
    brief = standing.brief(ship, ship.seat(this_seat(ship, args.seat)))
    if args.sources:
        lines = [f"{name:<14} {path or '(built in)'}" for name, path in brief.sources]
        return "\n".join(lines + [f"{'words':<14} {brief.words} (target under {standing.BRIEF_WORDS})"])
    return brief.text.rstrip("\n")


def cmd_status(args: argparse.Namespace) -> list[str] | str:
    ship = load_ship(args)
    if args.line:
        return watch.line(ship, Tmux.default())
    return watch.table(ship, Tmux.default())


def cmd_watch(args: argparse.Namespace) -> list[str] | str:
    ship = load_ship(args)
    if not args.seat:
        return watch.table(ship, Tmux.default())
    seat = seat_of(ship, args.seat)
    if args.save_screen:
        return f"screen saved to {watch.save_screen(seat)}"
    return watch.full(seat)


# -- post --------------------------------------------------------------------------------


def cmd_post(args: argparse.Namespace) -> str:
    ship = load_ship(args)
    store = post.Store(ship.runtime)
    if args.kind == "resend":
        if len(args.body) != 1:
            raise ShipError("resend takes one message id")
        return service.request(ship.runtime, "resend", message=store.get(args.body[0]).id)
    body = read_body(args)
    sender = args.sender or os.environ.get("QD_SEAT") or ship.operator.lower()
    to = args.to or (store.get(args.re).sender if args.re else None)
    if not to:
        raise ShipError(f"a {args.kind} needs --to <seat>" + (" or --re <message>" if args.kind != "order" else ""))
    if to.lower() == ship.operator.lower():
        to = ship.operator.lower()
    if to not in ship.seats and to not in (ship.operator.lower(), post.SHIP):
        raise ShipError(f"no seat {to!r} to address; declared: {', '.join(ship.seats)}, "
                        f"the operator {ship.operator.lower()}, or {post.SHIP} for the ship's own notices")
    message = store.new(args.kind, sender, to, body, re=args.re, subject=args.subject, due=args.due, speed=args.speed)
    return f"{message.short} to {to} · stored · {delivery_note(ship, to)}"


def read_body(args: argparse.Namespace) -> str:
    if args.body_file:
        return open(args.body_file).read().strip()
    if args.body == ["-"]:
        return sys.stdin.read().strip()
    body = " ".join(args.body).strip()
    if not body:
        raise ShipError("an empty body: give the text, --body-file, or '-' for stdin")
    return body


def delivery_note(ship: shipmod.Ship, to: str) -> str:
    if to not in ship.seats:
        return "addressed to the operator; read it with qd inbox"
    seat = seat_of(ship, to)
    if seat.session.human:
        armed = post.watch_armed(ship.runtime, to)
        return "watch armed, prints it" if armed else f"no watch armed on {to}; waits in the inbox"
    if not seat.is_up:
        return f"{to} is down; carried as its first message at qd up"
    if not service.alive(ship.runtime):
        return "service down; qd up restarts it, then it is typed"
    return f"the service types it into {to}'s pane"


def cmd_inbox(args: argparse.Namespace) -> str | None:
    ship = load_ship(args)
    seat = this_seat(ship, args.seat, operator_too=True)
    if not args.wait:
        return post.inbox_text(post.Store(ship.runtime), seat).rstrip("\n")
    if seat in ship.seats and not ship.session_of(ship.seat(seat)).human:
        raise ShipError(f"{seat} is reached through its door; --wait is for seats in a human's session")
    arrived = post.wait(ship.runtime, seat)
    if arrived is None:
        return None
    text, opened = arrived
    print(text, end="", flush=True)  # printed first, then recorded as delivered
    post.printed(ship.runtime, opened)
    return None


def cmd_service(args: argparse.Namespace) -> None:
    ship = load_ship(args)
    service.Service(ship, Tmux.default()).run()


def cmd_ledger(args: argparse.Namespace) -> list[str]:
    ship = load_ship(args)
    book = ledger.Ledger(ship.runtime / "ledger.sqlite")
    try:
        if args.tools:
            rows = [f"{'tool':<24} {'calls':>6} {'seconds':>9} {'failed':>6}"]
            return rows + [f"{name:<24} {count:>6} {seconds:>9.1f} {failed:>6}" for name, count, seconds, failed in book.tools()]
        if args.by:
            return ledger.table(book, args.by)
        return ledger.pools_table(book)
    finally:
        book.close()


def cmd_check(args: argparse.Namespace) -> list[str]:
    ship = load_ship(args)
    checks = check.check(ship, args.harness, Tmux.default())
    args.exit_code = 0 if all(c.passed for c in checks) else 1
    return [str(c) for c in checks]


def cmd_drill(args: argparse.Namespace) -> list[str]:
    ship = shipmod.load(shipmod.find(args.ship)) if args.ship or os.environ.get("QD_SHIP") else None
    passed, lines = drill.run(ship, keep=args.keep)
    args.exit_code = 0 if passed else 1
    return lines
