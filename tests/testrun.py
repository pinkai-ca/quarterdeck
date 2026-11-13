"""The repository's test tool. Run it as ``python3 -I tests/testrun.py run|clean|keep|retire``; see ``--help``.

Standard library only; it imports nothing from ``src/`` and writes no bytecode beside itself. The worktree it
tests is found by Git (``--target`` or the current directory), never from where this file lives, so a
worktree made before the tool existed is tested by the main checkout's copy with ``--target``.
"""
import sys

sys.dont_write_bytecode = True  # the runner leaves nothing in whichever checkout it is run from

from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # -I leaves the script's own folder off the path

from testrunner import cli  # noqa: E402

FILES = [HERE / 'testrun.py', *sorted((HERE / 'testrunner').glob('*.py'))]

if __name__ == '__main__':
    sys.exit(cli.main(sys.argv[1:], FILES))
