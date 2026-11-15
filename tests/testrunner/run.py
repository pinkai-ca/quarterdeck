"""``testrun.py run``: one run of a clean committed target, in fresh owned scratch, with a fixed ending.

Bounds: setup has its own 600 s, the wait for a slot not counted; ``--timeout`` bounds pytest alone, from its start;
``--wait`` alone bounds the wait for a slot.

Stages: refuse a dirty target; hold one of four slots; recover this change's abandoned runs; make the run folder and
its lock; check out the exact commit detached; make a venv on the ordinary ``python3`` with the system's
read-only test tools; install the checkout editable with no index; prove the imports; compile the run's own
code and make its package folders read-only; run pytest in its own session with the evidence plugin; then
analyse, write the result, the report and every failure, and remove everything else.
"""
from __future__ import annotations

import fcntl
from contextlib import contextmanager
from functools import partial
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from . import clean, environment, evidence, gitops, jail, owned, procs, report, resources, scratch as storage, sessions, socket_paths, socket_roots, usersite
from .layout import RESULTS, RUNS, RUN_SOCKETS, SOCKETS, Refused, Root, RunGuard, Slot, git_environment, identity, reserve_run_id, write_json

GRACE = 15.0
SETUP_SECONDS = 600.0  # setup's own bound, the slot wait not counted; --timeout bounds pytest alone
FINALIZATION_SECONDS = 60.0
EMERGENCY_SECONDS = 5.0
FINALIZATION_EXIT = 7
EXIT = {'passed': 0, 'failed': 1, 'refused': 2, 'timeout': 3, 'interrupted': 4, 'incomplete': 5, 'abandoned': 5,
        'no-tests': 5}
CLEANUP_HELD = 6
PREFLIGHT = r'''
import importlib.util, json, sys, sysconfig
# Match pytest's checkout-root import path while still requiring project code to resolve under src.
sys.path.insert(0, sys.argv[1])
out = {"python": sys.version.split()[0], "executable": sys.executable, "prefix": sys.prefix,
       "base_prefix": sys.base_prefix, "purelib": sysconfig.get_path("purelib"), "imports": {}}
for name in ("pytest", "xdist", "execnet", "pip", "setuptools"):
    try:
        module = __import__(name)
        out[name] = {"version": getattr(module, "__version__", None), "file": module.__file__}
    except ImportError:
        out[name] = None
for name in sys.argv[2:]:
    out["imports"][name] = __import__(name).__file__
tests_spec = importlib.util.find_spec("tests")
if tests_spec is None:
    out["tests_import"] = {"form": "absent", "file": None, "paths": None, "spec_origin": None}
else:
    tests = __import__("tests")
    spec = getattr(tests, "__spec__", None)
    file = getattr(tests, "__file__", None)
    paths = getattr(tests, "__path__", None)
    origin = getattr(spec, "origin", None)
    form = "unknown"
    if spec is not None and spec.submodule_search_locations is not None:
        if file is None and origin is None:
            form = "namespace"
        elif isinstance(file, str) and isinstance(origin, str):
            form = "regular"
    out["tests_import"] = {"form": form, "file": file,
                           "paths": list(paths) if paths is not None else None, "spec_origin": origin}
print(json.dumps(out))
'''


class Stop(Exception):
    """A signal or the time limit ended the run."""


class FinalizationTimeout(BaseException):
    """Escape cleanup error handlers; the remaining scratch and first result must stay."""


def selection(args, top: Path) -> dict:
    """Resolve a lane against the target project, without starting a test or changing files."""
    targets = list(args.targets)
    lane = getattr(args, 'lane', None)
    if lane is not None and targets:
        raise Refused('--fast and --whole cannot be combined with named targets')
    name = 'targets' if targets else (lane or 'fast')
    if name not in ('fast', 'whole', 'targets'):
        raise Refused('the test selection must be fast, whole or named targets')
    reason = None
    if name == 'fast':
        targets = [path for path in ('tests/unit', 'tests/contract') if (top / path).exists()]
        if not targets:
            name, reason = 'whole', 'no tests/unit or tests/contract'
            # The old project-wide pytest discovery, including projects with tests at their root.
    elif name == 'whole':
        targets = ['tests']
    maximum = 16 if name == 'fast' else 8
    workers = maximum if args.workers is None else args.workers
    if not 0 <= workers <= maximum:
        raise Refused(f'-n takes 0 to {maximum} workers for {name}')
    selected = {'name': name, 'targets': targets, 'workers': workers, 'slots': 2 if name == 'fast' else 1}
    if reason:
        selected['reason'] = reason
    return selected


class Runner:
    def __init__(self, args, runner_files: list[Path]):
        self.args = args
        self.ci_layout = getattr(args, 'ci_layout', False)
        self.selection = None  # resolve only once the run's target project is known
        self.runner_files = runner_files
        self.signal = None
        self.timed_out = False
        self.deadline = None  # the bound now running: setup's, then pytest's from its start
        self.limit = None  # what that bound is called when it ends the run
        self.armed = False  # inside time_limit: its alarm is the running bound's
        self.console = None
        self.last_exit = None
        self.started = time.time()
        self.monitor = None
        self.storage_refused = False
        self.listed = None  # what the jail holds, once the run checkout exists

    def jailed(self, argv, cwd):
        """A step that runs the target's code: inside the jail when --jail was given, else as it is."""
        if not getattr(self.args, 'jail', False):
            return argv
        if self.listed is None:
            raise Refused('the jail was asked for before the run checkout existed')
        return jail.argv(argv, env=self.env, cwd=cwd, listed=self.listed)

    # -- signals and process groups -------------------------------------------------------------------------
    def on_signal(self, number, frame):
        self.signal = number

    def on_timeout(self, number, frame):
        self.timed_out = True
        raise Stop(self.limit)

    def bound(self, seconds, limit):
        """Start a bound: the deadline every check reads and, inside time_limit, its alarm too."""
        self.deadline, self.limit = time.monotonic() + seconds, limit
        if self.armed:
            signal.setitimer(signal.ITIMER_REAL, seconds)

    @contextmanager
    def alarm(self, seconds, handler):
        """Main-thread budget; restore the caller's signal policy on every exit."""
        old_handler = signal.getsignal(signal.SIGALRM)
        old_timer = signal.getitimer(signal.ITIMER_REAL)
        started = time.monotonic()
        signal.signal(signal.SIGALRM, handler)
        try:
            signal.setitimer(signal.ITIMER_REAL, seconds)
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)
            if old_timer[0]:
                signal.setitimer(signal.ITIMER_REAL, max(0.000001, old_timer[0] - (time.monotonic() - started)),
                                 old_timer[1])

    @contextmanager
    def time_limit(self):
        """The running bound, setup's or pytest's, as an alarm; interrupted steps retain their bounded stop grace."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.on_timeout(signal.SIGALRM, None)
        with self.alarm(remaining, self.on_timeout):
            self.armed = True
            try:
                yield
            finally:
                self.armed = False

    def on_finalization_timeout(self, number, frame):
        raise FinalizationTimeout

    def check(self):
        if self.monitor is not None:
            try:
                self.monitor.check()
            except Refused:
                self.storage_refused = True
                raise
        if self.signal is not None:
            raise Stop(f'signal {self.signal}')
        if self.deadline is not None and time.monotonic() > self.deadline:
            self.timed_out = True
            raise Stop(self.limit)

    def stop_group(self, process):
        """Wait for the whole step session, even when its shell has already exited."""
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            groups = procs.session_groups(process.pid)
            if not groups:
                process.poll()
                return
            for pgid in groups:
                try:
                    os.killpg(pgid, number)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + GRACE
            while procs.session_groups(process.pid) and time.monotonic() < deadline:
                process.poll()  # reap the shell without confusing its exit with the session ending
                time.sleep(.05)
        if procs.session_groups(process.pid):
            raise Refused('stopped step still has live session members; retained for recovery')
        process.poll()

    def sweep(self, pgid: int) -> int:
        """Members left in a finished step's own group: ours by construction (its session, this boot, the run
        lock held); terminated, then killed. Returns how many there were."""
        groups = procs.session_groups(pgid)
        left = [pid for group in groups for pid in procs.group_members(group)]
        if left:
            for number in (signal.SIGTERM, signal.SIGKILL):
                for group in procs.session_groups(pgid):
                    try:
                        os.killpg(group, number)
                    except ProcessLookupError:
                        pass
                deadline = time.monotonic() + 5
                while procs.session_groups(pgid) and time.monotonic() < deadline:
                    time.sleep(0.1)
                if not procs.session_groups(pgid):
                    break
        return len(left)

    def execute(self, argv, *, env, cwd, title, jailed=False):
        """One step in its own session, its output appended to the console, the run lock inherited."""
        if jailed:
            argv = self.jailed(argv, cwd)
        self.console.write(f'\n## {title}\n$ {" ".join(map(str, argv))}\n')
        self.console.flush()
        process = subprocess.Popen([str(part) for part in argv], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=self.console, stderr=subprocess.STDOUT, start_new_session=True,
                                   pass_fds=(self.lock,),
                                   preexec_fn=partial(resources.apply_memory, self.args.memory_mib)
                                   if title == 'pytest' else None)
        try:
            self.state.setdefault('groups', []).append([process.pid, procs.start_time(process.pid)])
            write_json(self.scratch_fd, 'state.json', self.state)
            clean.record(self.root, self.run, self.state)
            while process.poll() is None:
                self.check()
                time.sleep(0.2)
        except (Stop, Refused):
            self.stop_group(process)
            raise
        finally:
            self.last_exit = process.returncode
            self.survivors += self.sweep(process.pid)
            self.console.write(f'## {title}: exit {process.returncode}\n')
            self.console.flush()
        return process.returncode

    # -- the run --------------------------------------------------------------------------------------------
    def main(self) -> int:
        args = self.args
        try:
            self.open_files = resources.apply_open_files()  # controller and children own the same policy
        except RuntimeError as error:
            raise Refused(str(error)) from error
        root = slot = None
        self.run_guard = None
        self.bound(SETUP_SECONDS, f'time limit of setup, {SETUP_SECONDS:g} s')
        try:
            with self.time_limit():
                target = gitops.resolve(Path(args.target) if args.target else Path.cwd())
                self.selection = selection(args, target.top)
                args.workers = self.selection['workers']
                print('testrun: ' + report.selection_label(self.selection), flush=True)
                problems = gitops.dirty(target.top)
                if problems:
                    shown = '\n  '.join(problems[:20]) + ('\n  ...' if len(problems) > 20 else '')
                    raise Refused(f'target {target.top} has uncommitted changes; commit them (a WIP commit is fine) so the '
                                  f'result names exactly what ran:\n  {shown}')
                targets = [check_target(target.top, item) for item in self.selection['targets']]
                if getattr(args, 'jail', False):
                    try:
                        jail.tool_outside(self.runner_files, target.top)
                        jail.program()
                        jail.python()
                        held = jail.git_credentials(target.project)
                        if held:
                            raise jail.JailRefused(
                                'the project\'s Git config holds what could be a credential, and the jail shows '
                                '.git read-only to the change: ' + '; '.join(held) + '. Keep credentials out of '
                                'the repository\'s config, then run again')
                    except jail.JailRefused as refused:
                        raise Refused(str(refused)) from None
                socket_paths.preflight(target.top, target.project, jailed=getattr(args, 'jail', False))
                change = None if (target.main_checkout or target.branch is None) else target.branch
                root = Root(target.project)
                slot = self.slot = Slot(root)
                self.run = None
            waiting = time.monotonic()  # the wait for a slot is governed by --wait alone, outside both bounds
            note = {'verb': 'run', 'run': self.run, 'change': change, 'seat': os.environ.get('QD_SEAT')}
            if self.selection['slots'] == 1:
                slot.acquire(note, args.wait)
            else:
                slot.acquire(note, args.wait, count=self.selection['slots'])
            self.deadline += time.monotonic() - waiting
            with self.time_limit():
                for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    signal.signal(number, self.on_signal)
                for line in clean.recover(root, change):
                    print(line)
                self.run = reserve_run_id(root)
                slot.describe({**note, 'run': self.run})
                self.run_guard = RunGuard(root, self.run, create=True)
                self.run_guard.acquire()
                if change:
                    clean.register(root, target, self.run)  # a refusal here leaves nothing made
            self.storage = (storage.prepare(root, self.run, memory_required=True) if self.ci_layout
                            else storage.prepare(root, self.run))
            return self.within(root, target, targets, change)
        except Stop as stopped:
            print(f'testrun: timeout before test setup: {stopped}; no test result claimed', file=sys.stderr)
            return EXIT['timeout']
        finally:
            if self.run_guard is not None:
                self.run_guard.release()
            if slot is not None:
                slot.release()
            if root is not None:
                root.close()

    def within(self, root: Root, target, targets, change) -> int:
        args, run = self.args, self.run
        self.root = root
        runs_fd = storage.parent(root, run)
        results_fd = root.child(RESULTS, run)
        self.scratch_fd = storage.child(root, run)
        scratch = storage.path(root, run)
        self.monitor = storage.Monitor(scratch) if self.storage["kind"] != "disk" else None
        self.disk_before = storage.diskstats(root.project)
        self.io_before = storage.process_io()
        owner = owned.mark(self.scratch_fd)
        self.lock = os.open('lock', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.scratch_fd)
        fcntl.flock(self.lock, fcntl.LOCK_EX)  # this descriptor is inherited: held while any run process lives
        self.survivors = 0
        self.state = {'run': run, 'change': change, 'target': str(target.top), 'head': target.head,
                      'pid': os.getpid(), 'pid_start': procs.start_time(os.getpid()), 'boot': procs.boot_id(),
                      'scratch': identity(os.fstat(self.scratch_fd)), 'owner': owner, 'stage': 'setup'}
        write_json(self.scratch_fd, 'state.json', self.state)
        clean.record(root, run, self.state)
        os.close(root.child(RESULTS, run, 'sessions'))
        result = {'run': run, 'change': change, 'selection': self.selection,
                  'mode': 'ci-layout' if self.ci_layout else 'isolated',
                  'ci_full_invocation': bool(self.ci_layout and self.selection is not None and self.selection['name'] == 'whole'
                                             and args.workers == 8), 'started': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                  'target': {'top': str(target.top), 'project': str(target.project), 'head': target.head,
                             'tree': target.tree, 'branch': target.branch},
                  'working_storage': self.storage,
                  'runner': {'argv': sys.argv, 'executable': sys.executable},
                  'outcome': {'status': 'running', 'wrapper_exit': None}}
        write_json(results_fd, 'result.json', result)
        self.console = open(scratch / 'console.txt', 'a', encoding='utf-8', errors='replace')
        sockets_before, target_before = [], None
        pytest_exit, note = None, None
        checkout = scratch / f'checkout-{run}'
        try:  # from here every failure, the runner's own included, reaches the same fixed ending
            with self.time_limit():
                result['runner'].update(gitops.runner_identity(self.runner_files))
                self.python = jail.python() if getattr(args, 'jail', False) else shutil.which('python3')
                if self.python is None:
                    raise Refused('no python3 on the ordinary PATH')
                user_options = {}
                if os.environ.get(storage.CI_DISK_ENV) == '1' and os.environ.get('TESTRUNS_USERBASE'):
                    user_options['carried_base'] = os.environ['TESTRUNS_USERBASE']
                self.user = usersite.resolve(self.python, **user_options)  # before HOME is replaced
                build_options = {'ci_layout': True} if self.ci_layout else {}
                env, settings = environment.build(scratch, run, self.user['base'] if self.user['exists'] else None,
                                                  **build_options)
                registry = str(root.path / RESULTS / run / 'sessions')
                if self.ci_layout:
                    env['TESTRUN_CI_SESSION_CONTEXT'] = json.dumps({'registry': registry, 'run': run})
                else:
                    env[sessions.RECORD_ENV] = registry
                if socket_roots.enabled(target.top):
                    env['TMUX_TMPDIR'] = str(socket_roots.ensure(root.project, RUN_SOCKETS))
                else:
                    os.close(root.child(RUN_SOCKETS))
                    env['TMUX_TMPDIR'] = str(root.path / RUN_SOCKETS)
                settings['fixed']['TMUX_TMPDIR'] = env['TMUX_TMPDIR']
                env[resources.MEMORY_ENV] = str(args.memory_mib)
                self.env = env
                result['jail'] = {'jailed': False}
                result['environment'] = {**settings, 'workers': args.workers, 'timeout': args.timeout,
                                         'memory_mib': args.memory_mib, 'memory_resource': 'RLIMIT_AS',
                                         'memory_soft_and_hard': True, 'open_files': self.open_files,
                                         'uid': os.getuid(), 'cpus': os.cpu_count()}
                result['tools'] = environment.tool_versions(env)
                target_before = {'tracked': gitops.tracked_manifest(target.top, target.head),
                                 'extra': gitops.extra_manifest(target.top)}
                pytest_exit = self.setup_and_test(target, targets, scratch, checkout, env, result)
        except Stop as stop:
            note = str(stop)
            pytest_exit = self.last_exit if self.state['stage'] == 'pytest' else None
        except Refused as refused:
            note = f'setup refused: {refused}'
        except Exception as error:  # the runner's own failure: recorded, then the same fixed ending
            note = f'runner error: {type(error).__name__}: {error}'
        try:
            with self.alarm(FINALIZATION_SECONDS, self.on_finalization_timeout):
                return self.finalize(root, target, target_before, checkout, scratch, sockets_before,
                                     result, results_fd, runs_fd, pytest_exit, note)
        except FinalizationTimeout:
            return self.emergency(root, scratch, result, results_fd, runs_fd)

    def finalize(self, root, target, target_before, checkout, scratch, sockets_before,
                 result, results_fd, runs_fd, pytest_exit, note):
        run = self.run
        self.console.close()
        console = (scratch / 'console.txt').read_text(encoding='utf-8', errors='replace')
        found = evidence.analyse(scratch / 'evidence', pytest_exit)
        if self.ci_layout:
            layout_problems = ci_layout_problems(found, checkout, self.args.workers)
            result.setdefault('ci_layout', {})['session_verified'] = not layout_problems
            if layout_problems:
                found.update(complete=False, causes=[*layout_problems, *found['causes']],
                             counts=dict.fromkeys(evidence.CATEGORIES))
        if note:
            found.update(complete=False, causes=[note, *found['causes']], counts=dict.fromkeys(evidence.CATEGORIES))
        status = 'refused' if self.storage_refused else decide(found, self.timed_out, self.signal)
        if self.monitor is not None:
            try:
                self.monitor.check(force=True)
            except Refused as refused:
                status = 'refused'
                note = str(refused)
            result['working_storage']['peak'] = self.monitor.peak
        self.monitor = None
        result['evidence'] = {key: value for key, value in found.items() if key != 'failures'}
        result['failures'] = report.write_failures(results_fd, found['failures'])
        result['manifests'] = {'target': manifests_target(target, target_before) if target_before else {}}
        if getattr(self, 'checkout_before', None) is not None and checkout.is_dir():
            result['manifests']['checkout'] = manifests_checkout(checkout, target.head, self.checkout_before)
        result['ended'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
        result['seconds'] = round(time.time() - self.started, 1)
        result['outcome'] = {'status': status, 'wrapper_exit': EXIT[status], 'pytest_exit': pytest_exit,
                             'signal': self.signal, 'note': note, 'group_survivors_stopped': self.survivors}
        result['cleanup'] = {'outcome': 'pending'}
        report.write(results_fd, result, console, found['failures'])
        cleanup = clean.retire_run(root, runs_fd, run, self.state)
        if getattr(self, 'user_before', None) is not None:
            after = usersite.stamps(self.user['site'])
            moved = usersite.changed(self.user_before, after)
            result['tools']['user_site']['bytecode_folders'] = {'before': self.user_before['count'],
                                                                'digest_before': self.user_before['digest'],
                                                                'digest_after': after['digest'], 'changed': moved}
            if moved:  # the operator's user site must only be read: held, and reported, never repaired
                cleanup['outcome'] = 'held'
                cleanup['user_site_changed'] = moved
        cleanup['socket_entries_left'] = [entry['path'] for entry in cleanup.get('sessions', {}).get('entries', [])
                                          if entry['outcome'] == 'held']
        result['cleanup'] = cleanup
        if cleanup['outcome'] == 'held' and status == 'passed':
            result['outcome']['wrapper_exit'] = CLEANUP_HELD
        report.write(results_fd, result, console, found['failures'])
        cleanup = clean.discard_records(root, run, self.state, cleanup)
        try:
            cleanup['orphan_socket_roots'] = socket_roots.sweep()
        except (OSError, Refused) as error:
            cleanup['orphan_socket_roots'] = {'outcome': 'held',
                                              'reason': f'{type(error).__name__}: {error}'}
            cleanup['outcome'] = 'held'
        result['drive_writes'] = storage.writes_since(root.project, self.disk_before, self.io_before)
        if cleanup['outcome'] == 'held' and status == 'passed':
            result['outcome']['wrapper_exit'] = CLEANUP_HELD
        report.write(results_fd, result, console, found['failures'])
        for fd in (self.lock, results_fd, runs_fd, self.scratch_fd):
            os.close(fd)
        where = root.path / RESULTS / run
        print(f'run {run}: {status} · {summary(found)} · exits wrapper {result["outcome"]["wrapper_exit"]}, '
              f'pytest {pytest_exit}, signal {self.signal}, cleanup {cleanup["outcome"]} '
              f'({cleanup.get("removed", {}).get("files", 0)} files, {cleanup.get("removed", {}).get("bytes", 0)} bytes '
              f'removed) · target {target.head[:12]} tree {target.tree[:12]} · {where / "result.json"} · '
              f'{where / "report.md"}' + (f' · {len(result["failures"])} failure file(s)' if result['failures'] else ''))
        return result['outcome']['wrapper_exit']

    def emergency(self, root, scratch, result, results_fd, runs_fd):
        """Do not overwrite a first result, or attempt another cleanup after its budget expired."""
        where = root.path / RESULTS / self.run
        notice = dict(outcome='finalization-timeout', wrapper_exit=FINALIZATION_EXIT,
                      finalization_seconds=FINALIZATION_SECONDS, emergency_seconds=EMERGENCY_SECONDS,
                      scratch=str(scratch), scratch_retained=True, first_result=str(where / 'result.json'),
                      first_outcome=result.get('outcome'),
                      note='Finalization timed out; first result/report/failures and remaining scratch retained. '
                           'No cleanup retry was attempted; inspect emergency.json before treating the run as passed.')
        recorded = False
        try:
            with self.alarm(EMERGENCY_SECONDS, self.on_finalization_timeout):
                write_json(results_fd, 'emergency.json', notice)
                recorded = True
        except (FinalizationTimeout, OSError):
            pass
        finally:
            for fd in (self.lock, results_fd, runs_fd, self.scratch_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
        print(f'run {self.run}: finalization-timeout · exit {FINALIZATION_EXIT} · '
              f'first result and remaining scratch retained · '
              f'{where / "emergency.json" if recorded else "emergency report could not be written"} · {scratch}',
              file=sys.stderr)
        return FINALIZATION_EXIT

    def independent_checkout(self, target, scratch, checkout, env):
        """A complete local checkout with its own history/common directory and no borrowed objects."""
        admin = checkout / '.git'
        self.state.update(repository_kind='independent', checkout=str(checkout), admin=str(admin), checkout_intent=1)
        write_json(self.scratch_fd, 'state.json', self.state)
        clean.record(self.root, self.run, self.state)
        ancestry = gitops.history(target.top, target.head, env=env)
        steps = [(['git', '-c', 'init.templateDir=', 'init', '--quiet', checkout], 'init CI repository'),
                 (['git', '-C', checkout, '-c', 'core.hooksPath=/dev/null', 'fetch', '--no-tags',
                   '--no-auto-maintenance', '--no-write-fetch-head', str(target.project), target.head],
                  'fetch exact target history'),
                 (['git', '-C', checkout, '-c', 'core.hooksPath=/dev/null', 'checkout', '--quiet',
                   '--detach', target.head], 'checkout CI commit')]
        for argv, title in steps:
            if self.execute(argv, env=env, cwd=scratch, title=title) != 0:
                raise Refused(f'{title} failed; see the console')
            self.state['checkout_identity'] = identity(os.lstat(checkout))
            write_json(self.scratch_fd, 'state.json', self.state)
            clean.record(self.root, self.run, self.state)
        actual = gitops.git(checkout, 'rev-parse', '--absolute-git-dir', env=env).strip()
        common = gitops.git(checkout, 'rev-parse', '--path-format=absolute', '--git-common-dir', env=env).strip()
        if (Path(actual) != admin or common != actual or admin.resolve() != admin or not admin.is_dir()
                or (admin / 'objects/info/alternates').exists()):
            raise Refused('CI repository does not have its own plain Git common directory and object store')
        actual_history = gitops.history(checkout, target.head, env=env)
        if actual_history != ancestry:
            raise Refused('CI repository ancestry differs from the actual target history')
        return admin, {'common_dir': common, 'history': actual_history, 'target_history': ancestry,
                       'repository_kind': 'independent'}

    def setup_and_test(self, target, targets, scratch, checkout, env, result):
        repository = {}
        if self.ci_layout:
            admin, repository = self.independent_checkout(target, scratch, checkout, env)
        else:
            hooks = ['-c', 'core.hooksPath=/dev/null'] if getattr(self.args, 'jail', False) else []
            # Git derives the admin name from the checkout's basename. A run-specific basename avoids its
            # checkout/checkout1 suffix allocation, so recovery knows the intended entry even if add is stopped.
            admin = target.project / '.git' / 'worktrees' / checkout.name
            if os.path.lexists(admin):
                raise Refused(f'run checkout admin {admin} already exists; nothing started')
            self.state.update(checkout=str(checkout), admin=str(admin), checkout_intent=1)
            write_json(self.scratch_fd, 'state.json', self.state)
            clean.record(self.root, self.run, self.state)
            if self.execute(['git', *hooks, '-C', target.project, 'worktree', 'add', '--detach', checkout, target.head],
                            env=git_environment(), cwd=scratch, title='checkout') != 0:
                raise Refused('git worktree add failed; see the console')
            actual = gitops.git(checkout, 'rev-parse', '--absolute-git-dir').strip()
            if actual != str(admin):
                self.state['checkout_admin_mismatch'] = actual
                write_json(self.scratch_fd, 'state.json', self.state)
                clean.record(self.root, self.run, self.state)
                raise Refused(f'run checkout admin {actual} differs from intended {admin}; retained')
            self.state.update(checkout_identity=identity(os.lstat(checkout)))
            write_json(self.scratch_fd, 'state.json', self.state)
            clean.record(self.root, self.run, self.state)
        verify_options = {'env': env} if self.ci_layout else {}
        tree = gitops.git(checkout, 'rev-parse', 'HEAD^{tree}', **verify_options).strip()
        head = gitops.git(checkout, 'rev-parse', 'HEAD', **verify_options).strip()
        result['checkout'] = {'path': str(checkout), 'head': head, 'tree': tree, 'admin': str(admin), **repository}
        if getattr(self.args, 'jail', False):
            self.listed = jail.binds(scratch, user_site=self.user['site'] if self.user['exists'] else None,
                                     git_dir=target.project / '.git', admin=Path(admin))
            result['jail'] = jail.statement(listed=self.listed, env=self.env, timeout=self.args.timeout,
                                            memory_mib=self.args.memory_mib, tool=result['runner'])
        if tree != target.tree or head != target.head:
            raise Refused('run checkout HEAD/tree differs from the exact target')
        python = self.python
        venv = checkout / '.venv' if self.ci_layout else scratch / 'venv'
        if self.execute([python, '-m', 'venv', '--system-site-packages', '--without-pip', venv],
                        env=env, cwd=scratch, title='venv', jailed=True) != 0:
            raise Refused('making the run venv failed; see the console')
        vpython = venv / 'bin' / 'python'
        if self.ci_layout:
            env['PATH'] = str(venv / 'bin') + os.pathsep + env.get('PATH', '')
            env['VIRTUAL_ENV'] = str(venv)
            result['environment']['PATH'] = env['PATH']
            result['environment']['fixed']['VIRTUAL_ENV'] = str(venv)
        if self.execute([vpython, '-I', '-m', 'pip', 'install', '--no-deps', '--no-build-isolation', '--no-index',
                         '-q', '-e', checkout], env=env, cwd=scratch, title='install', jailed=True) != 0:
            raise Refused('editable install of the run checkout failed (it needs pip and setuptools in the system '
                          'site-packages); see the console')
        user = dict(self.user, path_line=None)
        result['tools']['ordinary_python3'] = {'path': python, 'resolved': str(Path(python).resolve())}
        result['tools']['user_site'] = user
        if user['exists']:
            purelib = Path(json.loads(subprocess.run(self.jailed(
                [vpython, '-I', '-c', 'import json, sysconfig; print(json.dumps(sysconfig.get_path("purelib")))'],
                scratch), capture_output=True, text=True, timeout=60, env=env, check=True).stdout))
            user['path_line'] = str(usersite.add_path_line(purelib, user['site']))
        packages = sorted(path.parent.name for path in (checkout / 'src').glob('*/__init__.py'))
        proof = subprocess.run(self.jailed([vpython, '-I', '-c', PREFLIGHT, str(checkout), *packages], scratch), cwd=scratch, env=env,
                               capture_output=True, text=True, timeout=60)
        if proof.returncode != 0:
            raise Refused(f'import proof failed: {proof.stderr.strip()[-2000:]}')
        found = json.loads(proof.stdout)
        for name in packages:
            file = found['imports'].get(name)
            expected = checkout / 'src'
            if not isinstance(file, str) or not Path(file).resolve().is_relative_to(expected):
                raise Refused(f'{name} imports from {file}, not from {expected}; refusing to test another tree')
        tests = found.get('tests_import', {})
        expected = checkout / 'tests'
        if (expected / '__init__.py').exists():
            valid_tests = (tests.get('form') == 'regular'
                           and all(isinstance(tests.get(key), str)
                                   and Path(tests[key]).resolve().is_relative_to(expected)
                                   for key in ('file', 'spec_origin')))
        else:
            paths = tests.get('paths')
            valid_tests = (tests.get('form') == 'absent' or
                           (tests.get('form') == 'namespace' and tests.get('file') is None
                            and tests.get('spec_origin') is None and isinstance(paths, list) and bool(paths)
                            and all(isinstance(path, str) and Path(path).resolve().is_relative_to(expected)
                                    for path in paths)))
        if not valid_tests:
            raise Refused(f'tests import {tests} does not prove the required package form under {expected}')
        for name in ('pytest', 'xdist', 'execnet', 'pip', 'setuptools'):
            if found.get(name):
                found[name]['origin'] = origin(found[name]['file'], venv, user)
        if not found.get('pytest'):
            raise Refused('the ordinary python3 provides no pytest')
        if self.args.workers and not found.get('xdist'):
            raise Refused('-n needs pytest-xdist, which neither the system nor the user site of the ordinary '
                          'python3 provides; pass -n 0. Nothing is installed.')
        from_user = [str(Path(found[name]['file']).parent) for name in ('pytest', 'xdist', 'execnet')
                     if found.get(name) and found[name]['origin'] == 'user site']
        stale = usersite.stale_bytecode(vpython, from_user, env, wrap=lambda argv: self.jailed(argv, scratch))
        if stale:
            raise Refused(f'{len(stale)} module(s) of the test tools in the user site lack current bytecode, so the '
                          f'run would write into the operator\'s user site; refusing. First: {stale[:3]}')
        result['tools']['run_venv'] = found
        purelib = Path(found['purelib'])
        if not purelib.resolve().is_relative_to(venv):
            raise Refused(f'the run venv\'s own site-packages {purelib} lies outside the run venv')
        plugin = Path(__file__).with_name('evidence_plugin.py')
        shutil.copyfile(plugin, purelib / 'qd_testrun_evidence.py')
        result['tools']['evidence_plugin'] = {'source': str(plugin), 'blob': gitops.blob_sha(plugin.read_bytes())}
        if self.ci_layout:
            support = purelib / '_qd_testrun_ci_support'
            support.mkdir()
            names = ('__init__.py', 'layout.py', 'owned.py', 'procs.py', 'resources.py', 'sessions.py',
                     'servers.py', 'socket_roots.py')
            for name in names:
                shutil.copyfile(plugin.with_name(name), support / name)
            result['tools']['ci_session_support'] = {name: gitops.blob_sha((support / name).read_bytes())
                                                     for name in names}
        # The startup policy: the run's own code is complete, bytecode included, before its first reader, and
        # its package folders cannot change while readers run. Only run-owned folders are compiled.
        self.execute([vpython, '-I', '-m', 'compileall', '-q', checkout / 'src', checkout / 'tests', purelib],
                     env=env, cwd=scratch, title='compile (a file that cannot compile is listed, not fatal)', jailed=True)
        readonly = 0
        for folder, _, _ in os.walk(checkout / 'src'):
            os.chmod(folder, 0o555)
            readonly += 1
        result['checkout']['readonly_package_dirs'] = readonly
        self.checkout_before = {'tracked': gitops.tracked_manifest(checkout, target.head),
                                'extra': gitops.extra_manifest(checkout)}
        argv = pytest_argv(vpython, scratch, self.args.workers, targets, ci_layout=self.ci_layout)
        result['argv'] = [str(part) for part in argv]
        if self.ci_layout:
            result['ci_layout'] = {'pytest_cwd': str(checkout), 'testruns_run_present': 'TESTRUNS_RUN' in env,
                                   'registry_present': sessions.RECORD_ENV in env,
                                   'physical_storage': self.storage['kind'],
                                   'basetemp_override': False, 'expected_tmpdir': str(checkout / '.testruns' / 't'),
                                   'socket_root': env['TMUX_TMPDIR'], 'disk_only': env.get(storage.CI_DISK_ENV) == '1',
                                   'instrumentation': ['-p qd_testrun_evidence', 'TESTRUN_EVIDENCE', storage.CI_DISK_ENV,
                                                       'TESTRUN_CI_SESSION_CONTEXT']}
            if 'TESTRUNS_RUN' in env or sessions.RECORD_ENV in env:
                raise Refused('CI pytest must not inherit normal run/registry variables')
        self.state['stage'] = 'pytest'
        write_json(self.scratch_fd, 'state.json', self.state)
        self.bound(self.args.timeout, f'time limit of {self.args.timeout} s')  # pytest's own, from its start
        self.user_before = usersite.stamps(user['site']) if user['exists'] else None
        return self.execute(argv, env=env, cwd=checkout, title='pytest', jailed=True)


def pytest_argv(vpython: Path, scratch: Path, workers: int, targets: list[str], *, ci_layout: bool = False) -> list:
    """The runner owns pytest's scheduling and forwards each selected target unchanged."""
    argv = [vpython, '-m', 'pytest', '-q', '-rs', '-p', 'qd_testrun_evidence', '-p', 'no:cacheprovider',
            f'--basetemp={scratch / "tmp" / "b"}']
    if ci_layout:
        argv = [vpython, '-m', 'pytest', '-q', '-rs', '-p', 'qd_testrun_evidence']
    if workers:
        argv += ['-n', str(workers)]
        if not ci_layout:
            argv += ['--dist', 'worksteal']
    return argv + targets


def origin(file: str, venv: Path, user: dict) -> str:
    path = Path(file).resolve()
    if path.is_relative_to(venv):
        return 'run venv'
    if user.get('exists') and path.is_relative_to(Path(user['site']).resolve()):
        return 'user site'
    return 'system'


def ci_layout_problems(found: dict, checkout: Path, workers: int) -> list[str]:
    """CI layout is a measured session property, never an inference from the requested mode."""
    layouts = found.get('session_layouts', [])
    roles = {item['role'] for item in layouts}
    expected = {'main', *(f'gw{number}' for number in range(workers))}
    problems = []
    if not expected.issubset(roles):
        problems.append('CI layout session observations missing')
    for item in layouts:
        if (item.get('cwd') != str(checkout) or item.get('TMPDIR') != str(checkout / '.testruns' / 't')
                or item.get('tempdir') != item.get('TMPDIR') or item.get('testruns_run_present') is not False
                or item.get('disk_only') is not True or item.get('registry_present') is not False
                or (item['role'] == 'main' and item.get('basetemp') is not None)):
            problems.append(f'CI layout session {item["role"]} differs from the requested checkout/temp/environment')
    return problems


def decide(found: dict, timed_out: bool, signalled) -> str:
    if timed_out:
        return 'timeout'
    if signalled is not None:
        return 'interrupted'
    if not found['complete']:
        return 'incomplete'
    if not found['expected']:
        return 'no-tests'
    return 'failed' if (found['counts']['failed'] or found['counts']['error']) else 'passed'


def check_target(top: Path, item: str) -> str:
    path = item.split('::', 1)[0]
    if not path or Path(path).is_absolute() or '..' in Path(path).parts or item.startswith('-'):
        raise Refused(f'target {item!r} must be a path inside the worktree, relative to its top')
    if not (top / path).exists():
        raise Refused(f'target {item!r} does not exist in {top}')
    return item


def socket_entries(root: Root) -> list[str]:
    try:
        fd = root.child(SOCKETS)
    except Refused:
        return []
    try:
        return sorted(f'{name}:{os.stat(name, dir_fd=fd, follow_symlinks=False).st_ino}' for name in os.listdir(fd))
    finally:
        os.close(fd)


def manifests_target(target, before: dict) -> dict:
    tracked = gitops.tracked_manifest(target.top, target.head)
    extra = gitops.extra_manifest(target.top)
    head = gitops.git(target.top, 'rev-parse', 'HEAD').strip()
    return {'head_before': target.head, 'head_after': head,
            'tracked_before': before['tracked'], 'tracked_after': tracked,
            'extra_before': before['extra'], 'extra_after': extra,
            'extra_changes': gitops.compare(before['extra']['entries'], extra['entries']),
            'unchanged': (head == target.head and tracked == before['tracked'] and extra == before['extra'])}


def manifests_checkout(checkout: Path, head: str, before: dict) -> dict:
    tracked = gitops.tracked_manifest(checkout, head)
    extra = gitops.extra_manifest(checkout)
    return {'tracked_before': before['tracked'], 'tracked_after': tracked,
            'extra_before': {'count': before['extra']['count'], 'digest': before['extra']['digest']},
            'extra_after': {'count': extra['count'], 'digest': extra['digest']},
            'mutations': {'tracked': gitops.compare(before['tracked']['differ'], tracked['differ']),
                          'extra': gitops.compare(before['extra']['entries'], extra['entries'])}}


def summary(found: dict) -> str:
    if found['complete']:
        return ', '.join(f'{value} {key}' for key, value in found['counts'].items() if value) or 'no tests'
    return 'evidence incomplete: ' + '; '.join(found['causes'][:3])
