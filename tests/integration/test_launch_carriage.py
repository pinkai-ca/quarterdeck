"""A launch whose open messages are several times the kernel's per-argument limit still starts, every way.

Every start here is a real one on the drydock: a private tmux server, the seat's launch.sh,
``sh``, ``env`` and the exec of the fake program, which writes what it received to its own
record. Nothing here touches a ship of the crew, a native program or a provider.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from quarterdeck import post, seat as seatmod, ship as shipmod
from quarterdeck.tmux import Tmux

from .waiting import wait_for

LIMIT = 131_072  # the kernel's per-argument limit with its NUL, on 4 KiB pages
ACCOUNT_FILE = re.compile(r"are written in full in (\S+)$", re.MULTILINE)


@pytest.fixture
def ship(drydock: Path) -> shipmod.Ship:
    text = (drydock / "ship.toml").read_text() + '\n[seats.captain.settings]\nstep_seconds = "0.05"\n'
    (drydock / "ship.toml").write_text(text)
    return shipmod.load(drydock)


def body(tag: str, size: int) -> str:
    """A synthetic body of ``size`` bytes, every line naming where it sits, so order and wholeness show."""
    lines, total, index = [], 0, 0
    while total < size:
        item = f"{tag} line {index}: " + "synthetic words " * 6
        lines.append(item)
        total += len(item) + 1
        index += 1
    return "\n".join(lines)[:size]


def long_threads(store: post.Store, count: int = 4, size: int = 100_000) -> list[tuple[post.Message, post.Message]]:
    """``count`` open orders, each with an amendment in its thread: together several times the limit."""
    threads = []
    for index in range(count):
        order = store.new("order", "desk", "captain", body(f"order {index}", size), subject=f"Synthetic order {index}")
        amendment = store.new("order", "desk", "captain", body(f"amendment {index}", 2_000), re=order.id,
                              subject=f"Correction to order {index}")
        threads.append((order, amendment))
    return threads


def first_input(seat: seatmod.Seat, after: int = 0) -> str:
    """The input of the first turn the program recorded after ``after`` earlier turns."""
    record = wait_for(seat.record)
    wait_for(lambda: len(seat.harness.turns(record)) > after)
    return seat.harness.turns(record)[after].input


def received(seat: seatmod.Seat, opening: str) -> str:
    """Everything the seat was given: the first input, and the whole file it names when it names one."""
    found = ACCOUNT_FILE.search(opening)
    return opening if found is None else opening + "\n" + Path(found.group(1)).read_text(encoding="utf-8")


def words(seat: seatmod.Seat) -> list[str]:
    script = (seat.folder / "launch.sh").read_text(encoding="utf-8")
    return shlex.split(script.split("\n", 2)[2])


def start(way: str, seat: seatmod.Seat, ship: shipmod.Ship, tmux: Tmux, post_threads) -> tuple[str, int]:
    """Start the seat the named way, the long messages posted just before it; the earlier turns count."""
    if way == "up":
        post_threads()
        return seat.up(resume=False), 0
    seat.up(resume=False)
    wait_for(lambda: seat.harness.screen(tmux.capture(seat.pane().id)).box is not None)
    tmux.paste(seat.pane().id, "hello")
    tmux.keys(seat.pane().id, "Enter")
    record = wait_for(seat.record)
    wait_for(lambda: seat.harness.state(record).turns == 1)
    post_threads()
    if way == "restart":
        return seat.restart(), 1
    if way == "fresh":
        return seat.fresh(), 0
    from quarterdeck.service import Service
    seat.down()
    return Service(ship, tmux).relaunch(seat, False), 0  # the service's own start: down, so it starts fresh


@pytest.mark.parametrize("way", ["up", "restart", "fresh", "service"])
def test_a_carriage_several_times_the_argument_limit_starts_every_way(ship, tmux, way):
    store = post.Store(ship.runtime)
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    threads = []
    proof, earlier = start(way, captain, ship, tmux, lambda: threads.extend(long_threads(store)))
    assert proof and "8 open messages carried" in proof
    whole = sum(len(o.body.encode()) + len(a.body.encode()) for o, a in threads)
    assert whole > 3 * LIMIT, "the carriage is several times the per-argument limit"
    pane = captain.pane()
    assert pane is not None and not pane.dead
    opening = first_input(captain, earlier)
    assert opening.startswith("qd > briefed as captain, captain · 4 orders open")
    given = received(captain, opening)
    for order, amendment in threads:
        assert order.body in given and amendment.body in given, "every body arrives whole"  # law: context-is-cache
        assert given.index(order.body) < given.index(amendment.body), "an amendment follows its order"
    assert max(len(w.encode()) for w in words(captain)) + 1 <= LIMIT // 2
    assert not captain.pane().dead


@pytest.mark.parametrize("side", [0, 1])
def test_the_kernel_boundary_just_under_and_just_over_both_start(ship, tmux, side):
    """A carriage whose single argument would be 131,072 bytes with its NUL (allowed), or 131,073 (refused): both start."""
    store = post.Store(ship.runtime)
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    draft = store.prepare("order", "desk", "captain", "x" * 120_000, subject="Boundary")
    overhead = len((post.briefed_header("captain", "captain", [draft]) + "\n" + post.typed([draft])).encode()) - 120_000
    order = store.new("order", "desk", "captain", "x" * (LIMIT - 1 + side - overhead), subject="Boundary")
    first, _ = captain.first_message()
    assert len(first.encode()) + 1 == LIMIT + side, "the boundary is measured in bytes, the NUL included"
    proof = captain.up(resume=False)
    assert "1 open message carried" in proof
    given = received(captain, first_input(captain))
    assert order.body in given
    assert not captain.pane().dead


def test_a_short_carriage_is_typed_whole_as_before(ship, tmux):
    store = post.Store(ship.runtime)
    order = store.new("order", "desk", "captain", "A short synthetic order.\nNothing more.", subject="Short")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    assert captain.up(resume=False).endswith("1 open message carried")
    opening = first_input(captain)
    assert f"qd > order from desk · {order.short} · Short\nA short synthetic order.\nNothing more." in opening
    assert ACCOUNT_FILE.search(opening) is None
    assert not (captain.folder / "carried").exists(), "a short carriage writes no file"
