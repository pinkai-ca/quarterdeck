"""A refused message stays withheld inside the seat's own jail (law: context-is-cache). Real bubblewrap.

Inside a jail the refusal journal is not visible: runtime/trusted is an empty tmpfs there. So the service, outside,
writes the refused message into the seat's view of the post as its notice alone, and the seat's qd inbox and qd
thread show what the view holds. The ship's own store keeps the message whole. Synthetic messages only.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime

import pytest

from quarterdeck import post, post_view, refusal, sandbox, ship as shipmod, trusted_files

from .test_jail_runtime_claude_gates import jailed_qd, namespaces_allowed

BODY = 'The invented body the provider refused.'
SUBJECT = 'Invented refused subject'


def quarantine(runtime, seat: str, message: post.Message) -> None:
    refusal.save(runtime, seat, [{
        'stage': 'held', 'launch': {}, 'turn': None, 'code': None, 'reason': 'synthetic',
        'quarantined': [{'message': message.id, 'sender': message.sender, 'kind': message.kind}],
        'notices': [{'message': message.id, 'id': 'rep-20300101-0000-ship-' + '1' * 6,
                     'at': datetime.now().isoformat()}]}])


def read_inside(jail, path) -> str:
    done = subprocess.run(sandbox.command(jail, [sys.executable, '-I', '-c',
                                                 'import sys; print(open(sys.argv[1]).read())', str(path)]),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-400:]
    return done.stdout


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_a_jailed_seat_sees_only_the_notice_of_a_refused_message_and_nothing_while_the_journal_is_unreadable(
        drydock, tmp_path):
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    runtime = ship.runtime
    store = post.Store(runtime)
    kept = store.new('order', 'captain', 'worker-1', 'Synthetic order the seat may read.')
    refused = store.new('order', 'captain', 'worker-1', BODY, subject=SUBJECT)
    original = (store.dir / f'{refused.id}.json').read_text()
    quarantine(runtime, 'worker-1', refused)
    post.render_inbox(runtime, store, 'worker-1')
    jail, run = jailed_qd(ship, 'worker-1', tmp_path)  # its plan refreshes the view, as a launch does
    stored = runtime / 'post' / 'store' / f'{refused.id}.json'

    inbox, thread = run('inbox'), run('thread', refused.id)
    assert inbox.returncode == 0 and thread.returncode == 0, (inbox.stderr[-400:], thread.stderr[-400:])
    for shown in (inbox.stdout, thread.stdout, read_inside(jail, stored)):
        assert BODY not in shown and SUBJECT not in shown, shown
    assert refusal.WITHHELD in inbox.stdout and refusal.WITHHELD in thread.stdout
    assert 'Synthetic order the seat may read.' in inbox.stdout
    probe = run('thread', kept.id)
    assert 'Synthetic order the seat may read.' in probe.stdout, 'the journal unseen inside is not every body withheld'
    assert stored.read_text() == original, 'the ship keeps the message whole'

    journal = trusted_files.path(runtime, 'worker-1', refusal.FILE)
    journal.write_text('{not json')
    post_view.refresh(runtime, 'worker-1')
    inbox = run('inbox')
    assert inbox.returncode == 0, inbox.stderr[-400:]
    for shown in (inbox.stdout, read_inside(jail, runtime / 'post' / 'store' / f'{kept.id}.json')):
        assert 'Synthetic order the seat may read.' not in shown and BODY not in shown, shown
    assert post.UNREADABLE in inbox.stdout
