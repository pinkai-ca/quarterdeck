"""Every tool in tests/ that copies a ship's runtime data leaves out the desk's and every office seat's records
(tests/runtime_copy.py). A throwaway ship: a desk seat and an office seat in a human session, and a crew seat."""
from __future__ import annotations

import json
import sqlite3

import pytest

from quarterdeck import record_owner, trusted_files
from tests import measure_accounting_gate, measure_attribution_catchup, measure_reconcile_partition
from tests import measure_retirement_certificate, measure_status_service, runtime_copy

SHIP = '''[ship]
name = "Synthetic measure"
operator = "issuer"
[pools.fake]
provider = "synthetic"
[sessions.crew]
title = "Synthetic crew"
[sessions.office]
title = "Synthetic office"
human = true
[seats.desk]
session = "office"
role = "desk"
harness = "fake"
pool = "fake"
model = "fake-1"
[seats.writer]
session = "office"
role = "office-synthetic"
harness = "fake"
pool = "fake"
model = "fake-1"
[seats.builder]
session = "crew"
role = "worker"
harness = "fake"
pool = "fake"
model = "fake-1"
'''
PEOPLE = ('desk', 'writer')
NATIVE = '{"synthetic": "a native record no copy of the people may hold"}\n'


@pytest.fixture
def source(tmp_path):
    """The ship folder: each seat with a native record in its home, registered as its own, and a ledger row."""
    folder = tmp_path / 'ship'
    runtime = folder / 'runtime'
    folder.mkdir()
    (folder / 'ship.toml').write_text(SHIP)
    rows = []
    for seat in ('desk', 'writer', 'builder'):
        record = runtime / 'seats' / seat / 'home' / 'sessions' / 'record.jsonl'
        record.parent.mkdir(parents=True)
        record.write_text(NATIVE)
        trusted = trusted_files.folder(runtime, seat)
        trusted.mkdir(parents=True)
        record_owner.register(trusted, record, 'fake')
        (trusted / 'record').write_text(str(record) + '\n')
        (trusted / 'compaction.json').write_text('{}\n')
        (runtime / 'seats' / seat / 'record-launches.jsonl').write_text(json.dumps({'record': str(record)}) + '\n')
        rows.append((str(record), seat))
    db = sqlite3.connect(runtime / 'ledger.sqlite')
    db.execute('CREATE TABLE turns (record TEXT, turn INTEGER, seat TEXT)')
    db.executemany('INSERT INTO turns VALUES (?, 0, ?)', rows)
    db.commit()
    db.close()
    (runtime / 'post' / 'store').mkdir(parents=True)
    return folder


def files(root):
    return sorted(path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file())


def no_record_of_people(root):
    """No file of a person's seat folder, and no native record of theirs anywhere in the copy."""
    held = [path for path in files(root) if path.split('/')[-4:-3] and any(
        f'seats/{seat}/' in path for seat in PEOPLE)]
    assert held == [], held
    for path in root.rglob('*'):
        if path.is_file() and path.suffix == '.jsonl' and 'sessions' in path.parts:
            assert not any(seat in path.parts for seat in PEOPLE), path


def test_the_people_are_read_from_the_ship_file(source):
    assert runtime_copy.people(source) == {'desk', 'writer'}


def test_the_attribution_measures_copy_leaves_out_the_people(source, tmp_path):
    copy = tmp_path / 'copy' / 'ship' / 'runtime'
    counts = measure_attribution_catchup.copied_runtime(source / 'runtime', copy)
    assert 'seats/builder/home/sessions/record.jsonl' in files(copy)
    no_record_of_people(copy)
    assert counts['left_out_seats'] == ['desk', 'writer'] and counts['left_out_records'] == 2
    db = sqlite3.connect(copy / 'ledger.sqlite')
    assert [row[0] for row in db.execute('SELECT seat FROM turns')] == ['builder'], 'their rows leave the copy too'
    db.close()


def test_the_accounting_measures_copy_leaves_out_the_people(source, tmp_path):
    copy = tmp_path / 'copy' / 'ship' / 'runtime'
    counts = measure_accounting_gate.copied_runtime(source / 'runtime', copy)
    measure_accounting_gate.copied_selection_metadata(source / 'runtime', copy)
    assert 'seats/builder/home/sessions/record.jsonl' in files(copy)
    no_record_of_people(copy)
    assert counts['left_out_seats'] == ['desk', 'writer'] and counts['left_out_records'] == 2
    db = sqlite3.connect(copy / 'ledger.sqlite')
    assert [row[0] for row in db.execute('SELECT seat FROM turns')] == ['builder']
    db.close()


@pytest.mark.parametrize('tool', [measure_reconcile_partition, measure_retirement_certificate],
                         ids=['reconcile', 'retirement'])
def test_each_freeze_leaves_out_the_peoples_records(source, tmp_path, tool):
    frozen = tmp_path / 'frozen' / 'ship'
    tool.freeze(source, frozen)
    copied = files(frozen)
    assert 'runtime/seats/builder/home/sessions/record.jsonl' in copied, copied
    no_record_of_people(frozen / 'runtime')
    if tool is measure_reconcile_partition:  # the one freeze that keeps read aliases names the people private
        aliases = json.loads((frozen / 'frozen-native-records.json').read_text())
        assert sorted(prefix.split('/seats/')[1].split('/')[0] for prefix in aliases['private_prefixes']) == \
            ['desk', 'desk', 'writer', 'writer'], 'the people, by the ship file, are named private'


def test_the_status_measures_freeze_copies_no_native_record_at_all(source, tmp_path):
    frozen = tmp_path / 'frozen' / 'ship'
    result = measure_status_service.frozen_copy(source, frozen)
    assert result['native_records'] == 0
    assert not [path for path in files(frozen) if 'sessions/record.jsonl' in path]
