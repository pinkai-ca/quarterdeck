"""The service profiles one of its own full status collections when a systems or engineering seat asks (status_profile).

A synthetic ship whose seat's record, screen and messages carry distinctive strings under a distinctive record path:
the profile written holds none of them, nor any path of this machine, only the code's function keys, counts and times.
"""
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import post, record_owner, sandbox, status_profile
from quarterdeck.service import Service
from quarterdeck import ship as shipmod
from quarterdeck.tmux import Pane

PLANTED = ('qzxv-record-content-7731', 'qzxv-screen-4410', 'qzxv-body-9902', 'qzxv-record-path-5521')


SHIP = """[ship]
name = "example"
operator = "operator"
[pools.test]
provider = "openai"
[sessions.bridge]
title = "Test"
[sessions.office]
title = "Office"
human = true
""" + "".join(f"""[seats.{name}]
session = "{session}"
role = "{role}"
harness = "codex"
pool = "test"
model = "synthetic-1"
""" for name, session, role in (("worker", "bridge", "worker"), ("sentry", "bridge", "systems"),
                                ("builder", "bridge", "engineering"), ("minder", "office", "systems"),
                                ("desk", "office", "desk")))


@pytest.fixture
def ship(tmp_path: Path):
    (tmp_path / "ship.toml").write_text(SHIP)  # the collection loads the ship from its file
    ship = shipmod.load(tmp_path)
    pane = Pane("%1", "Test", "worker", 123, 80, 24, False, False, None)
    screen = f"  {PLANTED[1]}\n\n› \n\n  test low · /work\n"
    tmux = SimpleNamespace(pane=lambda p: pane, panes=lambda *a: [pane], capture=lambda *a, **k: screen,
                           clients_attached=lambda *a: False, sessions=lambda: [], windows=lambda *a: [],
                           has_session=lambda *a: False, session_view=lambda *a: None,
                           session_environment=lambda *a: None, running=lambda: True)
    svc = Service(ship, tmux)
    seat = svc.seat("worker")
    seat.folder.mkdir(parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    record = seat.folder / "home" / PLANTED[3] / "rollout.jsonl"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"type": "message", "payload": {"text": PLANTED[0]}}) + "\n")
    record_owner.register(seat.trusted, record, "codex")
    (seat.trusted / "record").write_text(str(record) + "\n")
    launch = {"pane": "%1", "pid": 123, "at": (post.now() - timedelta(minutes=1)).isoformat(), "pool": "test",
              "argv": ["codex"], "harness": "codex", "resume": None, "version": "synthetic"}
    (seat.trusted / "launch.json").write_text(json.dumps(launch))
    svc.store.new("order", "sentry", "worker", f"Synthetic order {PLANTED[2]}.", subject="Synthetic")
    yield svc, record
    svc.ledger.close()


def profiled(svc, caller="sentry"):
    answer = status_profile.ask(svc, caller)
    path = svc.status_profile["path"]
    assert str(path) in answer, 'the requester is told where the profile is written'
    return path


def test_a_profile_holds_the_code_and_nothing_of_a_record_a_screen_or_a_message(ship, tmp_path, monkeypatch):
    svc, record = ship
    seen = {'screen': 0, 'post': 0, 'record': 0}
    capture, listing, joined = svc.tmux.capture, svc.store.all, type(svc.seat('worker')).join

    def captured(*args, **kwargs):
        text = capture(*args, **kwargs)
        seen['screen'] += PLANTED[1] in text
        return text

    def listed(*args, **kwargs):
        messages = listing(*args, **kwargs)
        seen['post'] += any(PLANTED[2] in message.body for message in messages)
        return messages

    def join_of(seat, *args, **kwargs):
        found = joined(seat, *args, **kwargs)
        seen['record'] += PLANTED[3] in str(getattr(found, 'record', None))
        seen.setdefault('reasons', []).append(getattr(found, 'reason', None))
        return found

    svc.tmux.capture, svc.store.all = captured, listed
    monkeypatch.setattr(type(svc.seat('worker')), 'join', join_of)
    pane = svc.tmux.pane('%1')
    monkeypatch.setattr(type(svc.seat('worker')), 'pane', lambda seat: pane if seat.name == 'worker' else None)  # up
    from quarterdeck.tmux import Tmux
    monkeypatch.setattr(Tmux, 'for_ship', classmethod(lambda cls, ship: svc.tmux))  # the collection's panes are these
    path = profiled(svc)
    svc._status_due()  # the status duty: no one watches (clients_attached is False), and it collects all the same
    assert svc.status_profile is None
    assert all(seen[key] for key in ('screen', 'post', 'record')), f'the planted screen, message and record path passed through the profiled code: {seen}'
    raw = path.read_bytes()
    for planted in (*PLANTED, str(record), str(tmp_path), tmp_path.name, 'example'):
        assert planted.encode() not in raw, planted
        assert planted.lower().encode() not in raw.lower(), planted
    data = json.loads(raw)
    assert data['profiled'] is True and data['rows']
    assert set(data) == {'format', 'written', 'collection', 'profiled', 'functions', 'rows'}
    for row in data['rows']:
        assert set(row) == {'module', 'line', 'function', 'calls', 'primitive_calls', 'own_seconds',
                            'cumulative_seconds'}
        assert not row['module'].startswith('/'), row
    assert any(row['module'] == 'quarterdeck/watch.py' and row['function'] == 'collect' for row in data['rows'])


@pytest.mark.parametrize('caller, reason', [
    ('worker', 'worker is a worker seat'),
    ('desk', "desk sits in a person's session"),
    ('minder', "minder sits in a person's session"),  # a systems role in a person's session is still refused
    ('stranger', 'stranger is not a seat of example'),
    (None, 'QD_SEAT names no seat here'),
])
def test_only_a_systems_or_engineering_seat_of_the_crew_may_ask(ship, caller, reason):
    svc, record = ship
    answer = status_profile.ask(svc, caller)
    assert answer.startswith('refused: ') and reason in answer, answer
    assert getattr(svc, 'status_profile', None) is None


@pytest.mark.parametrize('caller', ['sentry', 'builder'])
def test_a_systems_or_an_engineering_seat_may_ask(ship, caller):
    svc, record = ship
    path = profiled(svc, caller)
    svc._status_due()
    assert path.is_file()


def test_a_second_request_while_one_is_in_hand_is_refused_and_named(ship):
    svc, record = ship
    first = profiled(svc)
    second = status_profile.ask(svc, 'builder')
    assert second.startswith('refused: ') and str(first) in second and 'one at a time' in second
    svc._status_due()
    assert first.is_file()
    assert 'one at a time' not in status_profile.ask(svc, 'builder'), 'once written, the next may ask'



def test_the_command_line_refuses_before_any_request(ship, monkeypatch):
    from quarterdeck import cli
    svc, record = ship
    filed = []
    monkeypatch.setattr(cli, 'load_ship', lambda args: svc.ship)
    monkeypatch.setattr(cli.service, 'request', lambda runtime, kind, **data: filed.append((kind, data)) or 'ok')
    args = cli.build_parser().parse_args(['profile-status'])
    for caller in ('worker', 'desk', 'minder'):
        monkeypatch.setenv('QD_SEAT', caller)
        with pytest.raises(cli.ShipError, match='refused: '):
            args.run(args)
    monkeypatch.setenv('QD_SEAT', 'builder')
    assert args.run(args) == 'ok'
    assert filed == [('profile-status', {'caller': 'builder'})]


def test_the_folder_is_shown_to_no_jail():
    assert status_profile.FOLDER not in sandbox.RUNTIME_SHOWN


def test_the_profile_is_bounded_in_size_and_in_number(ship, monkeypatch):
    svc, record = ship
    monkeypatch.setattr(status_profile, 'MAX_BYTES', 4096)
    written = []
    for second in range(status_profile.KEEP + 2):
        path = status_profile.folder(svc.runtime) / f'2032010{second}T000000Z.json'
        svc.status_profile = {'caller': 'sentry', 'at': 'synthetic', 'path': path}
        status_profile.run(svc, svc.status_profile, svc.publish_status)
        written.append(path)
        assert len(path.read_bytes()) <= 4096
    assert sorted(p.name for p in status_profile.folder(svc.runtime).iterdir()) == [p.name for p in written[-status_profile.KEEP:]]


@pytest.mark.parametrize('filename, module', [
    ('~', 'builtin'),
    ('<frozen posixpath>', '<frozen posixpath>'),
    ('/srv/elsewhere/qzxv-record-path-5521/code.py', 'other'),
    ('<string>', 'other'),
])
def test_a_code_file_is_named_by_its_place_never_its_path(filename, module):
    assert status_profile.module_of(filename) == module
    assert status_profile.module_of(status_profile.__file__) == 'quarterdeck/status_profile.py'
