"""Retire only a broken standard stream after a command has its own outcome."""

import os
import sys


def silence(name):
    """Python flushes again at exit: redirect the failed descriptor, not its peer.

    This follows signal's SIGPIPE guidance without changing the signal handler.
    A stream supplied by an embedding caller may not have a file descriptor.
    """
    stream = getattr(sys, name)
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        setattr(sys, name, open(os.devnull, 'w'))
        return
    sink = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(sink, descriptor)
    finally:
        if sink != descriptor:
            os.close(sink)


def finish(status):
    """Flush both streams while broken pipes can still preserve the act's status."""
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name)
        if stream is not None:
            try:
                stream.flush()
            except BrokenPipeError:
                silence(name)
    return status
