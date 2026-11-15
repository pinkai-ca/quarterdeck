"""A check owns its transient transport until teardown; a dead owner leaves no exemption."""

import json
import select
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import check, pending, post
from quarterdeck.drill import Check
from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.seat import Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane
from .test_removed_delivery import launch, no_keys


def synthetic_ship(root, survivor=False):
    specs = {"survivor": SeatSpec("survivor", "crew", "worker", "fake", ("fake",))} if survivor else {}
    return Ship(root, "synthetic", "operator", root, {"fake": Pool("fake", "fake")},
                {"crew": Session("crew", "Synthetic")}, specs)


def seed(seat, sid, pane="%1", pid=111):
    launch(seat, pane, pid)
    record = ClaudeShape(seat.home, seat.ship.workspace, sid, "fake-1")
    (seat.trusted / "record").write_text(str(record.path))
    return record


@pytest.mark.parametrize("outcome", ["delivered", "unresolved", "exception"])
def test_service_leaves_live_check_to_owner_and_teardown_retires_only_uncertainty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome) -> None:
    ship = synthetic_ship(tmp_path, survivor=True)
    sessions = set()
    tmux = SimpleNamespace(has_session=lambda name: name in sessions,
                           kill_session=lambda name: sessions.remove(name),
                           new_session=lambda name, **kwargs: (sessions.add(name) or name),
                           status_line=lambda *args: None, kill_session_id=lambda name: sessions.discard(name),
                           pane=lambda ident: Pane(ident, "Synthetic", "worker", 111 if ident == "%1" else 222,
                                                   80, 24, False, False, None), keys=no_keys, paste=no_keys)
    svc = Service(ship, tmux)
    survivor = svc.seat("survivor")
    record = seed(survivor, "synthetic-survivor")
    good = svc.store.new("order", "ship", survivor.name, "Synthetic surviving input")
    svc.attempts.close(svc.attempts.open(good, survivor.name, "%1", 111, survivor.launched().at), "queued")
    record.begin(post.typed([good]), "fake-1", "low")
    record.end("synthetic answer")
    observed = {}
    monkeypatch.setattr(FakeHarness, "version", lambda self: "synthetic")

    def probe(checked, seat, orders):
        assert seat.name in checked.seats and seat.name not in svc.ship.seats
        sessions.add(seat.session.title)
        record = seed(seat, "synthetic-check", "%2", 222)
        message = svc.store.new("order", "ship", seat.name, "Synthetic check input")
        orders.append(message)
        attempt = svc.attempts.open(message, seat.name, "%2", 222, seat.launched().at)
        proof = pending.Proof.begin(seat, attempt, post.typed([message]))
        proof.save(phase="submitted", shown=proof.data["payload"])
        observed.update(seat=seat, message=message, attempt=attempt, proof=proof)
        for _ in range(2):
            svc.reconcile()
            assert svc.attempts.latest()[message.id] is None
            assert json.loads(proof.path.read_text())["phase"] == "submitted"
            assert not svc.log_path.exists(), "an active check is neither abandoned nor judged by the service"
        assert svc.attempts.latest()[good.id] == "delivered", "the declared survivor still reconciles"
        if outcome == "exception":
            raise RuntimeError("synthetic check exception")
        if outcome == "delivered":
            record.begin(proof.data["payload"], "fake-1", "low")
            record.end("synthetic receipt")
            assert proof.confirmed(seat) == "delivered", "only the exact owned-record input confirms it"
            proof.finish("delivered")
            assert svc.attempts.close(attempt, "delivered", "exact synthetic record receipt").result == "delivered"
        return [Check("synthetic record receipt", outcome == "delivered")]

    def down(seat):
        # Ownership lasts through cleanup; the service cannot race it between probe and stop.
        with pending.transient_lock(ship.runtime, seat.name) as available:
            assert not available
        expected = "delivered" if outcome == "delivered" else "abandoned"
        assert svc.attempts.latest()[observed["message"].id] == expected
        assert not pending.active(seat)
        (seat.trusted / "launch.json").unlink(missing_ok=True)  # as a down that proved its program ended
        return "synthetic stop"
    monkeypatch.setattr(check, "probe", probe)
    monkeypatch.setattr(Seat, "down", down)
    if outcome == "delivered":  # the wake step's own seat lifetime is proved by its own gates
        monkeypatch.setattr(check, "probe_wake", lambda seat, orders: [Check("human-session waking line", True, "stubbed")])
    try:
        if outcome == "exception":
            with pytest.raises(RuntimeError, match="synthetic check exception"):
                check.check(ship, "fake", tmux)
        else:
            results = check.check(ship, "fake", tmux)
            assert all(r.passed for r in results) == (outcome == "delivered")
        seat, message, proof = observed["seat"], observed["message"], observed["proof"]
        with pending.transient_lock(ship.runtime, seat.name) as available:
            assert available
        expected = "delivered" if outcome == "delivered" else "abandoned"
        assert svc.attempts.latest()[message.id] == expected
        assert json.loads(proof.path.read_text())["phase"] == expected
        assert not seat.folder.exists() and not sessions
        before = svc.log_path.read_bytes() if svc.log_path.exists() else b""
        svc.reconcile()
        assert (svc.log_path.read_bytes() if svc.log_path.exists() else b"") == before
        events = [json.loads(line) for line in before.splitlines()]
        assert len(events) == (0 if outcome == "delivered" else 1)
        assert all(e["event"] == "abandoned" for e in events)
    finally:
        svc.ledger.close()


DEAD_OWNER = """
import sys
from pathlib import Path
from types import SimpleNamespace
from quarterdeck import check, doctor, pending, post
from quarterdeck.drill import Check
from quarterdeck.seat import Seat
from quarterdeck.harness.fake import FakeHarness
from tests.unit.test_check_lifetime import synthetic_ship
from tests.unit.test_removed_delivery import launch
ship = synthetic_ship(Path(sys.argv[1]))
tmux = SimpleNamespace(has_session=lambda name: False,
                       new_session=lambda name, **kwargs: name, status_line=lambda *args: None,
                       pane=lambda name: SimpleNamespace(id='%1', pid=111))
FakeHarness.version = lambda self: 'synthetic'
def probe(checked, seat, orders):
    launch(seat)
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    message = store.new('order', 'ship', seat.name, 'Synthetic killed-owner input')
    orders.append(message)
    attempt = attempts.open(message, seat.name, '%1', 111, seat.launched().at)
    proof = pending.Proof.begin(seat, attempt, post.typed([message]))
    proof.save(phase='submitted', shown=proof.data['payload'])
    print('ready', flush=True)
    sys.stdin.read()
    raise AssertionError('owner should be killed while holding its lifetime lock')
check.probe = probe
if sys.argv[2] == 'check':
    check.check(ship, 'fake', tmux)
else:
    doctor.shutil.which = lambda *args, **kwargs: 'synthetic-program'
    Seat.up = lambda self, **kwargs: probe(self.ship, self, [])
    doctor.fix(ship, [Check('synthetic', False)], tmux)
"""


@pytest.mark.parametrize("caller", ["check", "doctor"])
def test_killed_temporary_owner_releases_kernel_lock_and_is_abandoned_once(tmp_path: Path, caller) -> None:
    svc = Service(synthetic_ship(tmp_path), SimpleNamespace())
    process = subprocess.Popen([sys.executable, "-c", DEAD_OWNER, str(tmp_path), caller], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([process.stdout], [], [], 10)[0], "synthetic owner failed to become ready"
        assert process.stdout.readline().strip() == "ready"
        attempt, = svc.attempts.all()
        for _ in range(2):
            svc.reconcile()
            assert svc.attempts.latest()[attempt.message] is None
        process.kill()  # no finally/teardown: only kernel descriptor lifetime can release ownership
        process.communicate(timeout=10)
        svc.reconcile()
        assert svc.attempts.latest()[attempt.message] == "abandoned"
        proof, = (svc.runtime / "post" / "pending" / attempt.seat).glob("*.json")
        assert json.loads(proof.read_text())["phase"] == "abandoned"
        before = svc.log_path.read_bytes()
        assert len(before.splitlines()) == 1 and json.loads(before)["event"] == "abandoned"
        svc.reconcile()
        assert svc.log_path.read_bytes() == before
        assert len(svc.attempts.all()) == 1
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        svc.ledger.close()


@pytest.mark.parametrize("name", ["check-fake-unowned", "ordinary-removed"])
def test_name_and_stale_unlocked_registration_do_not_exempt_orphan(tmp_path: Path, name) -> None:
    svc = Service(synthetic_ship(tmp_path), SimpleNamespace())
    try:
        with pending.transient_lock(svc.runtime, name) as owned:
            assert owned
        message = svc.store.new("order", "ship", name, "Synthetic orphan input")
        svc.attempts.open(message, name, "%1", 111, None)
        svc.reconcile()
        assert svc.attempts.latest()[message.id] == "abandoned"
        assert len(svc.log_path.read_text().splitlines()) == 1
    finally:
        svc.ledger.close()
