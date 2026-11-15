"""Request latency in a copied ship running only the fake program."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import json
import threading
import time

from quarterdeck import cli, service, servicectl, ship
from quarterdeck.door import Door

from .waiting import wait_for


def test_submit_is_answered_before_later_work_in_the_first_pass(drydock, tmux, tmp_path, monkeypatch):
    current = ship.load(drydock)
    owner = service.Service(current, tmux)
    seat = owner.seat('captain')
    seat.up()
    wait_for(lambda: Door(seat).reading().open)
    # Keep the real request transport, claim, submit handler and refused answer.
    # All unrelated service duties are synthetic and explicitly controlled.
    duties = ('server_recovery reconcile file_drops groups cascades fleet_post inboxes views recoveries '
              'refusals answers looks jailed_watches deliveries follow compactions person_quiet '
              'fresh_after_order rebrief push_reminders credentials authentications model_differences '
              'failover account refresh_usage pool_forecasts retire_post').split()
    for name in (*duties, 'drain_drops', 'drain_accounting', 'heartbeat', 'status_due'):
        monkeypatch.setattr(owner, name, lambda: None)
    monkeypatch.setattr(servicectl, 'alive', lambda _: True)
    first_entered, first_release = threading.Event(), threading.Event()
    later_entered, later_release = threading.Event(), threading.Event()
    answered = threading.Event()
    before_later, dispatches = [], []
    original_log = owner.log

    def log(event, **fields):
        original_log(event, **fields)
        if event == 'request' and fields.get('kind') == 'submit':
            dispatches.append(fields['answer'])
            answered.set()

    def first():
        first_entered.set()
        assert first_release.wait(5), 'controller must release the first duty'

    def later():
        before_later.append(answered.is_set())
        later_entered.set()
        assert later_release.wait(5), 'controller must release the later duty'

    monkeypatch.setattr(owner, 'log', log)
    monkeypatch.setattr(owner, 'server_recovery', first)
    monkeypatch.setattr(owner, 'views', later)
    args = cli.build_parser().parse_args(['--ship', str(drydock), 'submit', 'captain'])

    def submit():
        started = time.monotonic()
        response = cli.cmd_submit(args)
        return response, time.monotonic() - started

    try:
        with ThreadPoolExecutor(max_workers=2) as threads:
            running = threads.submit(owner._tick_steps)
            assert first_entered.wait(5)
            client = threads.submit(submit)
            try:
                wait_for(lambda: any((owner.runtime / 'requests').glob('*.json')))
                first_release.set()
                assert later_entered.wait(5)
                try:
                    response, seconds = client.result(timeout=2)
                except TimeoutError:
                    # The old service cannot answer until the later duty ends.
                    # This controlled wait makes its first-pass latency measurable.
                    later_release.set()
                    response, seconds = client.result(timeout=5)
            finally:
                first_release.set()
                later_release.set()
            running.result(timeout=5)
        measured = dict(submit_wait_seconds=round(seconds, 6), answered_before_later_duty=before_later == [True],
                        dispatches=len(dispatches), response=response)
        (tmp_path / 'submit-timing.json').write_text(json.dumps(measured, indent=2) + '\n')
        assert response == "refused: captain's box is empty"
        assert len(dispatches) == 1 and dispatches[0] == response
        assert before_later == [True], measured
    finally:
        owner.ledger.close()
