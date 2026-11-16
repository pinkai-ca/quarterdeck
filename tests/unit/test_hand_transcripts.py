"""Old hand conversations retain their bytes and accounting identities after retirement."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from quarterdeck import hand_transcripts, pr_screen, record_location, record_owner, trusted_files
from quarterdeck.harness import get, reader_stamp
from quarterdeck.harness.base import record_fingerprint
from quarterdeck.harness.opencode import OpenCodeHarness
from quarterdeck.ledger import Ledger
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship, ShipError


def transcript(home, name, kind, at, *, complete=True, task=None):
    if kind == 'pi':
        path = home / 'sessions' / (name + '.jsonl')
        rows = [dict(type='session', id=name, timestamp=at.isoformat(), cwd='/synthetic'),
                dict(type='message', id='u', parentId=None, timestamp=at.isoformat(),
                     message=dict(role='user', content=task or 'Synthetic task ' + name))]
        rows.append(dict(type='message', id='a', parentId='u', timestamp=(at + timedelta(seconds=1)).isoformat(),
            message=dict(role='assistant', model='synthetic-model', provider='synthetic',
                content=[dict(type='text', text='Synthetic answer')], stopReason='stop',
                usage=dict(input=3, output=2, cacheRead=0, cacheWrite=0, totalTokens=5,
                           cost=dict(total=0.25)))))
        if not complete:
            rows.append(dict(type='message', id='u2', parentId='a', timestamp=(at+timedelta(seconds=2)).isoformat(),
                             message=dict(role='user', content='Synthetic unfinished task')))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    else:
        path = home / name / 'opencode' / 'opencode.db'
        path.parent.mkdir(parents=True)
        ms = int(at.timestamp() * 1000)
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE session (id TEXT, time_created INTEGER, time_updated INTEGER, parent_id TEXT);
                CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
                CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);''')
            db.execute('INSERT INTO session VALUES (?,?,?,NULL)', (name, ms, ms))
            db.execute('INSERT INTO message VALUES (?,?,?,?,?)', ('u', name, ms, ms,
                json.dumps(dict(role='user', time=dict(created=ms), model=dict(modelID='synthetic-model')))))
            db.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', ('text', 'u', name, ms, ms,
                json.dumps(dict(type='text', text=task or 'Synthetic task ' + name))))
            db.execute('INSERT INTO message VALUES (?,?,?,?,?)', ('a', name, ms+1000, ms+1000,
                json.dumps(dict(role='assistant', parentID='u', modelID='synthetic-model', finish='stop',
                    time=dict(created=ms+1000, completed=ms+1001), tokens=dict(input=3, output=2), cost=.25))))
            db.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', ('reply', 'a', name, ms+1000, ms+1000,
                json.dumps(dict(type='text', text='Synthetic answer'))))
            if not complete:
                db.execute('INSERT INTO message VALUES (?,?,?,?,?)', ('u2', name, ms+2000, ms+2000,
                    json.dumps(dict(role='user', time=dict(created=ms+2000)))))
                db.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', ('text2', 'u2', name, ms+2000, ms+2000,
                    json.dumps(dict(type='text', text='Synthetic unfinished task'))))
        db.close()
        # Match the native per-launch ancillary folders without importing native content.
        for folder in ('log', 'repos', 'snapshot'):
            (path.parent / folder).mkdir()
            (path.parent / folder / 'synthetic').write_bytes(b'synthetic retained launch data')
    for source in ([path] if kind == 'pi' else [path, *path.parent.rglob('*')]):
        os.utime(source, (at.timestamp(), at.timestamp()))
    return path


def world(tmp_path, monkeypatch, kind='pi', *, complete=True, age=timedelta(days=9), task=None):
    # opened() scans every process of the user, as it must on the live ship. Under xdist other tests run beside these
    # and start processes named after the real programs (node, opencode, pi), which a retirement here would wait for.
    # So each test's readers wait for a program name of the test's own, which only a process it starts can bear.
    import secrets
    from quarterdeck.harness.pi import PiHarness
    world.program = f'qdt-{secrets.token_hex(4)}'  # within the 15 bytes a process name holds
    for reader in (PiHarness, OpenCodeHarness):
        monkeypatch.setattr(reader, 'retirement_process_names', (world.program,))
    now = datetime.now(timezone.utc)
    old = now - age
    spec = SeatSpec('hand', 'crew', 'hand', kind, ('local',), cwd=tmp_path)
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path,
        {'local': Pool('local', 'synthetic-endpoint', endpoint='http://127.0.0.1:1/v1')},
        {'crew': Session('crew', 'crew')}, {'hand': spec})
    home = ship.runtime / 'seats' / 'hand' / 'home'
    trusted = trusted_files.folder(ship.runtime, 'hand')
    trusted.mkdir(parents=True)
    record = transcript(home, 'old' if kind == 'pi' else 'data-001', kind, old, complete=complete, task=task)
    current = transcript(home, 'current' if kind == 'pi' else 'data-002', kind, now)
    launched = SimpleNamespace(at=(now-timedelta(seconds=1)).isoformat(), pool='local', harness=kind, price=None)
    seat = SimpleNamespace(name='hand', folder=home.parent, trusted=trusted, home=home, spec=spec, harness=get(kind),
        session=SimpleNamespace(human=False), record=lambda: current, launched=lambda: launched, pane=lambda: None)
    (trusted / 'record-launches.jsonl').write_text(''.join(json.dumps(dict(record=str(r),
        at=(at-timedelta(seconds=2)).isoformat(), pool='local', harness=kind)) + '\n'
        for r, at in ((record, old), (current, now))))
    for path in (record, current):
        record_owner.register(trusted, path, kind)
    svc = Service(ship, SimpleNamespace())
    monkeypatch.setattr(svc, 'resolved_seats', lambda: [seat])
    logs = []
    monkeypatch.setattr(svc, 'log', lambda event, **data: logs.append((event, data)))
    svc.account_records(process_table={})
    assert not svc.ledger.catching_up()
    return SimpleNamespace(svc=svc, seat=seat, record=record, current=current, now=now.timestamp(), logs=logs)


def retire(w, **options):
    """The retirement's moves, with every guard that declined named in the failure, so a record that stays says why."""
    declined = []
    moves = hand_transcripts.run(w.svc, now=w.now, declined=declined, **options)
    if not moves:
        print('declined:', declined)
    retire.declined = declined
    return moves


def size(folder):
    return sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())


@pytest.mark.parametrize('kind', ['pi', 'opencode'])
def test_accounted_old_record_retires_and_size_falls_current_stays(tmp_path, monkeypatch, kind):
    w = world(tmp_path, monkeypatch, kind)
    try:
        before = size(w.seat.folder)
        native = w.record.read_bytes()
        reading = w.seat.harness.read(w.record)
        inputs = w.seat.harness.attribution_inputs(w.record)
        assert retire(w) == 1, f"declined: {retire.declined}"
        kept = record_location.resolve(w.record)
        assert kept != w.record and not w.record.exists() and kept.read_bytes() == native
        assert size(w.seat.folder) < before and w.current.is_file()
        assert w.seat.harness.read(w.record) == reading
        assert w.seat.harness.attribution_inputs(w.record) == inputs
        assert w.record in w.seat.harness.records(w.seat.home)
        assert any(owned.path == w.record and owned.read() == reading for owned in record_owner.records(w.seat))
        assert hand_transcripts.run(w.svc, now=w.now) == 0
    finally:
        w.svc.ledger.close()


@pytest.mark.parametrize('kind', ['pi', 'opencode'])
@pytest.mark.parametrize('hold', ['unaccounted', 'changed', 'current', 'young', 'open', 'catchup', 'active-planner'])
def test_unfinished_or_ineligible_sources_stay(tmp_path, monkeypatch, kind, hold):
    w = world(tmp_path, monkeypatch, kind, complete=hold != 'open')
    try:
        if hold == 'open':
            assert w.svc.ledger.db.execute('SELECT 1 FROM record_accounting WHERE record=?',
                                          (str(w.record),)).fetchone() == (1,)
        if hold == 'unaccounted':
            with w.svc.ledger.record_pass() as book:
                book.mark_accounted(w.record, None)
        elif hold == 'changed':
            os.utime(w.record, (w.now-8*86400, w.now-8*86400))
        elif hold == 'current':
            w.seat.record = lambda: w.record
        elif hold == 'young':
            os.utime(w.record, (w.now, w.now))
        elif hold == 'catchup':
            w.svc.ledger.begin_catchup(['synthetic-new-epoch'])
        elif hold == 'active-planner':
            w.svc.account_driver = SimpleNamespace(active=True)
        assert retire(w) == 0
        assert w.record.is_file() and record_location.resolve(w.record) == w.record
        named = {'unaccounted': 'not accounted at its checkpoint', 'changed': 'not accounted at its checkpoint',
                 'current': 'it is the current record', 'young': 'written within the week',
                 'catchup': 'the accounting is catching up', 'active-planner': 'an accounting job is open',
                 'open': 'a turn, a tool or a queue is still open'}[hold]
        assert named in [guard for _, _, guard in retire.declined], retire.declined
        held = [data for event, data in w.logs if event == 'hand-transcript-held' and data['guard'] == named]
        assert len(held) == 1, 'logged once'
        retire(w)
        assert len([data for event, data in w.logs if event == 'hand-transcript-held' and data['guard'] == named]) == 1, \
            'not again at the next pass'
    finally:
        w.svc.ledger.close()


@pytest.mark.parametrize('kind', ['pi', 'opencode'])
def test_open_program_descriptor_holds_old_record(tmp_path, monkeypatch, kind):
    w = world(tmp_path, monkeypatch, kind)
    try:
        with w.record.open('rb'):
            assert hand_transcripts.run(w.svc, now=w.now) == 0
        assert retire(w) == 1, f"declined: {retire.declined}"
    finally:
        w.svc.ledger.close()


@pytest.mark.parametrize('kind', ['pi', 'opencode'])
def test_changed_epoch_replay_of_copy_keeps_all_measurements_byte_identical(tmp_path, monkeypatch, kind):
    w = world(tmp_path, monkeypatch, kind)
    book = w.svc.ledger
    try:
        # Keep a real independent copy of the operational ledger, before retiring.
        copy = tmp_path / 'ledger-copy.sqlite'
        with sqlite3.connect(copy) as target:
            book.db.backup(target)
        tables = ('turns', 'tool_calls', 'turn_costs', 'record_launches', 'launch_prices', 'launch_harnesses')
        def rows(db):
            return {t: json.dumps(db.execute(f'SELECT * FROM {t} ORDER BY 1,2').fetchall(),
                                  sort_keys=True).encode() for t in tables}
        expected = rows(book.db)
        def verdicts(db):
            return json.dumps(db.execute('SELECT record,turn,identity,received,status,orders,reason,grouping,valid '
                                         'FROM turn_attribution ORDER BY record,turn').fetchall()).encode()
        expected_verdicts = verdicts(book.db)
        assert retire(w) == 1, f"declined: {retire.declined}"
        book.close()
        w.svc.ledger = book = Ledger(copy)
        # Fresh caches and a changed reader/attribution epoch force actual native replay.
        w.svc.read_marks.clear()
        w.svc.accounting_epoch = ['synthetic-changed-reader-and-attribution-epoch']
        book.begin_catchup(w.svc.accounting_epoch)
        keys = {name: ['synthetic-changed-reader', value] for name, value in reader_stamp.readers().items()}
        monkeypatch.setattr(reader_stamp, 'attributions', lambda: keys)
        w.svc.ledger.attributions.last = None
        w.svc.account_records(process_table={}, source_reader_keys=keys)
        assert not book.catching_up()
        assert rows(book.db) == expected
        assert verdicts(book.db) == expected_verdicts
        assert book.db.execute('SELECT * FROM records').fetchall() == []
        assert book.db.execute('SELECT COUNT(*) FROM turn_attribution WHERE valid=0').fetchone() == (0,)
        # Measurements are also idempotent on a second complete accounting pass.
        w.svc.account_records(process_table={}, source_reader_keys=keys)
        assert rows(book.db) == expected
        # A wholly new ledger also rediscovers and replays retained sources.
        book.close()
        w.svc.ledger = book = Ledger(tmp_path / 'rebuilt-ledger.sqlite')
        book.begin_catchup(w.svc.accounting_epoch)
        w.svc.read_marks.clear()
        w.svc.account_records(process_table={}, source_reader_keys=keys)
        assert rows(book.db) == expected
        assert verdicts(book.db) == expected_verdicts
    finally:
        book.close()


def test_sqlite_bundle_moves_once_with_wal_and_shm(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, 'opencode')
    try:
        # Empty native sidecars are valid; no live SQLite connection holds them.
        for suffix in ('-wal', '-shm'):
            path = Path(str(w.record) + suffix)
            path.write_bytes(b'')
            os.utime(path, (w.now-9*86400, w.now-9*86400))
        w.svc.read_marks.clear()
        w.svc.account_records(process_table={})
        original = {p.relative_to(w.record.parent): p.read_bytes()
                    for p in w.record.parent.rglob('*') if p.is_file()}
        moves = []
        rename = Path.rename
        def observed(source, target):
            moves.append((source, target))
            return rename(source, target)
        monkeypatch.setattr(Path, 'rename', observed)
        assert retire(w) == 1, f"declined: {retire.declined}"
        kept = record_location.resolve(w.record)
        assert moves == [(w.record.parent, kept.parent)]
        assert {p.relative_to(kept.parent): p.read_bytes() for p in kept.parent.rglob('*') if p.is_file()} == original
        assert OpenCodeHarness._data_dir(w.seat.home, None, 'hand').name == 'data-003'
        with pytest.raises(ShipError, match='conversation is retired'):
            OpenCodeHarness._data_dir(w.seat.home, 'data-001', 'hand')
        assert w.seat.harness.join(w.seat.home, datetime(2000,1,1,tzinfo=timezone.utc)).record == w.current
    finally:
        w.svc.ledger.close()


def test_pi_retired_conversation_refuses_native_resume(tmp_path, monkeypatch):
    from quarterdeck.endpoint import Choice
    w = world(tmp_path, monkeypatch)
    try:
        assert retire(w) == 1, f"declined: {retire.declined}"
        monkeypatch.setattr(Choice, 'read', lambda home: SimpleNamespace(model='synthetic-model'))
        with pytest.raises(ShipError, match='conversation is retired'):
            w.seat.harness.launch(w.seat.spec, SimpleNamespace(name='synthetic'), w.seat.home,
                                 tmp_path / 'brief', 'old', None)
    finally:
        w.svc.ledger.close()


@pytest.mark.parametrize('kind', ['pi', 'opencode'])
def test_unknown_reader_stays_and_retired_binding_cannot_be_rejoined(tmp_path, monkeypatch, kind):
    from quarterdeck.provenance import remember_launch
    from quarterdeck.seat import Seat
    w = world(tmp_path, monkeypatch, kind)
    try:
        record_owner.register(w.seat.trusted, w.record, 'fake')
        assert hand_transcripts.run(w.svc, now=w.now) == 0
        assert w.record.is_file()
        # A separate eligible record exercises both logical and physical retired bindings.
        name = 'eligible' if kind == 'pi' else 'data-003'
        old = datetime.fromtimestamp(w.now, timezone.utc) - timedelta(days=9)
        eligible = transcript(w.seat.home, name, kind, old)
        record_owner.register(w.seat.trusted, eligible, kind)
        with (w.seat.trusted / 'record-launches.jsonl').open('a') as journal:
            journal.write(json.dumps(dict(record=str(eligible), at=(old-timedelta(seconds=2)).isoformat(),
                                          pool='local', harness=kind)) + '\n')
        w.svc.account_records(process_table={})
        assert retire(w) == 1, f"declined: {retire.declined}"
        kept = record_location.resolve(eligible)
        for path in (eligible, kept):
            assert record_location.retired(path)
            with pytest.raises(ShipError, match='retired'):
                remember_launch(w.seat.trusted, path, w.seat.launched())
            (w.seat.trusted / 'record').write_text(str(path)+'\n')
            with pytest.raises(ShipError, match='conversation is retired'):
                Seat._resume_id(w.seat)
    finally:
        w.svc.ledger.close()


def test_busy_planner_does_not_delay_next_retirement_opportunity(monkeypatch):
    svc = SimpleNamespace(ledger=SimpleNamespace(catching_up=lambda: False),
                          account_driver=SimpleNamespace(active=True))
    calls = []
    monkeypatch.setattr(hand_transcripts, 'run', lambda service: calls.append(service))
    monkeypatch.setattr(hand_transcripts.time, 'monotonic', lambda: 1000)
    hand_transcripts.due(svc)
    assert calls == [] and not hasattr(svc, 'hand_transcripts_due')
    svc.account_driver.active = False
    hand_transcripts.due(svc)
    assert calls == [svc] and svc.hand_transcripts_due == 1300
    hand_transcripts.due(svc)
    assert calls == [svc]


def test_without_ledger_retirement_step_does_nothing_and_reads_nothing(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('retirement inspected runtime without a ledger')
    monkeypatch.setattr(hand_transcripts.time, 'monotonic', unexpected)
    monkeypatch.setattr(hand_transcripts.reader_stamp, 'readers', unexpected)
    monkeypatch.setattr(Path, 'iterdir', unexpected)
    monkeypatch.setattr(record_location, 'destination', unexpected)
    service = SimpleNamespace()
    Service.retire_hand_transcripts(service)
    hand_transcripts.due(service)
    assert hand_transcripts.run(service) == 0
    assert vars(service) == {}


# -- a screening launch's record (pr_screen) ------------------------------------------------------------------------

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'pr_screen'


def case_strings(name):
    """The planted case's own distinctive strings: its title and each line of its files longer than twenty letters."""
    case = json.loads((FIXTURES / name / 'pr.json').read_text())
    return [case['title'], *(line[1:].strip() for f in case['files'] for line in f['content'].splitlines()
                             if line.startswith('+') and len(line.strip()) > 20)]


def holds(home, strings):
    return sorted(str(path.relative_to(home)) for path in home.rglob('*') if path.is_file()
                  and any(s.encode() in path.read_bytes() for s in strings))


@pytest.mark.parametrize('kind', ['pi', 'opencode'])
def test_a_screening_record_leaves_the_home_once_accounted_without_the_weeks_wait(tmp_path, monkeypatch, kind):
    strings = case_strings('note-elsewhere')
    w = world(tmp_path, monkeypatch, kind, age=timedelta(minutes=2), task='\n'.join(strings))
    try:
        assert holds(w.seat.home, strings), 'the screening launch read the case'
        reading = w.seat.harness.read(w.record)
        assert hand_transcripts.run(w.svc, now=w.now) == 0, 'an ordinary record two minutes old waits its week'
        pr_screen.mark_screening(w.svc.runtime, 'hand', [str(w.record)])
        assert pr_screen.screening_left(w.svc.runtime, 'hand')
        assert retire(w) == 1, f"declined: {retire.declined}"
        assert holds(w.seat.home, strings) == [], 'nothing under the hand\'s home holds the case\'s text'
        kept = record_location.resolve(w.record)
        assert kept.is_relative_to(w.svc.runtime / 'retired' / 'hand-transcripts')
        assert w.seat.harness.read(w.record) == reading, 'the ledger reads it under its own name, figures kept'
        assert not pr_screen.screening_left(w.svc.runtime, 'hand'), 'nothing left: launches may resume again'
        hand_transcripts.run(w.svc, now=w.now)
        assert not (w.svc.runtime / 'pr-screen' / 'records' / 'hand.json').exists(), 'the spent list is deleted'
    finally:
        w.svc.ledger.close()


def trusted_launch(w, number):
    """The seat's current launch as qd's trusted launch record names it: the data folder it was given."""
    (w.seat.trusted / trusted_files.LAUNCH).write_text(json.dumps(dict(
        pane='%1', pid=1, at=datetime.fromtimestamp(w.now, timezone.utc).isoformat(), pool='local', resume=None,
        argv=['bwrap', '--setenv', 'XDG_DATA_HOME', str(w.seat.home / f'data-{number:03d}'), '--', 'opencode'],
        version=None)))


def cut_mid_turn(w, *, number=0, listed=True, age=timedelta(minutes=2), task='Synthetic screening instruction',
                 current=2):
    """A launch's OpenCode record that the screen's down cut mid-turn, as a hand's were on 15 November: its last step
    has no finish, and its tool part still reads running. ``number`` is its launch's: 0 is before the current one."""
    at = datetime.fromtimestamp(w.now, timezone.utc) - age
    record = w.seat.home / f'data-{number:03d}' / 'opencode' / 'opencode.db'
    record.parent.mkdir(parents=True)
    ms = int(at.timestamp() * 1000)
    with sqlite3.connect(record) as db:
        db.executescript('''CREATE TABLE session (id TEXT, time_created INTEGER, time_updated INTEGER, parent_id TEXT);
            CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);''')
        db.execute('INSERT INTO session VALUES (?,?,?,NULL)', ('cut', ms, ms))
        db.execute('INSERT INTO message VALUES (?,?,?,?,?)', ('u', 'cut', ms, ms,
            json.dumps(dict(role='user', time=dict(created=ms), model=dict(modelID='synthetic-model')))))
        db.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', ('text', 'u', 'cut', ms, ms,
            json.dumps(dict(type='text', text=task))))
        db.execute('INSERT INTO message VALUES (?,?,?,?,?)', ('a', 'cut', ms + 1000, ms + 1000,
            json.dumps(dict(role='assistant', parentID='u', modelID='synthetic-model', time=dict(created=ms + 1000),
                            tokens=dict(input=30, output=7, reasoning=0, total=37, cache=dict(read=0, write=0)),
                            cost=.25))))
        db.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', ('call', 'a', 'cut', ms + 1000, ms + 1000,
            json.dumps(dict(type='tool', tool='read', state=dict(status='running', time=dict(start=ms + 1000))))))
    db.close()
    for source in [record, *record.parent.rglob('*')]:
        os.utime(source, (at.timestamp(), at.timestamp()))
    with (w.seat.trusted / trusted_files.LAUNCHES).open('a') as stream:
        stream.write(json.dumps(dict(record=str(record), at=(at - timedelta(seconds=2)).isoformat(), pool='local',
                                     harness='opencode')) + '\n')
    record_owner.register(w.seat.trusted, record, 'opencode')
    trusted_launch(w, current)
    w.svc.account_records(process_table={})
    if listed:
        pr_screen.mark_screening(w.svc.runtime, 'hand', [str(record)])
    return record


def test_a_cut_turn_with_a_tool_pending_is_counted_with_its_tokens_once_a_later_launch_exists(tmp_path, monkeypatch):
    """Its launch has ended (data-002 came after it): the turn closes untimed at its last step, its tool noted as
    left running, and the ledger counts it as far as the record shows. Listed or not, the reading is the same."""
    w = world(tmp_path, monkeypatch, 'opencode')
    try:
        record = cut_mid_turn(w, listed=False)
        state, turns = w.seat.harness.read(record)
        assert state.turn_open is False and state.pending_tools == () and len(turns) == 1
        [turn] = turns
        assert not turn.timed and turn.tokens.written == 7 and [t.name for t in turn.tools] == ['read']
        assert turn.tools[0].failed is None, 'left running: its end unknown'
        assert w.svc.ledger.ingested(record) == 1, 'counted, not dropped'
    finally:
        w.svc.ledger.close()


def test_the_same_record_while_its_launch_is_the_latest_keeps_its_turn_open(tmp_path, monkeypatch):
    """No later launch of the home exists: nothing is closed by guess."""
    w = world(tmp_path, monkeypatch, 'opencode')
    try:
        record = cut_mid_turn(w, number=9, listed=False)  # after data-002: the latest launch
        state, turns = w.seat.harness.read(record)
        assert state.turn_open is True and state.pending_tools == ('read',) and turns == []
        assert w.svc.ledger.ingested(record) == 0
    finally:
        w.svc.ledger.close()


def test_a_forged_later_data_folder_leaves_the_current_launchs_turn_open(tmp_path, monkeypatch):
    """Security's follow-up: a folder the hand made in its home, an empty later data-<n>, counts for nothing. Only a
    launch qd's trusted record names closes a turn."""
    w = world(tmp_path, monkeypatch, 'opencode')
    try:
        record = cut_mid_turn(w, number=9, listed=False, current=9)  # its own launch is the current one
        forged = w.seat.home / 'data-999' / 'opencode' / 'opencode.db'
        forged.parent.mkdir(parents=True)
        forged.write_bytes(b'')  # planted by the hand: no launch of qd's
        w.svc.account_records(process_table={})
        state, turns = w.seat.harness.read(record)
        assert state.turn_open is True and state.pending_tools == ('read',) and turns == []
        assert w.svc.ledger.ingested(record) == 0, 'its accounting is not steered'
        trusted_launch(w, 10)  # a launch qd gave a later data folder
        state, turns = w.seat.harness.read(record)
        assert state.turn_open is False and len(turns) == 1
    finally:
        w.svc.ledger.close()


def test_an_accounted_record_is_read_again_when_a_later_launch_begins(tmp_path, monkeypatch):
    """Accounted while its launch was the latest, it is read once more when its successor starts, so its cut turn is
    counted then: the later launch is part of an OpenCode record's stamp."""
    w = world(tmp_path, monkeypatch, 'opencode')
    try:
        record = cut_mid_turn(w, number=9, listed=False)
        assert w.svc.ledger.ingested(record) == 0
        transcript(w.seat.home, 'data-010', 'opencode', datetime.fromtimestamp(w.now, timezone.utc))  # its successor
        trusted_launch(w, 10)  # qd's trusted record now names the later launch
        w.svc.account_records(process_table={})
        assert w.svc.ledger.ingested(record) == 1
    finally:
        w.svc.ledger.close()


def test_a_listed_screening_record_cut_mid_turn_retires_only_once_that_accounting_is_in(tmp_path, monkeypatch):
    """Security's gate: a hand cut off by the screen mid-turn, a tool call pending, has its record retired with no hand
    on it once its accounting is through; then nothing under its home holds the case's text."""
    strings = case_strings('note-elsewhere')
    w = world(tmp_path, monkeypatch, 'opencode')
    try:
        record = cut_mid_turn(w, task='\n'.join(strings))
        assert holds(w.seat.home, strings), 'the screening launch read the case'
        real = w.svc.ledger.accounted
        monkeypatch.setattr(w.svc.ledger, 'accounted',
                            lambda checked, checkpoint: checked != record and real(checked, checkpoint))
        hand_transcripts.run(w.svc, now=w.now, limit=0)
        assert record.is_file(), 'not yet accounted: it waits for that'
        monkeypatch.setattr(w.svc.ledger, 'accounted', real)
        assert w.svc.ledger.ingested(record) == 1, 'the cut turn is in the ledger first'
        assert hand_transcripts.run(w.svc, now=w.now, limit=0) == 1 and not record.exists()
        assert holds(w.seat.home, strings) == [], 'nothing under the hand\'s home holds the case\'s text'
        assert record_location.resolve(record).is_relative_to(w.svc.runtime / 'retired' / 'hand-transcripts')
        assert w.svc.ledger.ingested(record) == 1, 'the ledger keeps its keys'
    finally:
        w.svc.ledger.close()


def test_a_listed_cut_record_held_open_by_a_process_stays(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, 'opencode')
    try:
        record = cut_mid_turn(w)
        monkeypatch.setattr(hand_transcripts, 'opened', lambda files, names=None: True)  # a process holds it
        assert hand_transcripts.run(w.svc, now=w.now, limit=0) == 0 and record.is_file()
    finally:
        w.svc.ledger.close()


def test_a_screening_record_leaves_even_before_the_next_launch_has_a_record(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, 'pi', age=timedelta(minutes=2))
    try:
        monkeypatch.setattr(w.seat, 'record', lambda: None)  # the fresh launch after the screen has written nothing yet
        assert hand_transcripts.run(w.svc, now=w.now) == 0, 'without a screening record the hand waits, as before'
        pr_screen.mark_screening(w.svc.runtime, 'hand', [str(w.record)])
        assert retire(w) == 1 and not w.record.exists(), f"declined: {retire.declined}"
        assert w.current.is_file(), 'only the screening record moves'
    finally:
        w.svc.ledger.close()


@pytest.mark.parametrize('kind', ['pi', 'opencode'])
def test_the_home_sweep_keeps_a_screening_records_whole_unit_for_the_retirement(tmp_path, monkeypatch, kind):
    w = world(tmp_path, monkeypatch, kind, age=timedelta(minutes=2))
    try:
        live = pr_screen.LiveHands(w.svc.ship, Path('qd'), tmp_path / 'hands')
        assert live.home('hand') == w.seat.home
        kept = live.kept_in_home('hand', [str(w.record)])
        unit = w.record if kind == 'pi' else w.record.parent
        assert kept == [str(unit.relative_to(w.seat.home))], 'the adapter\'s own retirement unit, whole'
        if kind == 'opencode':
            (w.record.parent / 'planted.txt').write_text('written by a fooled hand')
            (w.record.parent / 'opencode.db-wal').write_bytes(b'wal')
            kept = live.kept_in_home('hand', [str(w.record)])
            assert sorted(kept) == sorted(str(p.relative_to(w.seat.home)) for p in
                                          (w.record, w.record.parent / 'opencode.db-wal'))
            before = {k: v for k, v in pr_screen.folder_listing(w.seat.home).items() if 'planted' not in k
                      and 'log' not in k and 'repos' not in k and 'snapshot' not in k}
            moved = pr_screen.move_aside_new(w.seat.home, before, tmp_path / 'aside', keep=kept)
            assert w.record.is_file() and (w.record.parent / 'opencode.db-wal').is_file()
            assert not (w.record.parent / 'planted.txt').exists() and moved
            assert w.seat.harness.retirement_bundle(w.seat.home, w.record, record_location.destination(w.record))
    finally:
        w.svc.ledger.close()


def screening_beside(w, kind='pi'):
    """A second earlier launch's record, two minutes old, listed as a screening launch's; accounted."""
    at = datetime.fromtimestamp(w.now, timezone.utc) - timedelta(minutes=2)
    record = transcript(w.seat.home, 'screened' if kind == 'pi' else 'data-003', kind, at)
    with (w.seat.trusted / trusted_files.LAUNCHES).open('a') as stream:  # where the move keeps it
        stream.write(json.dumps(dict(record=str(record), at=(at - timedelta(seconds=2)).isoformat(), pool='local',
                                     harness=kind)) + '\n')
    record_owner.register(w.seat.trusted, record, kind)
    w.svc.account_records(process_table={})
    pr_screen.mark_screening(w.svc.runtime, 'hand', [str(record)])
    return record


def test_a_listed_screening_record_moves_ahead_of_an_older_eligible_one_outside_the_limit(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, 'pi')  # its old record is nine days old: eligible
    try:
        screened = screening_beside(w)
        assert hand_transcripts.run(w.svc, now=w.now, limit=1) == 2, 'the screening walk is outside the limit'
        moved = [data['record'] for event, data in w.logs if event == 'hand-transcript-retired']
        assert moved == [str(screened), str(w.record)], 'the screening record first'
        assert [data.get('screening') for event, data in w.logs if event == 'hand-transcript-retired'] == [True, None]
    finally:
        w.svc.ledger.close()


def test_a_screening_record_moves_through_a_catch_up_and_between_intervals(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, 'pi')
    try:
        screened = screening_beside(w)
        monkeypatch.setattr(w.svc.ledger, 'catching_up', lambda: True)
        w.svc.hand_transcripts_due = float('inf')  # the five-minute interval has not come round
        hand_transcripts.due(w.svc)
        assert not screened.exists() and w.record.is_file(), 'the screening record alone, at once'
    finally:
        w.svc.ledger.close()


def test_a_screening_record_not_yet_accounted_waits_for_that_alone(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, 'pi')
    try:
        screened = screening_beside(w)
        real = w.svc.ledger.accounted
        monkeypatch.setattr(w.svc.ledger, 'accounted',
                            lambda record, checkpoint: record != screened and real(record, checkpoint))
        hand_transcripts.run(w.svc, now=w.now, limit=0)
        assert screened.is_file(), 'not yet accounted: it waits'
        monkeypatch.setattr(w.svc.ledger, 'accounted', real)  # undo() would drop the world's own patches
        assert hand_transcripts.run(w.svc, now=w.now, limit=0) == 1 and not screened.exists(), 'and for that alone'
        assert w.record.is_file(), 'limit 0: the screening walk alone' 
    finally:
        w.svc.ledger.close()


NAMED = r"""
import ctypes, sys
ctypes.CDLL(None).prctl(15, sys.argv[1].encode(), 0, 0, 0)  # PR_SET_NAME: the program name this test's reader waits for
"""

# The leader alone exits while a thread of its keeps the record open, as a node or bun process with workers can.
LEADER_EXITS = NAMED + r"""
import threading, time
held = open(sys.argv[2], 'rb')
started = threading.Event()
def keep():
    started.set()
    time.sleep(120)
threading.Thread(target=keep).start()
started.wait()
print('ready', flush=True)
ctypes.CDLL(None).pthread_exit(None)
"""


def task_states(pid):
    try:
        return {task: Path(f'/proc/{pid}/task/{task}/stat').read_text().rsplit(')', 1)[1].split()[0]
                for task in os.listdir(f'/proc/{pid}/task')}
    except FileNotFoundError:
        return {}


def until(predicate, bound=30.0):
    """Wait for the event itself; the bound only ends a test whose event never comes."""
    import time
    end = time.monotonic() + bound
    while not predicate():
        assert time.monotonic() < end, 'the awaited state never came'
        time.sleep(0.01)


def test_a_zombie_named_after_the_program_holds_nothing_and_the_record_moves(tmp_path, monkeypatch):
    """A child that has exited and is not yet reaped refuses its descriptor listing, but holds no descriptor: it no
    longer keeps an old record in the home (the runner's intermittent 0 moves, where other tests' fake programs
    named node or opencode were caught in that window)."""
    import subprocess, sys
    w = world(tmp_path, monkeypatch, 'opencode')
    name = world.program  # this test's own: no other test's retirement waits for it
    zombie = subprocess.Popen([sys.executable, '-I', '-c', NAMED, name])
    try:
        until(lambda: set(task_states(zombie.pid).values()) == {'Z'})
        with pytest.raises(PermissionError):
            os.listdir(f'/proc/{zombie.pid}/fd')
        assert Path(f'/proc/{zombie.pid}/comm').read_text().strip() == name
        assert not hand_transcripts.opened([w.record], (name,))
        assert retire(w) == 1, f"declined: {retire.declined}"
    finally:
        zombie.wait()
        w.svc.ledger.close()


def test_a_leader_that_exits_while_a_thread_holds_the_record_still_holds_the_move(tmp_path, monkeypatch):
    import subprocess, sys
    w = world(tmp_path, monkeypatch, 'opencode')
    name = world.program  # this test's own: no other test's retirement waits for it
    process = subprocess.Popen([sys.executable, '-I', '-c', LEADER_EXITS, name, str(w.record)],
                               stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'ready'
        until(lambda: task_states(process.pid).get(str(process.pid)) == 'Z')
        assert 'Z' in task_states(process.pid).values() and set(task_states(process.pid).values()) != {'Z'}
        with pytest.raises(PermissionError):
            os.listdir(f'/proc/{process.pid}/fd')
        assert hand_transcripts.opened([w.record], (name,))
        assert retire(w) == 0
        assert ('hand', str(w.record), 'a process may hold its files') in retire.declined
        assert w.record.is_file()
    finally:
        process.kill()
        process.wait()
        w.svc.ledger.close()


LAUNCH_PROGRAM = r'''
import json, os, sqlite3, sys, time
from pathlib import Path
folder, record, go, at = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])
tool_output = sys.argv[5] == 'yes'
while not go.exists():
    time.sleep(0.02)
text = '\n'.join(path.read_text() for piece in sorted(folder.glob('screen-*')) for path in sorted(piece.glob('f*.txt')))
record.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(record) as db:
    db.executescript("""CREATE TABLE session (id TEXT, time_created INTEGER, time_updated INTEGER, parent_id TEXT);
        CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER,
                           data TEXT);""")
    db.execute('INSERT INTO session VALUES (?,?,?,NULL)', ('screen', at, at))
    db.execute('INSERT INTO message VALUES (?,?,?,?,?)', ('u', 'screen', at, at,
        json.dumps(dict(role='user', time=dict(created=at), model=dict(modelID='synthetic-model')))))
    db.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', ('text', 'u', 'screen', at, at,
        json.dumps(dict(type='text', text=text))))
    db.execute('INSERT INTO message VALUES (?,?,?,?,?)', ('a', 'screen', at + 1000, at + 1000,
        json.dumps(dict(role='assistant', parentID='u', modelID='synthetic-model', time=dict(created=at + 1000),
                        tokens=dict(input=30, output=7, reasoning=0, total=37, cache=dict(read=0, write=0)), cost=0))))
    db.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', ('call', 'a', 'screen', at + 1000, at + 1000,
        json.dumps(dict(type='tool', tool='read', state=dict(status='running', time=dict(start=at + 1000))))))
db.close()
if tool_output:  # OpenCode keeps a large tool result in its launch's own tool-output folder
    (record.parent / 'tool-output').mkdir()
    (record.parent / 'tool-output' / 'tool_synthetic').write_text(text)
(go.parent / 'written').touch()
time.sleep(600)
'''


@pytest.mark.parametrize('tool_output', ['no', 'yes'])
def test_a_screen_killed_just_after_its_piece_leaves_the_record_listed_and_the_walk_retires_it(tmp_path, monkeypatch,
                                                                                               tool_output):
    """Security's gate for the listing written before a piece goes in, on a throwaway hand. The screen program is
    killed (SIGKILL) the moment the piece is written; the hand's launch, which outlives it, writes its record holding
    the piece's text only after that. The record is listed all the same, the hand's next launch starts fresh, the
    service's walk retires it with no hand on it once a later launch exists, and then nothing under the home holds
    the case's text."""
    import multiprocessing
    import signal
    import subprocess
    import sys
    w = world(tmp_path, monkeypatch, 'opencode')
    runtime, home = w.svc.runtime, w.seat.home
    hands_root, signals = tmp_path / 'hands', tmp_path / 'signals'
    signals.mkdir()
    named = home / 'data-003' / 'opencode' / 'opencode.db'
    at_ms = int((w.now - 120) * 1000)

    class Throwaway(pr_screen.LiveHands):
        def __init__(self):
            super().__init__(SimpleNamespace(runtime=runtime, seat=lambda name: SimpleNamespace(name='hand')), Path('qd'),
                             hands_root)
            self.launch = None

        def home(self, name):
            return home

        def records(self, name):
            return get('opencode').records(home)

        def fresh(self, name):  # as qd's fresh start: a launch, and its trusted launch record naming its data folder
            self.folder(name).mkdir(parents=True, exist_ok=True)
            self.launch = subprocess.Popen([sys.executable, '-I', '-c', LAUNCH_PROGRAM, str(self.folder(name)),
                                            str(named), str(signals / 'go'), str(at_ms), tool_output],
                                           start_new_session=True)
            start = Path(f'/proc/{self.launch.pid}/stat').read_text().rpartition(')')[2].split()[19]
            (w.seat.trusted / trusted_files.LAUNCH).write_text(json.dumps(dict(
                pane='%1', pid=self.launch.pid, start=start, harness='opencode', pool='local', resume=None,
                at=datetime.fromtimestamp(w.now, timezone.utc).isoformat(), version=None,
                argv=['bwrap', '--setenv', 'XDG_DATA_HOME', str(home / 'data-003'), '--', 'opencode'])))
            (signals / 'launch').write_text(json.dumps([self.launch.pid, start]))

        def down(self, name):
            raise AssertionError('the screen is killed before it reaches its down')

    case = 'note-elsewhere'
    source = pr_screen.FixtureSource(FIXTURES, [case])
    [pr] = source.open()
    prepared = pr_screen.prepare(source.texts(pr))
    numbers = next(iter(pr_screen.pieces(prepared)))
    corpus = pr_screen.load_corpus(FIXTURES / 'planted-test-corpus.json')

    def screen():  # the screen program, in a process of its own, killed the moment its piece is written
        real = pr_screen._write_piece

        def then_killed(*args):
            written = real(*args)
            os.kill(os.getpid(), signal.SIGKILL)
            return written
        pr_screen._write_piece = then_killed
        pr_screen.screen_piece(runtime, Throwaway(), 'hand', 'gate', 0, prepared, numbers, corpus=corpus,
                               answer_seconds=30, poll=0.05)
    child = multiprocessing.get_context('fork').Process(target=screen)
    child.start()
    child.join(120)
    launch = json.loads((signals / 'launch').read_text())
    try:
        assert child.exitcode == -signal.SIGKILL, 'killed by SIGKILL just after the piece was written'
        assert list(hands_root.glob('hand/screen-*/f*.txt')), 'the piece was written before the kill'
        assert not named.exists(), 'the launch has not written its record yet'
        assert pr_screen.screening_records(runtime, 'hand') == [str(named)], 'listed though it does not exist yet'
        (signals / 'go').touch()  # the hand's launch, outliving the screen, now writes its record with the piece
        deadline = datetime.now().timestamp() + 60
        while not (signals / 'written').exists() and datetime.now().timestamp() < deadline:
            __import__('time').sleep(0.02)
        strings = case_strings(case)
        assert holds(home, strings), 'the record written after the kill holds the case\'s text'
        if tool_output == 'yes':
            assert any('tool-output' in path for path in holds(home, strings)), 'and so does its tool output'
            assert w.seat.harness.retirement_bundle(home, named, record_location.destination(named)), 'whole'
        assert pr_screen.screening_left(runtime, 'hand'), 'so the hand\'s next launch starts fresh, never resumed'
    finally:
        os.killpg(launch[0], signal.SIGKILL)  # the next fresh start ends the old launch
        try:
            os.waitpid(launch[0], 0)
        except ChildProcessError:
            pass
    try:
        # qd joins the record to its launch, and the next fresh launch is named in the trusted record: data-004.
        with (w.seat.trusted / trusted_files.LAUNCHES).open('a') as stream:
            stream.write(json.dumps(dict(record=str(named), at=datetime.fromtimestamp(at_ms / 1000 - 2, timezone.utc)
                                         .isoformat(), pool='local', harness='opencode')) + '\n')
        record_owner.register(w.seat.trusted, named, 'opencode')
        trusted_launch(w, 4)
        w.svc.account_records(process_table={})
        assert w.svc.ledger.ingested(named) == 1, 'its cut turn is accounted'
        assert hand_transcripts.run(w.svc, now=w.now, limit=0) == 1 and not named.exists(), 'retired by the walk'
        assert holds(home, strings) == [], 'nothing under the hand\'s home holds the case\'s text'
        assert record_location.resolve(named).is_relative_to(runtime / 'retired' / 'hand-transcripts')
        assert not pr_screen.screening_left(runtime, 'hand')
    finally:
        w.svc.ledger.close()


def test_a_launchs_tool_output_folder_moves_with_its_record(tmp_path, monkeypatch):
    """OpenCode keeps a large tool result in the launch's own tool-output folder: per-launch data of that record, so
    the retirement moves it with the record, and its text leaves the home with it."""
    w = world(tmp_path, monkeypatch, 'opencode')
    try:
        output = w.record.parent / 'tool-output' / 'tool_synthetic'
        output.parent.mkdir()
        output.write_text('a large tool result of the old launch')
        os.utime(output, (w.now - 9 * 86400, w.now - 9 * 86400))
        os.utime(output.parent, (w.now - 9 * 86400, w.now - 9 * 86400))
        assert w.seat.harness.retirement_bundle(w.seat.home, w.record, record_location.destination(w.record))
        assert retire(w) == 1, f"declined: {retire.declined}"
        kept = record_location.resolve(w.record)
        assert (kept.parent / 'tool-output' / 'tool_synthetic').read_text() == 'a large tool result of the old launch'
        assert not output.exists()
    finally:
        w.svc.ledger.close()


def tool_output_world(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, 'opencode')
    return w, w.record.parent / 'tool-output'


def bundle_of(w):
    return w.seat.harness.retirement_bundle(w.seat.home, w.record, record_location.destination(w.record))


def test_a_tool_output_folder_reached_through_a_link_refuses_the_bundle_and_stays(tmp_path, monkeypatch):
    """A tool-output that is a link (to a folder shared across launches, or anywhere else) is never taken: the
    bundle is refused, the walk holds the record, and the link and what it names stay where they are."""
    w, output = tool_output_world(tmp_path, monkeypatch)
    try:
        shared = w.seat.home / 'shared-tool-output'
        shared.mkdir()
        (shared / 'tool_shared').write_text('a result of another launch')
        output.symlink_to(shared)
        assert bundle_of(w) is None
        assert retire(w) == 0 and w.record.is_file() and output.is_symlink()
        assert (shared / 'tool_shared').read_text() == 'a result of another launch'
    finally:
        w.svc.ledger.close()


def test_a_link_inside_the_tool_output_folder_refuses_the_bundle(tmp_path, monkeypatch):
    w, output = tool_output_world(tmp_path, monkeypatch)
    try:
        output.mkdir()
        outside = w.seat.home / 'elsewhere.txt'
        outside.write_text('not this launch\'s')
        (output / 'tool_link').symlink_to(outside)
        assert bundle_of(w) is None
        assert retire(w) == 0 and w.record.is_file() and (output / 'tool_link').is_symlink() and outside.is_file()
    finally:
        w.svc.ledger.close()


def test_a_tool_output_folder_outside_the_launchs_own_data_folder_is_never_taken(tmp_path, monkeypatch):
    """The bundle is this launch's own opencode directory, moved whole: a tool-output anywhere else in the home (one
    shared across launches) is not part of it, and stays where it is when the record moves."""
    w, output = tool_output_world(tmp_path, monkeypatch)
    try:
        output.mkdir()
        (output / 'tool_own').write_text('this launch\'s own result')
        shared = w.seat.home / '.local' / 'share' / 'opencode' / 'tool-output'
        shared.mkdir(parents=True)
        (shared / 'tool_shared').write_text('a result kept across launches')
        for path in (output / 'tool_own', output):
            os.utime(path, (w.now - 9 * 86400, w.now - 9 * 86400))  # as old as the record it belongs to
        folder, _, files = bundle_of(w)
        assert folder == w.record.parent and all(Path(f).is_relative_to(w.record.parent) for f in files)
        assert retire(w) == 1, f"declined: {retire.declined}"
        assert (shared / 'tool_shared').read_text() == 'a result kept across launches'
        assert (record_location.resolve(w.record).parent / 'tool-output' / 'tool_own').is_file()
    finally:
        w.svc.ledger.close()


@pytest.mark.skipif(os.geteuid() == 0, reason='a folder its owner cannot read needs a user that is not root')
def test_a_tool_output_folder_it_cannot_read_holds_the_record_and_is_never_dropped(tmp_path, monkeypatch):
    w, output = tool_output_world(tmp_path, monkeypatch)
    try:
        output.mkdir()
        (output / 'tool_unread').write_text('a result the walk cannot read')
        output.chmod(0)
        try:
            assert bundle_of(w) is None
            assert retire(w) == 0 and w.record.is_file()
        finally:
            output.chmod(0o700)
        assert (output / 'tool_unread').read_text() == 'a result the walk cannot read', 'never dropped'
    finally:
        w.svc.ledger.close()
