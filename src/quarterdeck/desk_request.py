"""Parse terminal requests, bound their input, and read their replies from the post store."""

from __future__ import annotations

import errno
import json
import os
import select
import shlex
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from . import closed_pipe, copy_state, fleet, post, post_index, ship as shipmod
from .ship import ShipError

FIRST_INPUT_SECONDS = 5
INPUT_MINUTES = 10
READ_BYTES = 64 * 1024
REPLY_INTERVAL = 1.0
PRINTED_REPLY = 'printed by terminal request'
EXIT = {
    'report': 0, 'filed': 0, 'question': 1, 'not sent': 2, 'failed': 3,
    'interrupt': 130, 'broken pipe': 141,
}
QUOTE_HINT = 'not sent; quote a sentence, or put the words after -t or -n'


@dataclass(frozen=True)
class Request:
    sentence: str = ''
    ship: str | None = None
    to: str | None = None
    no_wait: bool = False
    json: bool = False
    explicit: bool = False
    error: str | None = None


def help_text() -> str:
    return f'''Terminal requests
  qd "what is everyone working on?"
  qd -t captain "look into file.txt"
  qd -t reviewer@forum "check this"
  cat crash.log | qd "why does this crash?"
  git diff | qd -t captain "review this" > review.md
  qd -n "rebuild the docs overnight"
  qd "long job" &
  timeout 10m qd "check the build" </dev/null
  git diff >"$patch" && qd "review this" <"$patch"
  qd "..." </dev/null
  qd -- "$text"

The addressee's report on this order or a question back to you ends the wait.
If the seat only acknowledges, the wait stays open until timeout(1) or Ctrl-C.
Use -t or --to for the addressee, and use timeout(1) to limit the wait.
-n or --no-wait still reads a pipe and files the order before printing its ID.
An unknown word was not sent. Quote a sentence or put the words after -t, -n or --.

A stream must give its first byte or EOF within {FIRST_INPUT_SECONDS} seconds.
Stdin reading is limited to {INPUT_MINUTES} minutes total. The UTF-8 serialized
post, including its working directory and message fields, is limited to {post.DROP_MAX_BYTES:,} bytes.

Exit status
  {EXIT['report']}  Report received or order filed with -n
  {EXIT['question']}  Question received
  {EXIT['not sent']}  Order not sent or invalid syntax
  {EXIT['failed']}  Order filed but waiting or writing the reply failed
Signals use the shell's codes, including {EXIT['interrupt']} for Ctrl-C, 124 from
timeout(1), and {EXIT['broken pipe']} for a broken pipe.'''


def candidate(argv: list[str]) -> Request | None:
    """Recognize leading terminal flags, leaving ordinary verbs to argparse."""
    ship = to = None
    json_output = no_wait = explicit = ended = False
    i = 0
    while i < len(argv):
        argument = argv[i]
        option, equals, value = argument.partition('=')
        if argument == '--':
            ended = True
            i += 1
            break
        if argument in ('-h', '--help', '--version'):
            return None
        if option in ('-t', '--to', '--ship'):
            if option != '--ship' and to is not None:
                return Request(error='specify -t or --to only once')
            if not equals:
                if i + 1 == len(argv) or argv[i + 1].startswith('-'):
                    if option == '--ship':
                        return Request(error='--ship needs a ship folder') if explicit else None
                    return Request(error=f'{option} needs a seat address')
                i += 1
                value = argv[i]
            if option == '--ship':
                ship = value
            else:
                to, explicit = value, True
        elif argument in ('-n', '--no-wait'):
            no_wait = explicit = True
        elif argument == '--json':
            json_output = True
        elif argument.startswith('-') and argument != '-':
            return Request(error=f'unrecognized argument: {argument}') if explicit else None
        else:
            break
        i += 1

    words = argv[i:]
    if explicit or ended or words == ['-']:
        sentence = ' '.join(words)
    elif not words or any(char.isspace() for char in words[0]):
        sentence = ' '.join(words).strip()
    else:
        return None
    return Request(sentence, ship, to, no_wait, json_output, explicit or ended)


def help_flag(argv: list[str]) -> list[str]:
    """Give argparse leading help/version even when preceded by terminal flags."""
    i = 0
    while i < len(argv):
        argument = argv[i]
        if argument in ('-h', '--help', '--version'):
            return [argument]
        option, equals, _ = argument.partition('=')
        if option in ('-t', '--to', '--ship'):
            i += 1 if equals else 2
        elif argument in ('-n', '--no-wait', '--json'):
            i += 1
        else:
            break
    return argv


def origin(argument: str | None = None) -> tuple[shipmod.Ship | SimpleNamespace | None, ShipError | None, bool]:
    """Find the shell's ship and whether its inbox needs an explicit --ship."""
    chosen = argument or os.environ.get('QD_SHIP')
    handed_scope = os.environ.pop(copy_state.DESK_REQUEST_SCOPE_ENV, None)
    try:
        ship = shipmod.load(shipmod.find(argument))
        return ship, None, bool(argument) or handed_scope == str(ship.folder)
    except (ShipError, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        refusal = (error if isinstance(error, ShipError)
                   else ShipError('cannot load the selected ship; check its ship.toml'))
    if chosen:
        folder = Path(chosen).expanduser().resolve()
        hint = SimpleNamespace(folder=folder, kind='office' if folder.name == 'office' else 'crew', seats={})
        selection = '--ship' if argument else 'QD_SHIP'
        return hint, ShipError(f"{refusal}; check {selection} and {folder / 'ship.toml'}"), False
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

    declaration = project / 'office' / 'ship.toml'
    try:
        try:
            mode = declaration.lstat().st_mode
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(mode):
            raise ShipError('ship.toml must be a real file')
        office = shipmod.load(office_folder(project))
        if office.kind != 'office':
            raise ShipError('declare kind = "office" in [ship]')
    except (ShipError, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        reason = str(error) if isinstance(error, ShipError) else type(error).__name__
        reason = reason.removeprefix(f'{declaration}: ')
        raise ShipError(f'cannot use the project office declaration {declaration} ({reason}); fix it and retry') from None
    return office


def destination(origin: shipmod.Ship) -> tuple[shipmod.Ship, str]:
    """Find the desk in the separate office if declared, or on the acting ship."""
    project = fleet.project_of(origin)
    office = project_office(project) if project is not None else None
    ship = office or origin
    desks = [name for name, seat in ship.seats.items() if seat.role == 'desk' and ship.session_of(seat).human]
    alternative = 'or use qd -t <seat> "your request"'
    if not desks:
        raise ShipError(f'no desk on {ship.folder.name}; declare a seat with role = "desk" in a session with '
                        f'human = true, {alternative}')
    if len(desks) != 1:
        names = ', '.join(sorted(desks))
        raise ShipError(f'more than one desk on {ship.folder.name} ({names}); keep one desk {alternative}')
    return ship, desks[0]


def addressed_ship(origin: shipmod.Ship, target: str) -> shipmod.Ship:
    if target == origin.folder.name:
        return origin
    destination = fleet.peers(origin).get(target)
    if destination is None:
        raise ShipError(f'no ship {target!r} in this project')
    return destination


def wait_for_reply(store: post.Store, message: post.Message, sender: str, recipient: str, stream, *,
                   pause=None) -> post.Message:
    """Read the exact thread even if another inbox watch already printed the reply."""
    pause = time.sleep if pause is None else pause
    with post_index.MessageIndex(store) as index:
        while True:
            check_stdout(stream)
            try:
                replies = index.replies(message.id)
            except post_index.IndexUnavailable:
                with post.reading_pass(store=store):
                    replies = store.replies(message)
            for reply in replies:
                if reply.sender == recipient and (reply.kind == 'report'
                                                  or reply.kind == 'question' and reply.to == sender):
                    return reply
            pause(REPLY_INTERVAL)


def check_stdout(stream) -> None:
    """Stop early if a waiting command's output pipe has already lost its reader."""
    if stream is None or stream.closed:
        raise OSError('stdout is closed')
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return  # an embedded stream need not have a descriptor
    poller = select.poll()
    poller.register(fd, 0)
    for _, events in poller.poll(0):
        if events & select.POLLERR and stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise BrokenPipeError()
        if events & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
            raise OSError('stdout is unavailable')


def write_stdout(stream, text) -> None:
    document = text if text.endswith('\n') else text + '\n'
    try:
        if stream is None or stream.closed:
            raise OSError('stdout is closed')
        if stream.write(document) != len(document):
            raise OSError('incomplete reply output')
        stream.flush()
    except BrokenPipeError:
        raise  # the caller retires the broken pipe and returns its signal status
    except (OSError, UnicodeError, KeyboardInterrupt):
        # The entry point must not retry a buffered write after its outcome is known.
        closed_pipe.silence('stdout')
        raise


def reply_text(reply: post.Message, *, json_output: bool) -> str:
    if json_output:
        return json.dumps(dict(kind=reply.kind, id=reply.id, re=reply.re, body=reply.body), ensure_ascii=False)
    return reply.body


def stdin_is_input(stream) -> bool:
    if stream is None or stream.closed or stream.isatty():
        return False
    try:
        info = os.fstat(stream.fileno())
    except OSError:
        return False  # read_stdin has already handled or reported the failed descriptor
    return not (stat.S_ISCHR(info.st_mode) and info.st_rdev == os.stat(os.devnull).st_rdev)


def mark_printed(runtime: Path, sender: str, reply: post.Message) -> None:
    """Keep the ordinary watch receipt for exactly the reply whose output flushed."""
    with post.admission(runtime, sender):
        attempts = post.Attempts(runtime)
        opened = attempts.open(reply, sender, None, None, None)
        attempts.close(opened, 'watch', PRINTED_REPLY)


def printed_ids(runtime: Path, sender: str) -> set[str]:
    return {attempt.message for attempt in post.Attempts(runtime).all()
            if attempt.seat == sender and attempt.result == 'watch' and attempt.detail == PRINTED_REPLY}


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
