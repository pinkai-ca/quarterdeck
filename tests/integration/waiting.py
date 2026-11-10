"""Waiting on a private tmux server: a condition polled until its deadline."""

import time

from quarterdeck.tmux import PrivateTmuxError


def wait_for(predicate, seconds: float = 10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = predicate()
        except PrivateTmuxError:
            value = None  # retry fresh evidence within the same existing deadline
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")
