"""One small reason-only pointer to a failed paste; never admission or receipt evidence."""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path

from . import durable, post_dir
from .ship import ShipError

NAME = '.pane-obstruction'
MARKER_BYTES = 4096
PHASES = {'pasting', 'withheld'}


def marker_path(journal: Path) -> Path:
    return journal.parent / NAME  # not a journal's .json suffix


def link_held(error: BaseException) -> bool:
    """Whether a refusal is the post folder's link hold, and that refusal only. At the marker, the door's optional
    diagnostic, it reads as unknown: a planted link neither breaks a reading nor escapes a written journal. The link
    is named on the status line (held_links), never followed or removed."""
    return isinstance(error, ShipError) and str(error).startswith(post_dir.LINK_HELD)


def held_links(runtime: Path) -> list[str]:
    """Each seat's marker that is a link, named as the post folder names a link. Read-only and no-follow: a seat's
    pending folder that is itself a link is named the same way. The pending walk lists only journals, so without
    this a link at a marker would be named nowhere."""
    names = []
    try:
        root = post_dir.open_directory(runtime / 'post' / 'pending', create=False)
    except FileNotFoundError:
        return names
    except ShipError as error:
        return [str(error)] if link_held(error) else names
    except OSError:
        return names
    try:
        seats = sorted(os.listdir(root))
    finally:
        os.close(root)
    for seat in seats:
        folder = runtime / 'post' / 'pending' / seat
        try:
            descriptor = post_dir.open_directory(folder, create=False)
        except ShipError as error:
            if link_held(error):
                names.append(str(error))
            continue
        except OSError:
            continue  # a journal file at this level, or a folder gone: not a marker's place
        try:
            info = os.stat(NAME, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                names.append(f'{post_dir.LINK_HELD}{folder / NAME}')
        except OSError:
            pass
        finally:
            os.close(descriptor)
    return names


def _read(path: Path, limit: int | None = None):
    if not post_dir.under_post(path):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or limit is not None and info.st_size > limit:
                    return None
                raw = stream.read(limit + 1 if limit is not None else -1)
            if limit is not None and len(raw) > limit:
                return None
            return json.loads(raw)
        except (OSError, ValueError, UnicodeError, RecursionError):
            return None
    try:
        raw = post_dir.read_bytes(path, limit=limit)
    except ShipError as error:
        if str(error) in ('post path is not a regular file', 'post path exceeds its read limit'):
            return None
        raise
    except (OSError, RecursionError):
        return None
    if limit is not None and len(raw) > limit:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return None


def _when(value):
    try:
        when = datetime.fromisoformat(value)
        return when if when.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _marker(path: Path):
    value = _read(path, MARKER_BYTES)
    if (not isinstance(value, dict) or set(value) != {'format', 'journal', 'identity', 'geometry', 'since'}
            or type(value['format']) is not int or value['format'] != 1
            or not isinstance(value['journal'], str) or Path(value['journal']).name != value['journal']
            or not value['journal'].endswith('.json') or not isinstance(value['identity'], dict)
            or not isinstance(value['geometry'], list) or len(value['geometry']) != 2
            or any(type(n) is not int or n < 1 for n in value['geometry'])
            or _when(value['since']) is None):
        return None
    identity = value['identity']
    if (set(identity) != {'pane', 'pid', 'launch', 'harness'}
            or any(not isinstance(identity[key], str) for key in ('pane', 'launch', 'harness'))
            or type(identity['pid']) is not int):
        return None
    return value


def publish(proof) -> None:
    """The existing failed-paste owner publishes only after its journal is saved."""
    data = proof.data
    if data.get('phase') not in PHASES:
        return
    value = dict(format=1, journal=proof.path.name, identity=data['identity'],
                 geometry=data['paste_geometry'], since=data['since'])
    text = json.dumps(value, ensure_ascii=True) + '\n'
    if len(text.encode('ascii')) > MARKER_BYTES:
        return  # optional reason remains unknown; never alter the journal or transport
    durable.replace_file(marker_path(proof.path), text, encoding='ascii')


def reading(seat):
    """At most the marker and its one journal; no enumeration, attempt lookup or repair."""
    directory = seat.ship.runtime / 'post' / 'pending' / seat.name
    value = _marker(directory / NAME)
    if value is None:
        return None
    journal = _read(directory / value['journal'])
    if (not isinstance(journal, dict) or journal.get('phase') not in PHASES
            or journal.get('identity') != value['identity']
            or journal.get('paste_geometry') != value['geometry']
            or journal.get('since') != value['since'] or not isinstance(journal.get('payload'), str)):
        return None
    return value, journal


def transitioned(path: Path, data) -> None:
    """Reason metadata expires on its outcome or a later record-confirmed paste."""
    if data.get('phase') in PHASES:
        return
    marker = marker_path(path)
    value = _marker(marker)
    if value is None:
        return
    own_journal = value['journal'] == path.name
    confirmed = data.get('phase') == 'delivered' or (data.get('phase') == 'queued' and not data.get('screen_queue'))
    since = _when(data.get('since'))
    later = confirmed and since is not None and since > _when(value['since'])
    if own_journal or later:
        try:
            post_dir.remove(marker)
        except FileNotFoundError:
            return
