"""Checkpoint identities follow the code that reads each native format."""

import ast
import shutil
import json
from dataclasses import replace
from pathlib import Path

import pytest

from quarterdeck import harness
from quarterdeck.harness import reader_stamp
from tests.unit.test_account_record_stamps import restart
from tests.unit.test_ledger_pass_boundaries import world


def copied_modules(tmp_path):
    source = reader_stamp.Path(reader_stamp.__file__).parent.parent
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
def test_every_attribution_dependency_changes_verdict_keys_and_its_reader_dependents(tmp_path, module):
    root = copied_modules(tmp_path)
    before = reader_stamp.attributions(root)
    records = reader_stamp.readers(root)
    path = root / module
    path.write_bytes(path.read_bytes() + b'\n# synthetic verdict revision\n')
    assert all(reader_stamp.attributions(root)[name] != key for name, key in before.items())
    after = reader_stamp.readers(root)
    assert {name for name in records if after[name] != records[name]} == {
        name for name, modules in reader_stamp.MODULES.items() if module in modules
    }


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


def test_streamed_record_passes_share_the_jobs_reader_keys(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, records=('first', 'second'))
    keys = reader_stamp.readers()
    def unexpected():
        raise AssertionError('a streamed record must not rehash the package')
    monkeypatch.setattr(reader_stamp, 'readers', unexpected)
    w.svc.account_records(source_reader_keys=keys, replay=False)
    assert w.reads == w.paths
    restart(w)
    w.svc.account_records(source_reader_keys=keys, replay=False)
    assert w.reads == w.paths


def _package_of(reader):
    parent = Path(reader).parent
    return '.'.join(['quarterdeck', *parent.parts])


def _absolute_name(reader, level, module):
    """PEP 328: level counts dots, and the base package loses level-1 components."""
    parts = _package_of(reader).split('.')
    trim = level - 1
    if trim > len(parts):
        return ''
    if trim:
        parts = parts[:-trim]
    base = '.'.join(parts)
    if not module:
        return base
    return f'{base}.{module}' if base else module


def _file_for(root, dotted):
    """The module file for a dotted name. A sibling .py wins over a package."""
    if dotted != 'quarterdeck' and not dotted.startswith('quarterdeck.'):
        return None
    rel = dotted.split('.')[1:]
    if not rel:
        return '__init__.py' if (root / '__init__.py').is_file() else None
    as_file = root.joinpath(*rel[:-1], rel[-1] + '.py')
    as_package = root.joinpath(*rel, '__init__.py')
    if as_file.is_file():
        return as_file.relative_to(root).as_posix()
    if as_package.is_file():
        return as_package.relative_to(root).as_posix()
    return None


def _prefixes(root, dotted):
    if dotted != 'quarterdeck' and not dotted.startswith('quarterdeck.'):
        return []
    found = []
    parts = dotted.split('.')
    for end in range(1, len(parts) + 1):
        path = _file_for(root, '.'.join(parts[:end]))
        if path is not None and path not in found:
            found.append(path)
    return found


def _call_kind(node):
    if isinstance(node.func, ast.Name) and node.func.id in ('import_module', '__import__'):
        return node.func.id
    if (isinstance(node.func, ast.Attribute) and node.func.attr == 'import_module'
            and isinstance(node.func.value, ast.Name) and node.func.value.id == 'importlib'):
        return 'import_module'
    return None


def _argument(call, position, keyword):
    return (call.args[position] if len(call.args) > position else
            next((item.value for item in call.keywords if item.arg == keyword), None))


def _literal_call_module(call, reader, kind):
    name = _argument(call, 0, 'name')
    if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
        return None
    dotted = name.value
    if kind == '__import__':
        level = _argument(call, 4, 'level')
        if level is not None and (not isinstance(level, ast.Constant) or level.value != 0):
            return None  # relative __import__ depends on its globals, not just the file's package
    if not dotted.startswith('.'):
        return dotted
    package = _argument(call, 1, 'package')
    if isinstance(package, ast.Name) and package.id == '__package__':
        base = _package_of(reader)
    elif isinstance(package, ast.Constant) and isinstance(package.value, str):
        base = package.value
    else:
        return None  # a literal relative name with a computed package is still unresolved
    level = len(dotted) - len(dotted.lstrip('.'))
    parts = base.split('.')
    if level > len(parts):
        return None
    return '.'.join([*parts[:len(parts) - level + 1], dotted[level:]]).rstrip('.')


def _scope(node, parents):
    names = []
    while (node := parents.get(node)) is not None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return '.'.join(reversed(names)) or '<module>'


def _expression_key(text):
    return ast.dump(ast.parse(text, mode='eval').body, include_attributes=False)


def quarterdeck_modules_imported(root, reader):
    """Static imports and literal import calls, plus unresolved computed calls."""
    tree = ast.parse((root / reader).read_text())
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    found, computed = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = []
            for alias in node.names:
                for path in _prefixes(root, alias.name):
                    if path not in targets:
                        targets.append(path)
        elif isinstance(node, ast.ImportFrom):
            dotted = _absolute_name(reader, node.level, node.module) if node.level else (node.module or '')
            targets = _prefixes(root, dotted)
            for alias in node.names:
                if alias.name == '*':
                    continue
                child = f'{dotted}.{alias.name}' if dotted else alias.name
                for path in _prefixes(root, child):
                    if path not in targets:
                        targets.append(path)
        elif isinstance(node, ast.Call) and (kind := _call_kind(node)):
            dotted = _literal_call_module(node, reader, kind)
            if dotted is None:
                computed.append((node.lineno, _scope(node, parents), ast.unparse(node)))
                continue
            targets = _prefixes(root, dotted)
        else:
            continue
        statement = ast.unparse(node)
        for path in targets:
            found.append((path, node.lineno, statement))
    return found, computed


def imports_outside(root, readers, allowed, classified=()):
    calls = set()
    for module, scope, expression, reason in classified:
        assert module in readers and scope
        assert isinstance(reason, str) and reason.strip()
        call = ast.parse(expression, mode='eval').body
        assert isinstance(call, ast.Call) and _call_kind(call)
        calls.add((module, scope, _expression_key(expression)))
    problems = []
    for reader in readers:
        imports, computed = quarterdeck_modules_imported(root, reader)
        for imported, lineno, statement in imports:
            if imported not in allowed:
                problems.append(f'{reader}:{lineno} imports {imported} ({statement})')
        for lineno, scope, statement in computed:
            if (reader, scope, _expression_key(statement)) not in calls:
                problems.append(f'{reader}:{lineno} computed import in {scope} ({statement})')
    return problems


def _named_beside(table):
    named = set()
    for module, reason in table:
        assert isinstance(module, str) and module
        assert isinstance(reason, str) and reason.strip()
        named.add(module)
    return named


def test_a_reader_import_outside_its_set_is_named_beside_it():
    root = Path(reader_stamp.__file__).parent.parent
    assert set(reader_stamp.LAUNCH_ONLY) == set(reader_stamp.MODULES)
    assert set(reader_stamp.CONTROL_ONLY) == set(reader_stamp.MODULES)
    assert set(reader_stamp.COMPUTED_IMPORTS) == set(reader_stamp.MODULES)
    problems = []
    for name, modules in reader_stamp.MODULES.items():
        launch = _named_beside(reader_stamp.LAUNCH_ONLY[name])
        control = _named_beside(reader_stamp.CONTROL_ONLY[name])
        for module in launch | control:
            assert (root / module).is_file(), module
        allowed = set(modules) | launch | control
        for problem in imports_outside(root, modules, allowed, reader_stamp.COMPUTED_IMPORTS[name]):
            problems.append(f'{name}: {problem}')
    assert problems == []


def test_attribution_imports_are_hashed_or_named_beside_the_set():
    root = Path(reader_stamp.__file__).parent.parent
    control = _named_beside(reader_stamp.ATTRIBUTION_CONTROL_ONLY)
    native = {module for modules in reader_stamp.MODULES.values() for module in modules}
    allowed = set(reader_stamp.ATTRIBUTION) | native | control
    for module in control:
        assert (root / module).is_file(), module
    assert 'post_message.py' in reader_stamp.ATTRIBUTION
    assert 'post.py' not in allowed, 'Store and transport are not message interpretation'
    assert imports_outside(root, reader_stamp.ATTRIBUTION, allowed,
                           reader_stamp.ATTRIBUTION_COMPUTED_IMPORTS) == []


def test_the_import_walker_catches_a_planted_out_of_set_import(tmp_path):
    root = tmp_path / 'src' / 'quarterdeck'
    for name in ('__init__.py', 'harness/__init__.py', 'harness/kept.py', 'harness/stray.py',
                 'launch_mod.py', 'planted.py', 'outside.py'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('')
    (root / 'harness' / 'reader.py').write_text(
        'import os\n'
        'from . import kept\n'
        'from .. import launch_mod\n'
        '\n'
        'def inside():\n'
        '    import quarterdeck.harness.kept\n'
        '    import quarterdeck.planted\n'
        '    from . import stray\n'
        '    from quarterdeck.outside import piece\n'
    )
    allowed = {'harness/reader.py', 'harness/kept.py', 'harness/__init__.py', '__init__.py', 'launch_mod.py'}
    problems = imports_outside(root, ['harness/reader.py'], allowed)
    imported = [problem.split(' imports ', 1)[1].split(' ', 1)[0] for problem in problems]
    assert sorted(imported) == ['harness/stray.py', 'outside.py', 'planted.py']
    assert all(problem.startswith('harness/reader.py:') for problem in problems)
    assert all('kept' not in problem and 'launch_mod' not in problem for problem in problems)


@pytest.mark.parametrize('call', [
    "import_module('quarterdeck.planted')",
    "importlib.import_module('quarterdeck.planted')",
    "__import__('quarterdeck.planted')",
    "import_module(name='quarterdeck.planted')",
    "importlib.import_module('.planted', 'quarterdeck')",
    "import_module('..planted', __package__)",
])
def test_literal_dynamic_imports_check_the_planted_module(tmp_path, call):
    root = tmp_path / 'quarterdeck'
    (root / 'harness').mkdir(parents=True)
    (root / 'planted.py').write_text('')
    (root / 'harness/reader.py').write_text('def nested():\n    ' + call + '\n')
    problems = imports_outside(root, ['harness/reader.py'], {'harness/reader.py'})
    assert len(problems) == 1
    assert problems[0].startswith('harness/reader.py:2 imports planted.py (')
    assert call.split('(', 1)[0] in problems[0]
    assert imports_outside(root, ['harness/reader.py'], {'harness/reader.py', 'planted.py'}) == []


@pytest.mark.parametrize('call', ['import_module(module_name)',
                                 'importlib.import_module(module_name)', '__import__(module_name)'])
def test_a_computed_import_needs_its_exact_call_classified_with_a_reason(tmp_path, call):
    (tmp_path / 'reader.py').write_text('def nested():\n    ' + call + '\n')
    problems = imports_outside(tmp_path, ['reader.py'], {'reader.py'})
    assert len(problems) == 1
    assert problems[0] == f'reader.py:2 computed import in nested ({call})'
    classified = [('reader.py', 'nested', call, 'fictional fixed registry owns this selection')]
    assert imports_outside(tmp_path, ['reader.py'], {'reader.py'}, classified) == []
    # Classification of one call cannot hide another expression in the same scope.
    with (tmp_path / 'reader.py').open('a') as stream:
        stream.write('    ' + call.replace('module_name', 'other_name') + '\n')
    problems = imports_outside(tmp_path, ['reader.py'], {'reader.py'}, classified)
    assert len(problems) == 1 and problems[0].startswith('reader.py:3 computed import in nested (')


def test_a_literal_relative_call_with_a_computed_package_remains_unresolved(tmp_path):
    (tmp_path / 'reader.py').write_text("import_module('.example', package_name)\n")
    assert imports_outside(tmp_path, ['reader.py'], {'reader.py'}) == [
        "reader.py:1 computed import in <module> (import_module('.example', package_name))"]
