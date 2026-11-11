"""Git as the authority on what is tested: the target worktree, its project, commit, tree and branch, and
exact manifests of a checkout's tracked and untracked or ignored contents, by hash and stamp, never content."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import subprocess

from .layout import Refused, git_environment

MAIN = 'refs/heads/main'


def git(cwd: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, env=git_environment())
    if check and done.returncode:
        raise Refused(f'git {" ".join(args)} in {cwd}: {done.stderr.decode(errors="replace").strip()}')
    return done.stdout.decode(errors='surrogateescape')


def ok(cwd: Path, *args: str) -> bool:
    return subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, env=git_environment()).returncode == 0


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


def status(top: Path) -> list[tuple[str, str]]:
    """Every entry Git reports, ignored ones included: (two-letter code, path)."""
    raw = git(top, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignored=matching')
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


def committed(top: Path, commit: str) -> dict[str, tuple[str, str]]:
    raw = git(top, 'ls-tree', '-r', '-z', '--full-tree', commit)
    entries = {}
    for item in raw.split('\0'):
        if item:
            meta, name = item.split('\t', 1)
            mode, kind, sha = meta.split()
            if kind == 'blob':
                entries[name] = (mode, sha)
    return entries


def tracked_manifest(top: Path, commit: str) -> dict:
    """The commit's tracked files compared with what is on disk: the files that differ, by hash."""
    expected = committed(top, commit)
    differ = {}
    for name, entry in expected.items():
        actual = disk_entry(top / name)
        if actual != entry:
            differ[name] = {'committed': list(entry), 'disk': list(actual) if actual else None}
    digest = hashlib.sha256(''.join(f'{n}\0{m}\0{s}\n' for n, (m, s) in sorted(expected.items())).encode()).hexdigest()
    return {'commit': commit, 'files': len(expected), 'digest': digest, 'differ': differ}


def extra_manifest(top: Path) -> dict:
    """Untracked and ignored entries as Git lists them, each with its type, size and identity stamps."""
    entries = {}
    for code, name in status(top):
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
