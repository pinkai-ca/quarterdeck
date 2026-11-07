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

from . import __version__, ship as shipmod
from .ship import ShipError


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "run"):
        parser.print_help()
        return 0
    try:
        result = args.run(args)
    except ShipError as error:
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

    p = verbs.add_parser("set", help="change a seat: writes the ship file, then applies what it can")
    p.add_argument("seat")
    p.add_argument("changes", nargs="*", metavar="KEY=VALUE", help="model=…, effort=…, pool=a,b, up=true")
    p.set_defaults(run=cmd_set)
    return parser


# -- verbs -------------------------------------------------------------------------------


def load_ship(args: argparse.Namespace) -> shipmod.Ship:
    return shipmod.load(shipmod.find(args.ship))


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


def parse_value(raw: str) -> object:
    """``true``, ``42``, ``a,b`` (a list) or a string: the forms the ship file takes."""
    if raw in ("true", "false"):
        return raw == "true"
    if raw.lstrip("-").isdigit():
        return int(raw)
    if "," in raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    return raw
