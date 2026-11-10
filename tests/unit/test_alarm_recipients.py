"""Door alarms go to the systems seat when one is up; what only a person can clear goes to the desk too."""

import ast
from dataclasses import replace
from datetime import datetime, timezone
import inspect
from types import SimpleNamespace

import pytest

from quarterdeck import pending, service, ship as shipmod
from quarterdeck.harness.base import Interruption
from quarterdeck.seat import Launched, Seat
from quarterdeck.tmux import Pane
from tests.conftest import FIXTURES

WORKER = Pane('%1', 'Crew', 'worker', 123, 120, 40, False, False, None)
SPOCK = Pane('%2', 'Bridge', 'spock', 222, 120, 40, False, False, None)


@pytest.fixture
def alarms(drydock):
    """A pi worker whose screen the test sets, and a ship whose systems seat is up, down or absent."""
    def build(systems):
        ship = shipmod.load(drydock)
        worker = replace(ship.seat('worker-1'), harness='pi')
        ship.seats[worker.name] = worker
        if systems != 'absent':
            ship.seats['spock'] = replace(ship.seat('captain'), name='spock', role='systems', persona=None)
        whole = (FIXTURES / 'pi/0.74.2/fresh.txt').read_text()
        frame = [whole.replace('qwen3.8-27b • medium', '')]  # the partial frame reads as an unknown dialog
        panes = {WORKER.id: WORKER, **({SPOCK.id: SPOCK} if systems == 'up' else {})}
        def forbidden(*args, **kwargs):
            pytest.fail('an alarm never pastes, submits or presses a key')
        tmux = SimpleNamespace(pane=panes.get, read_pane=lambda pane, history=0: (panes[pane], frame[0]),
                               keys=forbidden, paste=forbidden)
        at = datetime.now(timezone.utc).isoformat()
        seat = Seat(ship, worker, tmux)
        seat.folder.mkdir(parents=True)
        Launched(WORKER.id, WORKER.pid, at, worker.pools[0], None, ['pi'], '0.74.2', 'pi').write(seat.folder / 'launch.json')
        if systems != 'absent':
            spock = Seat(ship, ship.seat('spock'), tmux)
            spock.folder.mkdir(parents=True)
            Launched(SPOCK.id, SPOCK.pid, at, spock.spec.pools[0], None, ['fake'], None, 'fake').write(spock.folder / 'launch.json')
        svc = service.Service(ship, tmux)
        built.append(svc)

        def show(reason):
            if reason == 'login':
                seat.harness.interruptions = lambda: (Interruption('login', 'Sign in to continue', key=None),)
                frame[0] = 'Sign in to continue'
            else:
                seat.harness.interruptions = lambda: ()
                frame[0] = whole.replace('qwen3.8-27b • medium', '') if reason == 'unknown' else reason

        def told():
            """Who was told what since the last call, as (addressee, reason) pairs."""
            sent = [(m.to, m.body) for m in svc.store.all() if m.id not in seen]
            seen.update(m.id for m in svc.store.all())
            assert all(m.sender == 'ship' and m.kind == 'report' for m in svc.store.all())
            return sorted((to, body.split('obstructed, ', 1)[1]) for to, body in sent)

        seen = set()
        return SimpleNamespace(svc=svc, seat=seat, show=show, told=told, panes=panes)

    built = []
    yield build
    for svc in built:
        svc.ledger.close()


EXPECTED = {
    ('up', 'unknown'): ['spock'],
    ('up', 'login'): ['desk', 'spock'],
    ('down', 'unknown'): ['desk'],
    ('down', 'login'): ['desk'],
    ('absent', 'unknown'): ['desk'],
    ('absent', 'login'): ['desk'],
}


@pytest.mark.parametrize('systems', ['up', 'down', 'absent'])
@pytest.mark.parametrize('reason,other', [('unknown', 'login'), ('login', 'unknown')])
def test_each_alarm_reaches_its_addressees_once_and_a_changed_reason_once_more(alarms, systems, reason, other):
    a = alarms(systems)
    a.show(reason)
    a.svc.needs_human(a.seat)
    first = a.told()
    assert [to for to, _ in first] == EXPECTED[systems, reason]
    assert all(said.startswith('login' if reason == 'login' else 'unknown dialog: ') for _, said in first)

    a.svc.needs_human(a.seat)
    assert a.told() == [], 'an identical second reading tells no one'

    a.show(other)
    a.svc.needs_human(a.seat)
    assert [to for to, _ in a.told()] == EXPECTED[systems, other], 'a changed reason tells its addressees once more'
    a.svc.needs_human(a.seat)
    assert a.told() == []


def test_a_cleared_door_then_the_same_reason_tells_again(alarms):
    a = alarms('up')
    a.show('unknown')
    a.svc.needs_human(a.seat)
    assert [to for to, _ in a.told()] == ['spock']
    a.show((FIXTURES / 'pi/0.74.2/fresh.txt').read_text())
    a.svc.needs_human(a.seat)
    assert a.told() == []
    a.show('unknown')
    a.svc.needs_human(a.seat)
    assert [to for to, _ in a.told()] == ['spock']


def test_recipients_changing_under_a_standing_reason_tell_only_the_newcomer(alarms):
    a = alarms('up')
    a.show('unknown')
    del a.panes[SPOCK.id]  # the systems seat is down when the alarm first rings
    a.svc.needs_human(a.seat)
    assert [to for to, _ in a.told()] == ['desk']
    a.panes[SPOCK.id] = SPOCK  # it comes up while the same reason stands
    a.svc.needs_human(a.seat)
    assert [to for to, _ in a.told()] == ['spock']
    del a.panes[SPOCK.id]  # and goes down again: the desk already knows this reason
    a.svc.needs_human(a.seat)
    assert a.told() == []


def test_a_systems_seat_that_is_not_its_own_launch_is_not_up(alarms):
    a = alarms('up')
    a.panes[SPOCK.id] = replace(SPOCK, pid=999)  # law: identity
    a.show('unknown')
    a.svc.needs_human(a.seat)
    assert [to for to, _ in a.told()] == ['desk']


def test_an_obstructed_systems_seat_is_never_its_own_recipient(alarms):
    a = alarms('up')
    ship = a.svc.ship
    ship.seats['worker-1'] = replace(ship.seat('worker-1'), role='systems')
    a.show('unknown')
    a.svc.needs_human(a.seat)
    assert [to for to, _ in a.told()] == ['spock'], 'the other systems seat that is up takes it'
    del a.panes[SPOCK.id]
    a.show('login')
    a.svc.needs_human(a.seat)
    assert [to for to, _ in a.told()] == ['desk']


def test_every_uncertain_delivery_alarm_is_person_only():
    """The pending journal's needs-a-human reasons are the uncertain paste/submit outcomes, named once in service."""
    tree = ast.parse(inspect.getsource(pending))
    reasons = []
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef) and function.name in ('reading', 'held', 'detached'):
            for call in ast.walk(function):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id in ('uncertain', 'inspection')):
                    reasons += [arg.value for arg in call.args
                                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
    assert len(reasons) >= 8
    assert all(service.person_only(pending.inspection(SimpleNamespace(name='w'), r)) for r in reasons), reasons
    assert service.person_only('login')
    for other in ('unknown dialog: Pick a model', 'box not found on the screen', 'text in the box',
                  'permission prompt', 'a human owns this session; thinking exhausted'):
        assert not service.person_only(other)
