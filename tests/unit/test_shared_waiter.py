"""The shared test waiter retries a record read that a concurrent write invalidated, as the fake harness's own does.

Each case runs on a fake clock, against the shared waiter and against test_fakeharness's local waiter, so the two
are held to one semantics: only that one refusal is retried, within the original deadline, and the last one is
kept as the cause when the deadline passes. Everything else escapes at once.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from quarterdeck.harness.json_record import RecordError
from quarterdeck.tmux import PrivateTmuxError
from tests.integration import test_fakeharness, waiting

CHANGED = "record changed while being read"
WAITERS = {'shared': waiting, 'fakeharness': test_fakeharness}


LIMIT = 1000  # far above any honest deadline here: a renewed deadline fails the case instead of looping forever


def bounded(calls):
    if len(calls) > LIMIT:
        pytest.fail('the waiter kept reading past its deadline')  # not an Exception: no waiter can swallow it


@pytest.fixture(params=sorted(WAITERS))
def waiter(request, monkeypatch):
    module = WAITERS[request.param]
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    return SimpleNamespace(wait_for=module.wait_for, clock=clock, name=request.param)


def test_the_shared_waiter_names_the_same_refusal_text_as_the_record_reader():
    assert waiting.RECORD_CHANGED == CHANGED


def test_a_transient_record_change_is_read_again_until_it_succeeds(waiter):
    calls = []

    def reading():
        calls.append(waiter.clock.now)
        if len(calls) < 3:
            raise RecordError(CHANGED)
        return 'synthetic fresh reading'

    assert waiter.wait_for(reading, seconds=0.2) == 'synthetic fresh reading'
    assert calls == [0.0, 0.05, 0.1], 'one poll interval between reads, nothing longer'


def test_record_changes_until_the_deadline_fail_with_the_last_refusal_as_cause(waiter):
    errors = []

    def reading():
        errors.append(RecordError(CHANGED))
        bounded(errors)
        raise errors[-1]

    with pytest.raises(AssertionError, match='condition not met in time') as failed:
        waiter.wait_for(reading, seconds=0.1)
    assert len(errors) == 2 and waiter.clock.now == pytest.approx(0.1)
    assert failed.value.__cause__ is errors[-1], 'the last cause, not the first'


def test_retries_never_renew_the_original_deadline(waiter):
    seen = []

    def reading():
        seen.append(waiter.clock.now)
        bounded(seen)
        raise RecordError(CHANGED)

    with pytest.raises(AssertionError):
        waiter.wait_for(reading, seconds=1.0)
    assert max(seen) < 1.0 and len(seen) == 20 and waiter.clock.now == pytest.approx(1.0)
    assert set(waiter.clock.sleeps) == {0.05}, 'no sleep extension'


@pytest.mark.parametrize('error', [ValueError('synthetic unrelated'), RecordError('record is not UTF-8 JSON lines'),
                                   RecordError(CHANGED + ' twice'), OSError(CHANGED), KeyError(CHANGED)],
                         ids=['value', 'other-record-refusal', 'longer-text', 'plain-oserror', 'keyerror'])
def test_every_other_failure_escapes_at_once(waiter, error):
    calls = []

    def reading():
        calls.append(waiter.clock.now)
        raise error

    with pytest.raises(type(error)) as failed:
        waiter.wait_for(reading, seconds=1.0)
    assert failed.value is error and calls == [0.0], 'not retried, not wrapped'


def test_a_false_reading_after_a_record_change_still_waits_and_keeps_the_cause(waiter):
    errors, calls = [], []

    def reading():
        calls.append(waiter.clock.now)
        bounded(calls)
        if len(calls) == 1:
            errors.append(RecordError(CHANGED))
            raise errors[-1]
        return None

    with pytest.raises(AssertionError) as failed:
        waiter.wait_for(reading, seconds=0.1)
    assert failed.value.__cause__ is errors[0], 'a refusal seen during the wait is kept for the reader of the failure'


def test_the_shared_waiter_still_retries_a_private_tmux_refusal(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(waiting, 'time', SimpleNamespace(monotonic=lambda: clock.now,
                                                         sleep=lambda s: setattr(clock, 'now', clock.now + s)))
    calls = []

    def reading():
        calls.append(clock.now)
        if len(calls) == 1:
            raise PrivateTmuxError('synthetic namespace briefly unavailable')
        return 'synthetic pane'

    assert waiting.wait_for(reading, seconds=0.2) == 'synthetic pane' and len(calls) == 2


def test_the_shared_waiter_fails_plainly_when_nothing_was_refused(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(waiting, 'time', SimpleNamespace(monotonic=lambda: clock.now,
                                                         sleep=lambda s: setattr(clock, 'now', clock.now + s)))
    with pytest.raises(AssertionError, match='condition not met in time') as failed:
        waiting.wait_for(lambda: None, seconds=0.1)
    assert failed.value.__cause__ is None
