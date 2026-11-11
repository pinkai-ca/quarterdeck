"""The user site reaches the run venv as one plain path line, read-only, with nothing in it processed."""
import os
import sys

import pytest

from tests.testrunner import layout, usersite


def test_the_path_line_is_one_plain_line_in_a_new_file(tmp_path):
    site = tmp_path / 'user-site'
    site.mkdir()
    purelib = tmp_path / 'purelib'
    purelib.mkdir()
    line = usersite.add_path_line(purelib, str(site))
    assert line.read_text() == f'{site}\n' and sorted(os.listdir(purelib)) == [usersite.PATH_LINE]
    with pytest.raises(FileExistsError):
        usersite.add_path_line(purelib, str(site))


@pytest.mark.parametrize('bad', ['relative/site', '/a\nimport os', 'import os; os.system("x")', '/a\0b'])
def test_anything_but_an_absolute_plain_path_is_refused(tmp_path, bad):
    with pytest.raises(layout.Refused):
        usersite.add_path_line(tmp_path, bad)
    assert os.listdir(tmp_path) == []


def test_the_user_site_is_resolved_from_the_callers_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHONUSERBASE', str(tmp_path / 'base'))
    found = usersite.resolve(sys.executable)
    assert found['base'] == str(tmp_path / 'base') and found['site'].startswith(str(tmp_path / 'base'))
    assert found['exists'] is False


def test_bytecode_folders_are_stamped_and_any_change_is_named(tmp_path):
    cache = tmp_path / 'pkg' / '__pycache__'
    cache.mkdir(parents=True)
    (cache / 'a.cpython-314.pyc').write_bytes(b'x')
    before = usersite.stamps(str(tmp_path))
    assert before['count'] == 1 and usersite.changed(before, usersite.stamps(str(tmp_path))) == []
    (cache / 'b.cpython-314.pyc').write_bytes(b'y')
    assert usersite.changed(before, usersite.stamps(str(tmp_path))) == ['pkg/__pycache__']


def test_stale_or_missing_bytecode_is_listed(tmp_path):
    package = tmp_path / 'tool'
    package.mkdir()
    (package / 'fresh.py').write_text('X = 1\n')
    (package / 'missing.py').write_text('Y = 1\n')
    import py_compile
    py_compile.compile(str(package / 'fresh.py'), doraise=True)
    stale = usersite.stale_bytecode(sys.executable, [str(package)], dict(os.environ))
    assert stale == [str(package / 'missing.py')]
