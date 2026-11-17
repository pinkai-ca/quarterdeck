"""One full status collection of the service's own, profiled when a systems or engineering seat asks.

``qd profile-status`` files a request (request_queue). The service takes one at a time: its answer names the file the
next full collection's profile will be written to, and that collection runs at once, whether or not anyone watches the
status line. A second request while one is in hand is refused, naming the first.

The file holds the code's function keys (module, function, line), call counts and times, and nothing else: it is built
from pstats' own function keys and numbers alone, never from the repr of anything, so no argument, value, record's
content or path can reach it. A module is named by its place in this package or the standard library, folders
included, and any other file by a fixed word; a function name outside a narrow alphabet is replaced. Each function's
callers are kept the same way: code locations, call counts and times. The service's other threads appear in the rows
(cProfile on sys.monitoring sees every thread), and the file says so; its CPU is the collecting thread's own. The file goes to
``runtime/status-profiles/``, a folder no jail is shown (sandbox.RUNTIME_SHOWN does not name it), and holds at most
KEEP files of at most MAX_BYTES each.
"""
from __future__ import annotations

import cProfile
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pstats
import re
import sysconfig
import time

FOLDER = 'status-profiles'
ROLES = ('systems', 'engineering')
TOP = 300  # functions kept, by cumulative time
MAX_BYTES = 256 * 1024
KEEP = 5  # newest profiles kept; older ones are removed by their exact names
NAME = re.compile(r"[A-Za-z0-9_<>.,' -]{1,120}")
SEGMENT = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}')  # one plain folder or file name: no '..', no leading dot
CALLERS = 8  # callers kept per function, by calls
NOTE = ("Other threads of the service appear in these rows: on Python 3.12 and later cProfile is built on "
        "sys.monitoring, which records every thread of the interpreter, and this tool cannot exclude them. "
        "collection.cpu_seconds is the collecting thread's own CPU (time.thread_time); wall_seconds its wall time.")
FILE = re.compile(r'\d{8}T\d{6}Z\.json')
_PACKAGE = Path(__file__).parent
_STDLIB = tuple({Path(sysconfig.get_paths()[key]) for key in ('stdlib', 'platstdlib')})


def folder(runtime: Path) -> Path:
    return Path(runtime) / FOLDER


def authority(ship, caller: str | None) -> str | None:
    """Why ``caller`` may not ask for a status profile, or None: a systems or engineering seat of this ship, never one
    in a person's session. The role is read from the ship file, as acts.authority reads it; the caller is QD_SEAT."""
    if not caller:
        return "refused: only a systems or engineering seat asks for a status profile, and QD_SEAT names no seat here"
    spec = ship.seats.get(caller)
    if spec is None:
        return f"refused: {caller} is not a seat of {ship.name}; only a systems or engineering seat asks for a status profile"
    session = ship.sessions.get(spec.session)
    if session is None or session.human:
        return f"refused: {caller} sits in a person's session; a status profile is asked by the crew's systems or engineering seat"
    if spec.role not in ROLES:
        return f"refused: {caller} is a {spec.role} seat; only a systems or engineering seat asks for a status profile"
    return None


def ask(service, caller: str | None) -> str:
    """The service's answer to a request: refused, or the path the next full collection's profile is written to."""
    if reason := authority(service.ship, caller):
        return reason
    held = getattr(service, 'status_profile', None)
    if held is not None:
        return (f"refused: a status profile asked by {held['caller']} at {held['at']} is not written yet; one at a time: "
                f"ask again once {held['path']} is written")
    at = datetime.now(timezone.utc)
    path = folder(service.runtime) / f"{at.strftime('%Y%m%dT%H%M%SZ')}.json"
    if path.exists():
        return f"refused: {path} already holds a profile from this second; ask again"
    service.status_profile = {'caller': caller, 'at': at.isoformat(timespec='seconds'), 'path': path}
    service.status_checked = None  # the next status duty collects in full at once
    service.status_refresh = 0.0
    return (f"profiling the next full status collection, at once; written to {path} when it ends "
            "(function names, call counts and times only)")


def run(service, profile: dict, collect):
    """Run ``collect`` (the full collection) under cProfile and write its profile; the request is used either way."""
    profiler = cProfile.Profile()
    began = time.thread_time(), time.perf_counter()  # the collecting thread's own CPU, not the whole process's
    try:
        profiler.enable()
    except ValueError:  # another profiler holds this thread: the collection runs unprofiled
        profiler = None
    try:
        return collect()
    finally:
        if profiler is not None:
            profiler.disable()
        service.status_profile = None
        try:
            write(profile['path'], profiler, time.thread_time() - began[0], time.perf_counter() - began[1])
        except OSError as error:
            service.log('error', step='status_profile', error=type(error).__name__)


def module_of(filename: str) -> str:
    """A code file's place, never its full path: this package's modules and the standard library's by name."""
    if filename == '~':
        return 'builtin'
    if re.fullmatch(r'<frozen [A-Za-z0-9_.]{1,80}>', filename):
        return filename
    path = Path(filename)
    for base, label in ((_PACKAGE, 'quarterdeck'), *((root, 'stdlib') for root in _STDLIB)):
        try:
            relative = path.relative_to(base).as_posix()
        except ValueError:
            continue
        parts = relative.split('/')
        if len(parts) <= 8 and all(SEGMENT.fullmatch(part) for part in parts):
            return f'{label}/{relative}'
        return label
    return 'other'


def location(key) -> tuple[str, int, str]:
    """A pstats function key as a code location only: module_of's name, the line, a plain function name or '?'."""
    filename, line, function = key
    return (module_of(str(filename)), int(line), function if isinstance(function, str) and NAME.fullmatch(function) else '?')


def rows(profiler) -> tuple[list[dict], int]:
    return rows_of(pstats.Stats(profiler).stats)


def rows_of(stats: dict) -> tuple[list[dict], int]:
    """pstats' stats as rows of code locations and numbers, each with its callers as the same; how many functions."""
    merged: dict[tuple, list] = {}
    for key, (primitive, calls, own, cumulative, callers) in stats.items():
        entry = merged.setdefault(location(key), [0, 0, 0.0, 0.0, {}])
        entry[0] += int(primitive)
        entry[1] += int(calls)
        entry[2] += float(own)
        entry[3] += float(cumulative)
        for caller, numbers in callers.items():
            # pstats keeps (primitive, calls, own, cumulative) per caller; only the count and the time are kept
            seen = entry[4].setdefault(location(caller), [0, 0.0])
            seen[0] += int(numbers[1])
            seen[1] += float(numbers[3])
    ordered = sorted(merged.items(), key=lambda item: -item[1][3])
    return ([{'module': module, 'line': line, 'function': function, 'calls': calls, 'primitive_calls': primitive,
              'own_seconds': round(own, 6), 'cumulative_seconds': round(cumulative, 6),
              'callers': [{'module': c_module, 'line': c_line, 'function': c_function, 'calls': c_calls,
                           'cumulative_seconds': round(c_cumulative, 6)}
                          for (c_module, c_line, c_function), (c_calls, c_cumulative)
                          in sorted(callers.items(), key=lambda item: (-item[1][0], item[0]))[:CALLERS]]}
             for (module, line, function), (primitive, calls, own, cumulative, callers) in ordered[:TOP]], len(merged))


def write(path: Path, profiler, cpu: float, wall: float) -> None:
    from . import durable
    data = {'format': 2, 'written': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'note': NOTE,
            'collection': {'cpu_seconds': round(cpu, 6), 'cpu_clock': 'thread_time', 'wall_seconds': round(wall, 6)}}
    if profiler is None:
        data['profiled'] = False  # another profiler held the service's thread
        listed = []
    else:
        listed, total = rows(profiler)
        data.update(profiled=True, functions=total)
    while True:
        data['rows'] = listed
        text = json.dumps(data, sort_keys=True) + '\n'
        if len(text.encode()) <= MAX_BYTES or not listed:
            break
        listed = listed[:len(listed) * 3 // 4]
    path.parent.mkdir(mode=0o700, exist_ok=True)
    durable.replace_file(path, text)
    kept = sorted(name for name in os.listdir(path.parent) if FILE.fullmatch(name))
    for name in kept[:-KEEP]:
        try:
            os.unlink(path.parent / name)
        except FileNotFoundError:
            pass
