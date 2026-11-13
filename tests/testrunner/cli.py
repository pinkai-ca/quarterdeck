"""The test tool's verbs. A refusal prints its reason and exits 2."""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys

from . import clean, resources, run
from .layout import Refused


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog='tests/testrun.py', description=__doc__)
    verbs = top.add_subparsers(dest='verb', required=True)
    p = verbs.add_parser('run', help='run the whole suite, or named targets, of a clean committed worktree')
    p.add_argument('targets', nargs='*', help='paths or node ids relative to the worktree top (default: the suite)')
    p.add_argument('--target', help='the worktree to test (default: the one the current directory is in)')
    p.add_argument('-n', dest='workers', type=int, default=8, help='xdist workers, 8 at most (0: none)')
    p.add_argument('--timeout', type=float, default=3600.0, help='seconds before the run is stopped (default 3600)')
    p.add_argument('--memory-mib', type=resources.memory_mib, default=resources.DEFAULT_MEMORY_MIB,
                   help=f'per-process address-space cap, at most {resources.DEFAULT_MEMORY_MIB} MiB (default)')
    p.add_argument('--wait', type=float, default=0.0, help='seconds to wait for a busy slot (default: refuse at once)')
    p = verbs.add_parser('clean', help='retire a registered change that is merged, or deliberately dropped')
    p.add_argument('--change', required=True, help='the change: its branch name')
    p.add_argument('--drop', action='store_true', help='discard the unmerged branch; needs --reason')
    p.add_argument('--reason', help='why the change is dropped')
    p.add_argument('--wait', type=float, default=0.0, help='seconds to wait for a busy slot')
    p = verbs.add_parser('keep', help='keep one run\'s scratch past its end and its change\'s clean')
    p.add_argument('run', help='the run id')
    p.add_argument('--reason', required=True, help='why it is kept')
    p = verbs.add_parser('retire', help='retire exactly one recorded crashed run, preserving its first result')
    p.add_argument('run', help='the exact recorded run id; never a path, branch, or age filter')
    p.add_argument('--wait', type=float, default=0.0, help='seconds to wait for a busy slot')
    return top


def main(argv: list[str] | None, runner_files: list[Path]) -> int:
    args = parser().parse_args(argv)
    try:
        if args.verb == 'run':
            if not math.isfinite(args.timeout) or args.timeout <= 0:
                raise Refused('--timeout must be finite and greater than zero')
            if not 0 <= args.workers <= 8:
                raise Refused('-n takes 0 to 8 workers')
            return run.Runner(args, runner_files).main()
        if args.verb == 'clean':
            return clean.clean(args)
        if args.verb == 'retire':
            if not math.isfinite(args.wait) or args.wait < 0:
                raise Refused('--wait must be finite and nonnegative')
            return clean.retire(args)
        return clean.keep(args)
    except Refused as refused:
        print(f'testrun: refused: {refused}', file=sys.stderr)
        return 2
