"""Read-only installed-copy selection, shared by the CLI and the small status path.

This module uses only the standard library. A reading never recovers an operation,
proves code, or changes a file. The installer alone owns those acts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import hashlib
import os
from pathlib import Path
import re
import tomllib

RECORD = 'installed.json'
MARKER = '.quarterdeck-copy.json'
FORMAT = 1
CHECKOUT_REFUSAL = ('this ship follows the checkout; it has no copy to sync; '
                    'remove code = "checkout" to give it one')
CODE_FORMS = 'ship.code must be absent, or "checkout"'
COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


def code_mode(head: dict) -> str | None:
    if not isinstance(head, dict):
        raise ValueError(CODE_FORMS)
    if 'code' not in head:
        return None
    if head['code'] != 'checkout':
        raise ValueError(CODE_FORMS)
    return 'checkout'


def folder_mode(folder: Path) -> str | None:
    data = tomllib.loads((folder / 'ship.toml').read_text())
    return code_mode(data.get('ship', {}))


def read(runtime: Path) -> dict | None:
    path = runtime / RECORD
    try:
        if path.stat().st_size > 262144:
            raise ValueError('installed state is too large')
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        raise ValueError('installed state is not an object')
    if 'format' in data and (type(data['format']) is not int or data['format'] != FORMAT):
        raise ValueError('unknown installed state format')
    return data


def valid_fact(fact) -> bool:
    shaped = (isinstance(fact, dict) and isinstance(fact.get('commit'), str)
            and COMMIT.fullmatch(fact['commit']) is not None
            and isinstance(fact.get('ref'), str) and bool(fact['ref'])
            and isinstance(fact.get('at'), str) and bool(fact['at'])
            and fact.get('proof') in (None, 'good'))
    if not shaped:
        return False
    try:
        if datetime.fromisoformat(fact['at']).tzinfo is None:
            return False
    except ValueError:
        return False
    if 'instance' in fact:
        return (fact.get('proof') == 'good' and isinstance(fact['instance'], str)
                and re.fullmatch(r'[0-9a-f]{32}', fact['instance']) is not None
                and isinstance(fact.get('package_digest'), str)
                and re.fullmatch(r'[0-9a-f]{64}', fact['package_digest']) is not None
                and type(fact.get('have_laws')) is bool)
    return True


def package_digest(package: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(package.rglob('*.py')):
        digest.update(path.relative_to(package).as_posix().encode() + b'\0')
        digest.update(path.read_bytes() + b'\0')
    return digest.hexdigest()


def loaded_identity(module_file: str, prefix: str) -> dict | None:
    """Capture at module load, before an installer could overwrite the live path."""
    try:
        root, package = Path(prefix).resolve(), Path(module_file).resolve().parent
        if not package.is_relative_to(root):
            return None
        marker = json.loads((root / MARKER).read_text())
        if (not isinstance(marker, dict) or set(marker) != {'commit', 'instance'}
                or not isinstance(marker['commit'], str) or not COMMIT.fullmatch(marker['commit'])
                or not isinstance(marker['instance'], str)
                or not re.fullmatch(r'[0-9a-f]{32}', marker['instance'])):
            return None
        return {**marker, 'package_digest': package_digest(package)}
    except (OSError, ValueError, TypeError, UnicodeError):
        return None


def current(data: dict | None) -> dict | None:
    if not data or data.get('proof') == 'failed':
        return None
    if 'format' in data:
        fact = data.get('current')
    else:
        # Historical success is current evidence only. Unknown optional fields cannot
        # grant a modern target capability or invent an instance/proof identity.
        fact = {key: data[key] for key in ('commit', 'ref', 'at', 'proof') if key in data}
    return fact if valid_fact(fact) else None


def previous(data: dict | None) -> dict | None:
    fact = data.get('previous_copy') if data and 'format' in data else None
    return fact if valid_fact(fact) else None


def root_id(path: Path) -> list[int] | None:
    if path.is_symlink():
        raise ValueError('copy root is a link')
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not path.is_dir():
        raise ValueError('copy root is not a directory')
    return [info.st_dev, info.st_ino]


def usable(path: Path, fact: dict | None) -> str | None:
    """Return a fixed reason, never native text or arbitrary state-file contents."""
    if not valid_fact(fact):
        return 'copy has no good recorded proof'
    if root_id(path) is None:
        return 'copy directory is missing'
    if not all((path / 'bin' / name).is_file() and os.access(path / 'bin' / name, os.X_OK)
               for name in ('python', 'qd')):
        return 'copy executable is missing or not executable'
    if fact.get('have_laws', True) and not (path / 'share' / 'quarterdeck').is_dir():
        return 'copy resources are missing'
    if fact.get('have_laws', True) and not (path / 'share/quarterdeck/standing-orders/laws.md').is_file():
        return 'copy laws resource is missing'
    if 'instance' in fact:
        try:
            marker = json.loads((path / MARKER).read_text())
        except (OSError, ValueError, UnicodeError):
            return 'copy identity is missing or unreadable'
        if marker != {'commit': fact['commit'], 'instance': fact['instance']}:
            return 'copy identity does not match its recorded proof'
        if fact.get('proof') != 'good':
            return 'copy has no good recorded proof'
    return None


@dataclass(frozen=True)
class Selection:
    qd: Path | None = None
    fact: dict | None = None
    state: dict | None = None
    warning: str | None = None
    checkout: bool = False


def select(folder: Path, *, mode: str | None = None, mode_known: bool = False) -> Selection:
    """Only a declared checkout or a usable recorded live copy may route a command."""
    try:
        mode = mode if mode_known else folder_mode(folder)
        if mode == 'checkout':
            return Selection(checkout=True)
        runtime = folder / 'runtime'
        data = read(runtime)
        fact = current(data)
        if data is None and not os.path.lexists(runtime / 'venv'):
            return Selection(warning='no copy yet; qd up makes one')
        if data and data.get('pending') is not None:
            return Selection(fact=fact, state=data, warning='copy operation interrupted or in progress; '
                             'qd up or qd sync recovers it before any seat starts')
        reason = usable(runtime / 'venv', fact)
        if reason:
            return Selection(fact=fact, state=data, warning=reason + '; use qd up, qd sync or qd doctor')
        return Selection(runtime / 'venv/bin/qd', fact, data)
    except (OSError, ValueError, TypeError, AttributeError, RecursionError, UnicodeError):
        return Selection(warning='ship code mode or installed proof unknown; inspect qd doctor; '
                         'run qd sync from the checkout to repair the copy')
