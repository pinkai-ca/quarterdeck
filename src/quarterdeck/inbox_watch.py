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

from . import durable, harness, post, post_dir, trusted_files
from .harness.base import record_fingerprint
from .procstat import EXITED
from .ship import ShipError


PRINT_BOUND = 1 << 20
EVIDENCE_BOUND = 4096


def batch_digest(names):
    """Bind a print to the exact set of immutable attempt filenames."""
    return hashlib.sha256(json.dumps(sorted(names), separators=(',', ':')).encode('ascii')).hexdigest()


def print_proof(runtime, attempts, document, stream):
    """Remember the emitted document's digest and size after stdout flushes."""
    if not attempts or len(document) > PRINT_BOUND:
        return None
    try:
        token = generation(runtime, attempts[0].seat)
        raw = document.encode(stream.encoding or 'utf-8', stream.errors or 'strict')
    except (OSError, ValueError, LookupError):
        return None
    if not 0 < len(raw) <= PRINT_BOUND:
        return None
    return dict(generation=token, batch=batch_digest(a.path.name for a in attempts), size=len(raw),
                sha256=hashlib.sha256(raw).hexdigest())


def _hex(value, width):
    return isinstance(value, str) and len(value) == width and all(c in '0123456789abcdef' for c in value)


def valid_print(proof, generation):
    return (isinstance(proof, dict) and proof.keys() == {'generation', 'batch', 'size', 'sha256'}
            and _hex(generation, 32) and proof['generation'] == generation
            and type(proof['size']) is int and 0 < proof['size'] <= PRINT_BOUND
            and _hex(proof['batch'], 64) and _hex(proof['sha256'], 64))


def _evidence_folder(runtime, generation):
    # Independent of the attempt's active or retired location. Only the trusted
    # post writer can publish here, including for an offer made by the broker.
    return runtime / 'post' / 'watch-evidence' / generation


def _evidence(attempt, generation, proof=None):
    return dict(attempt=attempt.path.name, seat=attempt.seat, message=attempt.message, at=attempt.at,
                generation=generation, print=proof)


def _read_evidence(path):
    return json.loads(post_dir.read_text(path, limit=EVIDENCE_BOUND))


def retain_open(runtime, attempt, generation):
    """Publish optional evidence before the open attempt leaves the writer's lock."""
    if not _hex(generation, 32):
        return
    try:
        path = _evidence_folder(runtime, generation) / attempt.path.name
        durable.replace_file(path, json.dumps(_evidence(attempt, generation)) + '\n')
    except (OSError, ShipError):
        pass  # this evidence serves only the passive status alert


def retain_print(runtime, attempt, proof):
    """Bind a flushed print to the original opened attempt, before accepting it."""
    token = proof.get('generation') if isinstance(proof, dict) else None
    if not valid_print(proof, token):
        return
    try:
        path = _evidence_folder(runtime, token) / attempt.path.name
        if _read_evidence(path) == _evidence(attempt, token):
            durable.replace_file(path, json.dumps(_evidence(attempt, token, proof)) + '\n')
    except (OSError, ShipError, ValueError, RecursionError):
        pass  # acceptance belongs to the ordinary attempt and receipt


def accepted_batch(runtime, generation, attempts):
    """Find every accepted member of this generation and verify its exact print proof."""
    if not _hex(generation, 32):
        return [], None
    batch, proof = [], None
    try:
        for path in post_dir.json_paths(_evidence_folder(runtime, generation)):
            attempt = attempts.get(path.name)
            evidence = _read_evidence(path)
            candidate = evidence.get('print') if isinstance(evidence, dict) else None
            if (attempt is None or attempt.result != 'watch' or not valid_print(candidate, generation)
                    or evidence != _evidence(attempt, generation, candidate)
                    or (proof is not None and candidate != proof)):
                return [], None
            batch.append(attempt)
            proof = candidate
    except (OSError, ShipError, ValueError, RecursionError):
        return [], None
    if proof is None or proof['batch'] != batch_digest(a.path.name for a in batch):
        return [], None
    return sorted(batch, key=lambda a: (a.at, a.path.name)), proof


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
        identity = {f'launch_{key}': data.get(key) for key in ('at', 'pid', 'pane', 'pool', 'harness')}
        return data['pid'], at, hashlib.sha256(raw).hexdigest(), identity
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None


def _generation_evidence(seat, token, source, text, process_start):
    return dict(seat=seat, generation=token, launch=source[2], watch=text, process_start=process_start,
                **source[3])


def unoffered_path(runtime, token):
    return runtime / 'post' / 'attempts' / f'.watch-{token}-unoffered'


def _offered_path(marker, token):
    return marker.with_name(f'.watch-{token}-offered')


def _attempt_marker(runtime, token):
    marker = unoffered_path(runtime, token)
    try:
        return _read_evidence(marker)
    except FileNotFoundError:
        return _read_evidence(_offered_path(marker, token))


def _previous_registration(runtime, name, source):
    """An earlier generation of this launch must never claim the same acknowledgement."""
    try:
        token = generation(runtime, name)
    except FileNotFoundError:
        return source[1].isoformat()
    try:
        before = _attempt_marker(runtime, token)
        if before['seat'] != name or before['generation'] != token:
            return None
        if any(before[key] != value for key, value in source[3].items()):
            previous = datetime.fromisoformat(before['launch_at'])
            return source[1].isoformat() if previous < source[1] else None
        return before['watch'].split()[1]
    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError, ShipError):
        return None


def begin_attempt(runtime, token):
    if not _hex(token, 32):
        return
    marker = unoffered_path(runtime, token)
    try:
        # The initial attempt's existing directory sync also persists this rename.
        # Once it can reach the watch, no restart can resurrect the unoffered name.
        post_dir.rename(marker, _offered_path(marker, token))
    except FileNotFoundError:
        pass  # a legacy, brokered or already offered generation proves no absence


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
            if admitted and source is not None and own is not None:
                try:
                    adapter = harness.get(source[3]['launch_harness'])
                    if callable(getattr(adapter, 'inbox_watch_binding_dir', None)):
                        evidence = _generation_evidence(name, token, source, text, own[1])
                        evidence['previous'] = _previous_registration(runtime, name, source)
                        durable.replace_file(unoffered_path(runtime, token), json.dumps(evidence) + '\n')
                except (OSError, ShipError):
                    pass  # the ordinary watch can run; only proving no output needs this marker
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


def children(pid):
    """The live processes whose parent is ``pid``, each read from its own stat, or None where the process table
    cannot be read or ``pid`` is no live process. The kernel's per-task list of children is exact only for stopped
    tasks: a sibling that exits during the read can hide a live child, so it is not used."""
    try:
        if type(pid) is not int or pid <= 0 or process(pid) is None:
            return None
        found = set()
        for entry in Path('/proc').iterdir():
            if entry.name.isdigit():
                seen = process(int(entry.name))
                if seen is not None and seen[0] == pid:
                    found.add(int(entry.name))
        return found
    except (OSError, ValueError, UnicodeError):
        return None


def current(seat, called_at):
    """Both native candidate and this seat's unchanged live generation must be proved."""
    if called_at is None or called_at.tzinfo is None:
        return False
    return _live(seat, lambda launched, registered: launched <= called_at <= registered)


def alone(seat):
    """This seat's unchanged live generation, proved where no record names its call: the screen's count stands
    for the native candidate, so the process table must show the watch as that count's whole. The registered
    process has no child, its parent is a shell whose only child it is, and that shell's parent is the launched
    program. A watch started beside other work in one shell, or under anything but the program, is not proved."""
    return _live(seat, lambda launched, registered: launched <= registered, shell_of_its_own=True)


def retain_binding(seat):
    """Retain the native acknowledgement's join to the current, live watch. Called under post admission."""
    adapter = seat.harness
    binding_dir = getattr(adapter, 'inbox_watch_binding_dir', None)
    if not callable(binding_dir):
        return
    runtime = seat.ship.runtime
    try:
        token = generation(runtime, seat.name)
        evidence = _attempt_marker(runtime, token)
        previous = evidence['previous']
        record = seat.lookup_record()
        if record is None:
            return
        folder = binding_dir(record)
        if folder is None or (folder / f'{token}.json').exists():
            return
        launched = seat.launched()
        if launched is None:
            return
        before = record_fingerprint(record)
        registered = datetime.fromisoformat(evidence['watch'].split()[1])
        observed = []
        if not _live(seat, lambda at, reg: at == launched.when and reg == registered, observe=observed.append):
            return
        acknowledgement = adapter.inbox_watch_acknowledgement(record, launched, registered, previous)
        pane = seat.pane()
        if (acknowledgement is None or pane is None or pane.dead or (pane.id, pane.pid) != (launched.pane, launched.pid)
                or observed != [{key: value for key, value in evidence.items() if key != 'previous'}]
                or seat.launched() != launched or seat.lookup_record() != record
                or record_fingerprint(record) != before):
            return
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = dict(format=1, **acknowledgement, **evidence)
        durable.replace_file(folder / f'{token}.json', json.dumps(data) + '\n')
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, RecursionError, ShipError):
        return  # incomplete evidence is never a binding


def _live(seat, admitted, shell_of_its_own=False, *, observe=None):
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
                or not admitted(source[1], registered) or not registered <= post.now()):
            return False
        root, parent, seen = source[0], observed[0], {pid}
        if shell_of_its_own:
            shell = process(parent)
            if (parent == root or shell is None or shell[0] != root
                    or children(parent) != {pid} or children(pid) != set()):
                return False
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
        stable = (process(pid) == observed and launch(seat.ship.runtime, seat.name) == source
                and stamp(path) == before and path.read_text() == text
                and stamp(companion) == proof_stamp and companion.read_text() == raw
                and stamp(token_path) == token_stamp
                and generation(seat.ship.runtime, seat.name) == token)
        if stable and observe is not None:
            observe(_generation_evidence(seat.name, token, source, text, observed[1]))
        return stable
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError):
        return False
