"""A held restart's notice settles only from the service's own receipt, at its one trusted place.

A receipt in the exact form the service writes, naming the notice and its request as applied, is forged in each
place a seat writes or files into: the request folder, the request's own answer (.done) and its metadata, the seat's
folder (where the seat's files lay before they moved to the trusted folder), its home, and its post drop. None of
them settles the notice: it still waits, and the trusted record reads as empty. Synthetic names only.
"""
import json

import pytest

from quarterdeck import post, request_queue, trusted_files
from tests.unit.test_held_restart_notices_settle import STEM, held
from tests.unit.test_rebrief_notices_settle import AT, store  # noqa: F401 (fixture)


def forged(message):
    """The service's own receipt form, exact, claiming the request applied."""
    row = dict(request=STEM, seat='reader', notice=message.id, state='applied', at=AT.isoformat(), stamp=[1] * 6)
    return json.dumps(dict(version=1, receipts={message.id: row}), sort_keys=True) + '\n'


PLACES = {
    'request folder': lambda runtime: runtime / 'requests' / request_queue.NOTICE_RECEIPTS,
    'request answer': lambda runtime: runtime / 'requests' / f'{STEM}.done',
    'seat folder': lambda runtime: runtime / 'seats' / 'reader' / request_queue.NOTICE_RECEIPTS,
    'seat home': lambda runtime: runtime / 'seats' / 'reader' / 'home' / request_queue.NOTICE_RECEIPTS,
    'post drop': lambda runtime: runtime / 'post' / 'drop' / 'reader' / request_queue.NOTICE_RECEIPTS,
}


@pytest.mark.parametrize('place', [*PLACES, 'request metadata'])
def test_a_receipt_forged_where_a_seat_writes_settles_nothing(store, monkeypatch, place):
    runtime = store.dir.parent.parent
    path, message = held(store, monkeypatch)
    trusted = trusted_files.path(runtime, 'reader', request_queue.NOTICE_RECEIPTS)
    if place == 'request metadata':
        meta = request_queue.metadata(path)
        meta.update(notice_terminal_state='applied', terminal='reader: restarted in synthetic pane')
        request_queue.save_metadata(path, meta)  # the request's own metadata, never processed by the service
    else:
        target = PLACES[place](runtime)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('reader: restarted in synthetic pane\n' if place == 'request answer' else forged(message))
    assert not trusted.exists(), 'the forgery never reaches the trusted receipt'
    assert request_queue.notice_stamp(runtime, 'reader') == (None,)
    assert request_queue.notice_records(runtime, 'reader') == {}
    assert request_queue.notice_state(runtime, STEM, 'reader', message.id) is None
    assert message.id not in store.settled_notices('reader'), f'{place}: nothing there is authority'
    assert post.Attempts(runtime).undelivered(store, 'reader') == [message], 'the notice still waits'
