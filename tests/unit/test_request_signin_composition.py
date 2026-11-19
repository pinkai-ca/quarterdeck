"""Ordinary display composition of pending requests and existing operational alerts."""

import pytest

from quarterdeck import headlines, ledger, request_queue, watch
from quarterdeck.door import Reading


def request():
    return dict(id='request-synthetic-waitg', seat='pilot', kind='restart',
                since='2030-01-01T00:00:00+00:00', reason='tools unknown', claimed=False)


@pytest.mark.parametrize('width', [160, 2000])
def test_request_signin_and_move_names_survive_both_status_line_forms(monkeypatch, width):
    monkeypatch.setattr(headlines, 'WIDTH', width)
    row = request()
    signin = 'navigator: sign-in failed; resume unsuccessful'
    move = 'engineer: move held: queue unknown'
    facts = watch.Facts(service='ok', waiting=0, oldest=0, blind=0,
                        request_waits=[request_queue.headline(row)],
                        sign_in_failures=[signin], move_holds=[move])
    line = facts.line()
    assert request_queue.headline(row) in line
    assert signin in line
    assert move in line
    assert row['id'] not in line
    assert line.endswith(' · qd status')


def test_request_details_keep_id_wait_and_cancel_beside_signin_in_status_and_watch():
    row = request()
    signin = 'sign-in failed; resume unsuccessful'
    seat = watch.SeatReading('pilot', watch.Activity('failed', signin),
                             Reading(False, signin, 'needs a human'), None,
                             ledger.Count(), 'pool', None, 'fake')
    status = '\n'.join(watch.render_table(dict(service='ok', seats=[seat], open_orders=[],
                                              requests=[row], usage=[])))
    detail = '\n'.join(watch.render_full(dict(seat=seat, requests=[row], record=None, state=None,
                                             record_absent=None, record_limits=None, screen=None,
                                             usage=[])))
    for text in [status, detail]:
        assert signin in text
        assert row['id'] in text
        assert 'waiting: tools unknown' in text
        assert 'since ' + row['since'] in text
        assert f'cancel: qd restart pilot --cancel {row["id"]}' in text
