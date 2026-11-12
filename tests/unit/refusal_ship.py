"""A synthetic ship for the turn-failure and refusal tests: Codex seats, invented messages and records."""
from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

from quarterdeck import post, ship as shipmod
from quarterdeck.record_owner import register
from quarterdeck.seat import Launched
from quarterdeck.service import Service
from quarterdeck.tmux import Pane

SHIP = '''[ship]
name = "synthetic"
operator = "operator"
[pools.test]
provider = "codex"
[sessions.crew]
title = "Crew"
[sessions.office]
title = "Office"
human = true
[seats.worker]
session = "crew"
role = "worker"
harness = "codex"
pool = "test"
[seats.tester]
session = "crew"
role = "engineer"
harness = "codex"
pool = "test"
[seats.spock]
session = "crew"
role = "systems"
harness = "codex"
pool = "test"
[seats.desk]
session = "office"
role = "desk"
harness = "codex"
pool = "test"
[seats.aide]
session = "office"
role = "counsel"
harness = "codex"
pool = "test"
'''
REFUSED = {'message': 'Synthetic refusal stand-in for an invented request.', 'codex_error_info': 'cyber_policy'}
OTHER = {'message': 'Synthetic transient trouble.', 'codex_error_info': 'internal_server_error'}


def forbidden(*args, **kwargs):
    raise AssertionError('nothing is typed, pasted or pressed here')


class World:
    """One ship, its service, and the panes the fake tmux shows."""

    def __init__(self, tmp_path: Path):
        (tmp_path / 'ship.toml').write_text(SHIP)
        self.panes: dict[str, Pane] = {}
        self.pid = 100
        self.tmux = SimpleNamespace(pane=self.panes.get, paste=forbidden, keys=forbidden,
                                    read_pane=lambda ident, history=0, **kw: None)
        self.svc = Service(shipmod.load(tmp_path), self.tmux)
        self.clock = post.now() - timedelta(minutes=30)
        self.rows: dict[str, list[dict]] = {}

    def seat(self, name):
        return self.svc.seat(name)

    def stamp(self, seconds=1):
        self.clock += timedelta(seconds=seconds)
        return self.clock.isoformat()

    def launch(self, name):
        seat = self.seat(name)
        seat.home.mkdir(parents=True, exist_ok=True)
        self.pid += 1
        pane = Pane(f'%{self.pid}', 'Crew', name, self.pid, 120, 40, False, False, None)
        self.panes[pane.id] = pane
        Launched(pane.id, pane.pid, self.stamp(), seat.pool.name, None, ['codex'], 'synthetic', 'codex').write(
            seat.folder / 'launch.json')
        record = seat.home / 'sessions' / f'rollout-{self.pid}.jsonl'
        record.parent.mkdir(parents=True, exist_ok=True)
        self.rows[name] = [{'type': 'session_meta', 'timestamp': self.stamp(), 'payload': {'id': f'synthetic-{self.pid}'}}]
        self._flush(name, record)
        (seat.folder / 'record').write_text(str(record))
        register(seat.folder, record, 'codex')
        return seat

    def _flush(self, name, record=None):
        record = record or self.seat(name).record()
        record.write_text(''.join(json.dumps(r) + '\n' for r in self.rows[name]))

    def event(self, name, payload):
        self.rows[name].append({'type': 'event_msg', 'timestamp': self.stamp(), 'payload': payload})
        self._flush(name)

    def item(self, name, payload):
        self.rows[name].append({'type': 'response_item', 'timestamp': self.stamp(), 'payload': payload})
        self._flush(name)

    def typed(self, name, *messages, result='delivered'):
        """The door typed each message on this launch and the record took it."""
        seat = self.seat(name)
        launched = seat.launched()
        attempts = []
        for message in messages:
            attempt = self.svc.attempts.open(message, name, launched.pane, launched.pid, launched.at)
            self.stamp()
            self.item(name, {'type': 'message', 'role': 'user',
                             'content': [{'type': 'input_text', 'text': post.typed([message])}]})
            attempts.append(self.svc.attempts.close(attempt, result, 'synthetic receipt'))
        return attempts

    def turn(self, name, *messages, error=None, output=False, last=None):
        self.event(name, {'type': 'task_started'})
        attempts = self.typed(name, *messages)
        if output:
            self.event(name, {'type': 'token_count', 'info': {'last_token_usage': {
                'input_tokens': 5, 'output_tokens': 9, 'reasoning_output_tokens': 4}}})
        payload = {'type': 'task_complete', 'last_agent_message': last}
        if error is not None:
            payload['error'] = error
        self.event(name, payload)
        return attempts

    def human_input(self, name, text):
        self.item(name, {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]})

    def order(self, to, body='Invented synthetic task.', sender='tester', kind='order'):
        return self.svc.store.new(kind, sender, to, body)
