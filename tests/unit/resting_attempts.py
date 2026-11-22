"""Made-up subscription attempts for ordinary lifecycle fixtures."""

from quarterdeck import relaunch_intent, resting_failover


UNSETTLED = ('reserved', 'launched', 'failed', 'login', 'rejected', 'quota-refused', 'releasing')


def save_attempt(seat, phase, *, pool=None):
    runtime = seat.ship.runtime
    assert not relaunch_intent.pending_resting(runtime, seat.name)
    launch = seat.launched()
    choice = resting_failover.Candidate(pool or launch.pool, ('invented account', 'invented generation'))
    attempt = resting_failover.claim(seat, choice, launch.at)
    assert attempt is not None
    if phase != 'reserved':
        assert resting_failover.outcome(runtime, attempt, phase, launch.at)
    return choice, resting_failover.attempt_for(seat)


def settle(seat, attempt):
    assert resting_failover.outcome(seat.ship.runtime, attempt, 'released', attempt.destination)
