"""The test tool's slot probe and a refused setup: throwaway slot and results folders under tmp_path only.

(4) Asking whether a run still holds its slot never takes a free slot: a run starting at that very moment gets it.
(3) A run refused or timed out before its work begins leaves no empty results/<run> folder behind; a folder with
anything in it is never removed. (2) A storage record that cannot be read names itself and how to clear it.
"""
import fcntl
import json
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.testrunner import cli, layout, run, scratch
from tests.testrunner.layout import RESULTS, ROOT_NAME, Refused
from .test_run_slots_ordinary import root_at
from .test_testrun_socket_paths import declare_suite

RUN = '20000101-000000-00a1'


def three_held(root):
    """Three slots held by live holders, each its own open file description."""
    held = []
    for index in range(3):
        pool = layout.Slot(root)
        pool.acquire({'verb': 'run', 'run': f'20000101-000000-01{index:02x}'})
        held.append(pool)
    return held


def crashed_note(folder, run_id):
    """A matching note with a verifiable original owner and no flock."""
    note = {'verb': 'run', 'run': run_id, 'pid': os.getpid(), 'owner': layout._slot_owner(os.getpid())}
    (folder / 'slot-4.lock').write_text(json.dumps(note))


class _Os:
    """layout's own os, with close observed: the moment the probe has finished with a slot file."""

    def __init__(self, on_close):
        self.on_close = on_close

    def __getattr__(self, name):
        return getattr(os, name)

    def close(self, fd):
        self.on_close(fd)
        os.close(fd)


@pytest.mark.parametrize('run_id', [RUN, '20000101-000000-00d4'])
def test_a_probe_never_takes_a_free_slot_from_a_run_starting_at_that_moment(tmp_path, monkeypatch, run_id):
    """Even a matching stale note leaves the fourth slot available to a wait-0 contender during the probe."""
    root = root_at(tmp_path)
    held = three_held(root)
    crashed_note(tmp_path, run_id)
    monkeypatch.setattr(layout, '_flock_files', lambda: set())
    fourth = layout.identity(os.stat(tmp_path / 'slot-4.lock'))
    outcome = {}

    def starting(fd):
        # When the probe lets go of the fourth slot's file, a wait-0 run tries to start: it must find that slot free.
        if outcome or layout.identity(os.fstat(fd)) != fourth:
            return
        outcome['tried'] = True
        competitor = layout.Slot(root)
        try:
            competitor.acquire({'verb': 'run', 'run': 'competitor'}, 0)
            outcome['slot'] = competitor.selected + 1
        except Refused as refused:
            outcome['refused'] = str(refused)
        finally:
            competitor.release()

    monkeypatch.setattr(layout, 'os', _Os(starting))
    try:
        assert layout.run_holds_slot(root, RUN) is False, 'a crashed run holds nothing'
        assert outcome.get('tried'), 'the probe read the fourth slot'
        assert outcome == {'tried': True, 'slot': 4}, outcome
    finally:
        monkeypatch.undo()
        for pool in held:
            pool.release()
        root.close()


@pytest.mark.parametrize('locks', ['proc', 'unreadable'])
def test_a_live_holder_is_seen_and_a_crashed_one_is_not(tmp_path, monkeypatch, locks):
    if locks == 'unreadable':
        def unreadable():
            raise OSError('no /proc/locks here')
        monkeypatch.setattr(layout, '_flock_files', unreadable)
    root = root_at(tmp_path)
    live = layout.Slot(root)
    try:
        live.acquire({'verb': 'run', 'run': RUN})
        assert layout.run_holds_slot(root, RUN) is True
        assert layout.run_holds_slot(root, '20000101-000000-00b2') is False, 'another run is not this one'
        live.held = []  # leave the note, as a crash does; the lock goes with the file description
        live.release()
        assert layout.run_holds_slot(root, RUN) is False
    finally:
        live.release()
        root.close()


@pytest.fixture
def setup_run(tmp_path, monkeypatch):
    """The run's setup on a throwaway project: a real root, slots, run id and result guard; only the target, the
    socket preflight and, per test, registration or storage are invented."""
    declare_suite(tmp_path)
    (tmp_path / 'tests/unit').mkdir(exist_ok=True)
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True, capture_output=True)
    (tmp_path / '.gitignore').write_text('.testruns/\n')  # the tool's own folder is ignored, as in any project
    target = SimpleNamespace(top=tmp_path, project=tmp_path, branch='change', main_checkout=False, head='0' * 40,
                             tree='0' * 40)
    monkeypatch.setattr(run.gitops, 'resolve', lambda path: target)
    monkeypatch.setattr(run.gitops, 'dirty', lambda path: [])
    monkeypatch.setattr(run, 'check_target', lambda top, item: item)
    monkeypatch.setattr(run.socket_paths, 'preflight', lambda *args, **kwargs: None)

    def results():
        folder = tmp_path / ROOT_NAME / RESULTS
        return sorted(path.name for path in folder.iterdir()) if folder.exists() else []

    return tmp_path, results


@pytest.mark.parametrize('has_results', [True, False])
def test_retire_missing_run_reports_a_refusal(setup_run, monkeypatch, capsys, has_results):
    tmp_path, results = setup_run
    monkeypatch.chdir(tmp_path)
    root = layout.Root(tmp_path)
    try:
        if has_results:
            os.close(root.child(RESULTS))
        assert cli.main(['retire', RUN], []) == 2
        assert capsys.readouterr().err == f'testrun: refused: run {RUN}: no recorded result directory; nothing removed\n'
        assert results() == []
    finally:
        root.close()


def test_a_refused_registration_leaves_no_empty_results_folder(setup_run, monkeypatch):
    tmp_path, results = setup_run
    def refuse(root, target, run_id):
        raise Refused('synthetic registration refusal')
    monkeypatch.setattr(run.clean, 'register', refuse)
    with pytest.raises(Refused, match='synthetic registration refusal'):
        run.Runner(cli.parser().parse_args(['run']), []).main()
    assert results() == []


def test_a_setup_timeout_leaves_no_empty_results_folder(setup_run, monkeypatch):
    tmp_path, results = setup_run
    def stop(root, target, run_id):
        raise run.Stop('synthetic setup time limit')
    monkeypatch.setattr(run.clean, 'register', stop)
    assert run.Runner(cli.parser().parse_args(['run']), []).main() == run.EXIT['timeout']
    assert results() == []


def test_a_refused_working_storage_leaves_no_empty_results_folder(setup_run, monkeypatch):
    tmp_path, results = setup_run
    monkeypatch.setattr(run.clean, 'register', lambda *args: None)
    def refuse(*args, **kwargs):
        raise Refused('synthetic storage refusal')
    monkeypatch.setattr(run.storage, 'prepare', refuse)
    with pytest.raises(Refused, match='synthetic storage refusal'):
        run.Runner(cli.parser().parse_args(['run']), []).main()
    assert results() == []


def test_a_results_folder_with_anything_in_it_is_never_removed(setup_run, monkeypatch):
    tmp_path, results = setup_run
    def refuse(root, target, run_id):
        (tmp_path / ROOT_NAME / RESULTS / run_id / 'evidence.txt').write_text('kept\n')
        raise Refused('synthetic refusal after a write')
    monkeypatch.setattr(run.clean, 'register', refuse)
    with pytest.raises(Refused):
        run.Runner(cli.parser().parse_args(['run']), []).main()
    kept, = results()
    assert (tmp_path / ROOT_NAME / RESULTS / kept / 'evidence.txt').read_text() == 'kept\n'


def test_an_unreadable_storage_record_names_itself_and_how_to_clear_it(tmp_path):
    root = root_at(tmp_path)
    try:
        folder = tmp_path / RESULTS / RUN
        folder.parent.mkdir(mode=0o700)
        folder.mkdir(mode=0o700)
        (folder / scratch.RECORD).write_text('{ not json')
        with pytest.raises(Refused) as refused:
            scratch.reserved(root, os.stat(tmp_path).st_dev)
        words = str(refused.value)
        assert str(folder / scratch.RECORD) in words
        assert 'every new run is refused' in words and 'move that one file' in words, words
    finally:
        root.close()


def test_a_live_holder_whose_line_is_missing_from_proc_locks_is_held(tmp_path, monkeypatch):
    """A live holder's missing global row is confirmed by its fdinfo."""
    monkeypatch.setattr(layout, '_flock_files', lambda: set())  # the line skipped, at its exact step
    root = root_at(tmp_path)
    live = layout.Slot(root)
    try:
        live.acquire({'verb': 'run', 'run': RUN})
        assert layout.run_holds_slot(root, RUN) is True
    finally:
        live.release()
        root.close()


def test_another_device_with_the_same_inode_does_not_hold_the_slot(tmp_path, monkeypatch):
    root = root_at(tmp_path)
    try:
        crashed_note(tmp_path, RUN)
        slot = (tmp_path / 'slot-4.lock').stat()
        major, other_minor = os.major(slot.st_dev), os.minor(slot.st_dev) ^ 1
        monkeypatch.setattr(layout, '_proc_locks',
                            lambda: f'1: FLOCK ADVISORY WRITE 42 {major:x}:{other_minor:x}:{slot.st_ino} 0 EOF\n')
        assert layout.run_holds_slot(root, RUN) is False
    finally:
        root.close()


def test_a_free_slot_with_an_empty_or_other_note_is_never_locked(tmp_path, monkeypatch):
    root = root_at(tmp_path)
    pool = layout.Slot(root)  # makes the four slot files
    pool.release()
    (tmp_path / 'slot-2.lock').write_text(json.dumps({'verb': 'run', 'run': '20000101-000000-00c3'}))
    locked = []
    real = layout.fcntl.flock
    monkeypatch.setattr(layout.fcntl, 'flock', lambda fd, op: locked.append(op) or real(fd, op))
    monkeypatch.setattr(layout, '_flock_files', lambda: set())
    try:
        assert layout.run_holds_slot(root, RUN) is False
        assert locked == [], 'no free slot was locked by the question'
    finally:
        monkeypatch.undo()
        root.close()


def test_a_stopped_legacy_holder_is_seen_and_its_unverified_note_holds_cleanup(tmp_path, monkeypatch):
    root = root_at(tmp_path)
    monkeypatch.setattr(layout, '_flock_files', lambda: set())
    holder = subprocess.Popen([sys.executable, '-B', '-c', '''
import json, os, signal, sys
from tests.testrunner import layout
from tests.unit.test_run_slots_ordinary import root_at
root = root_at(sys.argv[1])
slot = layout.Slot(root)
slot.acquire({"verb": "run", "run": sys.argv[2]})
note = json.dumps({"verb": "run", "run": sys.argv[2], "pid": os.getpid()}).encode()
os.ftruncate(slot.fd, 0)
os.pwrite(slot.fd, note, 0)
os.kill(os.getpid(), signal.SIGSTOP)
''', str(tmp_path), RUN])
    competitor = layout.Slot(root)
    try:
        _, status = os.waitpid(holder.pid, os.WUNTRACED)
        assert os.WIFSTOPPED(status)
        assert layout.run_holds_slot(root, RUN) is True
        holder.kill()
        holder.wait()
        with pytest.raises(Refused, match='cannot verify the owner of .*slot.lock'):
            layout.run_holds_slot(root, RUN)
        competitor.acquire({'verb': 'run', 'run': 'replacement'}, wait=0)
        assert competitor.selected == 0
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
        competitor.release()
        root.close()


def test_unreadable_owner_evidence_holds_cleanup(tmp_path, monkeypatch):
    root = root_at(tmp_path)
    os.close(root.child(RESULTS, RUN))
    monkeypatch.setattr(layout, '_flock_files', lambda: set())
    crashed_note(tmp_path, RUN)
    real_open = os.open

    def denied(name, *args, **kwargs):
        if name == 'fdinfo':
            raise PermissionError('synthetic unreadable fdinfo')
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr(layout.os, 'open', denied)
    try:
        with pytest.raises(Refused, match='cannot verify the owner of .*slot-4.lock'):
            layout.run_active(root, RUN)
    finally:
        root.close()


@pytest.mark.parametrize('inherited', [False, True])
def test_a_reaped_slot_owner_checks_for_a_surviving_descriptor(tmp_path, monkeypatch, inherited):
    root = root_at(tmp_path)
    other, retry, contender = (layout.Slot(root) for _ in range(3))
    release_read, release_write = os.pipe()
    retired_read, retired_write = os.pipe()
    holder = None
    guard = None
    try:
        other.acquire({'verb': 'run', 'run': 'other'})
        assert other.selected == 0
        holder = subprocess.Popen([sys.executable, '-B', '-c', '''
import os, subprocess, sys
from tests.testrunner import layout
from tests.unit.test_run_slots_ordinary import root_at
root = root_at(sys.argv[1])
slot = layout.Slot(root)
slot.acquire({"verb": "run", "run": sys.argv[2]})
if sys.argv[3] == "True":
    release, retired = map(int, sys.argv[4:6])
    subprocess.Popen([sys.executable, '-B', '-c',
                      'import os, sys; fd, release, retired = map(int, sys.argv[1:]); '
                      'os.read(release, 1); os.close(fd); os.write(retired, b"1")',
                      str(slot.fd), str(release), str(retired)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     pass_fds=(slot.fd, release, retired))
print(slot.selected, flush=True)
sys.stdin.read()
''', str(tmp_path), RUN, str(inherited), str(release_read), str(retired_write)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  pass_fds=(release_read, retired_write))
        assert holder.stdout.readline().strip() == b'1', 'the earlier peer put this controller in slot 2'
        stale = (tmp_path / 'slot-2.lock').read_bytes()
        holder.kill()
        holder.communicate()
        other.release()
        retry.acquire({'verb': 'run', 'run': 'retry'}, wait=0)
        assert retry.selected == 0, 'the next run uses the other peer’s newly freed slot'
        monkeypatch.setattr(layout, '_flock_files', lambda: set())
        made = layout.RunGuard(root, RUN, create=True)
        made.release()
        guard = layout.RunGuard(root, RUN)
        if inherited:
            with pytest.raises(layout.RunBusy, match='run slot is still held'):
                guard.acquire()
        else:
            guard.acquire()
        assert (tmp_path / 'slot-2.lock').read_bytes() == stale
        contender.acquire({'verb': 'run', 'run': 'contender'}, wait=0)
        assert contender.selected == (2 if inherited else 1)
        assert json.loads((tmp_path / 'slot.lock').read_bytes())['run'] == 'retry'
        if inherited:
            os.write(release_write, b'1')
            assert os.read(retired_read, 1) == b'1'
    finally:
        if guard is not None:
            guard.release()
        if holder is not None and holder.poll() is None:
            holder.kill()
            holder.communicate()
        for fd in (release_read, release_write, retired_read, retired_write):
            os.close(fd)
        for pool in (contender, retry, other):
            pool.release()
        root.close()


def test_a_line_skipped_by_a_moving_proc_locks_still_reads_held(tmp_path, monkeypatch):
    """The reproduction, deterministic: the probe reads /proc/locks in small reads; between them the newest of the
    locks above the holder's line are released, so the kernel's walk skips lines. The probe's answer must stay held."""
    original = os.sched_getaffinity(0)
    os.sched_setaffinity(0, {sorted(original)[0]})  # one CPU: one list of locks, in the order taken
    root = root_at(tmp_path)
    live = layout.Slot(root)
    extra = []
    try:
        live.acquire({'verb': 'run', 'run': RUN})
        for index in range(60):  # taken after the holder: above its line in the walk
            fd = os.open(tmp_path / f'other-{index}.lock', os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            extra.append(fd)

        def moving():
            fd = os.open('/proc/locks', os.O_RDONLY)
            text = b''
            try:
                while text.count(b'\n') < 45:
                    text += os.read(fd, 64)
                for other in extra[-40:]:  # already read past: every later line moves up
                    os.close(other)
                del extra[-40:]
                while chunk := os.read(fd, 4096):
                    text += chunk
            finally:
                os.close(fd)
            return text.decode('ascii', 'replace')

        monkeypatch.setattr(layout, '_proc_locks', moving)
        assert layout.run_holds_slot(root, RUN) is True
    finally:
        for other in extra:
            os.close(other)
        live.release()
        root.close()
        os.sched_setaffinity(0, original)
