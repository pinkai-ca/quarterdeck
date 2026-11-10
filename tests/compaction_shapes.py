"""Synthetic native histories; no record body or identity is copied from a real seat."""

from datetime import datetime, timedelta, timezone
import json

AT = datetime(2030, 1, 1, tzinfo=timezone.utc)


class Native:
    def __init__(self, path, program, conversation='synthetic-conversation'):
        self.path, self.program, self.conversation = path, program, conversation
        self.rows, self.previous, self.serial = [], None, 0
        if program == 'codex':
            self.add('session_meta', payload=dict(id=conversation))
        self.begin()

    def add(self, kind, **values):
        self.serial += 1
        row = dict(type=kind, timestamp=(AT + timedelta(seconds=self.serial)).isoformat(), **values)
        if self.program == 'claude':
            row.update(uuid=f'synthetic-{self.conversation}-{self.serial}', parentUuid=self.previous,
                       sessionId=self.conversation, isSidechain=False)
            self.previous = row['uuid']
        self.rows.append(row)
        self.flush()
        return row

    def flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in self.rows))

    def begin(self, text='Synthetic task'):
        if self.program == 'claude':
            self.add('user', message=dict(role='user', content=text))
        else:
            self.turn_id = f'synthetic-turn-{self.serial}'
            self.add('event_msg', payload=dict(type='task_started', turn_id=self.turn_id))
            self.add('response_item', payload=dict(type='message', role='user', content=[dict(type='input_text', text=text)]))

    def compact(self):
        if self.program == 'claude':
            parent = self.previous
            row = self.add('system', subtype='compact_boundary', compactMetadata=dict(trigger='auto', preTokens=100, postTokens=10))
            row.update(parentUuid=None, logicalParentUuid=parent)
            self.flush()
        else:
            self.add('compacted', payload=dict(message='Synthetic summary', replacement_history=[]))

    def call(self):
        call = f'synthetic-call-{self.serial}'
        if self.program == 'claude':
            self.add('assistant', message=dict(id=f'synthetic-step-{self.serial}', model='synthetic',
                content=[dict(type='tool_use', id=call, name='Bash', input=dict(command='synthetic attestation'))], stop_reason='tool_use'))
        else:
            self.add('response_item', payload=dict(type='function_call', name='shell_command', call_id=call,
                                                  arguments=json.dumps(dict(command='synthetic attestation'))))
        self.call_id = call

    def result(self):
        if self.program == 'claude':
            self.add('user', message=dict(role='user', content=[dict(type='tool_result', tool_use_id=self.call_id, content='Synthetic result')]))
        else:
            self.add('response_item', payload=dict(type='function_call_output', call_id=self.call_id, output='Synthetic result'))

    def finish(self):
        if self.program == 'claude':
            self.add('assistant', message=dict(id=f'synthetic-final-{self.serial}', model='synthetic',
                                              content=[dict(type='text', text='Synthetic done')], stop_reason='end_turn'))
            self.add('system', subtype='turn_duration', durationMs=1000)
        else:
            self.add('response_item', payload=dict(type='message', role='assistant', content=[dict(type='output_text', text='Synthetic done')]))
            self.add('event_msg', payload=dict(type='task_complete', turn_id=self.turn_id))
