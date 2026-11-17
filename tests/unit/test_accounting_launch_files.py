"""A known seat's own launch files are judged seat by seat: they move that seat, and the owners of the records its
registries name, never every seat. Synthetic seats, records and ledger only."""

import json
import os
from pathlib import Path

from quarterdeck import trusted_files
from quarterdeck.accounting_gate import Reading
from quarterdeck.accounting_scope import Partitions
from .test_accounting_scoped_jobs import change, world  # noqa: F401

ALPHA, BETA = 'ordinary-alpha', 'ordinary-beta'


def key(path):
    return str(Path(os.path.abspath(path)))


def registry(e, seat):
    return trusted_files.make(e.runtime, seat) / trusted_files.READERS


def line(record):
    return json.dumps({'record': str(record), 'harness': 'fake'}) + '\n'


def test_a_seats_own_launch_record_moving_reads_that_seat_alone(world):
    e = world
    (trusted_files.make(e.runtime, BETA) / trusted_files.LAUNCH).write_text('Ordinary new launch.\n')
    assert e.gate.allowed(e.service) and e.gate.scope == {BETA}, 'that seat alone, not every seat'
    assert e.begin({BETA})
    e.finish({BETA})
    assert not e.gate.allowed(e.service), 'read whole at its new launch: certified'


def test_a_registry_line_naming_another_seats_record_reaches_that_seat_too(world):
    e = world
    with registry(e, BETA).open('a') as stream:  # beta's registry gains a line naming alpha's record
        stream.write(line(e.paths[ALPHA]))
    assert e.gate.allowed(e.service) and e.gate.scope == {ALPHA, BETA}


def test_a_rewritten_registry_reaches_every_record_it_named_then_and_now(world):
    e = world
    registry(e, BETA).write_text(line(e.paths[ALPHA]))
    assert e.begin()
    e.finish()  # certified with beta's registry naming alpha's record
    registry(e, BETA).write_text(line(e.paths[BETA]))  # rewritten: alpha's record is no longer named
    assert e.gate.allowed(e.service) and e.gate.scope == {ALPHA, BETA}, 'named then (alpha) and now (beta)'


def test_a_named_record_whose_owner_the_partitions_do_not_know_forces_a_full_job(world):
    e = world
    retired = e.paths[ALPHA].with_name('ordinary-retired-record')
    retired.write_text('Ordinary synthetic native input.\n')
    e.db.execute('INSERT INTO turns VALUES (?,?)', (str(retired), 'ordinary-retired'))
    e.db.commit()
    assert e.begin()
    e.finish()
    with registry(e, BETA).open('a') as stream:
        stream.write(line(retired))
    assert e.gate.allowed(e.service) and e.gate.scope is None


def test_an_unknown_seats_launch_files_stay_a_global_barrier(world):
    e = world
    unknown = trusted_files.make(e.runtime, 'ordinary-unknown') / trusted_files.LAUNCH
    unknown.write_text('Ordinary launch of a seat the partitions do not know.\n')
    assert e.begin()
    e.finish()
    assert not e.gate.allowed(e.service)
    unknown.write_text('Ordinary launch, moved.\n')
    assert e.gate.allowed(e.service) and e.gate.scope is None


def test_a_young_launch_file_leaves_its_seat_dirty_not_every_seat(tmp_path):
    """A seat whose own launch file is young in a reading may still be moving: that seat is read again, alone."""
    launch = str(tmp_path / 'trusted' / ALPHA / trusted_files.LAUNCH)
    def reading(young):
        return Reading((((launch, (1, 2, 3, 10, 4, 5)),), (), 'synthetic ship', {'fake': ['reader']}, 'synthetic epoch'),
                       (1, 0, 0, 0, 0), 0, ((launch, 'launch-metadata'),), (launch,) if young else (), 0)
    partitions = Partitions()
    partitions.seats, partitions.runtime = {ALPHA, BETA}, tmp_path
    partitions.record_meta(reading(False), {ALPHA, BETA})
    assert partitions.meta_dirty(reading(False)) == set()
    assert partitions.meta_dirty(reading(True)) == {ALPHA}
    partitions.record_meta(reading(True), {ALPHA})
    assert partitions.meta_dirty(reading(False)) == {ALPHA}, 'certified at a young reading: read again'


def test_an_in_place_rewrite_of_an_earlier_line_that_grows_the_registry_reaches_what_it_names(world):
    """A rewrite under the same file that does not shrink it is no append: the digest of its first old-size bytes
    changed, so it reaches every record it names now and named then, not only the lines after the old size."""
    e = world
    path = registry(e, BETA)
    path.write_text(line(e.paths[BETA]))
    assert e.begin()
    e.finish()  # certified with beta's registry naming beta's own record
    inode = path.stat().st_ino
    with path.open('r+') as stream:  # the earlier line rewritten in place, naming alpha's record: a longer file
        stream.write(line(e.paths[ALPHA]))
    assert path.stat().st_ino == inode and path.stat().st_size > len(line(e.paths[BETA]))
    assert e.gate.allowed(e.service) and e.gate.scope == {ALPHA, BETA}, 'the rewritten line reaches alpha too'
