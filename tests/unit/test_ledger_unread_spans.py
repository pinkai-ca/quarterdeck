"""The ledger shows each span of a record it could not read within the reader's limits.

Every turn is accounted, or counted unattributed and reported (law: every-turn-accounted). A span the reader never
read is neither unless the ledger itself shows it: a row of its own with the seat, the record, the pool, its size, the
bound, when it was first seen past the bound and the turns counted before it. Its turns since are unknown, and no
figure is made for them. The table is additive: existing rows and readers are unchanged.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from quarterdeck import human_wake, ledger, ledger_reading, post, record_owner, trusted_files
from quarterdeck.harness import claude_record, get, json_record, record_file
from quarterdeck.ledger import Ledger
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from tests.compaction_shapes import AT, Native

from .test_ledger import accounting_example, sql_writes
from .test_claude_picture_projection import begin_tool, image, read_mirror, result, text

MiB = 1024 * 1024


def test_a_span_is_kept_with_its_figures_and_never_a_figure_for_its_turns(tmp_path):
    path, record = tmp_path / 'ledger.sqlite', tmp_path / 'record-z'
    with Ledger(path) as book:
        book.mark_unread_span(record, 'worker-z', 'pool-z', 130 * MiB, 128 * MiB, '2026-11-15T21:00:00+00:00')
        [span] = book.unread_spans()
        assert span == dict(record=str(record), seat='worker-z', pool='pool-z', size=130 * MiB, bound=128 * MiB,
                            passed_at='2026-11-15T21:00:00+00:00', seen_at='2026-11-15T21:00:00+00:00', counted=0,
                            turns_since=None)
        trace, before = [], book.db.total_changes
        book.db.set_trace_callback(trace.append)
        book.mark_unread_span(record, 'worker-z', 'pool-z', 130 * MiB, 128 * MiB, '2026-11-15T21:05:00+00:00')
        assert sql_writes(trace) == [] and book.db.total_changes == before, 'the same span: nothing written'
        book.db.set_trace_callback(None)
        book.mark_unread_span(record, 'worker-z', 'pool-z', 140 * MiB, 128 * MiB, '2026-11-15T21:10:00+00:00')
        [grown] = book.unread_spans()
        assert (grown['size'], grown['passed_at'], grown['seen_at']) == (
            140 * MiB, '2026-11-15T21:00:00+00:00', '2026-11-15T21:10:00+00:00'), 'when it passed stays'
        book.clear_unread_span(record)
        assert book.unread_spans() == []
    with Ledger.reading(path) as reading:
        assert reading.unread_spans() == []


def test_an_earlier_ledger_gains_the_table_and_keeps_its_rows_and_an_earlier_reader_reads_on(tmp_path):
    path, record = tmp_path / 'ledger.sqlite', tmp_path / 'record-z'
    with Ledger(path) as book:
        book.mark_record(record, 'worker-z', 'pool-z', 'synthetic unavailable measurement')
        book.db.execute('DROP TABLE unread_spans')  # the file as an earlier copy wrote it
        book.db.commit()
    with Ledger.reading(path) as reading:
        assert reading.unread_spans() == [], 'a reading of an earlier file: none, never an error'
    with Ledger(path) as book:  # the service opens it: the table is added, nothing else changes
        assert book.unread_spans() == []
        assert book.incomplete_records() == [(str(record), 'worker-z', 'pool-z', 'synthetic unavailable measurement')]
        book.mark_unread_span(record, 'worker-z', 'pool-z', 130 * MiB, 128 * MiB, '2026-11-15T21:00:00+00:00')
    # A reader of the earlier layout reads the tables it knows, as before.
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as earlier:
        assert earlier.execute('SELECT record, seat, pool, error FROM records').fetchall() == [
            (str(record), 'worker-z', 'pool-z', 'synthetic unavailable measurement')]
        assert earlier.execute('SELECT COUNT(*) FROM turns').fetchone() == (0,)


def test_the_raw_span_layout_remains_readable_with_projection_evidence(tmp_path):
    path, record = tmp_path / 'ledger.sqlite', tmp_path / 'record-z'
    first, later = '2026-11-15T21:00:00+00:00', '2026-11-15T21:05:00+00:00'
    with Ledger(path) as book:
        book.mark_unread_span(record, 'worker-z', 'pool-z', 130 * MiB, 128 * MiB, first)
        [raw] = book.unread_spans()
        book.db.execute('DROP TABLE unread_projection_spans')
    with Ledger.reading(path) as reader:
        assert reader.unread_spans() == [raw]
    with Ledger(path) as book:
        assert book.unread_spans() == [raw]
        book.mark_unread_span(record, 'worker-z', 'pool-z', 130 * MiB, 700_000, later,
                              resource='physical lines', unit='lines')
        [projected] = book.unread_spans()
        assert projected == {**raw, 'projection': dict(size=130 * MiB, resource='physical lines', limit=700_000,
                                                       unit='lines', passed_at=first, seen_at=later, counted=0)}
        assert book.db.execute('SELECT bound FROM unread_spans').fetchall() == [(128 * MiB,)], 'the older layout stays valid'
        trace = []
        book.db.set_trace_callback(trace.append)
        book.mark_unread_span(record, 'worker-z', 'pool-z', 130 * MiB, 700_000, first,
                              resource='physical lines', unit='lines')
        assert not sql_writes(trace)
        book.mark_unread_span(record, 'worker-z', 'pool-z', 140 * MiB, 128 * MiB, later)
        assert book.unread_spans() == [{**projected, 'size': 140 * MiB, 'seen_at': later}]
        book.clear_unread_span(record)
        assert book.unread_spans() == []


def test_the_service_keeps_a_span_past_the_bound_and_clears_it_when_read_whole(tmp_path, monkeypatch):
    svc, seat, record, turns, reads, at = accounting_example(tmp_path, monkeypatch, 2)
    svc.account()
    assert svc.ledger.ingested(record) == 2 and svc.ledger.unread_spans() == []
    harness = seat.harness
    read = harness.read

    def past(path):
        raise record_file.past_bound(path, 130 * MiB, 128 * MiB)
    monkeypatch.setattr(harness, 'read', past)
    record.write_text('synthetic record grown past its bound')
    svc.read_marks.clear(); svc.account_errors.clear()
    svc.account()
    [span] = svc.ledger.unread_spans()
    [(_, _, pool, error)] = svc.ledger.incomplete_records()
    assert 'record past the read bound' in error
    assert (span['record'], span['seat'], span['pool'], span['size'], span['bound'], span['counted']) == (
        str(record), 'worker-z', pool, 130 * MiB, 128 * MiB, 2), 'the pool as the accounting knows the record'
    assert span['passed_at'] and span['turns_since'] is None
    assert svc.ledger.ingested(record) == 2, 'the turns counted before stay; none is made up'

    monkeypatch.setattr(harness, 'read', read)
    record.write_text('synthetic record read whole again')
    svc.read_marks.clear(); svc.account_errors.clear()
    svc.account()
    assert svc.ledger.unread_spans() == [] and svc.ledger.incomplete_records() == []
    svc.ledger.close()


def test_compatibility_observation_coexists_with_older_ledger_writers(tmp_path):
    path, record = tmp_path / 'ledger.sqlite', tmp_path / 'synthetic.jsonl'
    earlier = record_file.ProjectionUsage((1, 2, 3, 4, 5), exhausted='native projection')
    earlier_sql = (str(record), json.dumps(earlier.stamp), None, None, None, earlier.exhausted)
    with Ledger(path) as book:
        book.db.execute('INSERT INTO record_projection_usage VALUES (?,?,?,?,?,?)', earlier_sql)
        book.db.commit()
    with Ledger.reading(path) as book:
        assert book.projection_usage(record) == earlier
    with Ledger(path) as book:
        assert book.projection_usage(record) == earlier
        book.save_projection_usage(record, record_file.ProjectionUsage(earlier.stamp, compatibility=True))
        book.db.commit()
    with Ledger.reading(path) as book:
        assert book.projection_usage(record).compatibility
    with sqlite3.connect(path) as earlier_writer:
        earlier_writer.execute('INSERT OR REPLACE INTO record_projection_usage VALUES (?,?,?,?,?,?)', earlier_sql)
    with Ledger.reading(path) as book:
        assert book.projection_usage(record) == earlier


@pytest.mark.parametrize('human', [False, True], ids=['crew', 'operator'])
@pytest.mark.parametrize('fallback', [False, True], ids=['stream', 'compatibility'])
def test_a_raised_claude_bound_reconciles_the_same_owned_record_after_reopening(tmp_path, monkeypatch, human, fallback):
    runtime = tmp_path / 'runtime'
    name = 'synthetic-seat'
    trusted = trusted_files.make(runtime, name)
    home = runtime / 'seats' / name / 'home'
    record = home / 'projects' / 'synthetic' / 'record.jsonl'
    ledger_path = runtime / 'ledger.sqlite'
    native = Native(record, 'claude')
    native.finish()
    old_bound = record.stat().st_size
    adapter = get('claude')
    record_owner.register(trusted, record, 'claude')
    seat = SimpleNamespace(name=name, home=home, trusted=trusted, harness=adapter, pane=lambda: None,
                           session=SimpleNamespace(human=human), record=lambda: record, launched=lambda: None)
    owned = record_owner.Owned(record, 'claude', adapter)

    def actor(book):
        service = Service.__new__(Service)
        service.runtime, service.store, service.ledger = runtime, post.Store(runtime), book
        service.read_marks, service.account_errors, service.meters = {}, {}, {}
        service.resolved_seats = lambda: [seat]
        return service

    def account(service):
        service.account_records(source_records={name: [owned]}, process_table={}, source_screens={}, replay=False)

    monkeypatch.setattr(record_file, 'CLAUDE_STREAM_BOUND', old_bound)
    if fallback:
        monkeypatch.setattr(claude_record, 'PROJECTED_BYTES', 1)
    with Ledger(ledger_path) as book:
        service = actor(book)
        account(service)
        assert book.ingested(record) == 1 and book.unread_spans() == []
        assert book.projection_usage(record).compatibility is fallback
        native.begin('Another synthetic prompt')
        native.finish()
        account(service)
        [before] = book.unread_spans()
        assert (before['record'], before['bound'], before['counted']) == (str(record), old_bound, 1)

    new_bound = record.stat().st_size
    monkeypatch.setattr(record_file, 'CLAUDE_STREAM_BOUND', new_bound)
    monkeypatch.setattr(claude_record, 'PROJECTED_BYTES', 16 * MiB)
    with Ledger(ledger_path) as book:
        service = actor(book)
        assert book.unread_spans() == [before]
        account(service)
        assert book.ingested(record) == 2 and book.unread_spans() == []
        assert book.projection_usage(record).lines == len(native.rows)
        assert not book.projection_usage(record).compatibility
        with record.open('ab') as stream:
            stream.write(b'\n')
        account(service)
        [after] = book.unread_spans()
        assert (after['record'], after['size'], after['bound'], after['counted']) == (
            str(record), new_bound + 1, new_bound, 2)


@pytest.mark.parametrize('human', [False, True], ids=['crew', 'operator'])
@pytest.mark.parametrize(('budget', 'field', 'resource', 'unit'), [
    ('CLAUDE_STREAM_ROWS', 'lines', 'physical lines', 'lines'),
    ('CLAUDE_STREAM_BYTES', 'retained', 'retained bytes', 'bytes'),
    ('CLAUDE_STREAM_MEMORY', 'memory', 'retained memory', 'bytes'),
    ('ROW_BYTES', None, 'retained bytes per row', 'bytes'),
])
def test_projection_spans_follow_owned_records_through_accounting(tmp_path, monkeypatch, human, budget, field,
                                                                  resource, unit):
    session = Session('synthetic-session', 'Synthetic', human=human)
    pool = Pool('synthetic-pool', 'anthropic')
    spec = SeatSpec('synthetic-seat', session.name, 'worker', 'claude', (pool.name,))
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {pool.name: pool},
                {session.name: session}, {spec.name: spec})
    tmux = SimpleNamespace(pane=lambda _: None)
    trusted = trusted_files.make(ship.runtime, spec.name)
    home = ship.runtime / 'seats' / spec.name / 'home'

    def launch(record, pid):
        Launched('%1', pid, '2029-12-31T00:00:00+00:00', pool.name, None, ['claude'], None, 'claude').write(
            trusted / trusted_files.LAUNCH)
        record_owner.register(trusted, record, 'claude')
        (trusted / trusted_files.RECORD).write_text(str(record) + '\n')

    def account(service):
        _, _, human_readings = service.account_records(process_table={})
        return [record for _, record, *_ in human_readings]

    old = home / 'projects' / 'synthetic' / 'old.jsonl'
    fresh = old.with_name('fresh.jsonl')
    native = Native(old, 'claude')
    native.finish()
    launch(old, 1)
    monkeypatch.setattr(claude_record, 'COMPATIBILITY_BOUND', 1)
    budget_module = claude_record if field is None else record_file
    production_limit = getattr(budget_module, budget)
    service = Service(ship, tmux)
    with service.ledger as book:
        assert account(service) == ([old] if human else [])
        assert book.ingested(old) == 1
        limit = 1024 if field is None else getattr(book.projection_usage(old), field)
        monkeypatch.setattr(budget_module, budget, limit)
        native.begin('x' * limit if field is None else 'Another synthetic prompt')
        native.finish()
        if field is None:
            with monkeypatch.context() as raw_bound:
                raw_bound.setattr(record_file, 'CLAUDE_STREAM_BOUND', old.stat().st_size - 1)
                account(service)
                [raw] = book.unread_spans()
                assert raw['bound'] == old.stat().st_size - 1
            with old.open('ab') as stream:
                stream.write(b'\n')
        account(service)
        [span] = book.unread_spans()
        projection = span['projection'] if field is None else span
        expected = dict(size=old.stat().st_size, resource=resource, limit=limit, unit=unit,
                        passed_at=span['passed_at'], seen_at=projection['seen_at'], counted=1)
        if field is None:
            assert span == {**raw, 'projection': expected}
        else:
            assert span == dict(record=str(old), seat=spec.name, pool=pool.name, turns_since=None, **expected)
            assert span['passed_at'] == span['seen_at']
        assert book.ingested(old) == 1
        assert book.projection_usage(old).exhausted == resource
        [seat] = service.resolved_seats()
        pressure = record_file.pressure(seat, old, book.projection_usage)
        assert pressure.first_bound == (128 * MiB if field is None else old.stat().st_size)
        assert pressure.exhausted == resource
        view = ledger_reading.collect(book, ship)
        assert view['pools'][0]['unread_spans'] == 1
        assert any(f"{projection['size']:,} bytes raw, projection {resource} limit of {limit:,} {unit} exceeded" in line
                   for line in ledger_reading.render(view))
        if field is None:
            assert view['projection_refusals'] == [
                dict(record=str(old), path=['message', 'content'], size=limit, layout='newline',
                     reason='budget', value_type='?', compatibility=False, exhausted=resource)]

    new = Native(fresh, 'claude')
    new.finish()
    launch(fresh, 2)
    service = Service(ship, tmux)
    with service.ledger as book:
        assert account(service) == ([fresh] if human else [])
        assert book.ingested(fresh) == 1
        assert book.unread_spans() == [span], 'the fresh launch reads its own record and leaves the old refusal'

        old.write_bytes(b'\xff\n')
        account(service)
        assert book.unread_spans() == [span], 'a different failure does not clear the unread span'
        assert book.projection_refusals() == [], 'the unread span never certifies an older field observation'
        assert book.ingested(old) == 1

        monkeypatch.setattr(budget_module, budget, production_limit)
        native.flush()
        record_stamp = ledger.record_stamp
        old_checks = 0

        def append_before_stability_check(owned):
            nonlocal old_checks
            if owned.path == old:
                old_checks += 1
                if old_checks == 2:
                    with old.open('ab') as stream:
                        stream.write(b'\n')
            return record_stamp(owned)

        with monkeypatch.context() as probe:
            probe.setattr(ledger, 'record_stamp', append_before_stability_check)
            account(service)
        assert old_checks == 2, 'a successful read reached the stability check'
        assert book.unread_spans() == [span]
        assert [row[0] for row in book.incomplete_records()] == [str(old)]
        account(service)
        assert book.ingested(old) == 2 and book.ingested(fresh) == 1
        assert book.unread_spans() == book.incomplete_records() == []
        assert book.projection_usage(old).exhausted is None


@pytest.mark.parametrize('mirror,layout', [(False, 'newline'), (True, 'newline'),
                                           (False, 'splitlines'), (True, 'splitlines')])
def test_picture_projection_recovers_the_same_operator_record_in_place(tmp_path, monkeypatch, mirror, layout):
    session = Session('synthetic-session', 'Synthetic', human=True)
    pool = Pool('synthetic-pool', 'anthropic')
    spec = SeatSpec('synthetic-seat', session.name, 'office', 'claude', (pool.name,))
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {pool.name: pool},
                {session.name: session}, {spec.name: spec})
    tmux = SimpleNamespace(pane=lambda _: None)
    trusted = trusted_files.make(ship.runtime, spec.name)
    pointer, launch = trusted / trusted_files.RECORD, trusted / trusted_files.LAUNCH
    path = ship.runtime / 'seats' / spec.name / 'home' / 'projects' / 'synthetic' / 'record.jsonl'
    native = Native(path, 'claude')
    native.finish()
    record_owner.register(trusted, path, 'claude')
    pointer.write_text(str(path) + '\n')
    Launched('%1', 1234, '2029-12-31T00:00:00+00:00', pool.name, None, ['claude'], None, 'claude').write(launch)
    files = pointer.read_bytes(), launch.read_bytes()
    inode = path.stat().st_dev, path.stat().st_ino

    def unexpected(*args, **kwargs):
        pytest.fail('accounting tried to act on an operator-owned seat')

    for action in ('up', 'down', 'restart', 'fresh', 'move_to'):
        monkeypatch.setattr(Seat, action, unexpected)
    monkeypatch.setattr(Service, 'request_fresh_handoff', unexpected)
    monkeypatch.setattr(Service, 'deliver', unexpected)

    def account(service, book, *, observed=False):
        _, _, human_readings = service.account_records(process_table={})
        assert [record for _, record, *_ in human_readings] == ([path] if observed else [])
        assert service.store.all() == []
        return book.db.execute('SELECT turn,started,ended FROM turns WHERE record=? ORDER BY turn',
                               (str(path),)).fetchall()

    service = Service(ship, tmux)
    with service.ledger as book:
        before = account(service, book, observed=True)
        assert len(before) == 1
        # Construct the rest elsewhere, then append it without rewriting the observed prefix.
        first_rows = len(native.rows)
        native.path = tmp_path / 'made-suffix.jsonl'
        picture = image(MiB)
        if mirror:
            native.begin('Synthetic Read request')
            begin_tool(native)
            native.add('user', version='2.1.325', message=dict(role='user', content=[result([picture])]),
                       toolUseResult=read_mirror(picture))
        else:
            native.add('user', version='2.1.325', message=dict(role='user', content=[
                picture, text('Synthetic pasted-image prompt')]))
        native.finish()
        padding = json.dumps({'type': 'file-history-snapshot', 'snapshot': 'x' * (4 * MiB)}).encode() + b'\n'
        with path.open('ab') as stream:
            if layout == 'splitlines':
                stream.write('\u2028'.encode())
            for row in native.rows[first_rows:]:
                stream.write(json.dumps(row).encode() + b'\n')
            for _ in range(32):
                stream.write(padding)
        size = path.stat().st_size
        assert size > 128 * MiB
        with monkeypatch.context() as older:
            older.setattr(record_file, 'CLAUDE_STREAM_BOUND', 128 * MiB)
            assert account(service, book) == before
        [raw] = book.unread_spans()
        assert (raw['record'], raw['size'], raw['bound'], raw['counted']) == (str(path), size, 128 * MiB, 1)

    json_record.release(path)
    with monkeypatch.context() as earlier_reader:
        if layout == 'splitlines' or mirror:
            earlier_reader.setattr(claude_record, 'mirror_path', lambda _: False)
        if layout == 'splitlines' or not mirror:
            earlier_reader.setattr(claude_record, 'PAYLOAD_PATHS', {})
        service = Service(ship, tmux)
        with service.ledger as book:
            assert account(service, book) == before
            [blocked] = book.unread_spans()
            assert blocked['bound'] == raw['bound']
            assert blocked['projection']['resource'] == 'retained bytes per row'
            assert blocked['projection']['counted'] == 1
            expected_path = ['message', 'content'] + (['content'] if mirror else []) + ['source', 'data']
            if layout == 'newline' and mirror:
                expected_path = ['toolUseResult', 'file', 'base64']
            assert book.projection_refusals() == [
                dict(record=str(path), path=expected_path, size=MiB, layout=layout, reason='budget', value_type='?',
                     compatibility=False, exhausted='retained bytes per row')]

    service = Service(ship, tmux)
    with service.ledger as book:
        after = account(service, book, observed=True)
        assert after[:1] == before and len(after) == 2
        assert book.unread_spans() == book.incomplete_records() == []
        assert book.projection_refusals() == []
        assert account(service, book) == after
        [seat] = service.resolved_seats()
        assert human_wake.owned_record(seat) == path
        assert (pointer.read_bytes(), launch.read_bytes()) == files
        assert (path.stat().st_dev, path.stat().st_ino) == inode
        assert path.stat().st_size == size


@pytest.mark.parametrize('layout,role,row_type,carrier', [
    ('newline', 'assistant', 'assistant', None), ('splitlines', 'assistant', 'assistant', 'tool_result'),
    ('newline', 'user', None, 'tool_result'), ('splitlines', 'user', 'future_result', 'tool_result'),
])
def test_message_picture_projection_recovers_owned_operator_record(tmp_path, monkeypatch, layout, role, row_type, carrier):
    compatibility_bound = 32 * 1024
    monkeypatch.setattr(claude_record, 'COMPATIBILITY_BOUND', compatibility_bound)
    monkeypatch.setattr(claude_record, 'ROW_BYTES', 4096)
    monkeypatch.setattr(claude_record, 'CHUNK', 512)
    session = Session('synthetic-session', 'Synthetic', human=True)
    pool = Pool('synthetic-pool', 'anthropic')
    spec = SeatSpec('synthetic-seat', session.name, 'office', 'claude', (pool.name,))
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {pool.name: pool},
                {session.name: session}, {spec.name: spec})
    tmux = SimpleNamespace(pane=lambda _: None)
    trusted = trusted_files.make(ship.runtime, spec.name)
    pointer, launch = trusted / trusted_files.RECORD, trusted / trusted_files.LAUNCH
    path = ship.runtime / 'seats' / spec.name / 'home' / 'projects' / 'synthetic' / 'record.jsonl'
    native = Native(path, 'claude')
    native.finish()
    record_owner.register(trusted, path, 'claude')
    pointer.write_text(str(path) + '\n')
    Launched('%1', 1234, '2029-12-31T00:00:00+00:00', pool.name, None, ['claude'], None, 'claude').write(launch)
    provenance = pointer.read_bytes(), launch.read_bytes(), path.stat().st_ino, path.stat().st_dev

    def unexpected(*args, **kwargs):
        pytest.fail('accounting tried to act on an operator-owned seat')

    for action in ('up', 'down', 'restart', 'fresh', 'move_to'):
        monkeypatch.setattr(Seat, action, unexpected)
    monkeypatch.setattr(Service, 'request_fresh_handoff', unexpected)
    monkeypatch.setattr(Service, 'deliver', unexpected)
    messages = []

    def account(service, book, *, observed=False):
        _, _, human_readings = service.account_records(process_table={})
        assert [record for _, record, *_ in human_readings] == ([path] if observed else [])
        assert service.store.all() == messages
        return book.db.execute('SELECT turn,started,ended FROM turns WHERE record=? ORDER BY turn',
                               (str(path),)).fetchall()

    service = Service(ship, tmux)
    with service.ledger as book:
        before = account(service, book, observed=True)
        assert len(before) == 1
        first_rows = len(native.rows)
        native.path = tmp_path / 'made-suffix.jsonl'
        order = post.Message('ord-synthetic', 'order', 'issuer', spec.name,
                             AT + timedelta(seconds=3, milliseconds=500), 'Synthetic image request')
        service.store.write(order)
        messages.append(order)
        native.begin(post.typed(messages))
        picture = image(48 * 1024)
        block = picture if carrier is None else dict(result([picture]), type=carrier)
        if role == 'user':
            begin_tool(native)
            row = native.add(row_type, version='2.1.325', message=dict(content=[block], role=role))
            if row_type is None:
                del row['type']
        reply_content = [block, text('Synthetic reply')] if role == 'assistant' else [text('Synthetic reply')]
        native.add('assistant', version='2.1.325', message=dict(content=reply_content,
                   role='assistant', model='synthetic', id='synthetic-image-reply',
                   usage=dict(input_tokens=2, output_tokens=3, cache_read_input_tokens=5,
                              cache_creation_input_tokens=7)))
        native.finish()
        with path.open('ab') as stream:
            if layout == 'splitlines':
                stream.write('\u2028'.encode())
            for row in native.rows[first_rows:]:
                stream.write(json.dumps(row).encode() + b'\n')
        assert path.stat().st_size > compatibility_bound
        with monkeypatch.context() as older:
            older.setattr(record_file, 'CLAUDE_STREAM_BOUND', compatibility_bound)
            assert account(service, book) == before
        [raw] = book.unread_spans()
        assert (raw['bound'], raw['counted']) == (compatibility_bound, 1)

    def refuse_before_projection(row, payloads, descriptor, remaining):
        field, capture = next(iter(payloads.items()))
        refusal = claude_record.RowRefusal(claude_record.schema_path(field), capture.size)
        shape = row if role == 'user' else None
        raise claude_record.projection_error(refusal, 'not-a-user-row', row=shape, path=field)

    json_record.release(path)
    with monkeypatch.context() as prior:
        prior.setattr(claude_record, 'project_images', refuse_before_projection)
        service = Service(ship, tmux)
        with service.ledger as book:
            assert account(service, book) == before
            [blocked] = book.unread_spans()
            assert blocked['bound'] == raw['bound'] and blocked['projection']['counted'] == 1
            [refusal] = book.projection_refusals()
            assert (refusal['layout'], refusal['reason']) == (layout, 'not-a-user-row')
            if role == 'user':
                assert (refusal['row_type'], refusal['message_role'], refusal['block_type'],
                        refusal['content_is_list']) == (row_type or 'missing', role, carrier, True)
            else:
                assert 'content_is_list' not in refusal

    service = Service(ship, tmux)
    with service.ledger as book:
        after = account(service, book, observed=True)
        assert after[:1] == before and len(after) == 2
        tokens = (5, 2, 7, 3) if role == 'assistant' else (12, 13, 12, 6)
        assert book.db.execute('SELECT read_cached,read_uncached,cache_written,written FROM turns '
                               'WHERE record=? AND turn=1', (str(path),)).fetchone() == tokens
        _, attribution = book.attributions.details()
        assert (attribution['status'], attribution['orders']) == ('attributed', [order.id])
        assert any(item.get('message') == order.id for item in attribution['evidence'])
        assert book.unread_spans() == book.incomplete_records() == book.projection_refusals() == []
        assert account(service, book) == after
        [seat] = service.resolved_seats()
        assert human_wake.owned_record(seat) == path
        assert (pointer.read_bytes(), launch.read_bytes(), path.stat().st_ino, path.stat().st_dev) == provenance


@pytest.mark.parametrize('layout', ['newline', 'splitlines'])
@pytest.mark.parametrize('legacy', [True, False])
def test_compatibility_field_evidence_is_persisted_only_for_its_exact_observation(tmp_path, layout, legacy):
    path, record = tmp_path / 'ledger.sqlite', tmp_path / 'synthetic.jsonl'
    native = Native(record, 'claude')
    native.finish()
    size = claude_record.ROW_BYTES + 123
    native.add('system', message={'fixture-only-name': [{'fixture-private-name': 'x' * size}]})
    if layout == 'splitlines':
        record.write_bytes(record.read_bytes().replace(b'\n', '\u2028'.encode(), 1))
    state, _ = get('claude').read(record)
    assert state.turn_open is False and state.projection_usage.compatibility
    usage = state.projection_usage
    with Ledger(path) as book:
        book.save_projection_usage(record, usage)
        if legacy:
            # This is the stored format from before layout and reason were measured.
            stamp = dict(stamp=usage.stamp, compatibility=True,
                         row_refusal=dict(path=['message', '?', '?'], size=size))
            book.db.execute('UPDATE record_projection_usage SET stamp=?', (json.dumps(stamp),))
        book.db.commit()
    with Ledger.reading(path) as reader:
        detail = reader.projection_usage(record).row_refusal
        row = dict(record=str(record), path=['message', '?', '?'], size=size,
                   compatibility=True, exhausted=None)
        if legacy:
            assert detail == claude_record.RowRefusal(('message', '?', '?'), size)
        else:
            assert detail == usage.row_refusal
            row.update(layout=layout, reason='budget', value_type='?')
        assert reader.projection_refusals() == [row]
        view = ledger_reading.collect(reader, by='seat')
        detail_note = '' if legacy else f' · {layout} · budget (?)'
        note = f'message.?.? ({size:,} encoded bytes) · read by compatibility reader{detail_note}'
        assert any(line.endswith(note) for line in ledger_reading.render(view))
        assert reader.incomplete_records() == reader.unread_spans() == []
        with record.open('ab') as stream:
            stream.write(b'\n')
        assert reader.projection_refusals() == []
        assert reader.projection_usage(record).compatibility
    refreshed, _ = get('claude').read(record)
    with Ledger(path) as book:
        book.save_projection_usage(record, refreshed.projection_usage)
        book.db.commit()
    with Ledger.reading(path) as reader:
        assert reader.projection_refusals()
        replacement = record.with_suffix('.replacement')
        replacement.write_bytes(record.read_bytes())
        replacement.replace(record)
        assert reader.projection_refusals() == []


def test_projection_refusal_reader_accepts_only_fixed_schema_metadata(tmp_path):
    record = tmp_path / 'synthetic.jsonl'
    record.write_text('{}\n')
    stamp = json_record._stamp(record.stat())
    detail = dict(path=['message', 'content'], size=42, layout='splitlines',
                  reason='image-media-type', value_type='some-made-up-identifier')
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        for changes in ({}, {'reason': 'made-up-reason'}, {'layout': 'made-up-layout'},
                        {'path': ['message', 'private-field']}, {'path': ['message', 3]}):
            stored = dict(stamp=stamp, row_refusal=dict(detail, **changes))
            book.db.execute('INSERT OR REPLACE INTO record_projection_usage VALUES (?,?,NULL,NULL,NULL,?)',
                            (str(record), json.dumps(stored), 'retained bytes per row'))
            rows = book.projection_refusals()
            if 'path' in changes:
                assert rows == []
            elif changes:
                assert 'layout' not in rows[0] and 'reason' not in rows[0] and 'value_type' not in rows[0]
            else:
                assert rows[0]['layout'] == 'splitlines'
                assert rows[0]['reason'] == 'image-media-type' and rows[0]['value_type'] == '?'


@pytest.mark.parametrize('reason', ['not-a-user-row', 'unknown-block-type'])
def test_projection_refusal_shape_survives_ledger_roundtrip(tmp_path, reason):
    record = tmp_path / 'synthetic.jsonl'
    record.write_text('{}\n')
    stamp = json_record._stamp(record.stat())
    base = claude_record.RowRefusal(('message', 'content'), 42, 'splitlines', reason, '?')
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        # This format had layout, reason and scalar type before the shape fields were added.
        book.save_projection_usage(record, claude_record.RefusedProjectionUsage(stamp, row_refusal=base))
        assert 'content_is_list' not in book.projection_refusals()[0]
        detail = base.with_shape('future_result', 'assistant', 'mcp_tool_result', True)
        book.save_projection_usage(record, claude_record.RefusedProjectionUsage(stamp, row_refusal=detail))
        assert book.projection_usage(record).row_refusal == detail
        view = ledger_reading.collect(book, by='seat')
        assert any(line.endswith('row future_result, message role assistant, block mcp_tool_result, content list')
                   for line in ledger_reading.render(view))

        tampered = dict(path=['message', 'content'], size=42, layout='splitlines', reason=reason, value_type='?',
                        row_type='private-row\n', message_role=['private-role'], block_type='private_tool_result',
                        content_is_list=False)
        book.db.execute('UPDATE record_projection_usage SET stamp=?',
                        (json.dumps(dict(stamp=stamp, row_refusal=tampered)),))
        assert book.projection_usage(record).row_refusal == base.with_shape('?', '?', '?', False)
        assert any(line.endswith('row ?, message role ?, block ?, content non-list')
                   for line in ledger_reading.render(ledger_reading.collect(book, by='seat')))
        with record.open('a') as stream:
            stream.write('\n')
        assert book.projection_refusals() == []


def test_qd_ledger_and_the_pool_reading_name_each_span(tmp_path):
    from types import SimpleNamespace
    path, record = tmp_path / 'ledger.sqlite', tmp_path / 'seats' / 'worker-z' / 'home' / 'record-z.jsonl'
    with Ledger(path) as book:
        book.mark_unread_span(record, 'worker-z', 'pool-z', 130 * MiB, 128 * MiB, '2026-11-15T21:00:00+00:00')
    pool = SimpleNamespace(name='pool-z', kind='subscription', provider='synthetic')
    with Ledger.reading(path) as reading:
        data = ledger_reading.collect(reading)
        assert data['unread_spans'][0]['size'] == 130 * MiB
        note = ('unread span: record-z.jsonl (worker-z, pool-z) · 130.0 MiB past its read bound of 128.0 MiB, first '
                'seen 2026-11-15T21:00:00+00:00; 0 turns counted before it, its turns since unknown')
        assert note in ledger_reading.render(data)
        rollup = ledger_reading.collect(reading, by='seat')
        assert note in ledger_reading.render(rollup)
    pools = dict(data, pools=[dict(name='pool-z', provider_name='Synthetic', kind='subscription', native_harness=None,
                                    unread_spans=1)], windows=[], usage_attempts=[], observed_walls=[],
                 usage_absent_reason=None)
    assert ledger_reading.render_pools(pools)[0].endswith(' · 1 unread span: turns unknown')
