"""One trusted pane death per seat, joined to the exact launch."""

import json
import os
import stat
from datetime import datetime

from . import durable

FILE = 'pane-death.json'  # in the seat's trusted folder
LIMIT = 512


def reading(trusted, launch):
    """Return a proved death's time and status, or None if no matching evidence reads."""
    try:
        descriptor = os.open(trusted / FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            return None
        row = json.loads(raw)
        if (row['pane'], row['pid'], row['launch_at']) != (launch.pane, launch.pid, launch.at):
            return None
        died = datetime.fromisoformat(row['died']) if row['died'] is not None else None
        status = row['status']
        if (died is not None and died.tzinfo is None) or (status is not None and type(status) is not int):
            return None
        return died, status
    except (OSError, ValueError, TypeError, KeyError):
        return None


def remember(trusted, launch, when, status):
    """Retain the facts the service could establish, including any before a restart."""
    previous = reading(trusted, launch)
    if previous is not None:
        before, exit_status = previous
        when = when if when is not None else before
        status = status if status is not None else exit_status
        if (when, status) == previous:
            return
    row = dict(pane=launch.pane, pid=launch.pid, launch_at=launch.at,
               died=when.isoformat(timespec='seconds') if when is not None else None, status=status)
    durable.replace_file(trusted / FILE, json.dumps(row) + '\n')
