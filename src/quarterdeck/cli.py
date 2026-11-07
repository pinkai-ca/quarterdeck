"""The ``qd`` command: one verb per act, every reading available as ``--json``.

Written for an agent at the keyboard as much as for a person: every act is followed by a
proof line saying what was done and how it was confirmed, and every refusal names the
reason and what would lift it.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__, seat as seatmod, ship as shipmod, standing
from .ship import ShipError
from .tmux import Tmux, TmuxError


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
    return 0


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

    p = verb("up", cmd_up, "bring up the ship (every seat marked up, resuming) or one seat")
    p.add_argument("seat", nargs="?")
    p.add_argument("--fresh", action="store_true", help="without resume: a new conversation")
    p = verb("down", cmd_down, "end one seat, or every seat of the ship")
    p.add_argument("seat", nargs="?")
    p = verb("restart", cmd_restart, "relaunch a seat with resume: the conversation continues")
    p.add_argument("seat")
    p = verb("fresh", cmd_fresh, "relaunch a seat without resume: a new conversation, briefed, with its open messages")
    p.add_argument("seat")
    p = verb("set", cmd_set, "change a seat: writes the ship file, then applies what it can")
    p.add_argument("seat")
    p.add_argument("changes", nargs="*", metavar="KEY=VALUE", help="model=…, effort=…, pool=a,b, up=true")
    p = verb("brief", cmd_brief, "print exactly what a seat reads at boot")
    p.add_argument("seat")
    p.add_argument("--sources", action="store_true", help="say which file each part came from")
    p = verb("status", cmd_status, "every seat on one screen")
    return parser


# -- verbs -------------------------------------------------------------------------------


def load_ship(args: argparse.Namespace) -> shipmod.Ship:
    return shipmod.load(shipmod.find(args.ship))


def seat_of(args: argparse.Namespace, ship: shipmod.Ship, name: str) -> seatmod.Seat:
    return seatmod.Seat(ship, ship.seat(name), Tmux.default())


def cmd_up(args: argparse.Namespace) -> list[str] | str:
    ship = load_ship(args)
    if args.seat:
        return seat_of(args, ship, args.seat).up(resume=not args.fresh)
    return seatmod.bring_up(ship, Tmux.default())


def cmd_down(args: argparse.Namespace) -> list[str] | str:
    ship = load_ship(args)
    if args.seat:
        return seat_of(args, ship, args.seat).down()
    return seatmod.bring_down(ship, Tmux.default())


def cmd_restart(args: argparse.Namespace) -> str:
    ship = load_ship(args)
    return seat_of(args, ship, args.seat).restart()


def cmd_fresh(args: argparse.Namespace) -> str:
    ship = load_ship(args)
    return seat_of(args, ship, args.seat).fresh()


def cmd_set(args: argparse.Namespace) -> list[str]:
    ship = load_ship(args)
    seat = ship.seat(args.seat)
    if not args.changes:
        raise ShipError("nothing to set: give KEY=VALUE pairs, as in model=claude-opus-5-5")
    proofs = []
    for change in args.changes:
        key, equals, raw = change.partition("=")
        if not equals:
            raise ShipError(f"{change!r} is not KEY=VALUE")
        proofs.append(shipmod.set_value(ship, f"seats.{seat.name}", key, parse_value(raw)))
    return proofs


def cmd_brief(args: argparse.Namespace) -> str:
    ship = load_ship(args)
    brief = standing.brief(ship, ship.seat(args.seat))
    if args.sources:
        lines = [f"{name:<14} {path or '(built in)'}" for name, path in brief.sources]
        return "\n".join(lines + [f"{'words':<14} {brief.words} (target under {standing.BRIEF_WORDS})"])
    return brief.text.rstrip("\n")


def cmd_status(args: argparse.Namespace) -> list[str]:
    ship = load_ship(args)
    tmux = Tmux.default()
    rows = [f"{'seat':<10} {'pane':<6} {'harness':<10} {'pool':<18} {'model':<22} record"]
    for seat in seatmod.seats(ship, tmux):
        pane = seat.pane()
        where = pane.id if pane and not pane.dead else ("exited" if pane else "down")
        join = seat.join() if pane else None
        record = (join.record.name if join and join.record else (join.reason if join else "-"))
        rows.append(f"{seat.name:<10} {where:<6} {seat.harness.name:<10} {seat.pool.name:<18} {seat.spec.model or '-':<22} {record}")
    return rows


def parse_value(raw: str) -> object:
    """``true``, ``42``, ``a,b`` (a list) or a string: the forms the ship file takes."""
    if raw in ("true", "false"):
        return raw == "true"
    if raw.lstrip("-").isdigit():
        return int(raw)
    if "," in raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    return raw
