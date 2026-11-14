"""The states /proc gives a process that has exited.

/proc shows an exited process as Z, a zombie, until its parent reaps it, then as X (x before Linux 4.14) for the
instant of its reaping, then not at all. Each is read as ended, never as live: a process caught between Z and its
disappearance is still one that has exited. The framework and its tests read these states from this one definition;
the test tool, which runs without the framework, keeps its own in tests/testrunner/procs.py.
"""

EXITED = frozenset({'Z', 'X', 'x'})
