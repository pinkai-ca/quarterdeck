"""Protections of a check host's own filing, with no service running: it files only its own seat's drops,
holds every forged, linked or special drop exactly as the service does, stands aside while a service is alive,
and never files one drop twice with another filer. Synthetic runtime; real processes where a race needs them."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import check, post, servicectl

CHECK = 'check-fake-synthetic'
OTHER = 'worker-1'


def host(tmp_path):
    runtime = tmp_path / 'runtime'
    store = post.Store(runtime)
    seat = SimpleNamespace(name=CHECK, ship=SimpleNamespace(runtime=runtime))
    order = store.new('order', 'ship', CHECK, 'Synthetic check task')
    return runtime, store, seat, order


def stored_ids(store):
    return sorted(m.id for m in store.all())


def holds(runtime):
    return sorted(post.drop_alarms(runtime))


def held_for(runtime, folder, reason):
    return f'post drop held: {folder}: {reason}' in post.drop_alarms(runtime)


def test_a_drop_whose_from_names_another_seat_is_held_and_never_filed(tmp_path):
    runtime, store, seat, order = host(tmp_path)
    forged = store.prepare('report', OTHER, 'ship', 'forged from', re=order.id)
    path = post.write_drop(runtime, forged, seat=CHECK)  # another seat's message, in the check seat's own folder
    before = stored_ids(store)
    assert check.closing_report(seat, store, order) is None
    assert stored_ids(store) == before and path.is_file(), 'held: the file stays, nothing is stored'
    assert held_for(runtime, f'drop/{CHECK}', 'from names another seat'), holds(runtime)


def test_a_drop_whose_id_names_another_seat_is_held_and_never_filed(tmp_path):
    runtime, store, seat, order = host(tmp_path)
    other = store.prepare('report', OTHER, 'ship', 'forged id', re=order.id)
    forged = dataclasses.replace(other, sender=CHECK)  # from is the check seat; the id still names another
    path = post.write_drop(runtime, forged, seat=CHECK)
    before = stored_ids(store)
    assert check.closing_report(seat, store, order) is None
    assert stored_ids(store) == before and path.is_file()
    assert held_for(runtime, f'drop/{CHECK}', 'from names another seat'), holds(runtime)


@pytest.mark.parametrize('kind', ['link to a message', 'link to a folder', 'fifo'])
def test_a_link_or_special_file_in_a_drop_is_held_and_never_followed(tmp_path, kind):
    runtime, store, seat, order = host(tmp_path)
    report = store.prepare('report', CHECK, 'ship', 'through a link', re=order.id)
    folder = post.drop_dir(runtime, CHECK)
    folder.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    name = folder / f'{report.id}.json'
    if kind == 'link to a message':
        target = outside / 'message.json'
        target.write_text(report.to_json())
        name.symlink_to(target)
    elif kind == 'link to a folder':
        target = outside
        name.symlink_to(target, target_is_directory=True)
    else:
        os.mkfifo(name)
    before = {p.name: p.stat().st_mtime_ns for p in outside.iterdir()}
    started = time.monotonic()
    assert check.closing_report(seat, store, order) is None
    assert time.monotonic() - started < 5, 'a FIFO is never opened for reading'
    assert report.id not in stored_ids(store)
    assert os.path.lexists(name), 'held: the name stays'
    assert {p.name: p.stat().st_mtime_ns for p in outside.iterdir()} == before, 'nothing followed or written there'
    assert held_for(runtime, f'drop/{CHECK}', 'not a regular file'), holds(runtime)


def test_a_drop_in_another_seats_folder_is_not_filed_by_the_host_and_its_holds_are_kept(tmp_path):
    runtime, store, seat, order = host(tmp_path)
    theirs = store.prepare('order', OTHER, 'ship', 'another seat\'s own post')
    path = post.write_drop(runtime, theirs)
    # Another seat's hold, left by an earlier service pass: the host's scoped pass must keep it.
    forged = store.prepare('report', 'captain', 'ship', 'forged into worker-1')
    post.write_drop(runtime, forged, seat=OTHER)
    post.file_drops(store)  # the service's pass, as it was: files worker-1's own, holds the forgery
    assert theirs.id in stored_ids(store) and not path.exists()
    assert held_for(runtime, f'drop/{OTHER}', 'from names another seat')
    later = store.prepare('order', OTHER, 'ship', 'posted after the service stopped')
    later_path = post.write_drop(runtime, later)
    report = store.prepare('report', CHECK, 'ship', 'done', re=order.id)
    post.write_drop(runtime, report)
    assert check.closing_report(seat, store, order) == report
    assert later.id not in stored_ids(store) and later_path.is_file(), 'the host files its own seat only'
    assert held_for(runtime, f'drop/{OTHER}', 'from names another seat'), 'another seat\'s hold is kept'


def test_the_host_files_nothing_while_a_service_of_another_process_is_alive(tmp_path):
    runtime, store, seat, order = host(tmp_path)
    report = store.prepare('report', CHECK, 'ship', 'done', re=order.id)
    path = post.write_drop(runtime, report)
    holder = subprocess.Popen([sys.executable, '-c', textwrap.dedent('''
        import sys
        from pathlib import Path
        from quarterdeck import servicectl
        handle = servicectl.acquire_lock(Path(sys.argv[1]))
        print('alive', flush=True)
        sys.stdin.readline()
    '''), str(runtime)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'alive'
        assert servicectl.alive(runtime)
        for _ in range(3):
            assert check.closing_report(seat, store, order) is None
        assert path.is_file() and report.id not in stored_ids(store), 'the service files; the host stands aside'
    finally:
        holder.communicate('\n', timeout=10)
    assert not servicectl.alive(runtime)
    assert check.closing_report(seat, store, order) == report


RACER = textwrap.dedent('''
    import json, sys, time
    from pathlib import Path
    from quarterdeck import post
    runtime, role, start = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
    while time.time() < start:
        time.sleep(0.001)
    store = post.Store(runtime)
    published = []
    real = post.Store.publish_text

    def counted(self, message, text):
        result = real(self, message, text)
        published.append(message.id)  # a publication this filer made, returned without error
        return result

    post.Store.publish_text = counted
    passes = 0
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if role == 'host':
            post.file_drops(store, seats=frozenset({sys.argv[4]}), service_absent=True)
        else:
            post.file_drops(store)
        passes += 1
        if not any(post.drop_dir(runtime, sys.argv[4]).glob('*.json')):
            break
    print(json.dumps(dict(role=role, passes=passes, published=published)))
''')


def test_two_filers_never_file_one_drop_twice(tmp_path):
    runtime, store, seat, order = host(tmp_path)
    reports = []
    for number in range(40):
        message = store.prepare('report', CHECK, 'ship', f'part {number}')
        post.write_drop(runtime, message)
        reports.append(message)
    start = time.time() + 1.0
    racers = [subprocess.Popen([sys.executable, '-c', RACER, str(runtime), role, str(start), CHECK],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
              for role in ('host', 'host', 'service')]
    outputs = [racer.communicate(timeout=60) for racer in racers]
    assert all(racer.returncode == 0 for racer in racers), outputs
    files = sorted(p.name for p in store.dir.glob('*.json'))
    ids = [m.id for m in post.Store(runtime).all()]
    assert len(ids) == len(set(ids)), 'one message, one file'
    assert {m.id for m in reports} <= set(ids), 'every drop is filed'
    assert len(files) == len(set(files)) == len(ids)
    assert not any(post.drop_dir(runtime, CHECK).glob('*.json')), 'the drop folder is empty'
    published = [name for out, _ in outputs for name in json.loads(out.strip().splitlines()[-1])['published']]
    assert sorted(published) == sorted(m.id for m in reports), 'each drop published once, by one filer'
    assert post.drop_alarms(runtime) == [], 'no drop held by the race'


def test_the_filing_lock_lies_where_every_jail_hides_it_and_none_is_left_under_the_post(tmp_path):
    """flock needs no write access: a filing lock a jail could open, it could hold, and every filer would step aside."""
    from quarterdeck import sandbox

    runtime, store, seat, order = host(tmp_path)
    report = store.prepare('report', CHECK, 'ship', 'done', re=order.id)
    post.write_drop(runtime, report)
    assert check.closing_report(seat, store, order) == report
    post.file_drops(store)
    assert (runtime / sandbox.LOCKS / 'filing.lock').is_file()
    assert list((runtime / 'post').rglob('filing.lock')) == [], 'no filing lock under runtime/post'


def namespaces_allowed() -> bool:
    from quarterdeck import sandbox
    if not sandbox.available():
        return False
    try:
        return subprocess.run(['bwrap', '--ro-bind', '/', '/', 'true'], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


JAIL_PROBE = textwrap.dedent(r'''
    import fcntl, json, os, sys, time
    runtime, work = sys.argv[1:3]
    lock = os.path.join(runtime, 'locks', 'filing.lock')
    seen = {}
    for flags, name in ((os.O_RDONLY, 'read'), (os.O_RDWR | os.O_CREAT, 'create')):
        try:
            descriptor = os.open(lock, flags, 0o600)
        except OSError as error:
            seen[name] = type(error).__name__
            continue
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # held for the jail's life, if it could open it
        seen[name] = 'held'
    held = []
    for folder, folders, files in os.walk(os.path.join(runtime, 'post'), followlinks=False):
        for name in files + folders:
            path = os.path.join(folder, name)
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(path)
            except OSError:
                pass
    seen['held_under_post'] = len(held)
    seen['locks_folder'] = sorted(os.listdir(os.path.join(runtime, 'locks')))
    with open(os.path.join(work, 'held.json'), 'w') as stream:
        json.dump(seen, stream)
    deadline = time.monotonic() + 120
    while not os.path.exists(os.path.join(work, 'done')) and time.monotonic() < deadline:
        time.sleep(0.02)
''')


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_a_jail_cannot_hold_the_filing_lock_and_the_host_still_files_while_it_holds_all_it_can(drydock: Path, tmp_path):
    """Real bubblewrap: the filing lock is absent inside every jail, and a jail holding every file it can open under
    runtime/post neither stops the host's filing nor makes it step aside."""
    import threading

    from quarterdeck import sandbox, ship as shipmod

    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    runtime = ship.runtime
    store = post.Store(runtime)
    seat = SimpleNamespace(name=CHECK, ship=SimpleNamespace(runtime=runtime))
    order = store.new('order', 'ship', CHECK, 'Synthetic check task')
    post.file_drops(store)  # the filing lock exists now, where the code keeps it
    lock = runtime / sandbox.LOCKS / 'filing.lock'
    assert lock.is_file()
    report = store.prepare('report', CHECK, 'ship', 'done while a jail holds what it can', re=order.id)
    post.write_drop(runtime, report)
    workspace = tmp_path / 'filing-work'
    workspace.mkdir()
    own = runtime / 'seats' / 'worker-1'
    (own / 'home').mkdir(parents=True, exist_ok=True)
    jail = sandbox.plan(ship, 'worker-1', own, own / 'home', workspace, '/usr/bin/python3', '/usr/bin', home=Path.home())
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    child = subprocess.Popen(sandbox.command(jail, [sys.executable, '-c', JAIL_PROBE, str(runtime), str(workspace)]),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    closed: list = []
    try:
        deadline = time.monotonic() + 60
        while not (workspace / 'held.json').exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (workspace / 'held.json').exists(), child.communicate(timeout=10)
        seen = json.loads((workspace / 'held.json').read_text())
        worker = threading.Thread(target=lambda: closed.append(check.closing_report(seat, store, order)), daemon=True)
        worker.start()
        worker.join(30)
    finally:
        (workspace / 'done').write_text('')
        try:
            child.communicate(timeout=60)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    assert seen['read'] == 'FileNotFoundError', seen
    assert seen['locks_folder'] in ([], ['filing.lock']), seen  # empty, or the jail's own lock in its private tmpfs
    assert seen['held_under_post'] > 0, 'the jail held what it could under runtime/post: the probe works'
    assert closed == [report], 'the host filed its report within the bound while the jail held everything it could'
    with open(lock) as outside:  # the jail's own lock never reached the host's file
        import fcntl
        fcntl.flock(outside, fcntl.LOCK_EX | fcntl.LOCK_NB)
