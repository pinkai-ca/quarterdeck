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
    p = verbs.add_parser('run', help='fast unit/contract tests by default; --whole for a signing run',
                        description='Default: existing tests/unit and tests/contract at 16 workers; with neither, '
                                    'whole project discovery at 8 workers, with the reason printed. Signing runs explicitly '
                                    'choose --whole (every test, 8 workers). Four eight-worker slots: fast takes two '
                                    'together, whole/named takes one; bounds unchanged.')
    p.add_argument('targets', nargs='*', help='named paths or node ids (8 workers by default); omit for fast where available')
    lanes = p.add_mutually_exclusive_group()
    lanes.add_argument('--fast', dest='lane', action='store_const', const='fast',
                       help='existing unit/contract directories, 16 workers by default; neither: whole project at 8')
    lanes.add_argument('--whole', dest='lane', action='store_const', const='whole',
                       help='every test, 8 workers by default; required for a signing run')
    p.add_argument('--target', help='the worktree to test (default: the one the current directory is in)')
    p.add_argument('--ci-layout', action='store_true',
                   help='use an independent owned repository and CI pytest/disk layout (local tools only)')
    p.add_argument('-n', dest='workers', type=int, default=None,
                   help='xdist workers: fast defaults to 16, at most 16; whole/named default to 8, at most 8 (0: none)')
    p.add_argument('--timeout', type=float, default=3600.0,
                   help='seconds pytest may run, counted from its start (default 3600); setup has its own bound of '
                        '600 s from admission to a slot, and the wait for a slot is bounded by --wait alone')
    p.add_argument('--memory-mib', type=resources.memory_mib, default=resources.DEFAULT_MEMORY_MIB,
                   help=f'per-process address-space cap, at most {resources.DEFAULT_MEMORY_MIB} MiB (default)')
    p.add_argument('--wait', type=float, default=0.0,
                   help='seconds to wait for the required slots together (fast: two, whole/named: one of four), outside both time bounds (default: refuse at once)')
    p.add_argument('--jail', action='store_true',
                   help='run the target\'s code inside bubblewrap, built by this copy of the tool: for a change '
                        'from outside the crew')
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
