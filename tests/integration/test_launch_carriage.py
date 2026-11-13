"""A launch whose open messages are several times the kernel's per-argument limit still starts, every way.

Every start here is a real one on the drydock: a private tmux server, the seat's launch.sh,
``sh``, ``env`` and the exec of the fake program, which writes what it received to its own
record. Nothing here touches a ship of the crew, a native program or a provider.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

from quarterdeck import post, seat as seatmod, ship as shipmod
from quarterdeck.tmux import Tmux

from .waiting import wait_for

LIMIT = 131_072  # the kernel's per-argument limit with its NUL, on 4 KiB pages
ACCOUNT_FILE = re.compile(r"^qd > read (\S+) whole before acting", re.MULTILINE)
OVERFLOW_FILE = re.compile(r"^qd > overflow index: (\S+);", re.MULTILINE)


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
    return "\n".join(lines)[:size - 1].rstrip() + "."  # the typed form strips trailing space; a body never ends in one


def long_threads(store: post.Store, count: int = 4, size: int = 100_000) -> list[tuple[post.Message, post.Message]]:
    """``count`` open orders, each with an amendment in its thread: together several times the limit."""
    threads = []
    for index in range(count):
        order = store.new("order", "desk", "captain", body(f"order {index}", size), subject=f"Synthetic order {index}")
        amendment = store.new("answer", "desk", "captain", body(f"amendment {index}", 2_000), re=order.id,
                              subject=f"Correction to order {index}")  # in the thread, not an open root itself
        threads.append((order, amendment))
    return threads


def first_input(seat: seatmod.Seat, after: int = 0) -> str:
    """The input of the first turn the program recorded after ``after`` earlier turns."""
    record = wait_for(seat.record)
    wait_for(lambda: len(seat.harness.turns(record)) > after)
    return seat.harness.turns(record)[after].input


def index_rows(opening: str) -> list[dict]:
    """Follow only the published bounded navigation, never recover a body from the store."""
    found = OVERFLOW_FILE.search(opening)
    if found is None:
        return []
    rows = []
    def visit(path):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith('id='):
                row, rest = {}, line
                while rest:
                    key, separator, value = rest.partition('=')
                    assert separator
                    decoded, end = json.JSONDecoder().raw_decode(value)
                    row[key] = decoded
                    rest = value[end:].lstrip()
                rows.append(row)
            else:
                visit(Path(json.loads(line)['path']))
    visit(Path(found.group(1)))
    return sorted(rows, key=lambda row: row['sequence'])


def read_view(path: Path) -> str:
    if path.name.startswith('view-'):
        return path.read_text(encoding="utf-8")
    parts = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return ''.join(read_view(Path(row['path'])) for row in parts)


def received(seat: seatmod.Seat, opening: str) -> str:
    """All reachable text: required whole view plus every indexed bounded read-view part."""
    found = ACCOUNT_FILE.search(opening)
    if found is None:
        return opening
    whole = Path(found.group(1)).read_text(encoding="utf-8")
    overflow = ''.join(read_view(Path(row['path'])) for row in index_rows(opening))
    return opening + "\n" + whole + overflow


def words(seat: seatmod.Seat) -> list[str]:
    script = (seat.folder / "launch.sh").read_text(encoding="utf-8")
    return shlex.split(script.split("\n", 2)[2])


def jail_words(seat: seatmod.Seat) -> list[str]:
    """The jail's own words, from bwrap to its "--": the launch's environment words before it name a fresh
    private TMUX_TMPDIR every launch."""
    found = words(seat)
    start = next(index for index, word in enumerate(found) if Path(word).name == "bwrap")
    return found[start:found.index("--")]


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


# -- the settled form, after the mend ----------------------------------------------------------


def carried_file(opening: str) -> Path:
    found = ACCOUNT_FILE.search(opening)
    assert found, "a long carriage names its file"
    return Path(found.group(1))


def test_a_long_carriage_is_an_account_and_a_whole_file_read_first(ship, tmux):
    from quarterdeck import carriage

    store = post.Store(ship.runtime)
    threads = long_threads(store)
    question = store.new("question", "desk", "captain", "A synthetic question?", subject="Which way?")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    _, incoming = captain.first_message()
    proof = captain.up(resume=False)
    assert "9 open messages carried in seats/captain/carried/" in proof
    opening = first_input(captain)
    assert carriage.utf8(opening) <= carriage.INLINE_BYTES
    lines = opening.splitlines()
    assert lines[0] == "qd > briefed as captain, captain · 4 orders open, 1 question open"
    counts = re.search(r'carriage: (\d+) whole; (\d+) overflow;', opening)
    assert counts and sum(map(int, counts.groups())) == len(incoming)
    rows = {row['id']: row for row in index_rows(opening)}
    for order, _ in threads:
        assert rows[order.id]['kind'] == 'order' and rows[order.id]['sender'] == 'desk'
        assert rows[order.id]['subject'] == order.subject and order.body not in opening
    path = carried_file(opening)
    text = path.read_text(encoding="utf-8")
    assert post.typed([question]) in text
    assert "included messages only" in lines[2] and lines[-1] == "qd > end of account"
    assert 'Overflow need not be read before acting on included work' in opening
    assert path.parent.parent == captain.folder / "carried" and path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert carriage.utf8(opening) + path.stat().st_size <= ship.carry_bytes
    assert all(file.stat().st_size <= ship.carry_bytes and file.stat().st_mode & 0o777 == 0o600
               for file in path.parent.iterdir())
    given = received(captain, opening)
    assert all(given.count(message.body) == 1 for message in incoming), 'every body reachable whole, exactly once'
    assert all(given.index(order.body) < given.index(amendment.body) for order, amendment in threads)
    latest = post.Attempts(ship.runtime).latest()
    assert {m.id for m in incoming} == {m for m, result in latest.items() if result == "launch"}, "exactly the incoming inventory"
    details = [a.detail for a in post.Attempts(ship.runtime).all() if a.result == "launch"]
    assert details and all(path.parent.name in detail and "not confirmed read" in detail for detail in details), "a file is no receipt"
    assert max(len(word.encode()) + 1 for word in words(captain)) <= carriage.WORD_BYTES


@pytest.mark.parametrize("letter,form", [("e", "whole"), ("é", "file")])
def test_the_line_between_whole_and_file_is_counted_in_utf8_bytes(ship, tmux, letter, form):
    store = post.Store(ship.runtime)
    store.new("order", "desk", "captain", letter * 17_000, subject="Bytes")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    first, _ = captain.first_message()
    assert len(first) < 32_768, "fewer characters than the line either way"
    captain.up(resume=False)
    opening = first_input(captain)
    assert (ACCOUNT_FILE.search(opening) is not None) == (form == "file")


def test_a_refused_message_is_only_its_notice_in_the_account_and_the_file(ship, tmux):
    from quarterdeck import refusal

    store = post.Store(ship.runtime)
    long_threads(store)
    refused = store.new("order", "desk", "captain", "SYNTHETIC REFUSED BODY", subject="SYNTHETIC REFUSED SUBJECT")
    amended = store.new("order", "desk", "captain", "SYNTHETIC REFUSED AMENDMENT", re=refused.id)
    refusal.save(ship.runtime, "captain", [{"stage": "held", "launch": {}, "turn": None,
                                            "quarantined": [{"message": refused.id}, {"message": amended.id}],
                                            "notices": []}])
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    captain.up(resume=False)
    opening = first_input(captain)
    text = carried_file(opening).read_text(encoding="utf-8")
    launch = (captain.folder / "launch.sh").read_text(encoding="utf-8")
    for shown in (opening, text, launch, *(path.read_text(encoding="utf-8") for path in carried_file(opening).parent.iterdir())):
        assert "SYNTHETIC REFUSED" not in shown, "neither its subject nor its body, anywhere"  # law: context-is-cache
    assert f"qd > open order from desk · {refused.id} · {refusal.WITHHELD}" in opening
    assert refusal.WITHHELD in text, "the required whole view names the withheld notices"
    assert refusal.stub(refused) in text and refusal.stub(amended) in text


def test_an_unreadable_refusal_journal_still_holds_the_launch(ship, tmux):
    from quarterdeck import refusal
    from quarterdeck.ship import ShipError

    store = post.Store(ship.runtime)
    long_threads(store)
    path = refusal.path(ship.runtime, "captain")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a journal")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    with pytest.raises(ShipError, match="inspect it before a launch carries anything"):
        captain.up(resume=False)
    assert captain.pane() is None and not (captain.folder / "carried").exists()


def test_a_resumed_seat_is_never_credited_with_what_only_its_file_held(ship, tmux):
    from quarterdeck import thread_context

    store = post.Store(ship.runtime)
    threads = long_threads(store)
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    captain.up(resume=False)
    first_path = carried_file(first_input(captain))
    first_bytes = {path: path.read_bytes() for path in first_path.parent.iterdir()}
    record = wait_for(captain.record)
    wait_for(lambda: captain.harness.state(record).turns >= 1)
    consumed = thread_context.consumed_ids(store, "captain", "captain", captain.harness.inputs(record))
    assert consumed == frozenset(), "the record holds only the account; no body is credited as read"
    proof = captain.restart()
    assert " · resumed " in proof and "8 open messages carried in " in proof, "every amendment again, by bounded views"
    second_opening = first_input(captain, 1)
    second_path = carried_file(second_opening)
    assert second_path != first_path and all(path.read_bytes() == data for path, data in first_bytes.items()), "an earlier generation is never rewritten"
    again = received(captain, second_opening)
    assert all(amendment.body in again for _, amendment in threads)


def test_a_jailed_seat_reads_its_own_file_and_no_other_seats_with_no_mount_added(ship, tmux, tmp_path):
    import hashlib
    from dataclasses import replace
    from .test_sandbox import namespaces_allowed

    if not namespaces_allowed():
        pytest.skip("bubblewrap namespace unavailable")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = replace(ship.seat("captain"), cwd=workspace, sandbox=True)
    other = ship.runtime / "seats" / "desk" / "carried"
    other.mkdir(parents=True)
    (other / "synthetic.txt").write_text("another seat's carriage")
    store = post.Store(ship.runtime)
    store.new("order", "desk", "captain", "short", subject="Short")
    captain = seatmod.Seat(ship, spec, tmux)
    captain.up(resume=False)
    short_mounts = jail_words(captain)
    first_input(captain)
    captain.down()
    long_threads(store)
    captain.up(resume=False)
    long_mounts = jail_words(captain)
    assert long_mounts == short_mounts, "the jail is the same: no mount is added for the file"
    path = carried_file(first_input(captain, 0))
    command = (f"$ sha256sum {path} > {workspace}/seen.txt; cat {other}/synthetic.txt > {workspace}/other.txt 2>&1; "
               f"echo $? > {workspace}/other.rc")
    wait_for(lambda: captain.harness.screen(tmux.capture(captain.pane().id)).box is not None)
    tmux.paste(captain.pane().id, command)
    tmux.keys(captain.pane().id, "Enter")
    seen = wait_for(lambda: (workspace / "other.rc").exists() and (workspace / "seen.txt").read_text())
    assert seen.split()[0] == hashlib.sha256(path.read_bytes()).hexdigest(), "read whole inside the jail"
    assert (workspace / "other.rc").read_text().strip() != "0", "another seat's folder stays hidden"
    assert "another seat's carriage" not in (workspace / "other.txt").read_text()


PROGRAM = ("import sys, json, time, pathlib; "
           "pathlib.Path(sys.argv[1]).write_text(json.dumps([len(a.encode()) for a in sys.argv[2:]] + [sys.argv[-1]])); "
           "time.sleep(60)")


@pytest.mark.parametrize("shape", ["last", "flag", "merged", "brief-argument"])
def test_every_way_an_adapter_places_the_first_message_stays_bounded(ship, tmux, tmp_path, monkeypatch, shape):
    """Appended last (Claude Code, Codex, Grok, pi), after a flag (OpenCode, the fake), joined to the brief (Antigravity),
    beside a brief passed as its own argument (Codex, Grok): a real program records what exec gave it."""
    import sys
    from quarterdeck import carriage
    from quarterdeck.harness.base import Launch

    out = tmp_path / "argv.json"
    brief_text = "synthetic brief " * (2_500 if shape == "merged" else 4_200)

    def launch(seat, pool, home, brief, resume, first):
        argv = [sys.executable, "-c", PROGRAM, str(out)]
        if shape == "last":
            argv += [first]
        elif shape == "flag":
            argv += ["--prompt", first]
        elif shape == "merged":
            argv += ["--prompt-interactive", brief_text + "\n\n" + first]
        else:
            argv += ["-c", "instructions=" + brief_text, first]
        return Launch(tuple(argv))

    store = post.Store(ship.runtime)
    long_threads(store)
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    monkeypatch.setattr(captain.harness, "launch", launch)
    proof = captain.up(resume=False)
    assert "8 open messages carried in " in proof
    import json
    given = wait_for(lambda: out.exists() and json.loads(out.read_text()))
    opening = given[-1]
    assert carried_file(opening).is_file()
    sizes = given[:-1]
    if shape == "brief-argument":
        assert max(sizes) > carriage.WORD_BYTES, "the brief keeps its own route, past the soft bound, and still starts"
        assert carriage.utf8(opening) + 1 <= carriage.WORD_BYTES
    else:
        assert max(sizes) + 1 <= carriage.WORD_BYTES
    assert not captain.pane().dead


def test_only_a_command_the_kernel_would_refuse_is_refused_naming_sizes_not_text(ship, tmux, monkeypatch):
    import sys
    from quarterdeck.harness.base import Launch
    from quarterdeck.ship import ShipError

    store = post.Store(ship.runtime)
    store.new("order", "desk", "captain", "short", subject="Short")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    oversized = "SYNTHETIC OVERSIZED BRIEF " * 6_000
    monkeypatch.setattr(captain.harness, "launch",
                        lambda *a, **kw: Launch((sys.executable, "-c", "pass", oversized, a[-1] or "")))
    with pytest.raises(ShipError, match="exec would refuse this launch for its size") as raised:
        captain.up(resume=False)
    from quarterdeck import harness
    prefix = seatmod.launch_words("p", [], socket=tmux.socket, tmux_tmpdir=Path("t"), markers=harness.inherited_markers())
    oversized_word = len(prefix) + 3  # after the program, "-c" and "pass"
    assert f"word {oversized_word} " in str(raised.value) and "SYNTHETIC" not in str(raised.value)
    assert captain.pane() is None


def test_an_account_too_long_for_its_budget_names_the_rest_and_the_file_lists_all(ship, tmux):
    store = post.Store(ship.runtime)
    orders = [store.new("order", "desk", "captain", f"synthetic body {i}.", subject=f"Synthetic subject {i} " + "s" * 300)
              for i in range(250)]
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    captain.up(resume=False)
    opening = first_input(captain)
    counts = re.search(r'carriage: (\d+) whole; (\d+) overflow;', opening)
    assert counts and sum(map(int, counts.groups())) == 250, "none dropped silently"
    path = carried_file(opening)
    whole = path.read_text(encoding="utf-8")
    rows = index_rows(opening)
    indexed = {row['id'] for row in rows}
    assert len(indexed) == len(rows) == int(counts.group(2))
    assert all(order.id in indexed or post.typed([order]) in whole for order in orders)
    given = received(captain, opening)
    assert all(given.count(order.body) == 1 for order in orders)
    assert len(opening.encode()) + path.stat().st_size <= ship.carry_bytes
    assert all(file.stat().st_size <= ship.carry_bytes for file in path.parent.iterdir())
