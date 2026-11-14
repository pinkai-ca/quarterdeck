"""Checkpoint identities follow the code that reads each native format."""

import shutil
import json
from dataclasses import replace

import pytest

from quarterdeck import harness, reader_stamp
from tests.unit.test_account_record_stamps import restart
from tests.unit.test_ledger_pass_boundaries import world


def copied_modules(tmp_path):
    source = reader_stamp.Path(reader_stamp.__file__).parent
    for name in {*reader_stamp.ATTRIBUTION,
                 *(name for modules in reader_stamp.MODULES.values() for name in modules)}:
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
    attribution_before = reader_stamp.attributions(first)
    (first / 'cli.py').write_text('synthetic unrelated command revision')
    assert reader_stamp.readers(first) == before
    assert reader_stamp.attributions(first) == attribution_before
    second = tmp_path / 'replacement-installation'
    shutil.copytree(first, second)
    assert reader_stamp.readers(second) == before
    assert reader_stamp.attributions(second) == attribution_before
    assert set(before) == set(harness.NAMES)
    assert all(before.values())


def test_missing_reader_code_never_certifies_reuse(tmp_path):
    root = copied_modules(tmp_path)
    (root / 'harness/grok_record.py').unlink()
    keys = reader_stamp.readers(root)
    assert keys['grok'] is None
    assert all(key is not None for name, key in keys.items() if name != 'grok')


@pytest.mark.parametrize('module', reader_stamp.ATTRIBUTION)
def test_every_attribution_dependency_changes_verdict_keys_only(tmp_path, module):
    root = copied_modules(tmp_path)
    before = reader_stamp.attributions(root)
    records = reader_stamp.readers(root)
    path = root / module
    path.write_bytes(path.read_bytes() + b'\n# synthetic verdict revision\n')
    assert all(reader_stamp.attributions(root)[name] != key for name, key in before.items())
    assert reader_stamp.readers(root) == records


def test_restart_outside_readers_reuses_all_and_changed_reader_rechecks_only_its_records(tmp_path, monkeypatch):
    root = copied_modules(tmp_path / 'package')
    native_readers = reader_stamp.readers
    monkeypatch.setattr(reader_stamp, 'readers', lambda root_override=None: native_readers(root))
    native_attributions = reader_stamp.attributions
    monkeypatch.setattr(reader_stamp, 'attributions', lambda: native_attributions(root))
    w = world(tmp_path / 'ship', monkeypatch, records=('first', 'second'))
    second = w.paths[1]
    w.owned[second] = replace(w.owned[second], name='grok')
    registry = w.seat.home.parent / 'record-readers.jsonl'
    rows = [json.loads(line) for line in registry.read_text().splitlines()]
    registry.write_text(''.join(json.dumps(dict(row, harness='grok' if row['record'] == str(second) else 'fake'))+'\n' for row in rows))
    native_get = harness.get
    monkeypatch.setattr(harness, 'get', lambda name: w.seat.harness if name == 'grok' else native_get(name))
    w.svc.account_records()
    assert w.reads == w.paths
    assert sorted(w.input_reads) == sorted(w.paths)
    prior_inputs = list(w.input_reads)
    restart(w)
    w.svc.status_framework = ['synthetic unrelated package landing']
    (root / 'cli.py').write_text('synthetic unrelated command change')
    w.svc.account_records()
    assert w.reads == w.paths
    assert w.input_reads == prior_inputs
    restart(w)
    path = root / 'harness/grok_record.py'
    path.write_bytes(path.read_bytes() + b'\n# synthetic native reader change\n')
    w.svc.account_records()
    assert w.reads == [*w.paths, second]
    assert w.input_reads == [*prior_inputs, second]
