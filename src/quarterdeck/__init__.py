"""Quarterdeck runs a crew of coding agents as visible terminal sessions under one human's command."""

__version__ = "0.1.0"


def main():
    """Keep a fresh tmux status job small; all other commands use the ordinary CLI."""
    import sys
    from .presentation import command_line, unavailable_line

    line = command_line(sys.argv[1:])
    if line is not None:
        text = line + '\n'
        if sys.stdout.write(text) != len(text):
            raise OSError('incomplete status output')
        sys.stdout.flush()
        return 0
    from .cli import main as command
    result = command()
    if result:
        alarm = unavailable_line(sys.argv[1:])
        if alarm is not None:
            print(alarm, flush=True)
    return result
