"""A clock that stalls for a moment, inside one module: a test proves its timing this way, never by load.

A load form, a busy loop or a CPU quota, rarely lands a pause in a window of a few milliseconds, so it can pass a
fault for ever. This places the pause exactly: the module's monotonic clock moves on at each of its first few reads,
then runs freely, as a machine that was busy for a moment and is free again. Only the named module's clock stalls;
every other reader, the test's own safety bounds included, keeps the real one.
"""
import time as _time
from types import SimpleNamespace


def stall(monkeypatch, module, *, reads=4, seconds=0.05):
    """Give ``module`` a ``time`` whose ``monotonic()`` moves ``seconds`` on at each of its first ``reads`` reads, then
    runs freely. Returns the stand-in, whose ``reads`` counts every read made."""
    real = _time.monotonic
    shift = [0.0]
    stand_in = SimpleNamespace(**{name: getattr(_time, name) for name in dir(_time) if not name.startswith('_')})
    stand_in.reads = 0

    def monotonic():
        stand_in.reads += 1
        if stand_in.reads <= reads:
            shift[0] += seconds
        return real() + shift[0]

    stand_in.monotonic = monotonic
    monkeypatch.setattr(module, 'time', stand_in)
    return stand_in
