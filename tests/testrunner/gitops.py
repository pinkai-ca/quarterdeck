"""Git as the authority on what is tested: the target worktree, its project, commit, tree and branch, and
exact manifests of a checkout's tracked and untracked or ignored contents, by hash and stamp, never content."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import stat
import subprocess

from .layout import Refused, git_environment

MAIN = 'refs/heads/main'
# Overrides on the tool's own git. Not a substitute for a git dir the jail cannot write.
PINNED_CONFIG = ('-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null', '-c', 'core.untrackedCache=false')


def git(cwd: Path, *args: str, check: bool = True, env: dict | None = None, config: tuple = ()) -> str:
    done = subprocess.run(['git', *config, '-C', str(cwd), *args], capture_output=True,
                          env=git_environment() if env is None else env)
    if check and done.returncode:
        raise Refused(f'git {" ".join(args)} in {cwd}: {done.stderr.decode(errors="replace").strip()}')
    return done.stdout.decode(errors='surrogateescape')


def ok(cwd: Path, *args: str) -> bool:
    return subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, env=git_environment()).returncode == 0


def history(top: Path, head: str, *, env: dict) -> dict:
    """Complete real ancestry of this exact commit, independent of ref names or traversal ordering."""
    if git(top, 'rev-parse', '--is-shallow-repository', env=env).strip() != 'false':
        raise Refused('CI layout needs complete target history, not a shallow repository')
    commits = sorted(git(top, 'rev-list', head, env=env).splitlines())
    return {'commits': len(commits), 'sha256': hashlib.sha256(('\n'.join(commits) + '\n').encode()).hexdigest()}


@dataclass(frozen=True)
class Target:
    top: Path        # the target worktree (or the project's main checkout)
    project: Path    # the parent of the Git common directory: where .testruns lives
    head: str
    tree: str
    branch: str | None

    @property
    def main_checkout(self) -> bool:
        return self.top == self.project


def worktrees(project: Path) -> dict[Path, dict]:
    listed, current = {}, None
    for line in git(project, 'worktree', 'list', '--porcelain').splitlines():
        if line.startswith('worktree '):
            current = Path(line[len('worktree '):])
            listed[current] = {}
        elif current is not None and ' ' in line:
            key, value = line.split(' ', 1)
            listed[current][key] = value
        elif current is not None and line:
            listed[current][line] = True
    return listed


def resolve(path: Path) -> Target:
    """The worktree ``path`` lies in, by Git, independent of where this tool's own files are."""
    path = Path(path).absolute()
    if not path.is_dir():
        raise Refused(f'target {path} is not a directory')
    top = Path(git(path, 'rev-parse', '--show-toplevel').strip())
    common = Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip())
    if common.name != '.git' or common.is_symlink() or not common.is_dir():
        raise Refused(f'target {top}: its Git common directory {common} is not a plain .git folder of a project')
    project = common.parent
    if top.resolve() != top or project.resolve() != project:
        raise Refused(f'target {top} or project {project} is reached through a link; name the real path')
    if top not in worktrees(project):
        raise Refused(f'target {top} is not a worktree Git lists for {project}')
    head = git(top, 'rev-parse', '--verify', 'HEAD^{commit}').strip()
    tree = git(top, 'rev-parse', '--verify', 'HEAD^{tree}').strip()
    branch = git(top, 'symbolic-ref', '-q', '--short', 'HEAD', check=False).strip() or None
    return Target(top, project, head, tree, branch)


def file_fingerprint(path: Path) -> str:
    """Identity of one file's own bytes, or of a symlink itself. Never a Git read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return 'missing'
    except OSError as error:
        if error.errno == errno.ELOOP:
            return 'link:' + hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
        return f'unreadable:{error.errno}'
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return 'not-a-file'
        digest = hashlib.sha256()
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        return digest.hexdigest()
    finally:
        os.close(fd)


def pinned_environment(git_dir, common_dir, work_tree, index: Path | None = None) -> dict:
    """Git environment for an outer call: the tool's recorded directories, set after the caller's are stripped."""
    env = git_environment()
    env['GIT_DIR'] = str(git_dir)
    env['GIT_COMMON_DIR'] = str(common_dir)
    env['GIT_WORK_TREE'] = str(work_tree)
    if index is not None:
        env['GIT_INDEX_FILE'] = str(index)
    return env


def private_index_environment(git_dir, common_dir, work_tree, index: Path, commit: str) -> dict:
    """An index the tool builds outside the checkout's git directory, holding ``commit`` and nothing else."""
    index.parent.mkdir(parents=True, exist_ok=True)
    if index.is_symlink() or index.exists():
        index.unlink()
    env = pinned_environment(git_dir, common_dir, work_tree, index)
    git(Path(work_tree), 'read-tree', commit, env=env, config=PINNED_CONFIG)
    return env


def status(top: Path, *, env: dict | None = None, config: tuple = ()) -> list[tuple[str, str]]:
    """Every entry Git reports, ignored ones included: (two-letter code, path)."""
    raw = git(top, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignored=matching',
              env=env, config=config)
    entries, parts = [], raw.split('\0')
    index = 0
    while index < len(parts):
        item = parts[index]
        index += 1
        if not item:
            continue
        code, name = item[:2], item[3:]
        if code[0] in 'RC':
            index += 1  # the rename's source path follows
        entries.append((code, name))
    return entries


def dirty(top: Path) -> list[str]:
    return [f'{code} {name}' for code, name in status(top) if code != '!!']


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def disk_entry(path: Path) -> tuple[str, str] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return '120000', blob_sha(os.fsencode(os.readlink(path)))
    if stat.S_ISREG(info.st_mode):
        return ('100755' if info.st_mode & stat.S_IXUSR else '100644'), blob_sha(path.read_bytes())
    return 'other', ''


def committed(top: Path, commit: str, *, env: dict | None = None, config: tuple = ()) -> dict[str, tuple[str, str]]:
    raw = git(top, 'ls-tree', '-r', '-z', '--full-tree', commit, env=env, config=config)
    entries = {}
    for item in raw.split('\0'):
        if item:
            meta, name = item.split('\t', 1)
            mode, kind, sha = meta.split()
            if kind == 'blob':
                entries[name] = (mode, sha)
    return entries


def tracked_manifest(top: Path, commit: str, *, git_at: Path | None = None, env: dict | None = None,
                     config: tuple = ()) -> dict:
    """The commit's tracked files compared with what is on disk: the files that differ, by hash.

    ``git_at`` chooses where ``commit`` is read. Disk is always ``top``.
    """
    expected = committed(git_at or top, commit, env=env, config=config)
    differ = {}
    for name, entry in expected.items():
        actual = disk_entry(top / name)
        if actual != entry:
            differ[name] = {'committed': list(entry), 'disk': list(actual) if actual else None}
    digest = hashlib.sha256(''.join(f'{n}\0{m}\0{s}\n' for n, (m, s) in sorted(expected.items())).encode()).hexdigest()
    return {'commit': commit, 'files': len(expected), 'digest': digest, 'differ': differ}


def extra_manifest(top: Path, *, env: dict | None = None, config: tuple = ()) -> dict:
    """Untracked and ignored entries as Git lists them, each with its type, size and identity stamps."""
    entries = {}
    for code, name in status(top, env=env, config=config):
        if code not in ('??', '!!'):
            continue
        try:
            info = (top / name.rstrip('/')).lstat()
            entries[name] = [code, stat.filemode(info.st_mode)[0], info.st_size, info.st_dev, info.st_ino,
                             info.st_mtime_ns]
        except FileNotFoundError:
            entries[name] = [code, 'missing']
    digest = hashlib.sha256(repr(sorted(entries.items())).encode()).hexdigest()
    return {'entries': entries, 'count': len(entries), 'digest': digest}


def compare(before: dict, after: dict) -> dict:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(name for name in set(before) & set(after) if before[name] != after[name])
    return {'added': added, 'removed': removed, 'changed': changed}


def runner_identity(files: list[Path]) -> dict:
    """The executing tool's own files, by blob hash, and whether they equal the commit of their checkout."""
    hashes = {str(path): blob_sha(path.read_bytes()) for path in files}
    folder = files[0].parent
    try:
        top = Path(git(folder, 'rev-parse', '--show-toplevel').strip())
        head = git(top, 'rev-parse', '--verify', 'HEAD^{commit}').strip()
        committed_entries = committed(top, head)
        relative = {str(path): str(path.resolve().relative_to(top)) for path in files}
        matches = all(committed_entries.get(relative[name], (None, None))[1] == sha for name, sha in hashes.items())
        return {'files': hashes, 'checkout': str(top), 'head': head, 'matches_head': matches}
    except (Refused, ValueError):
        return {'files': hashes, 'checkout': None, 'head': None, 'matches_head': False}
