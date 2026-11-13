"""Measured Claude API-error envelope, with invented text, paths and identities only."""
import json

START = '2001-02-03T04:05:00+00:00'
ERROR = '2001-02-03T04:05:01+00:00'
END = '2001-02-03T04:05:02+00:00'


def error_row():
    return dict(type='assistant', timestamp=ERROR, isApiErrorMessage=True,
                error='authentication_failed', isSidechain=False,
                cwd='/synthetic/workspace', entrypoint='synthetic-entry', gitBranch='synthetic-branch',
                parentUuid='synthetic-parent', perTurnEffort='synthetic-effort',
                sessionId='synthetic-session', session_id='synthetic-session',
                userType='synthetic-user', uuid='synthetic-row', version='synthetic-version',
                message=dict(container=None, context_management=None, diagnostics=None,
                             id='synthetic-api-message', model='synthetic-model', role='assistant',
                             stop_details=None, stop_reason='stop_sequence', stop_sequence='synthetic-stop',
                             type='message', content=[dict(type='text', text='Synthetic sign-in error text.')]))


def rows():
    return [dict(type='user', timestamp=START, message=dict(role='user', content='Synthetic task.')),
            error_row(), dict(type='system', subtype='turn_duration', timestamp=END)]


def write_record(path, entries=None):
    path.write_text(''.join(json.dumps(row) + '\n' for row in (rows() if entries is None else entries)))
    return path
