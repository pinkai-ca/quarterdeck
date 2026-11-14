"""One opt-in deferred launch opening, durably held until the ordinary Door confirms it.

An opted-in adapter uses this owner so its startup privacy proof precedes every input. Nothing
retries the opening, and other adapters keep their existing launch path.
"""

from dataclasses import replace
import hashlib
import json
import time

from . import durable, pending
from .ship import ShipError


def binding(seat, launched):
    return dict(pane=launched.pane, pid=launched.pid, launch=launched.at, harness=seat.harness.name)


def path_for(seat, launched):
    key = json.dumps(binding(seat, launched), sort_keys=True).encode()
    return seat.folder / 'launch-openings' / (hashlib.sha256(key).hexdigest() + '.json')


def prepare(seat, launched, text):
    path = path_for(seat, launched)
    if path.exists() or path.is_symlink():
        raise ShipError('launch opening already recorded; never overwritten or resent')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    durable.replace_file(path, json.dumps(dict(identity=binding(seat, launched), payload=text,
                                              phase='prepared', reason='launch opening not yet confirmed')) + '\n')


class Owner:
    def __init__(self, seat, launched):
        self.seat, self.launched = seat, launched
        self.path = path_for(seat, launched)
        self.data = json.loads(self.path.read_text())
        if (self.data.get('identity') != binding(seat, launched)
                or not isinstance(self.data.get('payload'), str) or self.data.get('phase') != 'prepared'):
            raise ShipError('launch opening is not an untouched reservation; never resent')

    def save(self, **fields):
        self.data = {**self.data, **fields}
        durable.replace_file(self.path, json.dumps(self.data) + '\n')

    def verifies(self):
        try:
            return (pending.current_identity(self.seat) == self.data['identity']
                    and self.seat.launched() == self.launched
                    and self.data['phase'] == 'delivering'
                    and not self.path.is_symlink()
                    and self.seat.harness.opening_current(self.seat.home)
                    and json.loads(self.path.read_text()) == self.data)
        except (OSError, ValueError, KeyError):
            return False


def hold(seat, owner=None):
    launched = seat.launched()
    if launched is None:
        return 'launch opening has no current launch; no input'
    try:
        path = path_for(seat, launched)
        if path.is_symlink():
            return 'launch opening journal replaced; no input'
        data = json.loads(path.read_text())
        if data.get('identity') != binding(seat, launched):
            return 'launch opening identity changed; no input'
        if data.get('phase') == 'delivered':
            return None
        if isinstance(owner, Owner) and owner.path == path and owner.verifies():
            return None  # this one owner bypasses only its own opening hold
        return 'launch opening unconfirmed; ordinary delivery held; never resent'
    except (OSError, ValueError, AttributeError, TypeError):
        return 'launch opening journal unavailable or unreadable; no input'


def delivered(seat, text):
    """Read the existing owner's exact confirmation without typing or retrying."""
    launched = seat.launched()
    if launched is None:
        return False
    try:
        path = path_for(seat, launched)
        if path.is_symlink():
            return False
        data = json.loads(path.read_text())
        return (data.get('identity') == binding(seat, launched)
                and data.get('payload') == text and data.get('phase') == 'delivered')
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def deliver(seat, launched):
    from .door import Door

    owner = Owner(seat, launched)
    door = Door(seat)
    deadline = time.monotonic() + seat.harness.start_seconds
    reason = 'launch opening startup observation expired'
    try:
        while time.monotonic() < deadline:
            if pending.current_identity(seat) != owner.data['identity'] or seat.launched() != launched:
                raise ShipError('launch changed before opening; no input')
            screen = door.screen()
            ready, reason = seat.harness.opening_readiness(seat.home, screen)
            if ready:
                break
            if screen is not None and not screen.starting:
                raise ShipError(reason)
            time.sleep(0.05)
        else:
            raise ShipError(reason)
        updated = replace(launched, notice='; '.join(filter(None, (launched.notice, reason))))
        if seat.launched() != launched:
            raise ShipError('launch changed before opening proof publication; no input')
        updated.write(seat.folder / 'launch.json')
        owner.launched = updated
        owner.save(phase='delivering', startup_proof=reason)
        door.opening_owner = owner
        result = door.deliver(owner.data['payload'], verify=owner.verifies)
        if result.result != 'delivered':
            raise ShipError(f'launch opening {result}; held without retry')
        owner.save(phase='delivered', reason='exact opening confirmed by its owned record')
        return updated
    except BaseException as error:
        owner.save(phase='held', reason=str(error) if isinstance(error, ShipError) else type(error).__name__)
        raise
