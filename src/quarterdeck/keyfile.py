"""The one file of API keys: where it is, what it holds, and how a pool names a key.

Every key lives in ``<the account's home>/.config/quarterdeck/keys.toml``, one line per key, ``<name> = "<key>"``. A
pool names a key with ``api_key = "keys:<name>"``, never the key. qd only reads the file (law: one-folder), at each
launch, so a changed key takes effect at the seat's next launch. The file holds credentials and no setting, so the
ship file stays a ship's only configuration (law: ship-file).

The home comes from the account, never from ``$HOME`` or ``XDG_CONFIG_HOME``, because a seat's programs run with
folders of their own and every process must read the one file.

A key is protected by its provider's limit and by never being printed. So no line here holds anything of the file's
content: a parser's error can quote the file, so none is kept or chained, and a bad name is not echoed, since a pasted
key can land on the left of the ``=``.

Tests keep off the real file by two small things. ``QD_TEST_CONFIG_HOME`` names a test's scratch config folder. A ship
inside a folder the test tool marked as its own never reads the account's file even where a test's child has emptied
its environment and lost that variable.

No record reader needs this module, and it is in no reader's stamp set.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import pwd
import re
import stat
import tomllib

TEST_CONFIG = 'QD_TEST_CONFIG_HOME'  # a test's scratch config folder; a name looks_secret passes, so its service keeps it
OWNER_MARK = '.owner'  # tests/testrunner/owned.py: what the test tool writes in a folder it made
_OWNER_TOKEN = re.compile(rb'[0-9a-f]{32}')
PREFIX = 'keys:'
RELATIVE = Path('.config') / 'quarterdeck' / 'keys.toml'
SHOWN = '~/.config/quarterdeck/keys.toml'
FILE_BYTES = 64 * 1024  # far above any list of keys; a larger file is not one
KEY_BYTES = 4096  # a key travels through one pipe into a jail, which holds more than this without a reader
_NAME = re.compile(r'[a-z0-9-]{1,40}')  # bounded: a key pasted where a name belongs is longer than any name
_FILE = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC  # non-blocking: a FIFO at the path never hangs a launch
_MARK = _FILE | os.O_NOFOLLOW

PRESENT, MISSING, REFUSED = 'present', 'missing', 'refused'


class KeyProblem(Exception):
    """Why a pool's key cannot be used, in one line that names the file and holds nothing of its content."""

    def __init__(self, state: str, line: str):
        super().__init__(line)
        self.state = state


@dataclass(frozen=True)
class Route:
    """How one program takes a key at launch. A name of a variable, never a value.

    ``variable``: the variable the launcher fills. ``helper``: the program asks a command for its key, which reads
    that variable, so its usual variable stays unset. ``login``: the program keeps the key in its own home after its
    own sign-in, read from stdin; the variable that names that home, and the file it writes there.
    """

    variable: str | None
    helper: bool = False
    login: tuple[str, ...] = ()
    login_home: str | None = None
    login_file: str | None = None
    state_file: str | None = None  # the program's own state, where a subscription's account can outlast a move
    state_account: str | None = None  # the field of that file that names the account


HELPER_VARIABLE = 'QD_PROGRAM_KEY'
HELPER_COMMAND = f'printenv {HELPER_VARIABLE}'
ENDPOINT_VARIABLE = 'QD_ENDPOINT_KEY'  # endpoint.KEY_VARIABLE, which the endpoint programs' settings name


def _routes() -> dict[str, Route]:
    from .harness.key_routes import ROUTES  # what each program takes is the adapters' knowledge
    return ROUTES


def account_file() -> Path:
    """The one key file, from the account's own home. Naming it touches nothing."""
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir) / RELATIVE
    except KeyError:
        raise KeyProblem(REFUSED, 'key file: this user has no account entry, so no key file is opened') from None


def marked(folder: Path) -> bool:
    """Whether this folder holds the test tool's owner mark: this user's regular file, read through no link."""
    try:
        descriptor = os.open(folder / OWNER_MARK, _MARK)
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and _OWNER_TOKEN.fullmatch(os.read(descriptor, 33)) is not None)
    except OSError:
        return False
    finally:
        os.close(descriptor)


def in_test_scratch(ship_folder: Path) -> bool:
    """Whether a ship's folder lies inside a folder the test tool owns, walked up from where it really is."""
    here = Path(os.path.realpath(ship_folder))
    return any(marked(folder) for folder in (here, *here.parents))


def path(ship_folder: Path | None) -> Path:
    """The file a ship reads: the account's, or in a test its scratch. With no ship, none."""
    if ship_folder is None:
        raise KeyProblem(REFUSED, 'key file: no ship names where it is, so no key file is opened')
    if scratch := os.environ.get(TEST_CONFIG):
        return Path(scratch) / 'quarterdeck' / 'keys.toml'
    if in_test_scratch(ship_folder):
        raise KeyProblem(MISSING, "no key file for a ship in the test tool's scratch")
    return account_file()


def _refused(file: Path, what: str) -> KeyProblem:
    return KeyProblem(REFUSED, f'key file {file}: {what}')


def _content(file: Path) -> bytes:
    """The bytes of the file the path names, bounded."""
    try:
        descriptor = os.open(file, _FILE)
    except FileNotFoundError:
        raise KeyProblem(MISSING, f'no key file at {file}') from None
    except OSError:
        raise _refused(file, 'it cannot be read') from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _refused(file, 'it is not a regular file')
        data = b''
        while len(data) <= FILE_BYTES and (part := os.read(descriptor, FILE_BYTES + 1 - len(data))):
            data += part
    except OSError:
        raise _refused(file, 'it cannot be read') from None
    finally:
        os.close(descriptor)
    if len(data) > FILE_BYTES:
        raise _refused(file, 'it is too large to be a list of keys')
    return data


def _parsed(data: bytes) -> dict | None:
    """The table, or None. The parser's error can quote the file, so nothing of it leaves this function."""
    try:
        return tomllib.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def read(file: Path) -> dict[str, str]:
    """Every key in the file, by name, or a KeyProblem that shows none of it."""
    table = _parsed(_content(file))
    if table is None:  # raised here, outside the parser's handler, so no exception carries its message
        raise _refused(file, 'it is not valid TOML')
    for name, value in table.items():
        if not _NAME.fullmatch(name):
            raise _refused(file, 'a key name is not lowercase letters, digits and hyphens')
        if (not isinstance(value, str) or not value or len(value.encode('utf-8', 'surrogatepass')) > KEY_BYTES
                or any(c.isspace() or not c.isprintable() for c in value)):
            raise _refused(file, 'a key is not one line of text with no spaces')
    return table


def name_of(reference: object) -> str | None:
    """The key's name in a pool's ``api_key``, or None where it is not the one accepted form."""
    if not isinstance(reference, str) or not reference.startswith(PREFIX):
        return None
    name = reference[len(PREFIX):]
    return name if _NAME.fullmatch(name) else None


def reference_refusal(pool_name: str, reference: object) -> str | None:
    """Why a ship file's ``api_key`` is refused, in a line that never holds what was written there."""
    if name_of(reference) is not None:
        return None
    return (f'pool {pool_name}: api_key must be "keys:<name>", naming a key in {SHOWN}; put the key in that file and '
            'write its name here (the key itself, an env: form and a file: form are not accepted)')


def value(pool) -> str:
    """The pool's key, for the one moment a launch or a check needs it. Never logged, stored or put in an error."""
    name = name_of(pool.api_key)
    if name is None:
        raise KeyProblem(REFUSED, reference_refusal(pool.name, pool.api_key))
    file = path(getattr(pool, 'ship_folder', None))
    keys = read(file)
    if name not in keys:
        raise KeyProblem(MISSING, f'key {name} is not in {file}')
    return keys[name]


def problem(pool) -> KeyProblem | None:
    """What stands between this pool and its key, or None. A pool no key pays for has nothing in the way."""
    if not getattr(pool, 'paid_by_key', False):
        return None
    try:
        value(pool)
    except KeyProblem as found:
        return found
    return None


def usable(pool) -> bool:
    return problem(pool) is None


def route(harness_name: str, pool) -> Route | None:
    """How this program takes this pool's key; None where no key pays for the pool or the program takes none."""
    return _routes().get(harness_name) if getattr(pool, 'paid_by_key', False) else None


def accepts(harness, pool, seat=None) -> str | None:
    """Why this program cannot run on this pool, or None: the program's own rule, then whether it takes a key."""
    if pool.provider == 'zai' and pool.paid_by_key:
        from .harness.key_routes import zai_accepts
        return zai_accepts(harness.name, pool, seat)
    if reason := harness.accepts(pool):
        return reason
    if pool.paid_by_key and harness.name not in _routes():
        return (f'pool {pool.name} is paid by a key, and {harness.name} takes no key; run it on a subscription, or run '
                'this key on a program that takes one')
    return None


def prepare_home(found: Route, home: Path) -> None:
    """A program that asks a command for its key is told which, in the settings its home was just built with."""
    if not found.helper:
        return
    import json

    from . import durable, seat_files

    settings_path = home / 'settings.json'
    text = seat_files.read_text(settings_path)  # the home is its jail's: never through a link planted there
    settings = json.loads(text) if text else {}
    settings['apiKeyHelper'] = HELPER_COMMAND
    durable.replace_file(settings_path, json.dumps(settings, indent=2) + '\n')
    if found.state_file:  # a subscription's account, left from before a move, is not this key's
        state_path = home / found.state_file
        if (text := seat_files.read_text(state_path)) and found.state_account in (state := json.loads(text)):
            del state[found.state_account]
            durable.replace_file(state_path, json.dumps(state, indent=2) + '\n')


def open_to_others(pool) -> bool:
    """Whether another account could read the pool's key file: a note for qd doctor, which refuses nothing for it."""
    try:
        return bool(stat.S_IMODE(os.stat(path(getattr(pool, 'ship_folder', None))).st_mode) & 0o077)
    except (KeyProblem, OSError):
        return False


def status_line(pool, *, jailed_caller: bool = False) -> str:
    """One line for qd status and qd doctor: the pool, and whether its key is present, missing or refused, naming the
    file it read. A jailed seat cannot see the file, so what it finds there is not called missing."""
    found = problem(pool)
    if found is None:
        note = ' · account balance unknown' if pool.provider == 'zai' else ''
        return f"pool {pool.name}: key present in {path(getattr(pool, 'ship_folder', None))}{note}"
    if jailed_caller and found.state == MISSING:
        return f'pool {pool.name}: key not visible from inside a jail'
    return f'pool {pool.name}: key {found.state}: {found}'
