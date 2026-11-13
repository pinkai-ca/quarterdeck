"""Independent functional seams; no environment, native command or network is run.

The tiny executable-shaped files contain inert text. Installation, archive, proof,
service and process work is simulated; production transaction/state code still runs.
"""

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from quarterdeck import copy_state, install
from quarterdeck.ship import Ship, ShipError


OLD = '1' * 40
NEW = '2' * 40
OLDER = '3' * 40
STAMP = '2001-02-03T04:05:06+00:00'


def fact(commit=OLD):
    # Historical facts deliberately need no fabricated modern capability.
    return dict(commit=commit, ref='synthetic-ref', at=STAMP, proof='good', have_laws=True)


def make_ship(folder, *, code=None):
    folder.mkdir(parents=True, exist_ok=True)
    declaration = '[ship]\nname = "synthetic-vessel"\n'
    if code is not None:
        declaration += 'code = "checkout"\n'
    (folder / 'ship.toml').write_text(declaration)
    return Ship(folder, 'synthetic-vessel', 'synthetic-operator', folder, {}, {}, {}, code=code)


def inert_copy(root, label):
    (root / 'bin').mkdir(parents=True, exist_ok=True)
    for name in ('qd', 'python'):
        path = root / 'bin' / name
        path.write_text('synthetic inert file; never execute\n')
        path.chmod(0o700)
    laws = root / 'share/quarterdeck/standing-orders/laws.md'
    laws.parent.mkdir(parents=True, exist_ok=True)
    laws.write_text('synthetic resource\n')
    (root / 'content.txt').write_text(label)


def state(ship, current=None, previous=None, *, legacy=False):
    ship.runtime.mkdir(parents=True, exist_ok=True)
    data = (dict(current) if legacy else
            dict(format=1, current=current, previous_copy=previous, pending=None, last_failure=None))
    (ship.runtime / 'installed.json').write_text(json.dumps(data) + '\n')
    return data


def snapshot(folder):
    """Bytes, modes and timestamps make a read/no-op assertion observable."""
    return {str(path.relative_to(folder)): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
            for path in folder.rglob('*') if path.is_file()}


def unexpected(*args, **kwargs):
    pytest.fail('unexpected external or mutating seam')


@pytest.fixture
def vessel(tmp_path, monkeypatch):
    # Scope every own fixture to tmp_path; inherited fixture bodies remain untouched.
    ship = make_ship(tmp_path / 'synthetic-vessel')
    monkeypatch.setattr(install.subprocess, 'run', unexpected)
    monkeypatch.setattr(install.subprocess, 'Popen', unexpected)
    monkeypatch.setattr(install.os, 'execv', unexpected)
    return ship


class Transaction:
    def __init__(self, ship, monkeypatch):
        self.ship = ship
        self.calls = []
        self.failure = None
        self.running = False
        self.commit = NEW

        @contextmanager
        def locked(selected, **kwargs):
            assert selected == ship
            # Production install_lock calls ship.fence, which creates the runtime.
            ship.runtime.mkdir(parents=True, exist_ok=True)
            self.calls.append(('lock',))
            yield

        @contextmanager
        def archive(**kwargs):
            self.calls.append(('archive',))
            yield SimpleNamespace(extractall=self.extract)

        monkeypatch.setattr(install, 'install_lock', locked)
        monkeypatch.setattr(install, 'source_repo', lambda selected: ship.folder / 'synthetic-source')
        monkeypatch.setattr(install, 'git', self.git)
        monkeypatch.setattr(install.tarfile, 'open', archive)
        monkeypatch.setattr(install, '_run', self.run)
        monkeypatch.setattr(install, '_proof', self.proof)
        monkeypatch.setattr(install, '_flush_tree', lambda path: self.calls.append(('flush', path.name)))
        monkeypatch.setattr(install.resources, 'plan', lambda source: ['synthetic-resource'])
        monkeypatch.setattr(install.resources, 'synchronize', self.resources)
        monkeypatch.setattr(install.resources, 'fallback', unexpected)
        monkeypatch.setattr(install.servicectl, 'alive', lambda runtime: self.running)
        monkeypatch.setattr(install.servicectl, 'stop', self.stop)
        monkeypatch.setattr(install.servicectl, 'start', self.start)
        monkeypatch.setattr(install.servicectl, 'process_start', lambda pid: 'synthetic-start')
        monkeypatch.setattr(install, 'brief_snapshot', lambda selected: dict(before={}, launches={}))
        monkeypatch.setattr(install, 'queue_rebrief', self.queue)
        monkeypatch.setattr(install, 'rebrief_result', lambda *args: 'synthetic rebrief confirmed')

    def seed(self, *, previous=False, legacy=False):
        inert_copy(self.ship.runtime / 'venv', 'original')
        if previous:
            inert_copy(self.ship.runtime / 'venv.prev', 'previous')
        state(self.ship, fact(), fact(OLDER) if previous else None, legacy=legacy)

    def git(self, repo, *args):
        self.calls.append(('git', *args))
        if args[0] == 'rev-parse':
            return self.commit.encode()
        if args[0] == 'status':
            return b''
        if args[0] == 'archive':
            return b'synthetic archive handled by fake opener'
        pytest.fail(f'unexpected local git operation: {args}')

    def extract(self, destination, **kwargs):
        self.calls.append(('extract',))
        (destination / 'synthetic-source.txt').write_text('archived commit, not working files')

    def run(self, argv, doing, **kwargs):
        live = self.ship.runtime / 'venv'
        if argv[0] == 'cp':
            self.calls.append(('copy',))
            assert argv == ['cp', '-a', '--', str(live), str(self.ship.runtime / 'venv.prev')]
            assert not (self.ship.runtime / 'venv.prev').exists()
            if self.failure == 'copy':
                inert_copy(self.ship.runtime / 'venv.prev', 'partial backup')
                raise ShipError('synthetic copy failed')
            # Fake cp semantics over inert text only, never a real venv or cp command.
            shutil.copytree(live, self.ship.runtime / 'venv.prev')
        elif argv[1:3] == ['-m', 'venv']:
            self.calls.append(('environment',))
            assert Path(argv[-1]) == live
            inert_copy(live, 'incomplete')
        elif argv[1:3] == ['-m', 'pip']:
            self.calls.append(('install',))
            assert '--no-index' in argv
            (live / 'content.txt').write_text('new candidate')
            if self.failure == 'install':
                raise ShipError('synthetic install failed')
        else:
            pytest.fail(f'unexpected command at synthetic seam: {doing}')
        return SimpleNamespace(returncode=0, stdout='synthetic')

    def resources(self, source, share, files):
        self.calls.append(('resources',))
        if self.failure == 'resources':
            raise ShipError('synthetic resources failed')

    def proof(self, ship, selected):
        live = ship.runtime / 'venv'
        self.calls.append(('proof', live.name, selected['commit'], (live / 'content.txt').read_text()))
        if self.failure == 'proof':
            raise ShipError('synthetic proof failed')
        selected.update(proof='good', safety=None)
        # The fake proves no native identity; retain an honest historical shape.
        selected.pop('instance', None)
        return 'synthetic-version', install.historical_notice(selected)

    def stop(self, runtime):
        assert runtime == self.ship.runtime
        self.calls.append(('stop',))
        self.running = False
        return 'synthetic service stopped'

    def start(self, ship, *, qd, **kwargs):
        assert qd == ship.runtime / 'venv/bin/qd'
        self.calls.append(('start', (ship.runtime / 'venv/content.txt').read_text()))
        self.running = True
        return 'synthetic service started'

    def queue(self, ship, commit, previous, before):
        self.calls.append(('rebrief',))
        return ship.runtime / 'synthetic-rebrief.json', {}

    def read(self):
        return json.loads((self.ship.runtime / 'installed.json').read_text())

    def named(self, name):
        return [call for call in self.calls if call[0] == name]


@pytest.fixture
def transaction(vessel, monkeypatch):
    return Transaction(vessel, monkeypatch)
