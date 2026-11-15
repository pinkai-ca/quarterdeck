"""The rendered brief's watch command in synthetic Claude background Bash rows."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import re

import pytest

from quarterdeck import ship as shipmod, standing
from quarterdeck.harness import claude


AT = datetime(2032, 4, 5, 6, 7, tzinfo=timezone.utc)


def watch_from_brief(ship):
    path = standing.render(ship, ship.seat('desk'))
    return re.search(r'Messages reach you through your watch: run `([^`]+)`', path.read_text())[1]


def background_state(record, command):
    def row(kind, second, **fields):
        return dict(type=kind, version='2.1.317',
                    timestamp=(AT + timedelta(seconds=second)).isoformat(), **fields)

    rows = [
        row('user', 0, message=dict(role='user', content='Synthetic input')),
        row('assistant', 1, message=dict(role='assistant', content=[dict(
            type='tool_use', id='call-synthetic-watch', name='Bash',
            input=dict(command=command, run_in_background=True))])),
        row('user', 2, message=dict(role='user', content=[dict(
            type='tool_result', tool_use_id='call-synthetic-watch', content='Synthetic result')]),
            toolUseResult=dict(backgroundTaskId='task-synthetic-watch')),
        row('system', 3, subtype='turn_duration'),
    ]
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return claude.state(record)


def record_in(ship):
    return ship.runtime / 'seats/desk/home/projects/synthetic/record.jsonl'


@pytest.mark.parametrize('folder', ['plain-ship', 'ship with spaces', "ship's folder"])
def test_reader_knows_the_watch_taken_from_the_rendered_brief(drydock, tmp_path, folder):
    ship = replace(shipmod.load(drydock), folder=tmp_path / folder)
    command = watch_from_brief(ship)
    if folder != 'plain-ship':
        assert command.startswith("'")  # exercise the brief's actual shell quoting
    state = background_state(record_in(ship), command)
    assert state.turn_open is False
    assert state.pending_tools == ('Bash',)  # recognition alone does not grant idle
    assert state.active_tools[0].inbox_watch_started == AT + timedelta(seconds=1)


def test_bare_watch_still_works_without_a_ship_record_layout(tmp_path):
    state = background_state(tmp_path / 'record.jsonl', 'qd inbox --wait')
    assert state.active_tools[0].inbox_watch_started == AT + timedelta(seconds=1)


@pytest.mark.parametrize('template', ['echo ready; {}', '{} && echo done', 'exec {}', '{}\necho done'])
@pytest.mark.parametrize('absolute', [False, True])
def test_watch_inside_a_longer_command_remains_ordinary_work(drydock, template, absolute):
    ship = shipmod.load(drydock)
    command = watch_from_brief(ship) if absolute else 'qd inbox --wait'
    state = background_state(record_in(ship), template.format(command))
    assert state.pending_tools == ('Bash',)
    assert state.active_tools[0].inbox_watch_started is None


def test_absolute_watch_requires_the_records_own_ship(drydock, tmp_path):
    ship = shipmod.load(drydock)
    command = watch_from_brief(ship)
    other = replace(ship, folder=tmp_path / 'other-ship')
    for record in (record_in(other), tmp_path / 'record.jsonl'):
        state = background_state(record, command)
        assert state.active_tools[0].inbox_watch_started is None
