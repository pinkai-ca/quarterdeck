"""Checkpoint identities follow the code that reads each native format."""

import shutil

import pytest

from quarterdeck import harness, reader_stamp


def copied_modules(tmp_path):
    source = reader_stamp.Path(reader_stamp.__file__).parent
    for name in {name for modules in reader_stamp.MODULES.values() for name in modules}:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    return tmp_path


@pytest.mark.parametrize('harness_name,module', [
    (name, module) for name, modules in reader_stamp.MODULES.items() for module in modules
])
def test_every_declared_reader_dependency_changes_its_key(tmp_path, harness_name, module):
    root = copied_modules(tmp_path)
    before = reader_stamp.readers(root)
    path = root / module
    path.write_bytes(path.read_bytes() + b'\n# synthetic reader revision\n')
    after = reader_stamp.readers(root)
    assert after[harness_name] != before[harness_name]
    assert {name for name in before if before[name] != after[name]} == {
        name for name, modules in reader_stamp.MODULES.items() if module in modules
    }


def test_non_reader_change_and_installation_location_leave_keys_unchanged(tmp_path):
    first = copied_modules(tmp_path / 'first-installation')
    before = reader_stamp.readers(first)
    (first / 'cli.py').write_text('synthetic unrelated command revision')
    assert reader_stamp.readers(first) == before
    second = tmp_path / 'replacement-installation'
    shutil.copytree(first, second)
    assert reader_stamp.readers(second) == before
    assert set(before) == set(harness.NAMES)
    assert all(before.values())


def test_missing_reader_code_never_certifies_reuse(tmp_path):
    root = copied_modules(tmp_path)
    (root / 'harness/grok_record.py').unlink()
    keys = reader_stamp.readers(root)
    assert keys['grok'] is None
    assert all(key is not None for name, key in keys.items() if name != 'grok')
