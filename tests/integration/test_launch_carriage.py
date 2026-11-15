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

from quarterdeck import post, post_threads, seat as seatmod, ship as shipmod
from quarterdeck.tmux import Tmux

from .waiting import wait_for

LIMIT = 131_072  # the kernel's per-argument limit with its NUL, on 4 KiB pages
ACCOUNT_FILE = re.compile(r"^qd > read (\S+) whole first, before acting; it is your handoff$", re.MULTILINE)
OVERFLOW_FILE = re.compile(r"^qd > thread index: (\S+);", re.MULTILINE)


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
    """Complete thread metadata, directly typed or reached through bounded index pages."""
    found = OVERFLOW_FILE.search(opening)
    if found is None:
        return [json.loads(line) for line in opening.splitlines() if line.startswith('{')]
    rows = []
    def visit(path):
        for line in path.read_text(encoding="utf-8").splitlines():
            data = json.loads(line)
            if 'first' in data:
                visit(Path(data['path']))
            else:
                rows.append(data)
    visit(Path(found.group(1)))
    return rows


def thread_text(seat: seatmod.Seat, reference: str) -> str:
    """The same read-only full-thread path used by qd thread ID, not an automatic launch body."""
    return post_threads.render(post_threads.whole(post.Store(seat.ship.runtime), reference, seat.name))


def received(seat: seatmod.Seat, opening: str) -> str:
    """Only full text automatically received: the first input and its own named handoff."""
    found = ACCOUNT_FILE.search(opening)
    return opening if found is None else opening + "\n" + Path(found.group(1)).read_text(encoding="utf-8")


def words(seat: seatmod.Seat) -> list[str]:
    script = (seat.folder / "launch.sh").read_text(encoding="utf-8")
    return shlex.split(script.split("\n", 2)[2])


def jail_words(seat: seatmod.Seat) -> list[str]:
    """The jail's own words, from bwrap to its "--": the launch's environment words before it name a fresh
    private TMUX_TMPDIR every launch.

    The covers and the socket masks are left out: a test-run folder or another ship's runtime inside a folder the jail
    binds back is covered, and a listening socket in sight is masked, as the machine holds them at each launch; a whole
    run's other tests make such folders and sockets (a CI-layout run makes its .testruns in the checkout the jail
    binds). Every other word is compared."""
    found = words(seat)
    start = next(index for index, word in enumerate(found) if Path(word).name == "bwrap")
    jail = found[start:found.index("--")]
    kept, index = [], 0
    while index < len(jail):
        if jail[index] == "--size" and jail[index + 2:index + 3] == ["--tmpfs"] and is_cover(Path(jail[index + 3]), seat):
            index += 4
            continue
        if jail[index:index + 2] == ["--ro-bind", "/dev/null"]:  # a socket mask: the sockets listening at that launch
            index += 3
            continue
        kept.append(jail[index])
        index += 1
    return kept


def is_cover(path: Path, seat: seatmod.Seat) -> bool:
    """A tmpfs over a test-run folder, or over a ship runtime other than this seat's own."""
    return path.name == ".testruns" or (path.name == "runtime" and (path.parent / "ship.toml").is_file()
                                        and path != seat.ship.runtime)


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
    assert proof and "4 threads indexed" in proof
    total = sum(len(order.body.encode()) + len(answer.body.encode()) for order, answer in threads)
    assert total > 3 * LIMIT, "the stored thread bodies remain several times the per-argument limit"
    pane = captain.pane()
    assert pane is not None and not pane.dead
    opening = first_input(captain, earlier)
    assert opening.startswith("qd > briefed as captain, captain · 4 orders open")
    rows = {row['id']: row for row in index_rows(opening)}
    for order, amendment in threads:
        assert order.body not in opening and amendment.body not in opening
        assert amendment.id in rows[order.id]['amendments']
        assert rows[order.id]['thread'] == f'qd thread {order.id}'
        whole = thread_text(captain, order.id)
        assert order.body in whole and amendment.body in whole, "one command prints every body whole"  # law: context-is-cache
        assert whole.index(order.body) < whole.index(amendment.body), "an amendment follows its order"
    assert len(opening.encode()) <= ship.carry_bytes
    assert max(len(word.encode()) for word in words(captain)) + 1 <= LIMIT // 2
    assert not captain.pane().dead


@pytest.mark.parametrize("side", [0, 1])
def test_the_kernel_boundary_just_under_and_just_over_both_start(ship, tmux, side):
    """The same boundary in a self-handoff; both sides use one full file and still start."""
    store = post.Store(ship.runtime)
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    draft = store.prepare("order", "captain", "captain", "x" * 120000, subject="Boundary")
    overhead = len((post.briefed_header("captain", "captain", [draft]) + "\n" + post.typed([draft])).encode()) - 120000
    order = store.new("order", "captain", "captain", "x" * (LIMIT - 1 + side - overhead), subject="Boundary")
    raw = post.briefed_header("captain", "captain", [order]) + "\n" + post.typed([order])
    assert len(raw.encode()) + 1 == LIMIT + side, "the boundary is measured in bytes, the NUL included"
    proof = captain.up(resume=False)
    assert "1 thread indexed" in proof
    opening = first_input(captain)
    assert order.body in received(captain, opening)
    assert carried_file(opening).read_text() == post.typed([order])
    assert max(len(word.encode()) + 1 for word in words(captain)) <= LIMIT // 2
    assert not captain.pane().dead


def test_a_short_ordinary_message_is_indexed_and_stays_a_file(ship, tmux):
    store = post.Store(ship.runtime)
    order = store.new("order", "desk", "captain", "A short synthetic order.\nNothing more.", subject="Short")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    assert captain.up(resume=False).endswith("1 thread indexed")
    opening = first_input(captain)
    row, = index_rows(opening)
    assert row['id'] == order.id and row['subject'] == 'Short'
    assert order.body not in opening and order.body in thread_text(captain, order.id)
    assert ACCOUNT_FILE.search(opening) is None
    assert not (captain.folder / "carried").exists(), "a short index writes no file"


# -- the settled form, after the mend ----------------------------------------------------------


def carried_file(opening: str) -> Path:
    found = ACCOUNT_FILE.search(opening)
    assert found, "a long carriage names its file"
    return Path(found.group(1))


def test_long_bodies_become_thread_metadata_not_a_full_carriage_dump(ship, tmux):
    from quarterdeck import carriage
    store = post.Store(ship.runtime)
    threads = long_threads(store)
    question = store.new("question", "desk", "captain", "A synthetic question?", subject="Which way?", due="tomorrow")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    proof = captain.up(resume=False)
    assert "5 threads indexed" in proof
    opening = first_input(captain)
    assert carriage.utf8(opening) <= min(carriage.INLINE_BYTES, ship.carry_bytes)
    assert opening.splitlines()[0] == "qd > briefed as captain, captain · 4 orders open, 1 question open"
    rows = {row['id']: row for row in index_rows(opening)}
    for order, answer in threads:
        assert rows[order.id]['kind'] == 'order' and rows[order.id]['sender'] == 'desk'
        assert rows[order.id]['subject'] == order.subject and answer.id in rows[order.id]['amendments']
        assert order.body not in opening and answer.body not in opening
        whole = thread_text(captain, order.id)
        assert whole.count(order.body) == whole.count(answer.body) == 1
    assert rows[question.id]['due'] == 'tomorrow' and question.body not in opening
    assert 'read that thread whole' in opening
    assert not (captain.folder / 'carried').exists(), 'no general body dump is produced'
    latest = post.Attempts(ship.runtime).latest()
    assert {order.id for order, _ in threads} | {answer.id for _, answer in threads} | {question.id} == {identifier for identifier, result in latest.items() if result == 'launch'}
    details = [attempt.detail for attempt in post.Attempts(ship.runtime).all() if attempt.result == 'launch']
    assert details and all('indexed at launch' in detail and 'not confirmed read' in detail for detail in details)
    assert max(len(word.encode()) + 1 for word in words(captain)) <= carriage.WORD_BYTES


@pytest.mark.parametrize("letter,form", [("e", "whole"), ("é", "file")])
def test_the_line_between_whole_and_file_is_counted_in_utf8_bytes(ship, tmux, letter, form):
    store = post.Store(ship.runtime)
    store.new("order", "captain", "captain", letter * 17_000, subject="Bytes")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    raw = captain.carriage().text
    assert len(raw) < 32_768, "fewer characters than the line either way"
    captain.up(resume=False)
    opening = first_input(captain)
    assert (ACCOUNT_FILE.search(opening) is not None) == (form == "file")


def test_a_refused_message_is_only_its_notice_in_the_account_and_the_file(ship, tmux):
    from quarterdeck import refusal
    store = post.Store(ship.runtime)
    long_threads(store)
    refused = store.new('order', 'desk', 'captain', 'SYNTHETIC REFUSED BODY', subject='SYNTHETIC REFUSED SUBJECT')
    amended = store.new('order', 'desk', 'captain', 'SYNTHETIC REFUSED AMENDMENT', re=refused.id)
    refusal.save(ship.runtime, 'captain', [{'stage': 'held', 'launch': {}, 'turn': None,
                   'quarantined': [{'message': refused.id}, {'message': amended.id}], 'notices': []}])
    captain = seatmod.Seat(ship, ship.seat('captain'), tmux)
    captain.up(resume=False)
    opening = first_input(captain)
    row = next(row for row in index_rows(opening) if row['id'] == refused.id)
    printed = thread_text(captain, refused.id)
    launch = (captain.folder / 'launch.sh').read_text(encoding='utf-8')
    folder = captain.folder / 'carried'
    generated = [path.read_text(encoding='utf-8') for generation in folder.iterdir() for path in generation.iterdir()] if folder.exists() else []
    for shown in (opening, printed, launch, *generated):
        assert 'SYNTHETIC REFUSED' not in shown, 'neither its subject nor its body, anywhere'  # law: context-is-cache
    assert row['sender'] == 'desk' and row['subject'] is None
    assert row['notice'] == refusal.WITHHELD and set(row['withheld']) == {refused.id, amended.id}
    assert printed.count(refusal.WITHHELD) == 2, 'each withheld message remains its notice in the complete thread'
    assert refusal.WITHHELD in opening


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


def test_resumed_seat_gets_index_again_without_retyping_its_handoff_or_crediting_reads(ship, tmux):
    from quarterdeck import thread_context
    store = post.Store(ship.runtime)
    threads = long_threads(store)
    handoff = store.new('order', 'captain', 'captain', body('self handoff', 150000), subject='Self handoff')
    captain = seatmod.Seat(ship, ship.seat('captain'), tmux)
    captain.up(resume=False)
    first_path = carried_file(first_input(captain))
    before = {path: path.read_bytes() for path in first_path.parent.iterdir()}
    record = wait_for(captain.record)
    wait_for(lambda: captain.harness.state(record).turns >= 1)
    consumed = thread_context.consumed_ids(store, 'captain', 'captain', captain.harness.inputs(record))
    assert consumed == frozenset(), 'an index/file path credits no body as read'
    proof = captain.restart()
    assert ' · resumed ' in proof and '5 threads indexed' in proof
    opening = first_input(captain, 1)
    assert ACCOUNT_FILE.search(opening) is None and handoff.body not in opening
    rows = {row['id']: row for row in index_rows(opening)}
    assert handoff.id in rows
    assert all(answer.id in rows[order.id]['amendments'] for order, answer in threads)
    assert all(path.read_bytes() == data for path, data in before.items()), 'an earlier generation is never rewritten'
    assert all(answer.body in thread_text(captain, order.id) for order, answer in threads)


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
    store.new("order", "captain", "captain", body("self handoff", 150000), subject="Self handoff")
    from quarterdeck import standing
    runs = standing.PROJECT_DIR / ".testruns"  # as a whole run's CI-layout test makes it, between two launches
    made = not runs.exists()
    if made:
        runs.mkdir()
    try:
        captain.up(resume=False)
    finally:
        if made:
            runs.rmdir()
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
    store.new("order", "captain", "captain", body("self handoff", 150000), subject="Self handoff")
    captain = seatmod.Seat(ship, ship.seat("captain"), tmux)
    monkeypatch.setattr(captain.harness, "launch", launch)
    proof = captain.up(resume=False)
    assert "1 thread indexed in " in proof
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
    prefix = seatmod.launch_words("p", [], socket=tmux.socket, tmux_tmpdir=Path("t"), markers=harness.inherited_markers(),
                                 qd_command=ship.runtime / 'bin/qd')
    oversized_word = len(prefix) + 3  # after the program, "-c" and "pass"
    assert f"word {oversized_word} " in str(raised.value) and "SYNTHETIC" not in str(raised.value)
    assert captain.pane() is None


def test_an_index_too_long_for_the_box_has_complete_bounded_pages(ship, tmux):
    store = post.Store(ship.runtime)
    orders = [store.new('order', 'desk', 'captain', f'synthetic body {n}.', subject=f'Synthetic subject {n} ' + 's' * 300) for n in range(250)]
    captain = seatmod.Seat(ship, ship.seat('captain'), tmux)
    captain.up(resume=False)
    opening = first_input(captain)
    rows = index_rows(opening)
    assert len(rows) == len({row['id'] for row in rows}) == 250, 'none dropped silently'
    assert {row['id'] for row in rows} == {order.id for order in orders}
    assert all(order.body not in opening and order.body in thread_text(captain, order.id) for order in orders)
    found = OVERFLOW_FILE.search(opening)
    assert found
    root = Path(found.group(1))
    assert len(opening.encode()) <= ship.carry_bytes
    assert all(path.stat().st_size <= ship.carry_bytes for path in root.parent.iterdir())
