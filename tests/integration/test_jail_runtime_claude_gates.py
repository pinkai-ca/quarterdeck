"""The ship's runtime inside a jail: only what a seat needs is there, and every tmpfs is bounded.

Real bubblewrap. A jail runs as the same user, so file modes do not keep it out: what it can see, it can read, and
what it can open, it can hold (flock needs no write access). The runtime is fenced like the fleet: an empty folder
inside, with the post, the briefs, the ship's own qd and the heartbeat shown read-only and the seat's own folders
writable. The ledger, the logs, the evidence, every usage reader's and parked sign-in, and the runtime scripts' own
folders are absent.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import cascade_runtime, ledger as ledgermod, post, post_view, sandbox, ship as shipmod, watch_broker


def clean_path() -> str:
    """A clean PATH for a planned jail: this checkout's interpreter and the system's, never a running ship's
    runtime that the test's own shell may have first on its PATH (a seat's shell does)."""
    return os.pathsep.join([str(Path(sys.executable).parent), '/usr/bin', '/bin'])


SECRET = 'synthetic-sign-in-never-inside'


def wait_for_reading(path: Path, child: subprocess.Popen, seconds: float):
    """The probe's report, once it parses whole, within the bound; the jail's own output if it never does."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            if child.poll() is not None:
                break
            time.sleep(0.02)
    pytest.fail(f'the jailed probe gave no whole report within {seconds:g}s: {child.communicate(timeout=10)}')


def namespaces_allowed() -> bool:
    if not sandbox.available():
        return False
    try:
        return subprocess.run(['bwrap', '--ro-bind', '/', '/', 'true'], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


PROBE = r'''
import fcntl, json, os, sqlite3, subprocess, sys, time
runtime, work = sys.argv[1:3]
seen, held = {}, []
for folder, folders, files in os.walk(runtime, followlinks=False):
    for name in files:
        path = os.path.join(folder, name)
        try:
            with open(path, 'rb') as stream:
                if b'synthetic-sign-in-never-inside' in stream.read():
                    seen.setdefault('secrets', []).append(path)
        except OSError:
            pass
    for name in files + folders:
        path = os.path.join(folder, name)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(path)
        except OSError:
            pass
seen['top'] = sorted(os.listdir(runtime))
try:
    connection = sqlite3.connect('file:' + os.path.join(runtime, 'ledger.sqlite') + '?mode=ro', uri=True)
    connection.execute('select count(*) from sqlite_master').fetchall()
    seen['ledger'] = 'opened'
except sqlite3.Error as error:
    seen['ledger'] = type(error).__name__
def bound(path):
    try:
        info = os.statvfs(path)
        return info.f_blocks * info.f_frsize
    except OSError as error:
        return type(error).__name__
seen['sizes'] = {name: bound(path) for name, path in (
    ('home', os.environ['QD_PROBE_HOME']), ('runtime', runtime), ('tmux', os.path.join(runtime, 'tmux')),
    ('locks', os.path.join(runtime, 'locks')))}
def write(path, size):
    try:
        with open(path, 'wb') as stream:
            stream.write(b'x' * size)
        return 'written'
    except OSError as error:
        return type(error).__name__
seen['runtime_write'] = write(os.path.join(runtime, 'planted'), 10)
seen['locks_flood'] = write(os.path.join(runtime, 'locks', 'flood'), 4 * 1024 * 1024)
seen['home_flood'] = write(os.path.join(os.environ['QD_PROBE_HOME'], 'flood'), 64 * 1024 * 1024)
with open(os.path.join(work, 'seen.json.tmp'), 'w') as stream:  # whole, then renamed: never read half-written
    json.dump(seen, stream)
os.replace(os.path.join(work, 'seen.json.tmp'), os.path.join(work, 'seen.json'))
deadline = time.monotonic() + 60
while not os.path.exists(os.path.join(work, 'done')) and time.monotonic() < deadline:
    time.sleep(0.02)
'''


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_a_jail_sees_only_what_a_seat_needs_of_the_runtime_and_every_tmpfs_is_bounded(drydock: Path, tmp_path):
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    runtime = ship.runtime
    seat = 'worker-1'
    # What the runtime holds besides what a seat needs: a usage reader's home and parked sign-ins, the ledger,
    # the service's log, evidence and a runtime script's own folder and lock.
    for relative in ('pool-usage/grok/auth.json', 'credentials/claude-primary-.credentials.json.stale',
                     'log/service.jsonl', 'evidence/kept.txt', 'grading/notes.txt'):
        path = runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SECRET)
    (runtime / 'grading' / 'dispatch.lock').write_text('')
    with ledgermod.Ledger(runtime / 'ledger.sqlite'):
        pass
    assert (runtime / 'ledger.sqlite').is_file()
    store = post.Store(runtime)
    order = store.new('order', 'captain', seat, 'Synthetic order the jail can read.')
    workspace = tmp_path / 'runtime-work'
    workspace.mkdir()
    own = runtime / 'seats' / seat
    (own / 'home').mkdir(parents=True, exist_ok=True)
    home = Path.home()
    jail = sandbox.plan(ship, seat, own, own / 'home', workspace, '/usr/bin/python3', clean_path(), home=home)
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    # The jail starts from nothing: the probe's one variable is declared, as a harness declares its program's.
    child = subprocess.Popen(sandbox.command(jail, [sys.executable, '-c', PROBE, str(runtime), str(workspace)],
                                             {'QD_PROBE_HOME': str(home)}),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        seen = wait_for_reading(workspace / 'seen.json', child, 90)
        # While the jail holds every file it can open, the ship reads its evidence outside without a refusal.
        assert json.loads(cascade_runtime.read_file(store.dir / f'{order.id}.json'))['id'] == order.id
    finally:
        (workspace / 'done').write_text('')
        try:
            child.communicate(timeout=60)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    assert 'secrets' not in seen, seen.get('secrets')
    hidden = {'pool-usage', 'credentials', 'log', 'evidence', 'grading', 'ledger.sqlite'}
    assert not hidden & set(seen['top']), seen['top']
    assert set(seen['top']) <= {*sandbox.RUNTIME_SHOWN, 'post', 'brief', 'seats', 'tmux', 'locks', 'check'}, seen['top']
    assert seen['ledger'] in ('OperationalError', 'DatabaseError'), seen['ledger']
    assert seen['runtime_write'] in ('OSError', 'PermissionError'), 'the runtime fence is read-only inside'
    assert seen['locks_flood'] == 'OSError', 'a hidden tmpfs is bounded: a large write fails'
    assert seen['home_flood'] == 'OSError', 'the hidden home is bounded too'
    sizes = seen['sizes']
    assert 0 < sizes['home'] <= sandbox.HOME_BYTES, sizes
    assert all(0 < sizes[name] <= sandbox.HIDDEN_BYTES for name in ('runtime', 'tmux', 'locks')), sizes


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_cascade_notes_append_while_a_jail_holds_every_file_it_can_open(drydock: Path, tmp_path):
    """The appender's exclusion is a ship lock: a jail holding the post's files cannot stop the cascade's notes."""
    import hashlib
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    runtime = ship.runtime
    ring = runtime / 'ring' / 'synthetic-question'
    ring.mkdir(parents=True)
    (ring / 'responses.md').write_text('first\n')
    raw = b'first\n'
    evidence = dict(size=len(raw), sha256=hashlib.sha256(raw).hexdigest(), pending='second\n')
    seat = 'worker-1'
    own = runtime / 'seats' / seat
    (own / 'home').mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / 'cascade-work'
    workspace.mkdir()
    post.Store(runtime).new('order', 'captain', seat, 'Synthetic order.')
    jail = sandbox.plan(ship, seat, own, own / 'home', workspace, '/usr/bin/python3', '/usr/bin', home=Path.home())
    hold = r'''
import fcntl, json, os, sys, time
runtime, work = sys.argv[1:3]
held = 0
for folder, folders, files in os.walk(runtime, followlinks=False):
    for name in files:
        try:
            descriptor = os.open(os.path.join(folder, name), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held += 1
        except OSError:
            pass
open(os.path.join(work, 'held.tmp'), 'w').write(json.dumps(held))
os.replace(os.path.join(work, 'held.tmp'), os.path.join(work, 'held'))
deadline = time.monotonic() + 60
while not os.path.exists(os.path.join(work, 'done')) and time.monotonic() < deadline:
    time.sleep(0.02)
'''
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    child = subprocess.Popen(sandbox.command(jail, [sys.executable, '-c', hold, str(runtime), str(workspace)]),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        assert wait_for_reading(workspace / 'held', child, 60) > 0
        notes = cascade_runtime.append_notes(ring, evidence)
    finally:
        (workspace / 'done').write_text('')
        try:
            child.communicate(timeout=60)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    assert (ring / 'responses.md').read_text() == 'first\nsecond\n'
    assert notes['pending'] is None
    assert not list(ring.glob('*.lock')), 'the notes are never their own lock'


def jailed_qd(ship: shipmod.Ship, seat: str, tmp_path: Path):
    """Run the qd of the code under test inside the seat's jail; never a qd an installed copy on the PATH names."""
    import quarterdeck

    source = Path(quarterdeck.__file__).resolve().parent.parent
    own = ship.runtime / 'seats' / seat
    (own / 'home').mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / f'{seat}-work'
    workspace.mkdir(exist_ok=True)
    jail = sandbox.plan(ship, seat, own, own / 'home', workspace, sys.executable, '/usr/bin:/bin', home=Path.home())
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    env.update(QD_SHIP=str(ship.folder), QD_SEAT=seat, PATH='/usr/bin:/bin')
    entry = 'import sys; sys.path.insert(0, sys.argv.pop(1)); from quarterdeck.cli import main; sys.exit(main(sys.argv[1:]))'

    def run(*arguments: str, timeout: float = 60, background: bool = False):
        argv = [sys.executable, '-I', '-c', entry, str(source), *arguments]
        command = sandbox.command(jail, ['timeout', str(int(timeout)), *argv])
        if background:
            return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        return subprocess.run(command, capture_output=True, text=True, env=env, timeout=timeout + 30)
    return jail, run


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_every_command_a_jailed_seat_may_run_works_inside_the_runtime_fence(drydock: Path, tmp_path):
    """The list of what the runtime shows comes from what a jailed seat really opens: each command it is told it may
    run, run inside real bwrap with only that list, and each must finish without a refusal."""
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    store = post.Store(ship.runtime)
    order = store.new('order', 'captain', 'worker-1', 'Synthetic order.')
    question = store.new('question', 'captain', 'worker-1', 'Synthetic question?')
    jail, run = jailed_qd(ship, 'worker-1', tmp_path)
    proof = ship.runtime / 'check' / 'worker-1' / 'worker-1.txt'
    outcomes = {
        'post order': run('post', 'order', '--to', 'captain', 'jailed order'),
        'post question': run('post', 'question', '--to', 'captain', 'jailed question?'),
        'post ack': run('post', 'ack', '--re', order.id, 'jailed ack'),
        'post report': run('post', 'report', '--re', order.id, 'jailed report'),
        'post answer': run('post', 'answer', '--re', question.id, 'jailed answer'),
        'thread': run('thread', order.id),
        'brief': run('brief', 'worker-1'),
        'inbox': run('inbox'),
        'check proof': subprocess.run(sandbox.command(jail, ['/bin/sh', '-c', f'printf "qd-check-synthetic\\n" > {proof}']),
                                      capture_output=True, text=True, timeout=60),
    }
    refused = {name: (done.returncode, done.stderr[-400:]) for name, done in outcomes.items() if done.returncode != 0}
    assert refused == {}, refused
    assert 'Synthetic order.' in outcomes['thread'].stdout and 'worker-1' in outcomes['brief'].stdout
    assert proof.read_text() == 'qd-check-synthetic\n'
    post.file_drops(store)
    filed = {(m.kind, m.body) for m in store.all() if m.sender == 'worker-1'}
    assert filed == {('order', 'jailed order'), ('question', 'jailed question?'), ('ack', 'jailed ack'),
                     ('report', 'jailed report'), ('answer', 'jailed answer')}, filed

    # The inbox watch, for a seat in a human's session, from inside its jail and its view of the post: it writes
    # only in its own seat folder, and the service serves it (watch_broker), as each pass does, until it prints.
    watched = store.new('order', 'captain', 'desk', 'Synthetic order for the watch.')
    _, desk = jailed_qd(ship, 'desk', tmp_path)
    logged = []
    broker = SimpleNamespace(store=store, attempts=post.Attempts(ship.runtime),
                             log=lambda event, **data: logged.append((event, data)))
    seat = SimpleNamespace(name='desk', ship=ship)
    watch = desk('inbox', '--wait', timeout=60, background=True)
    try:
        deadline = time.monotonic() + 60
        while watch.poll() is None and time.monotonic() < deadline:
            watch_broker.run(broker, seat)
            time.sleep(0.1)
        out, err = watch.communicate(timeout=30)
    finally:
        if watch.poll() is None:
            watch.kill()
            watch.wait(timeout=10)
    assert watch.returncode == 0, (watch.returncode, err[-600:], logged)
    assert 'Synthetic order for the watch.' in out
    watch_broker.run(broker, seat)  # the receipt the wait left in its own folder closes the attempt
    attempt, = post.Attempts(ship.runtime).for_message(watched.id)
    assert (attempt.seat, attempt.result) == ('desk', 'watch'), attempt


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_a_sign_in_in_a_runtime_folder_no_list_knows_is_absent_and_what_the_shown_folders_hold_is_seen(drydock: Path, tmp_path):
    """Synthetic files only. A folder the runtime grows later, under a name no list knows, is absent inside. Each
    folder the runtime shows a jail shows what it holds: so none of them may ever hold a sign-in."""
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    runtime = ship.runtime
    unknown = runtime / 'zz-grown-later' / 'deep' / 'auth.json'
    planted = {unknown: False, runtime / 'brief' / 'counsel.md': False, runtime / 'brief' / 'worker-1.md': True}
    for name in sandbox.RUNTIME_SHOWN:
        if name in ('post', 'installed.json', 'service.heartbeat'):
            continue  # the post is the seats' own exchange; the two files are not folders
        folder = runtime / name
        folder.mkdir(parents=True, exist_ok=True)
        planted[folder / 'synthetic-planted-auth.json'] = True
    for path in planted:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SECRET)
    store = post.Store(runtime)
    store.new('order', 'captain', 'worker-1', 'Synthetic order.')
    jail, _ = jailed_qd(ship, 'worker-1', tmp_path)
    probe = ('import json, sys, os; print(json.dumps({p: os.path.exists(p) and open(p).read() == sys.argv[1] '
             'for p in sys.argv[2:]}))')
    done = subprocess.run(sandbox.command(jail, [sys.executable, '-I', '-c', probe, SECRET, *map(str, planted)]),
                          capture_output=True, text=True, timeout=60)
    seen = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else {}
    assert seen.get(str(unknown)) is False, (seen, done.stderr[-400:])
    assert {path: seen[str(path)] for path in planted} == planted, 'unknown absent; each shown folder shows what it holds'


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_a_jailed_seat_opens_only_the_post_it_is_party_to(drydock: Path, tmp_path):
    """The post inside a jail is the seat's own view of it: what it sent or received, its own inbox, nothing else. A
    message between two other seats, another seat's inbox and the drop holds cannot be opened from inside."""
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    runtime = ship.runtime
    store = post.Store(runtime)
    mine = store.new('order', 'captain', 'worker-1', 'Synthetic order to the jailed seat.')
    theirs = store.new('order', 'captain', 'desk', 'Synthetic order between two other seats.')
    for name in ('desk', 'captain'):
        post.render_inbox(runtime, store, name)
    post.render_inbox(runtime, store, 'worker-1')
    holds = runtime / 'post' / 'drop-holds'
    holds.mkdir(parents=True, exist_ok=True)
    (holds / 'synthetic.json').write_text('{"format": 1, "folder": "drop/desk", "reason": "not a message"}\n')
    jail, run = jailed_qd(ship, 'worker-1', tmp_path)
    paths = {
        'mine': runtime / 'post' / 'store' / f'{mine.id}.json',
        'theirs': runtime / 'post' / 'store' / f'{theirs.id}.json',
        'own inbox': runtime / 'post' / 'inbox' / 'worker-1.md',
        'desk inbox': runtime / 'post' / 'inbox' / 'desk.md',
        'holds': holds / 'synthetic.json',
        # The service's index of who sent and received every message, and any view's own folder, lie under
        # runtime/views, which no jail is shown.
        'index': runtime / 'views' / post_view.INDEX,
        'desk view': runtime / 'views' / 'desk' / 'post' / 'store' / f'{theirs.id}.json',
    }
    post_view.refresh(runtime, 'desk')
    assert all(path.is_file() for name, path in paths.items() if name in ('index', 'desk view'))
    probe = ('import json, sys\nout = {}\nfor name, path in zip(sys.argv[1::2], sys.argv[2::2]):\n'
             '    try:\n        open(path).read(); out[name] = "read"\n'
             '    except OSError as error:\n        out[name] = type(error).__name__\nprint(json.dumps(out))')
    done = subprocess.run(sandbox.command(jail, [sys.executable, '-I', '-c', probe,
                                                 *[str(item) for pair in paths.items() for item in (pair[0], pair[1])]]),
                          capture_output=True, text=True, timeout=60)
    seen = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else {}
    assert seen == {'mine': 'read', 'theirs': 'FileNotFoundError', 'own inbox': 'read',
                    'desk inbox': 'FileNotFoundError', 'holds': 'FileNotFoundError', 'index': 'FileNotFoundError',
                    'desk view': 'FileNotFoundError'}, (seen, done.stderr[-400:])
    thread = run('thread', theirs.id)
    assert 'between two other seats' not in thread.stdout + thread.stderr
    reply = run('post', 'report', '--re', mine.id, 'done from the view')
    assert reply.returncode == 0, reply.stderr[-400:]
    post.file_drops(store)
    assert any(m.sender == 'worker-1' and m.re == mine.id for m in store.all())


LEASE_HOLDER = r'''
import sys
from pathlib import Path
from quarterdeck import pending
runtime, name = Path(sys.argv[1]), sys.argv[2]
with pending.transient_lock(runtime, name) as owned:
    if not owned:
        raise SystemExit('busy')
    with pending.check_inbox_registration(runtime, name, True, enabled=True):
        print('held', flush=True)
        sys.stdin.read()
'''


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_a_check_seats_lease_is_shown_to_its_own_jail_alone(drydock: Path, tmp_path):
    """A temporary check seat's own jailed qd inbox proves its lease by the real files; any other jail sees no lease,
    so none can hold one and make a dead check's registration look live."""
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    runtime = ship.runtime
    check = 'check-fake-synthetic'
    post.Store(runtime).new('order', 'ship', check, 'Synthetic check task.')
    post.Store(runtime).new('order', 'captain', 'worker-1', 'Synthetic order.')
    holder = subprocess.Popen([sys.executable, '-c', LEASE_HOLDER, str(runtime), check],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'held', holder.stderr.read()
        lock = runtime / 'post' / 'transient' / f'{check}.lock'
        assert lock.is_file()
        _, own = jailed_qd(ship, check, tmp_path)
        inbox = own('inbox')
        assert inbox.returncode == 0 and 'Synthetic check task.' in inbox.stdout, inbox.stderr[-600:]
        other, _ = jailed_qd(ship, 'worker-1', tmp_path)
        probe = ('import os, sys\ntry:\n    os.open(sys.argv[1], os.O_RDONLY)\n    print("opened")\n'
                 'except OSError as error:\n    print(type(error).__name__)\n'
                 'print(sorted(os.listdir(os.path.dirname(os.path.dirname(sys.argv[1])))))')
        done = subprocess.run(sandbox.command(other, [sys.executable, '-I', '-c', probe, str(lock)]),
                              capture_output=True, text=True, timeout=60)
        lines = done.stdout.strip().splitlines()
        assert lines[0] == 'FileNotFoundError', (done.stdout, done.stderr[-400:])
        assert 'transient' not in lines[1], 'no lease folder in another seat\'s view'
    finally:
        holder.communicate('', timeout=30)
