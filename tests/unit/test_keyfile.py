"""The one key file: where it is, what is refused, and that nothing of its content leaves it. Synthetic keys only."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from quarterdeck import keyfile, ship as shipmod
from quarterdeck.ship import Pool, ShipError

KEY = 'sk-synthetic-Q7wM2xC9zT4lP8rV3nH6'
TAIL = KEY[-8:]


def pool(folder, reference='keys:anthropic'):
    return Pool('paid', 'anthropic', api_key=reference, ship_folder=folder)


@pytest.fixture
def ship_folder(tmp_path):
    folder = tmp_path / 'ships' / 'test'
    folder.mkdir(parents=True)
    return folder


def refused(call) -> keyfile.KeyProblem:
    with pytest.raises(keyfile.KeyProblem) as caught:
        call()
    line = str(caught.value)
    assert '\n' not in line and KEY not in line and TAIL not in line
    assert caught.value.__context__ is None or KEY not in str(caught.value.__context__)
    return caught.value


# -- reading ---------------------------------------------------------------------------------------------------------

def test_a_named_key_is_read_and_a_changed_key_is_read_at_the_next_reading(key_file, ship_folder):
    key_file.write(anthropic=KEY)
    assert keyfile.value(pool(ship_folder)) == KEY
    key_file.write(anthropic=KEY + 'x')
    assert keyfile.value(pool(ship_folder)) == KEY + 'x'


def test_an_absent_name_and_an_absent_file_are_said_plainly(key_file, ship_folder):
    found = refused(lambda: keyfile.value(pool(ship_folder)))
    assert found.state == keyfile.MISSING and str(found) == f'no key file at {key_file.path}'
    key_file.write(openai=KEY)
    found = refused(lambda: keyfile.value(pool(ship_folder)))
    assert found.state == keyfile.MISSING and str(found) == f'key anthropic is not in {key_file.path}'


@pytest.mark.parametrize('mode', [0o600, 0o640, 0o644])
def test_a_files_mode_refuses_nothing(key_file, ship_folder, mode):
    key_file.write(anthropic=KEY).chmod(mode)
    assert keyfile.value(pool(ship_folder)) == KEY
    assert keyfile.open_to_others(pool(ship_folder)) is bool(mode & 0o077)


def test_a_link_is_read_as_what_it_names(key_file, tmp_path, ship_folder):
    behind = tmp_path / 'behind.toml'
    behind.write_text(f'anthropic = "{KEY}"\n')
    key_file.write()
    key_file.path.unlink()
    key_file.path.symlink_to(behind)
    assert keyfile.value(pool(ship_folder)) == KEY


def test_a_fifo_at_the_path_never_hangs_a_launch(key_file, ship_folder):
    key_file.write()
    key_file.path.unlink()
    os.mkfifo(key_file.path)
    assert 'not a regular file' in str(refused(lambda: keyfile.value(pool(ship_folder))))


@pytest.mark.parametrize('text', [
    f'anthropic = "{KEY}\n',                              # unterminated
    f'"{KEY}" = 1\n"{KEY}" = 2\n',                        # tomllib quotes a repeated name in its error
    f'anthropic = "{KEY}"\nanthropic = "{KEY}"\n',
    f'{KEY}\n',                                           # a key pasted alone
])
def test_a_file_that_is_not_toml_shows_nothing_of_itself(key_file, ship_folder, text):
    key_file.write()
    key_file.path.write_text(text)
    found = refused(lambda: keyfile.value(pool(ship_folder)))
    assert str(found) == f'key file {key_file.path}: it is not valid TOML'
    assert found.__cause__ is None and found.__context__ is None, 'no parser error is chained'


@pytest.mark.parametrize('text,what', [
    (f'"{KEY}" = "x"\n', 'a key name'),                   # a key pasted on the left of the =
    ('Upper = "x"\n', 'a key name'),
    ('name = 1\n', 'a key is not'),
    ('name = "two words"\n', 'a key is not'),
    ('name = ""\n', 'a key is not'),
    ('[table]\nname = "x"\n', 'a key is not'),
])
def test_a_bad_name_or_value_is_refused_without_echoing_it(key_file, ship_folder, text, what):
    key_file.write()
    key_file.path.write_text(text)
    line = str(refused(lambda: keyfile.value(pool(ship_folder))))
    assert what in line and 'Upper' not in line and 'two words' not in line


# -- where the file is ------------------------------------------------------------------------------------------------

def test_no_ship_folder_is_refused_and_never_the_account_file(key_file):
    assert refused(lambda: keyfile.path(None)).state == keyfile.REFUSED
    assert refused(lambda: keyfile.value(Pool('paid', 'anthropic', api_key='keys:anthropic'))).state == keyfile.REFUSED


def test_the_account_path_comes_from_the_account_and_never_from_the_environment(monkeypatch, tmp_path):
    import pwd
    monkeypatch.setenv('HOME', '/nowhere/else')
    monkeypatch.setenv('XDG_CONFIG_HOME', '/nowhere/else/config')
    expected = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.config' / 'quarterdeck' / 'keys.toml'
    assert keyfile.account_file() == expected
    monkeypatch.delenv(keyfile.TEST_CONFIG)
    monkeypatch.setattr(keyfile, 'marked', lambda folder: False)  # a ship in no folder of the test tool's
    assert keyfile.path(tmp_path) == expected  # the path alone: nothing here opens it


def test_a_test_reads_its_scratch_file(key_file, ship_folder):
    assert keyfile.path(ship_folder) == key_file.path


def test_a_ship_in_scratch_that_lost_the_variable_has_no_file(ship_folder, monkeypatch):
    monkeypatch.delenv(keyfile.TEST_CONFIG)
    assert keyfile.in_test_scratch(ship_folder)
    assert refused(lambda: keyfile.path(ship_folder)).state == keyfile.MISSING


CHILD = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
opened = []
sys.addaudithook(lambda event, args: opened.append(str(args[0])) if event == 'open' else None)
from pathlib import Path
from quarterdeck import keyfile
account = str(keyfile.account_file())
results = []
for folder in (Path(sys.argv[2]), None):
    try:
        results.append(str(keyfile.path(folder)))
    except keyfile.KeyProblem as problem:
        results.append(problem.state)
print(json.dumps(dict(results=results, opened_account=any(account == path for path in opened))))
'''


def test_a_scrubbed_child_with_a_ship_in_scratch_does_not_open_the_account_path(ship_folder):
    source = str(Path(keyfile.__file__).parents[1])
    done = subprocess.run([sys.executable, '-I', '-c', CHILD, source, str(ship_folder)], env={},
                          capture_output=True, text=True, check=True)
    assert json.loads(done.stdout) == {'results': [keyfile.MISSING, keyfile.REFUSED], 'opened_account': False}


def test_this_run_of_the_tests_is_inside_a_marked_folder(tmp_path):
    """Under testrun.py and under direct pytest alike: GitHub's run is direct pytest, and it must see the key tests."""
    assert keyfile.in_test_scratch(tmp_path)


@pytest.mark.parametrize('content', [b'', b'short', b'g' * 32, b'a' * 33])
def test_an_invalid_mark_is_no_mark(tmp_path, content):
    folder = tmp_path / 'folder'
    folder.mkdir()
    (folder / keyfile.OWNER_MARK).write_bytes(content)
    assert not keyfile.marked(folder)


def test_a_link_is_no_mark(tmp_path):
    folder = tmp_path / 'folder'
    folder.mkdir()
    real = tmp_path / 'real'
    real.write_bytes(b'a' * 32)
    (folder / keyfile.OWNER_MARK).symlink_to(real)
    assert not keyfile.marked(folder)
    real.replace(folder / keyfile.OWNER_MARK)
    assert keyfile.marked(folder)


# -- the ship file ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('written', ['env:SOME_VARIABLE', 'file:/some/file', KEY, 'keys:', 'keys:UPPER', 'keys:a b',
                                     'keys:' + 'a' * 41])
def test_the_ship_file_takes_only_a_key_name_and_never_prints_what_was_written(tmp_path, written):
    (tmp_path / 'ship.toml').write_text(f'[ship]\nname = "t"\n[pools.paid]\nprovider = "anthropic"\napi_key = "{written}"\n')
    with pytest.raises(ShipError) as caught:
        shipmod.load(tmp_path)
    line = str(caught.value)
    assert line.startswith('pool paid: api_key must be "keys:<name>"') and written not in line.replace('keys:<name>', '')


def test_the_ship_file_refuses_a_key_that_is_not_a_string(tmp_path):
    (tmp_path / 'ship.toml').write_text('[ship]\nname = "t"\n[pools.paid]\nprovider = "anthropic"\napi_key = 12345\n')
    with pytest.raises(ShipError, match='api_key must be') as caught:
        shipmod.load(tmp_path)
    assert '12345' not in str(caught.value)


def test_a_loaded_pool_knows_its_ship(tmp_path):
    (tmp_path / 'ship.toml').write_text('[ship]\nname = "t"\n[pools.paid]\nprovider = "anthropic"\napi_key = "keys:paid"\n')
    assert shipmod.load(tmp_path).pools['paid'].ship_folder == tmp_path


# -- what a launch writes in the home --------------------------------------------------------------------------------

def test_the_helper_setting_names_a_variable_and_never_a_key(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'settings.json').write_text('{"kept": 1}')
    keyfile.prepare_home(keyfile.Route(keyfile.HELPER_VARIABLE, helper=True), home)
    assert json.loads((home / 'settings.json').read_text()) == {'kept': 1, 'apiKeyHelper': keyfile.HELPER_COMMAND}
    assert stat.S_ISREG((home / 'settings.json').lstat().st_mode)
