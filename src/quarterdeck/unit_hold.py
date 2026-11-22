"""Read a declared user unit's activity and timer deadlines without changing them."""

import re
import subprocess
import time


NEAR_US = 5 * 60 * 1_000_000
NO_DEADLINE = (0, 2**64 - 1)
QUIET = ('inactive', 'failed')
UNKNOWN = 'configured user unit state could not be read'
RUNNING = 'configured user unit is running'
SOON = 'configured user timer is due within five minutes'


def valid_unit(name):
    return (isinstance(name, str) and len(name) <= 255
            and re.fullmatch(r'(?:[A-Za-z0-9_.@:-]|\\x[0-9a-fA-F]{2})+\.(?:service|timer)', name) is not None)


def _run(*command):
    return subprocess.run(command, capture_output=True, text=True, timeout=2)


def _show(name, *properties):
    keys = {'LoadState', 'ActiveState', *properties}
    result = _run('systemctl', '--user', 'show', '--all',
                  '--property=' + ','.join(sorted(keys)), '--', name)
    values = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition('=')
        if not separator or key not in keys or key in values:
            raise ValueError('invalid unit property')
        values[key] = value
    if (result.returncode in (0, 1) and values.get('LoadState') == 'not-found'
            and values.get('ActiveState') == 'inactive'):
        return None
    if result.returncode or values.keys() != keys or values['LoadState'] != 'loaded':
        raise ValueError('unknown unit state')
    return values


def _timer_due(name, wake_system):
    bus = ('busctl', '--user')
    destination = 'org.freedesktop.systemd1'
    unit = _run(*bus, 'call', destination, '/org/freedesktop/systemd1',
                destination + '.Manager', 'GetUnit', 's', name)
    path = re.fullmatch(r'o "(/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+)"', unit.stdout.strip())
    if unit.returncode or path is None:
        raise ValueError('timer unavailable')
    result = _run(*bus, 'get-property', destination, path[1], destination + '.Timer',
                  'NextElapseUSecRealtime', 'NextElapseUSecMonotonic')
    lines = result.stdout.splitlines()
    if result.returncode or len(lines) != 2 or any(not re.fullmatch(r't [0-9]{1,20}', line) for line in lines):
        raise ValueError('timer deadlines unavailable')
    deadlines = [int(line[2:]) for line in lines]
    if any(deadline > NO_DEADLINE[1] for deadline in deadlines):
        raise ValueError('timer deadline out of range')
    if wake_system == 'yes':
        monotonic = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    elif wake_system == 'no':
        monotonic = time.monotonic_ns()
    else:
        raise ValueError('timer clock unavailable')
    remaining = [deadline - now // 1000 for deadline, now in zip(deadlines, (time.time_ns(), monotonic))
                 if deadline not in NO_DEADLINE]
    if not remaining:
        raise ValueError('waiting timer without a deadline')
    return min(remaining) <= NEAR_US


def _timer(name, *, check_service):
    state = _show(name, 'SubState', 'Unit', 'WakeSystem')
    if state is None:
        return None
    if check_service:
        unit = state['Unit']
        if not valid_unit(unit) or not unit.endswith('.service'):
            raise ValueError('timer target unavailable')
        target = _show(unit)
        if target is not None and target['ActiveState'] not in QUIET:
            return RUNNING
    if state['ActiveState'] in QUIET:
        return None
    if state['ActiveState'] == 'active':
        if state['SubState'] == 'waiting':
            return SOON if _timer_due(name, state['WakeSystem']) else None
        if state['SubState'] == 'elapsed':
            return None
    return RUNNING


def reason(unit):
    if unit is None:
        return None
    try:
        if not valid_unit(unit):
            return UNKNOWN
        if unit.endswith('.timer'):
            return _timer(unit, check_service=True)
        state = _show(unit, 'TriggeredBy')
        if state is None:
            return None
        if state['ActiveState'] not in QUIET:
            return RUNNING
        for timer in state['TriggeredBy'].split():
            if timer.endswith('.timer'):
                if not valid_unit(timer):
                    return UNKNOWN
                if hold := _timer(timer, check_service=False):
                    return hold
        return None
    except (OSError, ValueError, subprocess.SubprocessError):
        return UNKNOWN
