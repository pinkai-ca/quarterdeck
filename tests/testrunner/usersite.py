"""The ordinary interpreter's user site, made visible to the run venv by one plain read-only path line.

A venv never sees its user's site-packages, and the run's HOME is its own, so tools a person installed with
``pip --user`` (pytest-xdist, typically) would vanish. The user site is resolved from the ordinary ``python3``
in the caller's own environment before HOME is replaced, and the run venv gets one ``.pth`` file holding that
directory as a plain path line. Python appends a plain line to ``sys.path`` and does not process the ``.pth``
files or finders inside the directory it names, and every interpreter of the run starts with PYTHONNOUSERSITE,
so none processes a user site of its own: the user's own editable installs and startup hooks stay out. The
directory is only read: the bytecode of the tools taken from it must already be current (the run would
otherwise write into it, and refuses), and every bytecode folder in it is stamped before and after.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

from .layout import Refused, write_new

PATH_LINE = 'qd-testrun-usersite.pth'
RESOLVE = 'import json, site; print(json.dumps({"base": site.getuserbase(), "site": site.getusersitepackages()}))'

CURRENT = r'''
import importlib.util, json, os, sys
stale = []
for package in sys.argv[1:]:
    for folder, _, files in os.walk(package):
        if "__pycache__" in folder.split(os.sep):
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            source = os.path.join(folder, name)
            cached = importlib.util.cache_from_source(source)
            try:
                with open(cached, "rb") as handle:
                    header = handle.read(16)
            except OSError:
                stale.append(source)
                continue
            info = os.stat(source)
            if (header[:4] != importlib.util.MAGIC_NUMBER or int.from_bytes(header[4:8], "little") != 0
                    or int.from_bytes(header[8:12], "little") != (int(info.st_mtime) & 0xFFFFFFFF)
                    or int.from_bytes(header[12:16], "little") != (info.st_size & 0xFFFFFFFF)):
                stale.append(source)
print(json.dumps(stale))
'''


def resolve(python: str) -> dict:
    """The user site as the ordinary interpreter computes it, in the caller's environment (HOME untouched), or,
    for a run started inside a run, at the base the outer run carried."""
    env = dict(os.environ)
    if (env.get('TESTRUNS_RUN') or env.get('TESTRUNS_CI_DISK') == '1') and env.get('TESTRUNS_USERBASE'):
        # CI omits TESTRUNS_RUN, but HOME is already replaced there too.
        env['PYTHONUSERBASE'] = env['TESTRUNS_USERBASE']
    # -s: the asking interpreter computes the user site and processes nothing in it
    done = subprocess.run([python, '-s', '-B', '-c', RESOLVE], capture_output=True, text=True, timeout=60, env=env)
    if done.returncode != 0:
        raise Refused(f'could not ask the ordinary {python} for its user site: {done.stderr.strip()[-500:]}')
    found = json.loads(done.stdout)
    found['enabled'] = not os.environ.get('PYTHONNOUSERSITE')  # in the caller's own environment
    site = Path(found['site'])
    found['exists'] = site.is_absolute() and site.is_dir() and not site.is_symlink()
    return found


def add_path_line(purelib: Path, site: str) -> Path:
    """One plain line naming the user site, in a file the run venv owns. Nothing else is written."""
    if not os.path.isabs(site) or any(char in site for char in '\n\r\0') or site.lstrip().startswith('import'):
        raise Refused(f'user site path {site!r} cannot be a plain .pth line')
    fd = os.open(purelib, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        write_new(fd, PATH_LINE, (site + '\n').encode())
    finally:
        os.close(fd)
    return purelib / PATH_LINE


def stale_bytecode(vpython: Path, packages: list[str], env: dict, wrap=None) -> list[str]:
    """Sources in the user site whose bytecode the run's interpreter would have to (re)write. ``wrap`` puts the
    check inside the run's jail, where the run's interpreter runs."""
    if not packages:
        return []
    argv = [str(vpython), '-I', '-B', '-c', CURRENT, *packages]
    done = subprocess.run(wrap(argv) if wrap else argv, capture_output=True, text=True, timeout=60, env=env)
    if done.returncode != 0:
        raise Refused(f'could not check the user site\'s bytecode: {done.stderr.strip()[-500:]}')
    return json.loads(done.stdout)


def stamps(site: str) -> dict:
    """Every bytecode folder in the user site, by identity, time and entry count; links are not followed."""
    found = {}
    for folder, dirs, files in os.walk(site):
        if os.path.basename(folder) == '__pycache__':
            info = os.lstat(folder)
            found[os.path.relpath(folder, site)] = [info.st_ino, info.st_mtime_ns, len(files)]
            dirs[:] = []
    digest = hashlib.sha256(json.dumps(sorted(found.items())).encode()).hexdigest()
    return {'folders': found, 'count': len(found), 'digest': digest}


def changed(before: dict, after: dict) -> list[str]:
    old, new = before['folders'], after['folders']
    return sorted(name for name in set(old) | set(new) if old.get(name) != new.get(name))
