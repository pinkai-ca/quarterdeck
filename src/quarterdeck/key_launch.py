"""The launcher that puts a pool's key into its program, on no process's command line.

Every process on the machine can read every command line, so a key passes through neither tmux's arguments nor
bwrap's. launch.sh execs this module with the key's name; it opens the key file itself and then:

- for a program that signs in with a key, runs that sign-in with the key on stdin, in a private folder, and puts the
  file it wrote into the seat's home by a rename that follows no link;
- for an unjailed seat, sets the one variable in its own environment and execs the program;
- for a jailed seat, writes bwrap's ``--setenv`` words to a pipe and hands bwrap ``--args <fd>``, just after its
  ``--clearenv``, so that the key is in the jail's environment and in no argument.

Nothing it prints holds anything of the key or of what a sign-in printed. No record reader needs this module.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from . import keyfile

MODULE = 'quarterdeck.key_launch'
EXIT_REFUSED = 78  # EX_CONFIG: what a launch needed of its key was not there
CLEAR = '--clearenv'  # jail_environment.CLEAR, bwrap's own word
LOGIN_PREFIX = 'key-login-'


def command(file: Path, pool, found: keyfile.Route, program: str, argv: list[str], *, home: Path, private: Path,
            jailed: bool) -> list[str]:
    """The launch's words around ``argv``: the key's name and where to read it, never the key."""
    words = [sys.executable, '-I', '-m', MODULE, '--file', str(file), '--name', keyfile.name_of(pool.api_key)]
    if found.variable:
        words += ['--variable', found.variable]
        if jailed:
            words.append('--jail')
    if found.login:
        words += ['--login-program', program, '--login-home', found.login_home, '--login-file', found.login_file,
                  '--home', str(home), '--private', str(private)]
        words += [f'--login-word={word}' for word in found.login]
    return [*words, '--', *argv]


def inner(argv):
    """The launch's own command, behind this launcher where a key pays for it. Anything else is returned as it is."""
    if (isinstance(argv, (list, tuple)) and list(argv[1:4]) == ['-I', '-m', MODULE] and '--' in argv):
        return list(argv[list(argv).index('--') + 1:])
    return argv


def jail_words(argv: list[str], variable: str, key: str) -> list[str]:
    """``argv`` with bwrap told to read the key's ``--setenv`` from a pipe. The pipe holds it until bwrap drains it."""
    if CLEAR not in argv:
        raise ValueError('no jail to hand the key to')
    reader, writer = os.pipe()
    try:
        os.write(writer, b'\0'.join((b'--setenv', variable.encode(), key.encode(), b'')))
    finally:
        os.close(writer)
    os.set_inheritable(reader, True)
    at = argv.index(CLEAR) + 1
    return [*argv[:at], '--args', str(reader), *argv[at:]]


def sign_in(key: str, options: argparse.Namespace) -> None:
    """The program's own key login, run in a private folder; its file is put in the home without following a link."""
    from . import seat_files

    refused = keyfile.KeyProblem(keyfile.REFUSED, f'{Path(options.login_program).name} did not accept the key '
                                                  f'{options.name} at its sign-in')
    for left in Path(options.private).glob(f'{LOGIN_PREFIX}*'):  # what a killed launch could not remove
        shutil.rmtree(left, ignore_errors=True)

    def ended(signum, frame):
        raise SystemExit(128 + signum)  # so that the folder below is removed on a signal as well

    before = {signum: signal.signal(signum, ended) for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    folder = Path(tempfile.mkdtemp(prefix=LOGIN_PREFIX, dir=options.private))
    try:
        environment = {**os.environ, options.login_home: str(folder)}
        try:
            done = subprocess.run([options.login_program, *options.login_word], input=key + '\n', text=True,
                                  env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except OSError:
            raise refused from None
        written = folder / options.login_file
        if done.returncode != 0 or written.is_symlink() or not written.is_file():
            raise refused
        seat_files.write_bytes(Path(options.home) / options.login_file, written.read_bytes(), mode=0o600)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        for signum, handler in before.items():
            signal.signal(signum, handler)
    if folder.exists():
        raise keyfile.KeyProblem(keyfile.REFUSED, 'the sign-in\'s private folder could not be removed; nothing was started')


def main(args: list[str] | None = None) -> int:
    args = sys.argv[1:] if args is None else args
    if '--' not in args:
        print('key_launch: usage: --file F --name N [--variable V [--jail]] -- COMMAND', file=sys.stderr)
        return 2
    at = args.index('--')
    head, argv = args[:at], args[at + 1:]
    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    for flag in ('--file', '--name', '--variable', '--login-program', '--login-home', '--login-file', '--home',
                 '--private'):
        parser.add_argument(flag)
    parser.add_argument('--login-word', action='append', default=[])
    parser.add_argument('--jail', action='store_true')
    try:
        options = parser.parse_args(head)
    except (argparse.ArgumentError, SystemExit):
        print('key_launch: unreadable arguments', file=sys.stderr)
        return 2
    if not argv or not options.file or not options.name:
        print('key_launch: usage: --file F --name N [--variable V [--jail]] -- COMMAND', file=sys.stderr)
        return 2
    try:
        keys = keyfile.read(Path(options.file))
        if options.name not in keys:
            raise keyfile.KeyProblem(keyfile.MISSING, f'key {options.name} is not in {options.file}')
        key = keys[options.name]
        if options.login_program:
            sign_in(key, options)
    except keyfile.KeyProblem as problem:
        print(f'key_launch: {problem}', file=sys.stderr)
        return EXIT_REFUSED
    environment = dict(os.environ)
    if options.variable and options.jail:
        argv = jail_words(argv, options.variable, key)
    elif options.variable:
        environment[options.variable] = key
    try:
        os.execvpe(argv[0], argv, environment)
    except OSError as error:
        print(f'key_launch: cannot start {Path(argv[0]).name}: {error.strerror}', file=sys.stderr)
    return 127


if __name__ == '__main__':
    sys.exit(main())
