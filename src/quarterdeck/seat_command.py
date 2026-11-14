"""The ship's own shell command, shared by its brief and record readers."""

from pathlib import Path
import shlex


def qd_command(folder: Path, *arguments: str) -> str:
    """Render exactly the command a seat is told to run, including shell quoting."""
    return shlex.join([str(folder / 'runtime' / 'bin' / 'qd'), *arguments])
