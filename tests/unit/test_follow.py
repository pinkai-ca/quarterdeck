"""The follow view's room, event sources and terminal recording."""

from io import StringIO
import json
from types import SimpleNamespace

import pytest

from quarterdeck import cli, follow, follow_view, post, post_dir, ship as shipmod


def test_attach_parser_retains_ordinary_session_and_accepts_follow_flags():
    parser = cli.build_parser()
    ordinary = parser.parse_args(['attach', 'Crew'])
    assert ordinary.session == 'Crew' and not ordinary.follow
    followed = parser.parse_args(['attach', '--follow', '--record', 'take.cast', '--fit', 'crew', 'Office'])
    assert followed.follow and followed.fit and followed.session == 'crew' and followed.sessions == ['Office']
    assert followed.record.name == 'take.cast'


def test_declared_sessions_and_selected_seats_are_the_only_columns(drydock):
    ship = shipmod.load(drydock)
    assert [session.name for session in follow.sessions(ship, [])] == ['bridge', 'office', 'crew']
    chosen = follow.sessions(ship, ['Crew', 'crew'])
    viewer = follow.Viewer(ship, None, chosen, None)
    assert viewer.sessions == {'Crew'}
    assert viewer.room.columns == (('worker-1',),)
    with pytest.raises(shipmod.ShipError, match='no session'):
        follow.sessions(ship, ['unknown'])


def test_room_gives_arrivals_priority_and_retains_working_labels():
    activity = {name: follow_view.Activity() for name in ('worker', 'stalled', 'arrival')}
    activity['worker'].observe(True, 0)
    activity['stalled'].observe(True, 0.5)
    room = follow_view.Room((tuple(activity),), activity)
    assert room.update(1, 10) == [{'stalled': 7}]
    activity['arrival'].arrived = 2
    assert room.update(2, 10)[0]['stalled'] == 7
    middle = room.update(2.17, 10)[0]
    assert sum(middle.values()) == 7 and set(middle.values()) == {3, 4}
    assert room.update(2.35, 10) == [{'arrival': 7}]
    activity['arrival'].observe(True, 2.4)
    activity['arrival'].observe(False, 2.5)
    assert room.update(3, 10) == [{'arrival': 7}]  # coalesce recent moves
    room.update(4.35, 10)
    assert room.update(4.7, 10) == [{'stalled': 7}]
    rows = follow_view.compose(room.columns, room.target, [35], dict(zip(activity, activity)), {}, activity, 4.7)
    assert any('worker working' in row for row in rows)
    assert sum('──' in row for row in rows) == 3
    assert len(rows) == 10


def test_room_lingers_after_turn_and_leaves_idle_seats_as_labels():
    state = follow_view.Activity()
    room = follow_view.Room((('seat',), ()), {'seat': state})
    assert room.update(1, 12) == [{None: 11}, {None: 12}]
    state.observe(True, 2)
    room.update(2, 12)
    assert room.update(2.5, 12) == [{'seat': 11}, {None: 12}]
    state.observe(None, 3)
    opening = room.update(3, 12)
    assert opening[0] == {'seat': 11}
    rows = follow_view.compose(room.columns, opening, [25, 25], {'seat': 'Seat'}, {}, room.activity, 3)
    assert 'Seat unread' in rows[-1]
    state.observe(False, 4)
    assert room.update(6.99, 12)[0] == {'seat': 11}
    room.update(7, 12)
    assert room.update(7.5, 12) == [{None: 11}, {None: 12}]


def test_display_keeps_bottom_rows_styles_and_crops_the_right():
    source = 'TOP\n\x1b[38;5;11m漢字abcdef\nlast\x1b]0;title\x07!\x1b[2J\n'
    rows = follow_view.screen_rows(source, 6, 2)
    assert '漢字ab' in rows[0] and 'abcdef' not in rows[0] and 'TOP' not in ''.join(rows)
    assert rows[1].startswith('\x1b[38;5;11mlast!')
    assert not any(token in ''.join(rows) for token in ('title', '\x1b[2J'))


def test_cast_records_only_the_terminal_output_and_resize():
    terminal, cast = StringIO(), StringIO()
    frames = follow_view.TerminalFrames(terminal, cast, clock=iter(range(12)).__next__)
    frames.paint((30, 20), ('first',))
    frames.paint((30, 20), ('first',))
    frames.paint((30, 20), ('next',))
    frames.paint((26, 15), ('next',))
    frames.close()
    header, *events = map(json.loads, cast.getvalue().splitlines())
    assert (header['version'], header['width'], header['height']) == (2, 30, 20)
    assert [event[1] for event in events] == ['o', 'o', 'r', 'o', 'o']
    assert events[2][2] == '26x15'
    assert events[-1][2] == '' and events[-1][0] > events[-2][0]
    assert ''.join(event[2] for event in events if event[1] == 'o') == terminal.getvalue()


def test_cast_exclusive_creation_keeps_existing_recording(tmp_path):
    path = tmp_path / 'original.cast'
    with follow.open_cast(path) as stream:
        stream.write('original recording')
    with pytest.raises(shipmod.ShipError, match='cannot create cast'):
        follow.open_cast(path)
    assert path.read_text() == 'original recording'


def test_store_arrivals_read_only_new_messages_and_ignore_ack(drydock, monkeypatch):
    ship = shipmod.load(drydock)
    store = post.Store(ship.runtime)
    historical = store.new('order', 'issuer', 'worker-1', 'historical')
    events = follow.Arrivals(ship, {'worker-1'})
    read = post_dir.read_bytes
    touched = []

    def save_read(path, **options):
        if path.parent == store.dir:
            touched.append(path.stem)
        return read(path, **options)

    monkeypatch.setattr(post_dir, 'read_bytes', save_read)
    try:
        assert events.recipients() == set()
        new = store.new('order', 'issuer', 'worker-1', 'arrives')
        store.new('ack', 'captain', 'worker-1', 'claims')
        store.new('order', 'issuer', 'desk', 'other session')
        touched.clear()
        assert events.recipients() == {'worker-1'}
        assert new.id in touched and historical.id not in touched
        assert events.recipients() == set()
    finally:
        events.close()


def test_record_observation_does_not_discover_or_capture_a_screen(drydock, monkeypatch):
    ship = shipmod.load(drydock)
    spec = ship.seats['worker-1']
    launch = SimpleNamespace(pane='%55', pid=123, at='start')
    seen = []

    class Synthetic:
        def __init__(self, record):
            self.spec = spec
            self.record = record

        def launched(self):
            return launch

        def pane(self):
            return SimpleNamespace(dead=False)

        def lookup_record(self):
            return self.record

    monkeypatch.setattr(follow, 'Seat', Synthetic)
    monkeypatch.setattr(follow.record_follow, 'state',
                        lambda seat, record: seen.append(record) or SimpleNamespace(turn_open=True, model='observed'))
    monkeypatch.setattr(follow, 'read_seat', lambda *_: Synthetic(None))
    assert follow.observe(ship, (spec,), None)[spec.name].turn_open is None and not seen
    monkeypatch.setattr(follow, 'read_seat', lambda *_: Synthetic('pinned'))
    reading = follow.observe(ship, (spec,), None)[spec.name]
    assert reading.turn_open is True and reading.model == 'observed' and seen == ['pinned']
