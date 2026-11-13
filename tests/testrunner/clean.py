"""Registration of a change, retirement of a run's scratch, same-change recovery of abandoned runs, ``clean``
after a change is merged or deliberately dropped, and ``keep``. Eligibility comes from the registration and
Git state, never from a file's name; anything unrecorded refuses."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import sys
import time

from . import evidence, gitops, owned, procs, report, sessions
from .layout import (CHANGES, DIRECTORY, KEEPS, RESULTS, RETIREMENTS, RUNS, SOCKETS, Refused, Root, Slot, change_file,
                     identity, read_json, run_name, write_json)


def now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


# -- registration ---------------------------------------------------------------------------------------------
def register(root: Root, target, run: str) -> None:
    fd = root.child(CHANGES)
    try:
        name = change_file(target.branch) + '.json'
        data = read_json(fd, name, root.path / CHANGES / name)
        worktree_id = identity(os.lstat(target.top))
        if data is None:
            data = {'change': target.branch, 'branch': target.branch, 'worktree': str(target.top),
                    'worktree_identity': worktree_id, 'registered': now(),
                    'main_at_registration': gitops.git(target.project, 'rev-parse', gitops.MAIN).strip(),
                    'runs': [], 'drop': None, 'retired': None}
        if data.get('retired'):
            raise Refused(f'change {target.branch} was retired {data["retired"]["at"]}; a new change needs a new branch')
        if data['worktree'] != str(target.top) or data['worktree_identity'] != worktree_id:
            raise Refused(f'change {target.branch} is registered to worktree {data["worktree"]} '
                          f'{data["worktree_identity"]}, not {target.top} {worktree_id}')
        data['runs'].append({'run': run, 'head': target.head, 'tree': target.tree, 'at': now()})
        write_json(fd, name, data)
    finally:
        os.close(fd)


# -- one run's scratch ----------------------------------------------------------------------------------------
def kept(root: Root, run: str) -> dict | None:
    fd = root.child(KEEPS)
    try:
        return read_json(fd, run + '.json', root.path / KEEPS / (run + '.json'))
    finally:
        os.close(fd)


def retire_run(root: Root, runs_fd: int, run: str, state: dict) -> dict:
    """Cleanup is reported separately from the first test outcome, including ordinary cleanup errors."""
    try:
        return _retire_run(root, runs_fd, run, state)
    except Exception as error:
        # The finalization alarm is a BaseException: its emergency ending still owns that deadline.
        return {'outcome': 'held', 'scratch': str(root.path / RUNS / run),
                'reason': f'{type(error).__name__}: {error}; exact remainder retained'}


def _retire_run(root: Root, runs_fd: int, run: str, state: dict) -> dict:
    """Remove a finished run's scratch by its recorded identity, and its checkout's Git admin entry."""
    note = kept(root, run)
    if note:
        return {'outcome': 'kept', 'reason': note['reason'], 'scratch': str(root.path / RUNS / run)}
    admin = state.get('admin')
    if admin:
        # The forced removal below must reach this run's own checkout only. state.json is read back from disk
        # on recovery, so before anything goes it must name what run.py made: the run's own checkout path and
        # an admin entry directly under the project's .git/worktrees. Otherwise nothing is touched.
        own = root.path / RUNS / run / 'checkout'
        checkout = state.get('checkout')
        if checkout != str(own) or Path(admin).parent != root.project / '.git' / 'worktrees':
            return {'outcome': 'held', 'scratch': str(root.path / RUNS / run),
                    'reason': f'state.json names checkout {checkout} and Git admin {admin}, not this run\'s own '
                              f'{own} under {root.project / ".git" / "worktrees"}; nothing removed'}
    budget = owned.Budget()
    cleanup = owned.retire(runs_fd, run, state.get('scratch'), root.path / RUNS / run,
                           state.get('owner'), budget=budget)
    cleanup['sessions'] = sessions.retire(root, run, budget=budget)
    if cleanup['sessions']['outcome'] == 'held':
        cleanup['outcome'] = 'held'
    if admin and cleanup['outcome'] == 'complete':
        checkout = state['checkout']
        done = gitops.ok(root.project, 'worktree', 'remove', '--force', checkout)
        cleanup['git_admin'] = {'path': admin, 'removed': not Path(admin).exists(), 'command_ok': done}
        if Path(admin).exists():
            cleanup['outcome'] = 'held'
            cleanup['reason'] = f'git worktree admin {admin} remains; `git worktree remove --force {checkout}` clears it'
    return cleanup


def record(root: Root, run: str, state: dict) -> None:
    """Keep exact ownership evidence outside scratch before it can be partially retired."""
    fd = root.child(RESULTS, run)
    try:
        write_json(fd, 'ownership.json', state)
    finally:
        os.close(fd)


def discard_records(root: Root, run: str, state: dict, cleanup: dict) -> dict:
    """Recovery records survive only a crash, keep or incomplete retirement, never a clean ending."""
    if cleanup['outcome'] != 'complete':
        return cleanup
    fd = None
    try:
        budget = owned.Budget()
        sessions.discard_records(root, run, budget)
        fd = root.child(RESULTS, run, create=False)
        for name in ('retirement.json', 'retirement-timeout.json', 'ownership.json'):
            budget.check(root.path / RESULTS / run / name)
            data = read_json(fd, name, root.path / RESULTS / run / name)
            if data is None:
                continue
            if data.get('run') != run:
                raise Refused(f'{name}: not this run\'s recovery record; retained')
            if name == 'ownership.json' and (data.get('scratch') != state.get('scratch')
                                            or data.get('owner') != state.get('owner')):
                raise Refused('ownership record changed; retained')
            owned.unlink_file(fd, name, root.path / RESULTS / run / name)
    except Exception as error:
        cleanup.update(outcome='held', reason=f'recovery records retained: {error}')
    finally:
        if fd is not None:
            os.close(fd)
    return cleanup


def retirement_result(root: Root, run: str, cleanup: dict) -> None:
    """The explicit command's result is beside, never inside or over, the original test evidence."""
    fd = root.child(RETIREMENTS)
    try:
        write_json(fd, run + '.json', {'run': run, 'at': now(), 'cleanup': cleanup,
                                      'first_result_preserved': True})
    finally:
        os.close(fd)


def retire(args) -> int:
    """Retry exactly one recorded crashed run, retaining its first result and every other run."""
    # Reuse the landed finalization policy; the local import avoids the runner/cleanup import cycle.
    from .run import Runner, FinalizationTimeout, FINALIZATION_SECONDS, EMERGENCY_SECONDS, FINALIZATION_EXIT

    run = run_name(args.run)
    project = gitops.resolve(Path.cwd()).project
    root = Root(project, create=False)
    slot = Slot(root)
    acquired = False
    timer = Runner(args, [])
    try:
        slot.acquire({'verb': 'retire', 'run': run, 'seat': os.environ.get('QD_SEAT')}, args.wait)
        acquired = True
        with timer.alarm(FINALIZATION_SECONDS, timer.on_finalization_timeout):
            return retire_within(root, run)
    except FinalizationTimeout:
        where = root.path / RESULTS / run
        try:
            with timer.alarm(EMERGENCY_SECONDS, timer.on_finalization_timeout):
                fd = root.child(RESULTS, run, create=False)
                try:
                    write_json(fd, 'retirement-timeout.json', {'run': run, 'outcome': 'retirement-timeout',
                               'scratch': str(root.path / RUNS / run), 'first_result_preserved': True,
                               'finalization_seconds': FINALIZATION_SECONDS, 'emergency_seconds': EMERGENCY_SECONDS})
                finally:
                    os.close(fd)
        except (FinalizationTimeout, OSError, Refused):
            pass
        print(f'run {run}: retirement timed out; first result and exact remaining paths retained · {where}',
              file=sys.stderr)
        return FINALIZATION_EXIT
    finally:
        if acquired:
            slot.release()
        else:
            os.close(slot.fd)
        root.close()


def retire_within(root: Root, run: str) -> int:
    where = root.path / RUNS / run
    results = runs = scratch = lock = None
    try:
        results = root.child(RESULTS, run, create=False)
        runs = root.child(RUNS, create=False)
        state = read_json(results, 'ownership.json', root.path / RESULTS / run / 'ownership.json')
        try:
            scratch = root.child(RUNS, run, create=False)
        except FileNotFoundError:
            pass
        if state is None and scratch is not None:
            state = read_json(scratch, 'state.json', where / 'state.json')
        if state is None and scratch is None:
            previous = anchor = None
            try:
                try:
                    previous = root.child(RETIREMENTS, create=False)
                    done = read_json(previous, run + '.json', root.path / RETIREMENTS / (run + '.json'))
                except FileNotFoundError:
                    done = None
                completed = done.get('cleanup', {}) if done and done.get('run') == run else {}
                if completed.get('outcome') == 'complete':
                    try:
                        anchor = root.child(SOCKETS, create=False)
                    except FileNotFoundError:
                        pass
                    for entry in completed.get('sessions', {}).get('entries', []):
                        path = Path(entry['path'])
                        if path.parent != root.path / SOCKETS:
                            raise Refused(f'{path}: not a recorded session path in this project; held')
                        if anchor is not None:
                            try:
                                os.stat(path.name, dir_fd=anchor, follow_symlinks=False)
                            except FileNotFoundError:
                                pass
                            else:
                                raise Refused(f'{path}: recorded retirement is no longer absent; held')
                    for name in ('sessions', 'retirement.json', 'retirement-timeout.json'):
                        try:
                            os.stat(name, dir_fd=results, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            raise Refused(f'{name}: recovery metadata remains without ownership; held')
                    print(f'run {run}: already retired; first result/report/failures retained')
                    return 0
            finally:
                for fd in (anchor, previous):
                    if fd is not None:
                        os.close(fd)
        if not state or state.get('run') != run or not state.get('scratch') or not state.get('owner'):
            raise Refused(f'{where}: no exact recorded run identity and owner; nothing inferred or removed')
        if kept(root, run):
            raise Refused(f'{where}: kept by note; nothing removed')
        first = read_json(results, 'result.json', root.path / RESULTS / run / 'result.json')
        if first is None and scratch is None:
            raise Refused(f'{where}: no first result or scratch evidence; recovery records retained')
        if scratch is not None:
            if identity(os.fstat(scratch)) != state['scratch']:
                raise Refused(f'{where}: run directory identity changed; nothing removed')
            verified = owned._open_exact(runs, run, state['scratch'], where, state['owner'])
            os.close(verified)
            lock = try_lock(scratch)
            if lock is None:
                raise Refused(f'{where}: run lock is held; nothing removed')
        if state.get('boot') == procs.boot_id():
            pid, start = state.get('pid'), state.get('pid_start')
            actual_start = procs.start_time(pid) if pid else None
            if pid and (actual_start is not None and actual_start == start or actual_start is None
                        and Path(f'/proc/{pid}').exists()):
                raise Refused(f'{where}: recorded runner is live or uninspectable; nothing removed')
            if any(procs.group_members(pgid) for pgid, _ in state.get('groups', [])):
                raise Refused(f'{where}: recorded group still has live processes; held, never signalled')
        record(root, run, state)
        if scratch is not None:
            finish_abandoned(root, run, state, scratch_fd=scratch)
        cleanup = retire_run(root, runs, run, state)
        retirement_result(root, run, cleanup)  # durable completion before the last recovery record goes
        cleanup = discard_records(root, run, state, cleanup)
        retirement_result(root, run, cleanup)
        print(f'run {run}: retirement {cleanup["outcome"]} · {where} · '
              f'{root.path / RETIREMENTS / (run + ".json")}')
        if cleanup.get('reason'):
            print(cleanup['reason'])
        return 0 if cleanup['outcome'] == 'complete' else 2
    except OSError as error:
        raise Refused(f'{where}: retirement held ({error}); remaining paths and first result retained') from None
    finally:
        for fd in (lock, scratch, runs, results):
            if fd not in (None, -1):
                os.close(fd)


def try_lock(fd: int) -> int | None:
    """The run's lock if no process of the run holds it, else None."""
    try:
        lock = os.open('lock', os.O_RDWR | os.O_NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        return -1  # made before its lock: nothing of it can be running
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock
    except BlockingIOError:
        os.close(lock)
        return None


def recover(root: Root, change: str | None) -> list[str]:
    """Finish and remove this change's abandoned runs; report every other leftover and touch none of it."""
    lines = []
    runs_fd = root.child(RUNS)
    try:
        for name in sorted(os.listdir(runs_fd)):
            try:
                run_name(name)
                fd = root.child(RUNS, name, create=False)
            except Refused as refused:
                lines.append(f'runs/{name}: left alone ({refused})')
                continue
            try:
                state = read_json(fd, 'state.json', root.path / RUNS / name / 'state.json')
                if state is None or state.get('change') != change:
                    owner = 'unknown' if state is None else state.get('change')
                    lines.append(f'run {name} of change {owner} has scratch left; only its own change recovers it')
                    continue
                if kept(root, name):
                    lines.append(f'run {name}: kept by note; left alone')
                    continue
                lock = try_lock(fd)
                if lock is None:
                    lines.append(f'run {name}: live (its lock is held by a run process); left alone')
                    continue
                try:
                    live = [pid for pgid, _ in state.get('groups', []) for pid in procs.group_members(pgid)] \
                        if state.get('boot') == procs.boot_id() else []
                    if live:
                        lines.append(f'run {name}: processes {live} remain in its own groups; held, not killed')
                        continue
                    lines.append(f'recovering abandoned run {name} of change {change}: removing '
                                 f'{root.path / RUNS / name} {state["scratch"]} and Git admin {state.get("admin")}')
                    finish_abandoned(root, name, state)
                    record(root, name, state)
                    cleanup = retire_run(root, runs_fd, name, state)
                    cleanup = discard_records(root, name, state, cleanup)
                    lines.append(f'run {name}: abandoned · cleanup {cleanup["outcome"]} '
                                 f'{cleanup.get("removed", {})} {cleanup.get("reason", "")}'.rstrip())
                finally:
                    if lock not in (None, -1):
                        os.close(lock)
            finally:
                os.close(fd)
    finally:
        os.close(runs_fd)
    return lines


def finish_abandoned(root: Root, run: str, state: dict, *, scratch_fd=None) -> None:
    results_fd = root.child(RESULTS, run)
    try:
        result = read_json(results_fd, 'result.json', root.path / RESULTS / run / 'result.json') or {'run': run}
        if result.get('outcome', {}).get('status') not in (None, 'running'):
            return  # its own ending was written; only the scratch removal was interrupted
        scratch = root.path / RUNS / run
        console_path = scratch / 'console.txt'
        if scratch_fd is None:
            console = console_path.read_text(encoding='utf-8', errors='replace') if console_path.is_file() else ''
        else:
            try:
                console = evidence.read_owned(scratch_fd, 'console.txt', console_path)
            except FileNotFoundError:
                console = ''
        found = evidence.analyse(scratch / 'evidence', None, parent_fd=scratch_fd)
        found.update(complete=False, counts=dict.fromkeys(evidence.CATEGORIES),
                     causes=['abandoned: the runner ended without writing its ending', *found['causes']])
        result.setdefault('target', {'top': state.get('target'), 'head': state.get('head'), 'tree': None})
        result.setdefault('runner', {})
        result['evidence'] = {key: value for key, value in found.items() if key != 'failures'}
        result['failures'] = report.write_failures(results_fd, found['failures'])
        result['outcome'] = {'status': 'abandoned', 'wrapper_exit': 5, 'pytest_exit': None, 'signal': None,
                             'note': f'recovered {now()}'}
        result['cleanup'] = {'outcome': 'recovery'}
        report.write(results_fd, result, console, found['failures'])
    finally:
        os.close(results_fd)


# -- clean ----------------------------------------------------------------------------------------------------
def clean(args) -> int:
    project = gitops.resolve(Path.cwd()).project
    root = Root(project, create=False)
    slot = Slot(root)
    slot.acquire({'verb': 'clean', 'change': args.change, 'seat': os.environ.get('QD_SEAT')}, args.wait)
    try:
        return clean_within(root, args)
    finally:
        slot.release()
        root.close()


def clean_within(root: Root, args) -> int:
    change, project = args.change, root.project
    changes_fd = root.child(CHANGES)
    name = change_file(change)
    try:
        data = read_json(changes_fd, name + '.json', root.path / CHANGES / (name + '.json'))
        if data is None:
            raise Refused(f'change {change} is not registered (no run of it was made by this tool); nothing touched')
        if data.get('retired'):
            print(f'change {change} was already retired {data["retired"]["at"]}; nothing to do')
            return 0
        branch, worktree = data['branch'], Path(data['worktree'])
        if branch == 'main' or worktree == project:
            raise Refused('main and the project checkout are never cleaned')
        for line in recover(root, change):
            print(line)
        leftover = [run['run'] for run in data['runs'] if (root.path / RUNS / run['run']).exists()
                    and not kept(root, run['run'])]
        if leftover:
            raise Refused(f'runs {leftover} of {change} still have scratch (live or held); nothing more touched')
        tip = gitops.git(project, 'rev-parse', '--verify', '-q', f'refs/heads/{branch}', check=False).strip() or None
        merged = tip is not None and gitops.ok(project, 'merge-base', '--is-ancestor', tip, gitops.MAIN)
        drop = data.get('drop')
        if args.drop:
            if not args.reason:
                raise Refused('--drop needs --reason TEXT: why this unmerged work is discarded')
            if drop and drop['tip'] != tip:
                raise Refused(f'a drop of {change} was recorded at tip {drop["tip"]}; the branch is now {tip}; '
                              'new work since the drop: nothing touched')
            if not drop:
                drop = {'tip': tip, 'reason': args.reason, 'at': now(), 'seat': os.environ.get('QD_SEAT')}
                data['drop'] = drop
                write_json(changes_fd, name + '.json', data)  # the intent is durable before anything goes
        elif not merged and tip is not None:
            if not drop or drop['tip'] != tip:
                raise Refused(f'branch {branch} at {tip} is not merged into main; discarding it takes '
                              '--drop --reason TEXT; nothing touched')
        plan = eligible(project, worktree, data, branch, tip)
        journal = name + '.clean.json'
        write_json(changes_fd, journal, {'change': change, 'started': now(), 'plan': plan, 'drop': drop,
                                         'merged': merged, 'done': []})
        print(f'cleaning {change}: ' + '; '.join(step['what'] for step in plan) if plan else
              f'cleaning {change}: worktree and branch already gone')
        done = []
        for step in plan:
            done.append(perform(project, step, drop))
            write_json(changes_fd, journal, {'change': change, 'started': now(), 'plan': plan, 'drop': drop,
                                             'merged': merged, 'done': done})
        data['retired'] = {'at': now(), 'merged': merged, 'dropped': bool(drop), 'removed': done}
        write_json(changes_fd, name + '.json', data)
        kept_runs = [run['run'] for run in data['runs'] if kept(root, run['run'])]
        for item in done:
            print(f'removed {item["what"]}: {item.get("counts", "")}'.rstrip(': '))
        print(f'change {change} retired · {len(data["runs"])} result(s) and report(s) kept under '
              f'{root.path / RESULTS} · ' + (f'kept scratch of runs {kept_runs} remains' if kept_runs
                                             else 'no kept scratch'))
        return 0
    finally:
        os.close(changes_fd)


def eligible(project: Path, worktree: Path, data: dict, branch: str, tip: str | None) -> list[dict]:
    """What may go, proven now: the registered worktree, unchanged and holding nothing unrecorded, and the
    branch. A worktree or branch already gone (a resumed clean) is simply not in the plan."""
    plan = []
    if os.path.lexists(worktree):
        if identity(os.lstat(worktree)) != data['worktree_identity']:
            raise Refused(f'{worktree} is not the registered worktree (replaced or a link); nothing touched')
        listed = gitops.worktrees(project).get(worktree)
        if listed is None or listed.get('branch') != f'refs/heads/{branch}':
            raise Refused(f'{worktree} is not a Git worktree on {branch}; nothing touched')
        entries = gitops.status(worktree)
        if entries:
            shown = '\n  '.join(f'{code} {path}' for code, path in entries[:30])
            raise Refused(f'{worktree} holds material the tool did not record (modified, untracked or ignored); '
                          f'its owner removes it or commits it first; nothing touched:\n  {shown}')
        users, uninspectable = procs.users_of(worktree)
        if users:
            raise Refused(f'{worktree} is in use by {users}; nothing touched')
        parent = os.open(worktree.parent, DIRECTORY)
        try:
            counts = owned.measure(parent, worktree.name, data['worktree_identity'], worktree).as_dict()
        finally:
            os.close(parent)
        plan.append({'what': f'worktree {worktree}', 'kind': 'worktree', 'path': str(worktree), 'counts': counts,
                     'uninspectable_processes_not_participants': uninspectable})
    if tip is not None:
        plan.append({'what': f'branch {branch} at {tip}', 'kind': 'branch', 'branch': branch, 'tip': tip})
    return plan


def perform(project: Path, step: dict, drop: dict | None) -> dict:
    if step['kind'] == 'worktree':
        gitops.git(project, 'worktree', 'remove', step['path'])  # not forced: Git refuses anything unclean
        if os.path.lexists(step['path']):
            raise Refused(f'{step["path"]} remains after git worktree remove')
        return {'what': step['what'], 'counts': step['counts']}
    tip = gitops.git(project, 'rev-parse', '--verify', f'refs/heads/{step["branch"]}').strip()
    if tip != step['tip']:
        raise Refused(f'branch {step["branch"]} moved to {tip} during clean; left alone')
    if drop:
        if drop['tip'] != tip:
            raise Refused(f'branch {step["branch"]} is not at the recorded drop tip {drop["tip"]}; left alone')
        gitops.git(project, 'branch', '-D', step['branch'])
        return {'what': step['what'], 'counts': f'dropped; restore with `git branch {step["branch"]} {tip}` '
                                                 'while the commit exists'}
    gitops.git(project, 'branch', '-d', step['branch'])
    return {'what': step['what'], 'counts': 'merged'}


# -- keep -----------------------------------------------------------------------------------------------------
def keep(args) -> int:
    run = run_name(args.run)
    if not args.reason or not args.reason.strip():
        raise Refused('keep needs --reason TEXT')
    project = gitops.resolve(Path.cwd()).project
    root = Root(project, create=False)
    try:
        try:
            fd = root.child(RUNS, run, create=False)
        except (Refused, FileNotFoundError):
            raise Refused(f'run {run} has no scratch left to keep; its result, report and failure output are '
                          'always retained') from None
        try:
            state = read_json(fd, 'state.json', root.path / RUNS / run / 'state.json')
            if state is None or identity(os.fstat(fd)) != state['scratch']:
                raise Refused(f'run {run}: scratch identity unrecorded or changed; nothing kept')
        finally:
            os.close(fd)
        keeps_fd = root.child(KEEPS)
        try:
            write_json(keeps_fd, run + '.json', {'run': run, 'change': state.get('change'), 'reason': args.reason,
                                                 'at': now(), 'seat': os.environ.get('QD_SEAT'),
                                                 'scratch': state['scratch']})
        finally:
            os.close(keeps_fd)
        print(f'kept run {run} scratch at {root.path / RUNS / run}: {args.reason} · its end and its change\'s clean '
              'leave it; the note is ' + str(root.path / KEEPS / (run + '.json')))
        return 0
    finally:
        root.close()
