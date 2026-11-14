"""Ordinary scheduling of a CLI declaration edit during an already loaded service pass."""

import subprocess
import sys

from quarterdeck import cli

from .waiting import wait_for


DRIVER = r'''
import json, sys, time
from quarterdeck import cli, request_queue, service
requests = service.Service.requests
begin = service.Service.begin_model_control
continuing = service.Service.continue_model_control

def dispatch(self):
    if (self.runtime / 'declaration-race-arm').exists() and not getattr(self, 'race_armed', False):
        self.race_armed = True
        (self.runtime / 'declaration-race-loaded').write_text(self.ship.seat('captain').model)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            paths = (self.runtime / 'requests').glob('*.json')
            if any(request_queue.load(path).get('kind') == 'control' for path in paths):
                break
            time.sleep(.005)
        else:
            raise RuntimeError('the CLI did not queue its control')
    return requests(self)

def beginning(self, seat, *args):
    self.race_started_from = seat.spec.model
    return begin(self, seat, *args)

def continuation(self, seat, *args):
    if not getattr(self, 'race_deferred', False):
        self.race_deferred = True
        self.request_wait = ('control-confirmation', 'test waits for the next service pass')
        return None
    result = continuing(self, seat, *args)
    (self.runtime / 'declaration-race-observed').write_text(json.dumps(dict(
        started_from=self.race_started_from, continued_from=seat.spec.model)))
    return result

service.Service.requests = dispatch
service.Service.begin_model_control = beginning
service.Service.continue_model_control = continuation
sys.exit(cli.main(['--ship', sys.argv[1], 'service']))
'''


def force_edit_between_load_and_dispatch(monkeypatch):
    """Let the real CLI write after the pass loads; observe the control on the following pass."""
    popen = subprocess.Popen
    setting = cli.set_seat

    def spawn(argv, *args, **kwargs):
        if isinstance(argv, list) and argv[-1:] == ['service'] and '--ship' in argv:
            folder = argv[argv.index('--ship') + 1]
            argv = [sys.executable, '-c', DRIVER, str(folder)]
        return popen(argv, *args, **kwargs)

    def edit(ship, *args, **kwargs):
        (ship.runtime / 'declaration-race-arm').touch()
        wait_for(lambda: (ship.runtime / 'declaration-race-loaded').exists(), seconds=8)
        return setting(ship, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    monkeypatch.setattr(cli, 'set_seat', edit)
