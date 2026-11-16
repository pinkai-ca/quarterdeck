"""Explicit-only paired reconciliation proof on a disk-owned, credential-free frozen copy."""

from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict
import cProfile
import ast
import hashlib
import json
import os
from pathlib import Path
import pstats
import re
import shutil
import socket
import sqlite3
import stat
import statistics
import subprocess
import sys
import time
import tomllib
import types

from tests import runtime_copy
from quarterdeck import pending, post, record_owner, service, service_timing, servicectl, ship as shipmod, trusted_files
from quarterdeck.seat import Seat
from quarterdeck.tmux import Tmux
from tests.measure_status_service import copy_file


FAMILIES = ('post', 'trusted', 'status', 'brief', 'request-state', 'hwa', 'proven/check-outcomes')
ROOT_FILES = ('accounting-health.json', 'pool-forecast-alerts.json', 'status-line.json',
              'retirement-status.json', 'seat-holds.json', 'server-recovery.json')


def tree_copy(source, destination, counts, family, skip=None):
    """``skip(relative)``: a file the copy leaves out, counted under 'left-out' (tests/runtime_copy.py)."""
    if not source.is_dir() or source.is_symlink():
        return
    for parent, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()]
        for name in files:
            old = Path(parent) / name
            if old.is_symlink() or name.endswith(('.lock', '.tmp', '.sh')):
                continue
            if skip is not None and skip(old.relative_to(source)):
                counts['left-out'][family] = counts['left-out'].get(family, 0) + 1
                continue
            new = destination / old.relative_to(source)
            for attempt in range(5):
                try:
                    size, _ = copy_file(old, new)
                    counts[family]['files'] += 1
                    counts[family]['bytes'] += size
                    break
                except (FileNotFoundError, RuntimeError):
                    if attempt == 4:
                        raise RuntimeError('unstable copied metadata') from None


def backup(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as old:
        with sqlite3.connect(destination) as new:
            old.backup(new)


def freeze(source, destination):
    private = runtime_copy.people(source)  # the desk's and every office seat's: by the ship file (tests/runtime_copy.py)
    source = source.resolve()
    original = shipmod.load(source)
    counts = defaultdict(lambda: dict(files=0, bytes=0))
    destination.mkdir(parents=True)
    copy_file(source / 'ship.toml', destination / 'ship.toml')
    if (source / 'orders.md').is_file():
        copy_file(source / 'orders.md', destination / 'orders.md')
    runtime = destination / 'runtime'
    # The people's seat-named files in every family (trusted/<seat>/, brief/<seat>.md, post/inbox/<seat>.md, ...),
    # and every file of an office-internal message, read for its sender and recipient alone, are left out.
    internal = runtime_copy.office_internal(original.runtime / 'post' / 'store', source, private)
    counts['left-out'] = {}
    counts['office-internal-messages'] = dict(files=len(internal), bytes=0)
    def theirs(relative):
        return runtime_copy.seat_named(relative, private) or runtime_copy.of_message(relative.name, internal)
    for family in FAMILIES:
        tree_copy(original.runtime / family, runtime / family, counts, family, skip=theirs)
    for name in ('roles',):
        tree_copy(source.parent.parent / name, destination.parent.parent / name, counts, 'standing-inputs')
    for name in ROOT_FILES:
        old = original.runtime / name
        if old.is_file() and not old.is_symlink():
            size, _ = copy_file(old, runtime / name)
            counts['root-control']['files'] += 1
            counts['root-control']['bytes'] += size
    for spec in original.seats.values():
        if spec.name in private:
            continue  # the people's own seat files stay out of the copy
        for name in ('watch', 'watch-proof.json', 'watch-generation', 'thread-reads.json'):
            old = original.runtime / 'seats' / spec.name / name
            if old.is_file() and not old.is_symlink():
                copy_file(old, runtime / 'seats' / spec.name / name)
    if (original.runtime / 'ledger.sqlite').exists():
        backup(original.runtime / 'ledger.sqlite', runtime / 'ledger.sqlite')
        counts['ledger'] = dict(files=1, bytes=(runtime / 'ledger.sqlite').stat().st_size)

    # Ownership metadata is read for all seats; private native records are never opened.
    excluded = set()
    history = set()
    for spec in original.seats.values():
        paths = set(record_owner.bindings(Seat(original, spec, object()), publish=False))
        history.update(paths)
        if spec.name in private:
            excluded.update(paths)
    current = {}
    for spec in original.seats.values():
        if spec.name in private:
            continue
        seat = Seat(original, spec, object())
        try:
            path = seat.lookup_record()
        except Exception:
            path = None
        if path is not None and path not in excluded:
            if any(path.is_relative_to(original.runtime / 'seats' / name / 'home') for name in private):
                continue
            current[path] = (seat.harness.name, spec.role)

    # Rebase all logical record names into the copy. External legacy names get an absent copy-only location.
    external = {str(path): str(runtime / 'frozen-records' / str(index) / path.name)
                for index, path in enumerate(sorted(history | set(current)))
                if not path.is_relative_to(original.runtime)}
    def mapped(path):
        return Path(external.get(str(path), str(path).replace(str(original.runtime), str(runtime))))
    record_counts = defaultdict(lambda: dict(files=0, bytes=0))
    copied = {}
    for old, (program, role) in current.items():
        new = mapped(old)
        with old.open('rb') as stream:
            sqlite = stream.read(16) == b'SQLite format 3\x00'
        if sqlite:
            backup(old, new)
        else:
            for attempt in range(5):
                try:
                    copy_file(old, new)
                    break
                except RuntimeError:
                    if attempt == 4:
                        raise RuntimeError('unstable crew record') from None
        record_counts[program + '/' + role]['files'] += 1
        record_counts[program + '/' + role]['bytes'] += new.stat().st_size
        copied[str(old)] = dict(path=str(new), program=program, role=role)

    # Copy-only read aliases, never a receipt or a rewrite of a saved journal.
    # Only records whose crew bodies were actually copied enter the allowed map.
    aliases = dict(copied=copied,
                   history=sorted({str(path) for path in history} | {str(mapped(path)) for path in history}),
                   private=sorted({str(path) for path in excluded} | {str(mapped(path)) for path in excluded}),
                   private_prefixes=[str(root / 'seats' / name / 'home') + '/'
                                     for root in (original.runtime, runtime) for name in private])
    (destination / 'frozen-native-records.json').write_text(json.dumps(aliases))

    def rewrite(value):
        if isinstance(value, str):
            return external.get(value, value.replace(str(original.runtime), str(runtime)))
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, dict):
            return {key: (2147483647 if key in ('pid', 'owner_pid') and type(item) is int else rewrite(item))
                    for key, item in value.items()}
        return value

    # Only framework metadata is interpreted here, never a native record or a message body.
    for family in ('trusted', 'status', 'request-state', 'hwa'):
        for path in (runtime / family).rglob('*') if (runtime / family).exists() else ():
            if not path.is_file():
                continue
            if path.suffix == '.json':
                try:
                    value = json.loads(path.read_text())
                except (ValueError, UnicodeError):
                    continue
                path.write_text(json.dumps(rewrite(value)) + '\n')
            elif path.name in ('record', 'record-readers.jsonl', 'record-launches.jsonl'):
                text = path.read_text()
                for old, new in external.items():
                    text = text.replace(old, new)
                path.write_text(text.replace(str(original.runtime), str(runtime)))

    text = (destination / 'ship.toml').read_text()
    text = shipmod.edit(text, 'ship', 'workspace', str(destination / 'workspace'))
    for name, pool in original.pools.items():
        if pool.config_dir is not None:
            text = shipmod.edit(text, 'pools.' + name, 'config_dir', str(destination / 'absent-signins' / name))
    for spec in original.seats.values():
        text = shipmod.edit(text, 'seats.' + spec.name, 'up', False)
        if spec.cwd:
            text = shipmod.edit(text, 'seats.' + spec.name, 'cwd', str(destination / 'workspace'))
    (destination / 'workspace').mkdir()
    (destination / 'ship.toml').write_text(text)
    return dict(families=dict(counts), current_records=dict(record_counts),
                private_record_seats_absent=len(private), historical_logical_names_absent=len(history - set(current)),
                excluded=['all credential homes and sign-ins', 'desk and office native records',
                          'historical native bodies', 'active request queue', 'native programs and live panes',
                          'live service locks, heartbeat, installed environment and process bindings'])


class Audit:
    def __init__(self, runtime, source):
        self.runtime = str(runtime)
        self.source = str(source)
        self.stack = []
        self.calls = defaultdict(Counter)
        self.blocked = Counter()
        self.confirmations = Counter()
        self.enabled = False
        self.in_os_open = False

    def guard(self, path):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return
        text = os.fsdecode(path)
        if text == self.source or text.startswith((self.source + '/', 'file:' + self.source + '/')):
            self.blocked['source-access'] += 1
            raise RuntimeError('profile source access refused')

    def note(self, operation, path):
        if not self.enabled:
            return
        if isinstance(path, int):
            try:
                path = os.readlink('/proc/self/fd/' + str(path))
            except OSError:
                return
        try:
            text = os.fsdecode(path)
        except TypeError:
            return
        prefix = self.runtime + '/'
        if text.startswith(prefix):
            parts = text[len(prefix):].split('/')
            if parts[0] == 'post':
                kind = '/'.join(parts[:2])
            elif parts[0] in ('seats', 'trusted'):
                safe = {'record', 'record-readers.jsonl', 'record-launches.jsonl', 'launch.json', 'launch-price.json',
                        'pool', 'compaction.json', 'move-hold.json', 'turn-failure.json', 'human-wake-status.json',
                        'person-quiet.json', 'human-alert.json', 'watch', 'watch-proof.json', 'watch-generation',
                        'thread-reads.json', 'model-mismatch.json', 'selection-alert.json', 'refusal.json',
                        'retiring.json', 'conversations.owned'}
                leaf = parts[-1] if parts[-1] in safe else 'other-metadata'
                kind = parts[0] + '/' + ('native-record' if 'home' in parts[2:] else leaf)
            else:
                kind = parts[0]
            self.calls[self.stack[-1] if self.stack else 'between-duties'][operation + ':' + kind] += 1

    def hook(self, event, args):
        if event == 'open':
            self.guard(args[0])
            flags = args[2]
            if not self.in_os_open:
                self.note('open-write' if flags & (os.O_WRONLY | os.O_RDWR) else 'open-read', args[0])
        elif event in ('os.listdir', 'os.scandir', 'pathlib.Path.glob', 'pathlib.Path.rglob'):
            self.guard(args[0])
            self.note(event, args[0])
        elif event == 'sqlite3.connect':
            self.guard(args[0])
        elif event == 'socket.connect' and args[0].family != socket.AF_UNIX:
            self.blocked['network'] += 1
            raise RuntimeError('profile network access refused')
        elif event == 'subprocess.Popen':
            executable = Path(args[0]).name
            if executable in ('qd', 'tmux'):
                self.blocked['qd-or-tmux-subprocess'] += 1
                raise RuntimeError('profile process or pane action refused')
            if executable not in ('git', 'stat') and not executable.startswith('python'):
                self.blocked['native-program'] += 1
                raise RuntimeError('profile native program refused')


class MeasuredTimings(service_timing.Timings):
    def __init__(self, audit):
        super().__init__()
        self.audit = audit

    @contextmanager
    def measure(self, name):
        self.audit.stack.append(name)
        try:
            with super().measure(name):
                yield
        finally:
            self.audit.stack.pop()


def profile_rows(profile):
    rows = []
    for (file, line, name), (primitive, calls, cpu, cumulative, callers) in pstats.Stats(profile).stats.items():
        parts = Path(file).parts
        label = '/'.join(parts[-2:]) if not file.startswith('~') else 'builtins'
        rows.append(dict(function=label + ':' + str(line) + ':' + name, calls=calls,
                         primitive_calls=primitive, cpu_seconds=cpu, cumulative_cpu_seconds=cumulative))
    return sorted(rows, key=lambda row: row['cumulative_cpu_seconds'], reverse=True)[:40]


def post_evidence(runtime):
    # Only aggregate digests leave the disk scratch. Original message/journal bodies stay opaque.
    families = {}
    for name in ('store', 'attempts', 'pending', 'retired/attempts', 'retired/pending'):
        rows = []
        root = runtime / 'post' / name
        for path in sorted(root.rglob('*.json')) if root.exists() else ():
            rows.append((str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest()))
        families[name] = dict(files=len(rows), digest=hashlib.sha256(json.dumps(rows).encode()).hexdigest())
    # Batch names are random. Compare the immutable intent bytes, not their random filenames.
    indexes = runtime / 'post' / 'retired' / 'index'
    intents = sorted(hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in indexes.glob('*.json')) if indexes.exists() else []
    complete = runtime / 'post' / 'retired' / 'completed'
    done = sorted(hashlib.sha256((indexes / path.name).read_bytes()).hexdigest()
                  for path in complete.glob('*.json')) if complete.exists() else []
    families['intents'] = intents
    families['completed_intents'] = done
    return families


def install_baseline(source):
    node = next(node for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name == '_reconcile_inventory')
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'baseline/pending.py', 'exec'), pending.__dict__)


def test_synthetic_abandonments_match_baseline_bytes(tmp_path, monkeypatch):
    """Invented history exercises real abandoned writes, archived rows and unchanged terminals."""
    from datetime import datetime, timezone
    from quarterdeck import post_retirement
    monkeypatch.setattr(post, 'now', lambda: datetime(2032, 1, 1, tzinfo=timezone.utc))
    source = subprocess.check_output(['git', 'show', os.environ['QD_RECONCILE_BASE']
                                     + ':src/quarterdeck/pending.py'], text=True)
    candidate = pending._reconcile_inventory
    seed = tmp_path / 'seed' / 'runtime'
    store, attempts = post.Store(seed), post.Attempts(seed)
    names = ['former-' + str(number) for number in range(8)]
    for name in names:
        message = store.new('answer', 'lead', name, 'Synthetic archived work')
        row = attempts.open(message, name, '%1', 100, '2000-01-01T00:00:00+00:00')
        attempts.close(row, 'delivered')
    retiring = types.SimpleNamespace(runtime=seed, store=store, attempts=attempts,
                                     ledger=types.SimpleNamespace(catching_up=lambda: False))
    for _ in range(20):
        result = post_retirement.run(retiring, seconds=float('inf'), complete_reads=True)
        if not result['attempts'] and not result['journals']:
            break
    assert len(list((seed / 'post' / 'retired' / 'attempts').glob('*.json'))) == len(names)
    states = (None, 'pending', 'blind', 'queued', 'uncertain', 'delivered',
              'launch', 'watch', 'abandoned', 'not-in-closed-record')
    for name in names:
        for index, state in enumerate(states):
            message = store.new('answer', 'lead', name, 'Synthetic current work')
            row = attempts.open(message, name, '%1', 100, '2000-01-01T00:00:00+00:00')
            if state is not None:
                row = attempts.close(row, state)
            if index < 2:
                proof = pending.Proof(seed / 'post' / 'pending' / name / row.path.name, {})
                proof.save(seat=name, identity=dict(pane='%1', pid=100, harness='fake', launch=row.launched_at),
                           at=row.at, since=row.at, payload=post.typed([message]), phase='queued',
                           record=None, shown=None, attempts=[row.path.name])
    results = []
    for index, variant in enumerate(('before', 'after')):
        monkeypatch.setattr(pending, '_reconcile_inventory', candidate)
        if variant == 'before':
            install_baseline(source)
        runtime = tmp_path / ('variant' + str(index)) / 'runtime'
        shutil.copytree(seed, runtime)
        actor = types.SimpleNamespace(runtime=runtime, attempts=post.Attempts(runtime),
                                      ship=types.SimpleNamespace(seats={}))
        paths = pending.reconcile(actor)
        log = (runtime / 'log' / 'service.jsonl').read_bytes()
        assert len(log.splitlines()) == 5 * len(names)
        results.append((
            {str(path.relative_to(runtime)): path.read_bytes() for path in (runtime / 'post').rglob('*.json')},
            sorted(str(path.relative_to(runtime)) for path in paths),
            [{key: value for key, value in asdict(row).items() if key != 'path'} for row in actor.attempts.all()],
            log))
        before = results[-1]
        pending.reconcile(actor)
        assert before[0] == {str(path.relative_to(runtime)): path.read_bytes()
                             for path in (runtime / 'post').rglob('*.json')}
        assert before[3] == (runtime / 'log' / 'service.jsonl').read_bytes()
    assert results[0] == results[1]


def run_child(folder, source, instrumented, variant='candidate', baseline=None):
    if variant == 'before':
        install_baseline(Path(baseline).read_text())
    plans = []
    abandon = post.Attempts.abandon
    def planned(reader, name, reason, *, attempts=None):
        plans.append(len(attempts) if attempts is not None else None)
        return abandon(reader, name, reason, attempts=attempts)
    post.Attempts.abandon = planned
    ship = shipmod.load(Path(folder))
    audit = Audit(ship.runtime, source)
    sys.addaudithook(audit.hook)
    from quarterdeck import input_receipt
    aliases = json.loads((Path(folder) / 'frozen-native-records.json').read_text())
    copied = aliases['copied']
    local = {value['path']: value for value in copied.values()}
    private, history = set(aliases['private']), set(aliases['history'])
    confirm = input_receipt.confirmed
    def copied_confirmation(reader, record, text, since):
        logical = str(record)
        if logical in copied:
            entry = copied[logical]
            audit.confirmations['mapped'] += 1
            audit.confirmations['mapped:' + entry['program'] + '/' + entry['role']] += 1
            record = Path(entry['path'])
        elif logical in local:
            audit.confirmations['already-copy'] += 1
        else:
            reason = ('private' if logical in private or logical.startswith(tuple(aliases['private_prefixes']))
                      else 'history' if logical in history else 'not-in-snapshot')
            audit.confirmations['absent:' + reason] += 1
            raise FileNotFoundError('saved record absent from the measured current crew inventory')
        return confirm(reader, record, text, since)
    input_receipt.confirmed = copied_confirmation
    def refused(name):
        def stop(*args, **kwargs):
            label = name
            if name in ('kill', 'killpg'):
                label += '-probe' if len(args) > 1 and args[1] == 0 else '-signal'
            audit.blocked[label] += 1
            raise PermissionError('profile process or pane action refused')
        return stop
    for name in ('kill', 'killpg', 'pidfd_send_signal'):
        if hasattr(os, name):
            setattr(os, name, refused(name))
    import signal
    if hasattr(signal, 'pidfd_send_signal'):
        signal.pidfd_send_signal = refused('pidfd_send_signal')

    # The controller verified a private server with no sessions. These read-only facts are frozen;
    # the measured process has no tmux-client or pane-action capability at all.
    class EmptyTmux:
        socket = os.environ['QD_TMUX_SOCKET']
        legacy = False
        def running(self): return True
        def sessions(self): return []
        def panes(self, *args): return []
        def windows(self, *args): return []
        def pane(self, *args): return None
        def has_session(self, *args): return False
        def clients_attached(self, *args): return False
        def session_environment(self, *args): return None
        def session_view(self, *args): return None
        def started(self): return None
        run = refused('tmux-client')
        _run = refused('tmux-client')
        _invoke = refused('tmux-client')
        new_session = refused('pane-action')
        kill_session = refused('pane-action')
        kill_window = refused('pane-action')
        send_keys = refused('pane-action')
        paste = refused('pane-action')
        capture = refused('pane-action')
    tmux = EmptyTmux()
    Tmux.for_ship = classmethod(lambda cls, ship: tmux)
    Tmux.run = refused('tmux-client')
    Tmux._run = refused('tmux-client')
    Tmux._invoke = refused('tmux-client')
    from quarterdeck import cli
    main = cli.main
    def own_command(argv=None):
        argv = sys.argv[1:] if argv is None else argv
        if '--ship' not in argv or Path(argv[argv.index('--ship') + 1]).resolve() != ship.folder:
            return refused('qd-outside-copy')()
        return main(argv)
    cli.main = own_command
    if instrumented:
        native_open = os.open
        def opened(path, flags, mode=0o777, *, dir_fd=None):
            absolute = path
            if dir_fd is not None and not os.path.isabs(path):
                absolute = os.path.join(os.readlink('/proc/self/fd/' + str(dir_fd)), path)
            audit.guard(absolute)
            operation = ('open-directory' if flags & os.O_DIRECTORY else
                         'open-write' if flags & (os.O_WRONLY | os.O_RDWR) else 'open-read')
            audit.note(operation, absolute)
            audit.in_os_open = True
            try:
                return native_open(path, flags, mode, dir_fd=dir_fd)
            finally:
                audit.in_os_open = False
        os.open = opened
        for name in ('stat', 'lstat'):
            original = getattr(os, name)
            def counted(path, *args, _original=original, _name=name, **kwargs):
                audit.guard(path)
                audit.note(_name, path)
                return _original(path, *args, **kwargs)
            setattr(os, name, counted)
    actor = service.Service(ship, tmux)
    actor.usage_hold = {name: time.monotonic() + 3600 for name in ship.pools}
    # Headless periodic status is profiled in demand, as the site's caller requests it.
    from quarterdeck import status_snapshot
    status_snapshot.request(ship)
    lock = servicectl.acquire_lock(ship.runtime)
    blocked = Counter()
    def no_lifecycle(*args, **kwargs):
        blocked['lifecycle'] += 1
        raise RuntimeError('native lifecycle disabled in measurement')
    for name in ('up', 'down', 'fresh', 'restart'):
        setattr(Seat, name, no_lifecycle)
    try:
        actor.first_beat()
        # Initial render/recovery/catch-up is separate from unchanged warm passes.
        priming = []
        for number in range(40):
            before = time.process_time(), time.perf_counter()
            actor.tick()
            priming.append(dict(cpu_seconds=time.process_time() - before[0], wall_seconds=time.perf_counter() - before[1]))
            driver = getattr(actor, 'account_driver', None)
            retired = getattr(actor, 'post_retirement_counts', {})
            if (number >= 2 and not (driver and driver.active) and not actor.ledger.catching_up()
                    and not any(retired.get(key, 0) for key in ('attempts', 'journals'))
                    and (getattr(actor, 'post_retirement_gate', None) is None
                         or actor.post_retirement_gate.certificate is not None)):
                break
        else:
            raise RuntimeError('copy did not finish its initial housekeeping')
        result = dict(priming=priming, passes=[], instrumented=instrumented, variant=variant)
        plans.clear()
        profiler = cProfile.Profile(timer=time.process_time)
        if instrumented:
            actor.service_timings = MeasuredTimings(audit)
            for module, name, label in ((json, 'loads', 'json-parse'), (tomllib, 'loads', 'toml-parse')):
                original = getattr(module, name)
                def parsed(*args, _original=original, _label=label, **kwargs):
                    if audit.enabled:
                        audit.calls[audit.stack[-1] if audit.stack else 'between-duties'][_label] += 1
                    return _original(*args, **kwargs)
                setattr(module, name, parsed)
        for number in range(1 if instrumented else 3):
            actor.service_timings = MeasuredTimings(audit) if instrumented else service_timing.Timings()
            # Force the whole periodic status once; other quiet passes retain real cadence.
            if number == 0:
                actor.next_status = 0
                actor.status_checked = None
                actor.status_refresh = 0
            audit.enabled = instrumented
            guard_before = Counter(audit.blocked)
            confirmations_before = Counter(audit.confirmations)
            plan_before = len(plans)
            before = time.process_time(), time.perf_counter()
            if instrumented:
                profiler.enable()
            actor.tick()
            if instrumented:
                profiler.disable()
            audit.enabled = False
            result['passes'].append(dict(cpu_seconds=time.process_time() - before[0],
                wall_seconds=time.perf_counter() - before[1], duties=actor.timings().rows,
                accounting_child_active=bool(getattr(actor, 'account_driver', None) and actor.account_driver.active),
                catching_up=actor.ledger.catching_up(), store_messages=len(actor.store.all()),
                attempt_records=len(actor.loaded_attempts or ()),
                undeclared_attempt_seats=len({row.seat for row in actor.loaded_attempts or ()} - set(ship.seats)),
                retired=dict(getattr(actor, 'post_retirement_counts', {})),
                blocked_attempts=dict(Counter(audit.blocked) - guard_before),
                confirmations=dict(Counter(audit.confirmations) - confirmations_before),
                abandonment_calls=len(plans) - plan_before,
                abandonment_planning_rows=sum(value for value in plans[plan_before:] if value is not None)))
        if instrumented:
            result['profile_top'] = profile_rows(profiler)
            result['io_by_duty'] = {name: dict(calls) for name, calls in audit.calls.items()}
        result['post_evidence'] = post_evidence(ship.runtime)
        result['attempt_results'] = hashlib.sha256(json.dumps([
            {key: value for key, value in asdict(row).items() if key != 'path'}
            for row in actor.attempts.all()], sort_keys=True).encode()).hexdigest()
        result['blocked'] = dict(blocked)
        result['blocked'].update(audit.blocked)
        result['confirmations'] = dict(audit.confirmations)
        errors, kinds, frames = Counter(), Counter(), Counter()
        for line in actor.log_path.read_text().splitlines():
            row = json.loads(line)
            if row.get('event') != 'error':
                continue
            step = row.get('step', 'unknown')
            errors[step] += 1
            kind = re.match(r'([A-Za-z_][A-Za-z_0-9]*)\(', str(row.get('error', '')))
            kinds[step + '/' + (kind.group(1) if kind else 'other')] += 1
            if step == 'reconcile':
                for file, number, function in re.findall(r'File "[^"]*/quarterdeck/([^"/]+\.py)", line (\d+), in ([A-Za-z_][A-Za-z_0-9]*)', row.get('trace', '')):
                    frames[file + ':' + number + ':' + function] += 1
        result['errors'], result['error_kinds'], result['error_frames'] = dict(errors), dict(kinds), dict(frames)
    finally:
        if driver := getattr(actor, 'account_driver', None):
            # Closing our own protocol ends the idle worker naturally; no process signal is available here.
            if driver.process is not None:
                driver.process.stdin.close()
                driver.process.wait(timeout=20)
            for child in driver.reaping:
                child.wait(timeout=20)
        actor.ledger.close()
        lock.close()
    print(json.dumps(result, sort_keys=True), flush=True)


def test_paired_reconciliation_service(tmp_path, monkeypatch, capsys, tmux):
    stage = 'snapshot'
    try:
        filesystem = subprocess.check_output(['stat', '-f', '-c', '%T', str(tmp_path)], text=True).strip()
        if filesystem in ('tmpfs', 'ramfs') or tmp_path.is_relative_to('/tmp'):
            raise RuntimeError('measurement requires disk scratch')
        source = Path(os.environ['QD_QUIET_PROFILE_SOURCE'])
        frozen = tmp_path / 'frozen' / source.name
        snapshot = freeze(source, frozen)
        # Owned server with no sessions: the service cannot see or recover the real crew's server.
        stage = 'private-tmux'
        pane = tmux.new_session('measurement-setup')
        tmux.kill_session('measurement-setup')
        assert tmux.running() and tmux.sessions() == []
        monkeypatch.setenv('QD_TMUX_SOCKET', tmux.socket)
        for key in ('QD_SHIP', 'QD_SEAT', 'TMUX', 'TMUX_PANE'):
            monkeypatch.delenv(key, raising=False)
        result = dict(snapshot=snapshot, filesystem=filesystem, samples=[])
        baseline = tmp_path / 'baseline-pending.py'
        baseline.write_text(subprocess.check_output(['git', 'show', os.environ['QD_RECONCILE_BASE'] + ':src/quarterdeck/pending.py'], text=True))
        child_environment = {key: value for key, value in os.environ.items()
                             if not key.endswith(('_API_KEY', '_TOKEN')) and key not in
                             ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'PI_CODING_AGENT_DIR', 'OPENCODE_CONFIG_DIR', 'GROK_HOME')}
        code = '''import sys, json, traceback
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tests.measure_reconcile_partition import run_child
try:
    run_child(sys.argv[2], sys.argv[3], False, sys.argv[4], sys.argv[5])
except BaseException as error:
    print(json.dumps(dict(kind=type(error).__name__, frames=[
        [Path(frame.filename).name, frame.lineno, frame.name]
        for frame in traceback.extract_tb(error.__traceback__)])), file=sys.stderr)
    raise SystemExit(1)
'''
        for index, mode in enumerate(('before', 'after', 'after', 'before', 'before', 'after')):
            stage = 'child-' + str(index)
            folder = tmp_path / ('trial' + str(index)) / source.name
            shutil.copytree(frozen, folder)
            for family in ('trusted', 'status', 'request-state', 'hwa'):
                for path in (folder / 'runtime' / family).rglob('*') if (folder / 'runtime' / family).exists() else ():
                    if path.is_file():
                        try:
                            value = path.read_text()
                        except UnicodeError:
                            continue
                        path.write_text(value.replace(str(frozen), str(folder)))
            declaration = folder / 'ship.toml'
            declaration.write_text(declaration.read_text().replace(str(frozen), str(folder)))
            mapping = folder / 'frozen-native-records.json'
            aliases = json.loads(mapping.read_text())
            for value in aliases['copied'].values():
                value['path'] = value['path'].replace(str(frozen), str(folder))
            for key in ('history', 'private', 'private_prefixes'):
                aliases[key] = [value.replace(str(frozen), str(folder)) for value in aliases[key]]
            mapping.write_text(json.dumps(aliases))
            done = subprocess.run([sys.executable, '-I', '-c', code, str(Path.cwd()), str(folder), str(source), mode, str(baseline)],
                                  capture_output=True, text=True, env=child_environment)
            if done.returncode:
                safe = json.loads(done.stderr.splitlines()[-1])
                raise RuntimeError('profile child failed: ' + json.dumps(safe))
            result['samples'].append(json.loads(done.stdout))
    except Exception as error:
        if stage.startswith('child-') and isinstance(error, RuntimeError):
            raise RuntimeError('quiet profile failed at ' + stage + ': ' + str(error)) from None
        raise RuntimeError('quiet profile failed at ' + stage + ': ' + type(error).__name__ + '; opaque data withheld') from None
    evidence = [(sample['post_evidence'], sample['attempt_results']) for sample in result['samples']]
    assert all(item == evidence[0] for item in evidence), 'frozen reconciliation bytes or results differ'
    assert all(sample['blocked'].get('source-access', 0) == 0 for sample in result['samples']), 'original source open attempted'
    with capsys.disabled():
        print('RECONCILE_PARTITION_PROOF ' + json.dumps(result, sort_keys=True), flush=True)
