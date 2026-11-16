"""A full job some seats moved under certifies, with its retries, what it read whole, seat by seat (Partitions.stitch).
Synthetic seats, records and ledger only."""

import json
import os
from pathlib import Path

from quarterdeck import trusted_files
from quarterdeck.accounting_gate import BACKSTOP
from .test_accounting_scoped_jobs import change, scopes_of, world  # noqa: F401

ALPHA, BETA = 'ordinary-alpha', 'ordinary-beta'


def moved(e, seat):
    """The seat's identity moves, as a fresh start moves it: its launch metadata changes."""
    path = trusted_files.make(e.runtime, seat) / trusted_files.READERS
    path.write_text(path.read_text() + 'Synthetic relaunch.\n')


def full_then_retry(e, *, during_full=(), between=(), during_retry=(), retry_read=True, span=30):
    """A full job that finds ALPHA moved, then ALPHA's retry, which empties the continuation. ``retry_read=False``:
    the retry's guarded read of ALPHA fails, so it collects no seat."""
    e.clock.mono = 1000.0
    assert e.begin()
    started = e.clock.mono
    moved(e, ALPHA)
    change(e.paths[ALPHA])
    for act in during_full:
        act()
    e.clock.mono += span
    e.gate.finish(e.service, e.db.total_changes, full=False, committed=False, moved={ALPHA}, scopes=scopes_of(e.gate))
    assert e.gate.partitions.pending is not None and e.gate.partitions.baseline is None
    for act in between:
        act()
    assert e.gate.allowed(e.service, retry=True)
    if retry_read:
        assert e.begin({ALPHA})
    else:
        e.gate.configure(e.service, [], {ALPHA})
        e.gate.begin(e.service, [])
        assert e.gate.confirm(e.service, [])
    for act in during_retry:
        act()
    e.clock.mono += 5
    e.gate.finish(e.service, e.db.total_changes, full=False, committed=True, emptied=True, scopes=scopes_of(e.gate))
    return started, e.clock.mono


def key(path):
    return str(Path(os.path.abspath(path)))


def test_a_full_job_with_one_seat_moved_certifies_every_other_seat_then_reads_only_dirty_seats(world):
    e = world
    _, stitched = full_then_retry(e)
    p = e.gate.partitions
    assert e.gate.previous['stitched'] == 1 and p.baseline is not None and p.pending is None
    assert key(e.paths[BETA]) in p.marks, 'the full job read beta whole and it did not move'
    assert key(e.paths[ALPHA]) in p.marks, 'the retry read alpha whole and it did not move within the retry'
    assert p.due == stitched + BACKSTOP, 'the backstop runs from the stitch, as from a full job\'s end'
    assert not e.gate.allowed(e.service), 'nothing dirty: no job at all, not another full one'
    change(e.paths[BETA])
    assert e.gate.allowed(e.service) and e.gate.scope == {BETA}, 'a scoped job for the dirty seat alone'


def test_a_span_longer_than_the_backstop_still_gives_a_certificate_until_the_stitch_plus_the_backstop(world):
    """A whole re-read with its retries can take longer than the backstop: the certificate it stitches holds for the
    backstop from the stitch, as a completed full job's holds from its end, and is not due at once."""
    e = world
    started, stitched = full_then_retry(e, span=BACKSTOP + 14)
    p = e.gate.partitions
    assert e.gate.previous['stitched'] == 1 and p.due == stitched + BACKSTOP and stitched > started + BACKSTOP
    assert not e.gate.allowed(e.service), 'not due at once: no job at all'
    e.clock.mono = stitched + BACKSTOP - 1
    assert not e.gate.allowed(e.service), 'still held one second before the stitch plus the backstop'
    e.clock.mono = stitched + BACKSTOP
    assert e.gate.allowed(e.service) and e.gate.scope is None and 'backstop' in e.gate.decision


def test_a_record_that_changed_while_the_job_read_it_is_never_marked(world):
    e = world
    full_then_retry(e, during_full=[lambda: change(e.paths[BETA])])
    p = e.gate.partitions
    assert p.baseline is not None and key(e.paths[BETA]) not in p.marks and key(e.paths[BETA]) in p.dirty
    assert e.gate.allowed(e.service) and e.gate.scope == {BETA}


def test_a_global_change_across_the_span_still_forces_a_full_job(world):
    e = world
    settings = e.service.ship.folder / 'ship.toml'
    full_then_retry(e, between=[lambda: settings.write_text('Synthetic declaration, changed.\n')])
    p = e.gate.partitions
    assert e.gate.previous['stitched'] == 0 and p.baseline is None
    assert e.gate.allowed(e.service) and e.gate.scope is None
    assert 'no-completed-certificate' in e.gate.decision


def test_another_seats_launch_metadata_still_forces_a_full_job(world):
    e = world
    full_then_retry(e, during_retry=[lambda: moved(e, BETA)])
    assert e.gate.previous['stitched'] == 0 and e.gate.partitions.baseline is None
    assert e.gate.allowed(e.service) and e.gate.scope is None


def test_a_retried_seat_that_moves_again_within_its_retry_certifies_nothing(world):
    e = world
    full_then_retry(e, during_retry=[lambda: moved(e, ALPHA)])
    assert e.gate.previous['stitched'] == 0 and e.gate.partitions.baseline is None


def test_a_retry_that_failed_to_read_its_seat_certifies_nothing_of_it(world):
    """A retry whose guarded read of its seat failed did not read it whole: the seat whose identity moved was never
    re-read, so its moved launch metadata stands as a global change, nothing is certified and the next job is full."""
    e = world
    full_then_retry(e, retry_read=False)
    p = e.gate.partitions
    assert e.gate.previous['stitched'] == 0 and p.baseline is None and not p.marks
    assert e.gate.allowed(e.service) and e.gate.scope is None


def two_seats(tmp_path, monkeypatch):
    """The real driver's world with two seats, each with its own record; nothing reads as young."""
    from types import SimpleNamespace
    from quarterdeck import accounting_gate, accounting_slices, record_owner
    from quarterdeck.harness.fake import FakeHarness
    from quarterdeck.harness.synthetic_record import ClaudeShape
    from .test_accounting_slices import world as slices_world
    svc, changed, record, _ = slices_world(tmp_path, monkeypatch, 3)
    home = svc.runtime / 'seats' / 'synthetic-other' / 'home'
    writer = ClaudeShape(home, tmp_path / 'workspace', 'other-session', 'synthetic-model')
    for _ in range(3):
        writer.begin('synthetic input', 'synthetic-model', 'high')
        writer.end('synthetic output')
    record_owner.register(trusted_files.of_home(home), writer.path, 'fake')
    other = SimpleNamespace(name='synthetic-other', home=home, trusted=trusted_files.of_home(home), harness=FakeHarness(),
                            session=SimpleNamespace(human=False), pane=lambda: None,
                            launched=lambda: None, record=lambda: writer.path)
    for seat in (changed, other):
        seat.spec, seat.pool = SimpleNamespace(model='synthetic-model'), SimpleNamespace(name='synthetic-pool')
    svc.resolved_seats = lambda: [changed, other]
    svc.ship.file.write_text('[ship]\nname="synthetic"\ncode="checkout"\n')
    sources = {changed.name: record, other.name: writer.path}
    monkeypatch.setattr(record_owner, 'records', lambda s: [record_owner.Owned(sources[s.name], 'fake', s.harness)])
    monkeypatch.setattr(accounting_gate.time, 'time_ns', lambda: 9_000_000_000_000_000_000)  # nothing reads as young
    monkeypatch.setattr(accounting_slices, 'BUDGET', 0.02)
    return svc, changed, other


def fresh_start(seat):
    """The seat starts fresh: its identity, its trusted registry, moves."""
    from quarterdeck import record_owner
    with record_owner.registry(seat.trusted).open('a') as stream:
        stream.write('\n')


def test_through_the_slices_a_full_job_reset_by_one_seat_and_its_retry_leave_a_certificate(tmp_path, monkeypatch):
    """The real driver: a full job finds one seat's identity moved (a fresh start), retries that seat alone, and the
    gate then holds a stitched certificate: the next pass reads nothing, not all the seats again."""
    from quarterdeck.accounting_slices import Driver
    from .test_accounting_slices import finish
    svc, changed, other = two_seats(tmp_path, monkeypatch)
    starts = []

    class Moved(Driver):
        def start(self, service):
            starts.append(self.scope)
            super().start(service)
            if len(starts) == 1:  # the changed seat starts fresh while the full job reads it
                fresh_start(changed)
    driver = Moved()
    try:
        finish(driver, svc)
        assert starts == [None] and driver.retry_seats == {changed.name}, 'the full job was reset by one seat'
        finish(driver, svc)
        assert starts == [None, {changed.name}], 'its retry read that seat alone'
        p = driver.gate.partitions
        assert driver.gate.previous.get('stitched') == 1 and p.baseline is not None, 'a certificate, stitched'
        assert driver.gate.allowed(svc) is False, 'the next pass reads nothing: not another full job'
    finally:
        driver.close()
        svc.ledger.close()


def test_a_retry_reset_by_one_seat_requeues_the_rest_and_the_continuation_then_empties(tmp_path, monkeypatch):
    """A retry that is itself reset re-queues every seat still in the continuation, not only the seat that moved
    again: the seats it read are not stranded, the continuation empties after one more read, and a stitch follows."""
    from quarterdeck.accounting_slices import Driver
    from .test_accounting_slices import finish
    svc, changed, other = two_seats(tmp_path, monkeypatch)
    both = {changed.name, other.name}
    starts = []

    class Moved(Driver):
        def start(self, service):
            starts.append(self.scope)
            super().start(service)
            if len(starts) == 1:  # both seats start fresh while the full job reads them
                fresh_start(changed)
                fresh_start(other)
            elif len(starts) == 2:  # one of them starts fresh again during their retry
                fresh_start(changed)
    driver = Moved()
    try:
        finish(driver, svc)
        assert starts == [None] and driver.retry_seats == both
        finish(driver, svc)
        assert starts == [None, both]
        assert driver.retry_seats == both, 'the retry reset by one seat re-queues every seat still in the continuation'
        finish(driver, svc)
        assert starts == [None, both, both]
        assert driver.full_continuation is None, 'the continuation emptied'
        assert driver.gate.previous.get('stitched') == 1, ('and the stitch followed', driver.gate.previous.get('stitch-refused'))
        assert driver.gate.allowed(svc) is False
    finally:
        driver.close()
        svc.ledger.close()


def test_a_refused_stitch_logs_its_barrier_class_only(world):
    """The stitch's refusal names which barrier refused it, as a class: never a path or a seat."""
    e = world
    logged = []
    e.service.log = lambda event, **fields: logged.append((event, fields))
    settings = e.service.ship.folder / 'ship.toml'
    full_then_retry(e, between=[lambda: settings.write_text('Synthetic declaration, changed.\n')])
    refused = [fields for event, fields in logged if event == 'accounting-stitch-refused']
    assert refused == [dict(classes=['settings'])]
    assert e.gate.previous['stitch-refused'] == ['settings']


def test_a_message_that_lands_after_the_retry_began_keeps_the_post_due_after_the_stitch(world):
    """The race: the certificate's post stands only at the stamps the newest digests were taken after. A message that
    lands after the retry began is not in them: after the stitch the post is due, and a job takes the scopes again."""
    e = world
    late = e.runtime / 'post' / 'store' / 'synthetic-late-message.json'
    full_then_retry(e, during_retry=[lambda: late.write_text(json.dumps({'id': 'synthetic-late'}))])
    assert e.gate.previous['stitched'] == 1, 'the post is no global barrier'
    assert e.gate.allowed(e.service) and e.gate.scope == set() and e.gate.decision.get('post-scopes') == 1


def test_a_message_that_lands_before_the_retry_began_is_covered_by_its_scopes(world):
    e = world
    early = e.runtime / 'post' / 'store' / 'synthetic-early-message.json'
    full_then_retry(e, between=[lambda: early.write_text(json.dumps({'id': 'synthetic-early'}))])
    assert e.gate.previous['stitched'] == 1
    assert not e.gate.allowed(e.service), 'the retry\'s scopes were taken after it: covered'
