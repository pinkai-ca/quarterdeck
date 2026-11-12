"""Independent validation: a status reading of a real seat on a private fake pi writes nothing."""

import pytest

from quarterdeck import structure_outcome
from quarterdeck.seat import Seat
from tests.integration.test_structure_outcome import setup_structure  # noqa: F401  (fixture)


def tree(folder, skip):
    entries = []
    for path in sorted(folder.rglob('*')):
        if path == skip or skip in path.parents:
            continue
        info = path.lstat()
        entries.append((str(path.relative_to(folder)), info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns))
    return entries


@pytest.mark.parametrize('kind', ['forum', 'cascade'])
def test_status_from_a_fresh_seat_object_reads_the_wait_and_writes_nothing(setup_structure, tmux, kind):
    ship, svc, seat, root, order = setup_structure(kind, 'stop')
    svc.tick()
    expected = ['waiting on alpha: turn ended, no report']
    lock = ship.runtime / 'post/lifecycle/alpha.lock'
    assert lock.exists()
    home = ship.runtime / 'seats/alpha/home'  # the program's own files are not the reader's
    before = tree(ship.runtime, home)
    # A separate status process builds its own Seat; it must not join, register or repair anything.
    reader = Seat(ship, ship.seat('alpha'), tmux)
    for _ in range(3):
        assert structure_outcome.waits(ship, [reader]) == expected
    assert tree(ship.runtime, home) == before
    svc.tick()
    recipient = 'lead' if kind == 'forum' else 'conductor'
    assert len([m for m in svc.store.for_seat(recipient) if m.sender == 'ship']) == 1
    assert svc.store.is_open(order) and svc.store.is_open(root)
