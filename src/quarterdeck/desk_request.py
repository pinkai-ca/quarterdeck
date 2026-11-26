"""Read terminal requests without waiting indefinitely on an open input stream."""

from __future__ import annotations

import errno
import os
import select
import shlex
import shutil
import stat
import time
from pathlib import Path
from types import SimpleNamespace

from . import copy_state, fleet, post, ship as shipmod
from .ship import ShipError

FIRST_INPUT_SECONDS = 5
INPUT_MINUTES = 10
READ_BYTES = 64 * 1024


def help_text() -> str:
    return (f'From a shell outside a seat, qd "a request for the desk" or qd with UTF-8 stdin '
            f'posts to the desk. A pipe or other stream waits up to {FIRST_INPUT_SECONDS} seconds for its first '
            f'byte or EOF. Stdin reading is limited to {INPUT_MINUTES} minutes total. The UTF-8 serialized post, '
            f'including its working directory and message fields, is limited to {post.DROP_MAX_BYTES:,} bytes.')


def candidate(argv: list[str]) -> bool:
    return not argv or (len(argv) == 1 and any(char.isspace() for char in argv[0]))


def origin() -> tuple[shipmod.Ship | SimpleNamespace | None, ShipError | None, bool]:
    """Find the shell's ship, a refusal if unavailable, and whether its reply needs --ship."""
    chosen = os.environ.get('QD_SHIP')
    handed_scope = os.environ.pop(copy_state.DESK_REQUEST_SCOPE_ENV, None)
    try:
        ship = shipmod.load(shipmod.find())
        return ship, None, handed_scope == str(ship.folder)
    except (ShipError, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        refusal = (error if isinstance(error, ShipError)
                   else ShipError('cannot load the selected ship; check its ship.toml'))
    if chosen:
        folder = Path(chosen).expanduser().resolve()
        hint = SimpleNamespace(folder=folder, kind='office' if folder.name == 'office' else 'crew', seats={})
        return hint, ShipError(f'{refusal}; check QD_SHIP and the ship declaration'), False
    refusal = ShipError(f'{refusal}; to use the shortcut with a named ship, set QD_SHIP to its folder')
    try:
        ships = shipmod.ships_folder()
    except ShipError:
        return None, refusal, False
    hint = SimpleNamespace(folder=ships.parent / 'office', kind='office', seats={})
    try:
        office = project_office(ships.parent)
        if not shipmod.candidates(ships.parent) and not os.path.lexists(ships / shipmod.MARK):
            if office is not None:
                return office, None, True  # qd init can create the office before a crew exists
            return hint, ShipError('no project office or crew; run qd init to create the office'), False
    except ShipError as error:
        return hint, error, False
    except OSError:
        return hint, refusal, False
    return office or hint, refusal, False


def project_office(project: Path) -> shipmod.Ship | None:
    from .setup import office_folder

    folder = office_folder(project)
    if not folder.exists():
        return None
    try:
        if (folder / 'ship.toml').is_symlink():
            raise ShipError('ship.toml must be a real file')
        office = shipmod.load(folder)
        if office.kind != 'office':
            raise ShipError('declare kind = "office" in [ship]')
    except (ShipError, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        reason = str(error) if isinstance(error, ShipError) else type(error).__name__
        raise ShipError(f'cannot use the project office at {folder} ({reason}); fix its ship.toml and retry') from None
    return office


def destination(origin: shipmod.Ship) -> tuple[shipmod.Ship, str]:
    """Find the desk in the project's office or a crew's legacy human session."""
    project = fleet.project_of(origin)
    office = project_office(project) if project is not None else None
    peers = {**fleet.peers(origin), origin.folder.name: origin}
    if office is not None:
        peers[office.folder.name] = office
    desks = [(ship, name) for ship in peers.values() for name, seat in ship.seats.items()
             if seat.role == 'desk' and ship.session_of(seat).human]
    if not desks:
        raise ShipError('no desk in this fleet; run qd init to create the project office, or use qd post --to <seat>')
    if len(desks) != 1:
        addresses = ', '.join(sorted(f'{name}@{ship.folder.name}' for ship, name in desks))
        raise ShipError(f'more than one desk in this fleet ({addresses}); keep one office desk or use qd post --to <seat>')
    return desks[0]


def read_stdin(stream, *, clock=time.monotonic) -> tuple[str, bool]:
    """Return the input and whether the initial wait ended without a byte or EOF."""
    if stream is None or stream.closed:
        return '', False
    try:
        if stream.isatty():
            return '', False
        fd = stream.fileno()
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
        blocking = os.get_blocking(fd)
    except OSError as error:
        if error.errno == errno.EBADF:
            return '', False
        raise unreadable(error) from None
    try:
        try:
            if blocking:
                os.set_blocking(fd, False)
            return read_fd(fd, clock, regular=regular)
        finally:
            if blocking:
                os.set_blocking(fd, True)
    except OSError as error:
        raise unreadable(error) from None


def unreadable(error: OSError) -> ShipError:
    return ShipError(f'cannot read stdin ({error}); retry with a readable UTF-8 file or redirect stdin from /dev/null')


def read_fd(fd: int, clock, *, regular: bool) -> tuple[str, bool]:
    start = clock()
    total = start + INPUT_MINUTES * 60
    deadline = total if regular else start + FIRST_INPUT_SECONDS
    data = bytearray()
    while True:
        remaining = deadline - clock()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0] or clock() >= deadline:
            if not regular and not data:
                return '', True
            raise timed_out()
        try:
            chunk = os.read(fd, min(READ_BYTES, post.DROP_MAX_BYTES + 1 - len(data)))
        except BlockingIOError:
            continue  # readiness can change before the read
        if clock() >= total:
            raise timed_out()
        if not chunk:
            try:
                return data.decode('utf-8'), False
            except UnicodeDecodeError:
                raise ShipError('stdin must be UTF-8; convert the input to UTF-8 and retry') from None
        data.extend(chunk)
        if len(data) > post.DROP_MAX_BYTES:
            raise post.drop_size_error()
        deadline = total


def timed_out() -> ShipError:
    return ShipError(f'stdin did not reach EOF within {INPUT_MINUTES} minutes. '
                     'Close the pipe or redirect a complete UTF-8 file.')


def body(sentence: str, attachment: str) -> str:
    try:
        cwd = os.getcwd()
    except OSError as error:
        raise ShipError(f'cannot determine the working directory ({error}); change to an existing directory and retry') from None
    header = f'Working directory: {cwd}'
    if sentence and attachment:
        return f'{header}\n\n{sentence}\n\nAttached stdin:\n{attachment}'
    return f'{header}\n\n{sentence or attachment}'


def command_entry(executable: str) -> str:
    """Keep the original shell command across handoff to a ship's copy."""
    if handed := os.environ.pop(copy_state.DESK_REQUEST_ENTRY_ENV, None):
        return handed
    entry = Path(executable)
    return str(entry.absolute()) if entry.name == 'qd' and shutil.which('qd') is None else 'qd'


def shell_command(qd: str, scope: Path | None, *args: str) -> str:
    command = [qd]
    if scope is not None:
        command.extend(['--ship', str(scope)])
    return shlex.join([*command, *args])
