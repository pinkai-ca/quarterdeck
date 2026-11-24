"""The ship's own shell command, shared by its brief and record readers."""

from pathlib import Path
import shlex


def qd_command(folder: Path, *arguments: str) -> str:
    """Render exactly the command a seat is told to run, including shell quoting."""
    return shlex.join([str(folder / 'runtime' / 'bin' / 'qd'), *arguments])


def watch_instruction(command: str) -> str:
    return ("qd > Finish your current task, then read and act on the delivered messages. "
            f"To re-arm your inbox watch, run this command in the background.\n{command}")
