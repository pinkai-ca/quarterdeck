"""The run's environment: the caller's ordinary one, PATH untouched, minus what contaminates where the target
is imported from or how Python, pip, pytest and git start, plus owned locations for every generated product.
Only names are recorded for what was removed; the values recorded are PATH and the run's own paths.

PYTHONNOUSERSITE is fixed, so no interpreter of the run, pytest's, an xdist worker's or a child a test starts,
processes a user site: the operator's user site reaches the run only as the run venv's plain path line. Its
base is carried under the tool's own name, read by nothing but a run started inside this one."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

STRIP_PREFIXES = ('PYTHON', 'PIP_', 'PYTEST_', 'GIT_', 'XDG_')
STRIP_NAMES = ('VIRTUAL_ENV', 'VIRTUAL_ENV_PROMPT', 'CONDA_PREFIX', 'CONDA_DEFAULT_ENV', '__PYVENV_LAUNCHER__',
               'TMUX_TMPDIR', 'TMPDIR', 'TEMP', 'TMP', 'HOME', 'QD_SHIP', 'QD_SEAT', 'QD_TMUX_SOCKET',
               'TESTRUN_EVIDENCE', 'TESTRUNS_RUN', 'TESTRUNS_USERBASE', 'TESTRUNS_MEMORY_MIB',
               'OPENBLAS_NUM_THREADS')
KEEP_GIT = ('GIT_CONFIG_NOSYSTEM',)  # nothing else of git's environment may redirect a test's repository


def owned_paths(scratch: Path) -> dict[str, Path]:
    home = scratch / 'home'
    return {'HOME': home, 'XDG_CACHE_HOME': home / '.cache', 'XDG_CONFIG_HOME': home / '.config',
            'XDG_DATA_HOME': home / '.local' / 'share', 'XDG_STATE_HOME': home / '.local' / 'state',
            'XDG_RUNTIME_DIR': scratch / 'xdg-runtime', 'TMPDIR': scratch / 'tmp',
            'PIP_CACHE_DIR': home / '.cache' / 'pip', 'TESTRUN_EVIDENCE': scratch / 'evidence'}


def build(scratch: Path, run: str, userbase: str | None = None) -> tuple[dict, dict]:
    stripped = sorted(name for name in os.environ
                      if (name.startswith(STRIP_PREFIXES) and name not in KEEP_GIT) or name in STRIP_NAMES)
    env = {name: value for name, value in os.environ.items() if name not in stripped}
    paths = owned_paths(scratch)
    for name, path in paths.items():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        env[name] = str(path)
    fixed = {'PIP_NO_INDEX': '1', 'PIP_NO_INPUT': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1',
             'PYTHONNOUSERSITE': '1', 'TESTRUNS_RUN': run, 'OPENBLAS_NUM_THREADS': '1'}
    if userbase:
        fixed['TESTRUNS_USERBASE'] = userbase  # resolved before HOME was replaced; for a run inside this one only
    env.update(fixed)
    record = {'PATH': env.get('PATH', ''), 'removed_names': stripped,
              'owned': {name: str(path) for name, path in paths.items()}, 'fixed': fixed}
    return env, record


def tool_versions(env: dict) -> dict:
    """Version and location of each native program a run uses, found on the ordinary PATH."""
    found = {}
    for name, argv in (('python3', ['--version']), ('git', ['--version']), ('tmux', ['-V']), ('bwrap', ['--version'])):
        where = shutil.which(name, path=env.get('PATH'))
        if where is None:
            found[name] = {'path': None}
            continue
        try:
            done = subprocess.run([where, *argv], capture_output=True, text=True, timeout=10, env=env)
            version = (done.stdout or done.stderr).strip().splitlines()[0] if (done.stdout or done.stderr).strip() else ''
        except (OSError, subprocess.TimeoutExpired):
            version = 'unreadable'
        found[name] = {'path': where, 'resolved': str(Path(where).resolve()), 'version': version}
    return found
