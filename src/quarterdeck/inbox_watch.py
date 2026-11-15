"""Current own-watch registration; native command classification stays in the adapter.

The primary registration retains exactly its existing two pid/time fields. A
companion proof shares the canonical watch generation, never a second identity.
Old or incomplete registrations grant no idle exception.
"""

from contextlib import nullcontext
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import stat

from . import trusted_files
from . import durable, post
from .procstat import EXITED


def process(pid):
    """A live process's parent and kernel start marker, without command or environment reads."""
    try:
        if type(pid) is not int or pid <= 0:
            return None
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] in EXITED:
            return None
        return int(fields[1]), fields[19]
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def launch(runtime, name):
    try:
        raw = trusted_files.path(runtime, name, trusted_files.LAUNCH).read_bytes()
        data = json.loads(raw)
        if type(data['pid']) is not int or data['pid'] <= 0:
            return None
        at = datetime.fromisoformat(data['at'])
        if at.tzinfo is None:
            return None
        return data['pid'], at, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None


def stamp(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('watch registration is not a regular file')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def generation_path(runtime, name):
    return post.watch_file(runtime, name).with_name('watch-generation')


def proof_path(runtime, name):
    return post.watch_file(runtime, name).with_name('watch-proof.json')


def generation(runtime, name):
    value = generation_path(runtime, name).read_text().strip()
    if len(value) != 32 or any(char not in '0123456789abcdef' for char in value):
        raise ValueError('watch generation unknown')
    return value


def register(runtime, name, *, validate=None, on_registered=None, admitted=True):
    """``admitted=False`` is the jailed wait's: it cannot take the admission lock; its files are its seat folder's."""
    path = post.watch_file(runtime, name)
    with post.admission(runtime, name) if admitted else nullcontext():
        if validate is not None:
            validate()
        # Lock acquisition and validation remain interruptible. Only publication and handing its
        # token to the already guarded waiter defer SIGINT; the caller's mask is restored exactly.
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            own = process(os.getpid())
            source = launch(runtime, name)
            text = f'{os.getpid()} {post.now().isoformat(timespec="microseconds")}\n'
            token = secrets.token_hex(16)
            path.parent.mkdir(parents=True, exist_ok=True)
            # This is the same canonical generation as the waking reader consumes.
            # A partial publication cannot match the companion's complete binding.
            durable.replace_file(generation_path(runtime, name), token + '\n')
            durable.replace_file(path, text)
            info = stamp(path)
            proof = json.dumps(dict(version=1, generation=token, watch=text, stamp=info,
                                    process_start=own[1] if own else None,
                                    launch=source[2] if source else None), sort_keys=True) + '\n'
            durable.replace_file(proof_path(runtime, name), proof)
            registration = dict(watch=text, stamp=info, generation=token, proof=proof)
            if on_registered is not None:
                on_registered(registration)
            return registration
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def retire(runtime, name, registration, *, admitted=True):
    path = post.watch_file(runtime, name)
    with post.admission(runtime, name) if admitted else nullcontext():
        try:
            if stamp(path) == registration['stamp'] and path.read_text() == registration['watch']:
                try:
                    marker = generation(runtime, name)
                except FileNotFoundError:
                    post.watch_retirement_warning(path, registration['generation'])
                    return
                if marker != registration['generation']:
                    # This is still our watch, not a newer registration. Bind the warning to the
                    # observed marker so its PID cannot turn uncertainty into a not-armed reading.
                    # A later real registration writes a new generation and releases this hold.
                    post.watch_retirement_warning(path, marker)
                    return
                path.unlink()
                companion = proof_path(runtime, name)
                if companion.read_text() == registration['proof']:
                    companion.unlink()
                # The canonical generation survives watch retirement.
        except FileNotFoundError:
            pass


def current(seat, called_at):
    """Both native candidate and this seat's unchanged live generation must be proved."""
    if called_at is None or called_at.tzinfo is None:
        return False
    path = post.watch_file(seat.ship.runtime, seat.name)
    companion = proof_path(seat.ship.runtime, seat.name)
    token_path = generation_path(seat.ship.runtime, seat.name)
    try:
        before = stamp(path)
        text = path.read_text()
        proof_stamp = stamp(companion)
        raw = companion.read_text()
        proof = json.loads(raw)
        token_stamp = stamp(token_path)
        token = generation(seat.ship.runtime, seat.name)
        fields = text.split()
        if (len(fields) != 2 or type(proof['version']) is not int or proof['version'] != 1
                or proof['generation'] != token or proof['watch'] != text
                or proof['stamp'] != list(before)):
            return False
        pid = int(fields[0])
        registered = datetime.fromisoformat(fields[1])
        source = launch(seat.ship.runtime, seat.name)
        observed = process(pid)
        if (source is None or observed is None or registered.tzinfo is None
                or observed[1] != proof['process_start'] or source[2] != proof['launch']
                or not source[1] <= called_at <= registered <= post.now()):
            return False
        root, parent, seen = source[0], observed[0], {pid}
        for _ in range(64):
            if parent in seen or parent <= 0:
                return False
            ancestor = process(parent)
            if ancestor is None:
                return False
            if parent == root:
                break
            seen.add(parent)
            parent = ancestor[0]
        else:
            return False
        return (process(pid) == observed and launch(seat.ship.runtime, seat.name) == source
                and stamp(path) == before and path.read_text() == text
                and stamp(companion) == proof_stamp and companion.read_text() == raw
                and stamp(token_path) == token_stamp
                and generation(seat.ship.runtime, seat.name) == token)
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError):
        return False
